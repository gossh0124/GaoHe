from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import re
import socket
import sqlite3
from threading import Thread
from urllib.parse import urlencode

import pytest

import gaohe.setup_flow as setup_flow
from gaohe.config import Settings
from gaohe.domain import Source
from gaohe.setup_flow import (
    SETUP_PAGE_CSP, SetupState, _handler, _page, setup_state, validate_setup_form, write_setup_config,
)
from gaohe.storage import Store


FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


def valid_form(**changes: str) -> dict[str, str]:
    form = {
        "provider": "gemini",
        "model": "gemini-2.5-flash-lite",
        "api_key": "private-key",
        "media_name": "Example News",
        "source_url": "https://example.test/feed.xml",
    }
    form.update(changes)
    return form


@contextmanager
def setup_server(settings, store, env_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(settings, store, env_path))
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        yield server, connection
    finally:
        connection.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def form_token(connection) -> str:
    connection.request("GET", "/")
    page = connection.getresponse().read().decode("utf-8")
    match = re.search(r"name='form_token' value='([^']+)'", page)
    assert match, "setup page must embed a form token"
    return match.group(1)


def post(connection, form, headers=None):
    body = urlencode(form).encode("utf-8")
    connection.request("POST", "/", body, {**FORM_HEADERS, **(headers or {})})
    response = connection.getresponse()
    return response, response.read().decode("utf-8")


def initialized_store(tmp_path):
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    return store


def assert_security_headers(response, csp=SETUP_PAGE_CSP):
    assert response.getheader("Content-Security-Policy") == csp
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    assert response.getheader("Referrer-Policy") == "no-referrer"
    assert response.getheader("Cache-Control") == "no-store"


@pytest.mark.parametrize(("field", "value", "error"), [
    ("provider", "   ", "請填寫AI 服務供應商。"),
    ("model", "\t", "請填寫AI 模型。"),
    ("api_key", "", "請填寫AI API 金鑰。"),
    ("media_name", "\n", "請填寫媒體名稱。"),
    ("source_url", "", "請填寫來源網址。"),
])
def test_setup_validation_rejects_required_or_whitespace_fields(field, value, error):
    assert error in validate_setup_form(valid_form(**{field: value}))


def test_setup_validation_rejects_missing_and_non_string_fields():
    errors = validate_setup_form({"provider": None, "model": 3})  # type: ignore[dict-item]

    assert errors == [
        "請填寫AI 服務供應商。", "請填寫AI 模型。", "請填寫AI API 金鑰。", "請填寫媒體名稱。", "請填寫來源網址。",
    ]


def test_setup_validation_accepts_valid_form():
    assert validate_setup_form(valid_form()) == []


@pytest.mark.parametrize("url", [
    "not a url", "ftp://example.test/feed", "https://user:secret@example.test/feed", "https://@example.test/feed",
    "https://example.test/a b", "https://example.test:abc/feed", "https://example.test:99999/feed",
    "https://example.test/feed\nnext",
])
def test_setup_validation_rejects_unsafe_urls_without_echoing_secrets(url):
    errors = validate_setup_form(valid_form(source_url=url, api_key="private-key"))

    assert "來源網址必須是有效的 HTTP(S) 網址。" in errors or "來源網址含有無效字元。" in errors
    assert "private-key" not in " ".join(errors)
    assert "secret" not in " ".join(errors)


def test_setup_validation_rejects_unsupported_provider_without_echoing_input():
    errors = validate_setup_form(valid_form(provider="unknown-private"))

    assert errors == ["目前只支援 Gemini，請使用 Gemini 作為 AI 服務供應商。"]
    assert "unknown-private" not in " ".join(errors)


def test_setup_validation_never_echoes_key():
    errors = validate_setup_form(valid_form(api_key="key\nprivate"))

    assert errors == ["AI API 金鑰含有無效字元。"]
    assert "private" not in " ".join(errors)


def test_write_setup_config_is_atomic_utf8_without_bom_and_preserves_other_values(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("OTHER=value\nLLM_MODEL=old\n# keep this\n", encoding="utf-8")

    write_setup_config(valid_form(), env_path)

    raw = env_path.read_bytes()
    text = raw.decode("utf-8")
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert "OTHER=value" in text and "# keep this" in text
    assert "LLM_PROVIDER=gemini" in text
    assert "LLM_MODEL=gemini-2.5-flash-lite" in text
    assert "LLM_API_KEY=private-key" in text
    assert list(tmp_path.glob(".env.tmp-*")) == []


def test_write_setup_config_rejects_invalid_form_without_creating_temp_files(tmp_path):
    env_path = tmp_path / ".env"

    with pytest.raises(ValueError, match="invalid setup form"):
        write_setup_config(valid_form(api_key="private\nkey"), env_path)

    assert not env_path.exists()
    assert list(tmp_path.glob(".env.tmp-*")) == []


def test_write_setup_config_cleans_temp_file_when_replace_fails(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(setup_flow.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_setup_config(valid_form(), env_path)

    assert list(tmp_path.glob(".env.tmp-*")) == []


def test_local_setup_route_masks_key_and_does_not_duplicate_article_url_source(tmp_path):
    store = initialized_store(tmp_path)
    store.add_source(Source(None, "Existing", "https://example.test/list", "https://example.test/feed.xml"))
    env_path = tmp_path / ".env"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(Settings(), store, env_path))
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/")
        page = connection.getresponse().read().decode("utf-8")
        assert "private-key" not in page and "AI 金鑰未設定" in page
        token = re.search(r"name='form_token' value='([^']+)'", page).group(1)

        response, body = post(connection, {**valid_form(), "form_token": token})
        assert response.status == 200
        assert "稿核已完成設定" in body and "private-key" not in body
    finally:
        connection.close()
        thread.join(timeout=2)
        server.server_close()

    assert len(store.list_sources()) == 1
    assert "private-key" in env_path.read_text(encoding="utf-8")


def test_setup_state_masks_key_and_source_registration_is_idempotent(tmp_path):
    store = initialized_store(tmp_path)
    store.add_source(Source(None, "Example", "https://example.test/feed.xml"))

    state = setup_state(Settings(llm_api_key="private-key"), store)

    assert state == SetupState(True, True, 1, ())
    assert "private-key" not in repr(state)


def test_setup_state_warns_in_zh_tw_until_configured(tmp_path):
    state = setup_state(Settings(), initialized_store(tmp_path))

    assert state.warnings == ("請填寫下方欄位以完成本機設定。",)


def test_setup_page_identifies_gemini_as_the_only_current_provider():
    page = _page(SetupState(False, False, 0, ()))

    assert "Gemini（目前唯一支援）" in page
    assert "其他 AI 服務供應商將在未來版本支援。" in page


def test_setup_page_is_zh_tw_and_explains_own_key_and_external_transfer():
    page = _page(SetupState(False, True, 2, ()), form_token="tok'en")

    assert '<html lang="zh-Hant-TW">' in page
    assert "自己的 Gemini API 金鑰" in page and "不提供共用的雲端帳號" in page
    assert "文章內容可能會送到你選擇的 AI 服務供應商" in page
    assert "不會繞過登入、付費牆、CAPTCHA" in page
    assert "本機狀態：AI 金鑰已設定；媒體來源 2 個。" in page
    assert "value='tok&#x27;en'" in page
    assert "type='password'" in page and "autocomplete='off'" in page


def test_setup_page_escapes_error_messages():
    page = _page(SetupState(False, False, 0, ()), ("<b>bad</b>",))

    assert "&lt;b&gt;bad&lt;/b&gt;" in page and "<b>bad</b>" not in page
    assert "請檢查以下欄位" in page


def test_setup_complete_page_is_zh_tw():
    page = _page(SetupState(True, True, 1, ()), complete=True)

    assert "稿核已完成設定" in page and "gaohe serve" in page and "<form" not in page


def test_setup_server_sends_security_headers_and_embeds_token(tmp_path):
    with setup_server(Settings(), initialized_store(tmp_path), tmp_path / ".env") as (_, connection):
        connection.request("GET", "/")
        response = connection.getresponse()
        page = response.read().decode("utf-8")

    assert response.status == 200
    assert_security_headers(response)
    assert "form-action 'self'" in response.getheader("Content-Security-Policy")
    assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    assert "name='form_token'" in page


@pytest.mark.parametrize("host", ["evil.example", "evil.example:{port}", "127.0.0.1:{other}", "localhost"])
def test_setup_server_rejects_foreign_host_for_get_and_post(tmp_path, host):
    env_path = tmp_path / ".env"
    store = initialized_store(tmp_path)
    with setup_server(Settings(), store, env_path) as (server, connection):
        token = form_token(connection)
        header = host.format(port=server.server_port, other=server.server_port + 1)
        connection.request("GET", "/", headers={"Host": header})
        get_response = connection.getresponse()
        get_body = get_response.read().decode("utf-8")
        post_response, post_body = post(connection, {**valid_form(), "form_token": token}, {"Host": header})

    assert get_response.status == 421 and post_response.status == 421
    assert_security_headers(get_response)
    assert_security_headers(post_response)
    assert "form_token" not in get_body and "private-key" not in post_body
    assert not env_path.exists()
    assert store.list_sources() == []


@pytest.mark.parametrize("token", [None, "", "wrong-token"])
def test_setup_post_requires_the_page_token(tmp_path, token):
    env_path = tmp_path / ".env"
    store = initialized_store(tmp_path)
    form = valid_form()
    if token is not None:
        form["form_token"] = token
    with setup_server(Settings(), store, env_path) as (_, connection):
        response, body = post(connection, form)

    assert response.status == 403
    assert "表單已失效" in body and "private-key" not in body
    assert_security_headers(response)
    assert not env_path.exists()
    assert store.list_sources() == []


def test_setup_post_validation_errors_are_zh_tw_and_keep_token(tmp_path):
    env_path = tmp_path / ".env"
    with setup_server(Settings(), initialized_store(tmp_path), env_path) as (_, connection):
        token = form_token(connection)
        response, body = post(connection, {**valid_form(provider="other-secret", api_key="k\x01private"), "form_token": token})

    assert response.status == 400
    assert "目前只支援 Gemini" in body and "AI API 金鑰含有無效字元。" in body
    assert "other-secret" not in body and "private" not in body
    assert f"value='{token}'" in body
    assert not env_path.exists()


def raw_request(port: int, payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(payload)
        chunks = []
        while chunk := connection.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.parametrize(("length", "status"), [("abc", b"400"), ("-1", b"400"), ("\u00b2", b"400"), ("70000", b"413")])
def test_setup_post_rejects_malformed_or_oversized_bodies(tmp_path, length, status):
    env_path = tmp_path / ".env"
    with setup_server(Settings(), initialized_store(tmp_path), env_path) as (server, _):
        port = server.server_port
        request = (
            f"POST / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/x-www-form-urlencoded\r\n"
            f"Content-Length: {length}\r\nConnection: close\r\n\r\n"
        ).encode("utf-8")
        reply = raw_request(port, request)

    assert reply.split(b" ", 2)[1] == status
    assert b"Content-Security-Policy: default-src 'none'" in reply
    assert not env_path.exists()


def test_setup_post_rejects_too_many_fields(tmp_path):
    env_path = tmp_path / ".env"
    with setup_server(Settings(), initialized_store(tmp_path), env_path) as (_, connection):
        response, _ = post(connection, {f"field{index}": "x" for index in range(40)})

    assert response.status == 400
    assert not env_path.exists()


def test_setup_server_unknown_path_and_methods_are_rejected_with_headers(tmp_path):
    with setup_server(Settings(), initialized_store(tmp_path), tmp_path / ".env") as (_, connection):
        connection.request("GET", "/other")
        not_found = connection.getresponse()
        not_found.read()
        connection.request("PUT", "/", b"", FORM_HEADERS)
        not_allowed = connection.getresponse()
        not_allowed.read()
        connection.request("HEAD", "/")
        head = connection.getresponse()
        head_body = head.read()

    assert not_found.status == 404
    assert_security_headers(not_found)
    assert not_allowed.status == 405 and not_allowed.getheader("Allow") == "GET, HEAD, POST"
    assert_security_headers(not_allowed)
    assert head.status == 200 and head_body == b"" and int(head.getheader("Content-Length")) > 0


class FailingSourceStore:
    def __init__(self, fail_list: bool = False):
        self.fail_list = fail_list

    def list_sources(self):
        if self.fail_list:
            raise sqlite3.OperationalError("database is locked at C:/secret/path")
        return []

    def add_source(self, source):
        raise sqlite3.OperationalError("disk I/O error near secret-detail")


def test_setup_post_store_failure_returns_fixed_message(tmp_path):
    env_path = tmp_path / ".env"
    with setup_server(Settings(), FailingSourceStore(), env_path) as (_, connection):
        token = form_token(connection)
        response, body = post(connection, {**valid_form(), "form_token": token})

    assert response.status == 500
    assert "無法儲存本機設定。" in body
    assert "secret-detail" not in body and "private-key" not in body


def test_setup_get_store_failure_returns_fixed_message(tmp_path):
    with setup_server(Settings(), FailingSourceStore(fail_list=True), tmp_path / ".env") as (_, connection):
        connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read().decode("utf-8")

    assert response.status == 500
    assert "目前無法讀取本機資料" in body and "secret" not in body
    assert_security_headers(response)
