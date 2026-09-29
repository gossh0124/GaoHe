from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
import socket
import sqlite3
from threading import Thread

import pytest

import gaohe.web as web
from gaohe.config import Settings
from gaohe.domain import Finding
from gaohe.web import STATUS_PAGE_CSP, STORE_UNAVAILABLE_NOTICE, is_loopback_host, security_headers, start_server


SECRET_KEY = "AIza-web-server-secret-key"


def settings(**changes) -> Settings:
    values = {
        "llm_provider": "gemini",
        "llm_model": "gemini-2.5-flash-lite",
        "llm_api_key": SECRET_KEY,
        "web_search_provider": "none",
        "data_dir": Path("local-data"),
    }
    values.update(changes)
    return Settings(**values)


def snapshot(title: str = "市府公布預算") -> dict[str, object]:
    text = "市府表示預算增加三成，議員質疑數字。"
    return {
        "inbox": [{
            "article_id": 1, "revision_id": 11, "title": title,
            "url": "https://news.example/a?api_key=article-url-secret", "source": "範例日報",
            "published_at": "2026-09-20T01:00:00Z", "updated_at": "2026-09-20T02:00:00Z", "text": text,
            "analysis_status": "completed",
            "annotations": (Finding(7, 11, 3, "factual_contradiction", "預算數字與公報不符", 5, 11, "resolved", "retrieved", True),),
            "evidence": [{
                "url": "https://record.example/budget?token=evidence-url-secret", "title": "公報",
                "provider": "direct", "retrieved_at": "2026-09-20T03:00:00Z", "relation": "contradicts",
                "status": "retrieved", "source_kind": "direct", "rationale": "公報寫增加一成",
            }, {
                "url": "https://gov.example/report", "title": "預算書", "provider": "direct",
                "retrieved_at": "2026-09-20T03:00:00Z", "relation": "contradicts", "status": "retrieved",
                "source_kind": "direct", "rationale": "預算書列出增加一成",
            }],
            "pending_findings": 0,
        }],
        "findings": [{
            "id": 7, "revision_id": 11, "article_id": 1, "article_title": title,
            "article_url": "https://user:pw-secret@news.example/a", "source": "範例日報",
            "finding_type": "factual_contradiction", "summary": "預算數字與公報不符", "start": 5, "end": 11,
            "status": "resolved", "evidence_status": "retrieved", "visible": True, "review_status": "unreviewed",
            "reviewed_at": None, "evidence": [],
        }],
        "comparisons": [],
        "sources": [{
            "id": 1, "name": "範例日報", "enabled": True, "status": "ok", "checked_at": "2026-09-20T02:00:00Z",
            "candidates_seen": 4, "error": None,
        }],
        "last_run": None,
        "analysis": {"pending": 0, "running": 0, "completed": 1, "failed": 0, "skipped": 0, "unanalyzed": 0},
    }


class FakeStore:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = snapshot() if result is None else result
        self.error = error
        self.calls = 0

    def dashboard_snapshot(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


@contextmanager
def running(store, config: Settings | None = None):
    server = start_server(config or settings(), store)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def fetch(server, method: str = "GET", path: str = "/", headers=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response, response.read().decode("utf-8")
    finally:
        connection.close()


def raw_request(port: int, payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(payload)
        chunks = []
        while chunk := connection.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def assert_security_headers(response):
    assert response.getheader("Content-Security-Policy") == STATUS_PAGE_CSP
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    assert response.getheader("Referrer-Policy") == "no-referrer"
    assert response.getheader("Cache-Control") == "no-store"


def test_round_trip_renders_store_snapshot_with_security_headers():
    store = FakeStore()
    with running(store) as server:
        response, page = fetch(server)

    assert response.status == 200
    assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    assert int(response.getheader("Content-Length")) == len(page.encode("utf-8"))
    assert_security_headers(response)
    assert STATUS_PAGE_CSP == (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'"
    )
    assert '<html lang="zh-Hant-TW">' in page
    for expected in ("市府公布預算", "範例日報", "預算數字與公報不符", "已完成分析", "預算書列出增加一成", "gemini-2.5-flash-lite"):
        assert expected in page
    assert "公報寫增加一成" not in page  # its evidence URL carried a token, so the whole entry is withheld
    assert "mark-factual_contradiction" in page
    assert response.getheader("Server") == "GaoHe"
    assert SECRET_KEY not in page
    assert store.calls == 1


def test_round_trip_never_shows_secrets_carried_in_urls():
    with running(FakeStore()) as server:
        _, page = fetch(server)

    for secret in ("article-url-secret", "evidence-url-secret", "pw-secret", SECRET_KEY):
        assert secret not in page
    assert "https://gov.example/report" in page
    assert "news.example/a" not in page  # both article links carried credentials, so neither became a link


def test_every_request_reads_a_fresh_snapshot():
    store = FakeStore()
    with running(store) as server:
        _, first = fetch(server)
        store.result = snapshot(title="第二篇報導")
        _, second = fetch(server)

    assert "市府公布預算" in first and "第二篇報導" in second
    assert store.calls == 2


def test_store_runtime_section_is_replaced_by_settings():
    hostile = snapshot()
    hostile["runtime"] = {"llm_api_key": "store-supplied-leak", "llm_model": "store-model"}
    with running(FakeStore(hostile)) as server:
        _, page = fetch(server)

    assert "store-supplied-leak" not in page and "store-model" not in page
    assert "gemini-2.5-flash-lite" in page


@pytest.mark.parametrize("error", [
    sqlite3.OperationalError("database is locked at C:/Users/secret-user/gaohe.db"),
    sqlite3.DatabaseError("file is not a database secret-detail"),
    OSError("permission denied secret-detail"),
    ValueError("unsupported schema secret-detail"),
])
def test_store_failure_renders_runtime_only_with_zh_tw_notice(error):
    with running(FakeStore(error=error)) as server:
        response, page = fetch(server)

    assert response.status == 200
    assert_security_headers(response)
    assert STORE_UNAVAILABLE_NOTICE in page
    assert "secret" not in page
    assert "gemini-2.5-flash-lite" in page and "id='runtime'" in page and "id='help'" in page
    for section in ("id='inbox'", "id='findings'", "id='comparisons'", "id='sources'", "id='analysis'"):
        assert section not in page
    assert "目前沒有新文章" not in page  # an unreadable store never claims there is nothing new


@pytest.mark.parametrize("result", [["not", "a", "mapping"], "text", 3])
def test_non_mapping_store_result_renders_notice(result):
    store = FakeStore()
    store.result = result
    with running(store) as server:
        _, page = fetch(server)

    assert STORE_UNAVAILABLE_NOTICE in page


def test_missing_store_renders_notice():
    with running(None) as server:
        response, page = fetch(server)

    assert response.status == 200 and STORE_UNAVAILABLE_NOTICE in page


def test_unexpected_store_error_returns_fixed_500_without_details():
    with running(FakeStore(error=RuntimeError("boom secret-detail"))) as server:
        response, page = fetch(server)

    assert response.status == 500
    assert_security_headers(response)
    assert "secret-detail" not in page and "boom" not in page
    assert "稿核本機頁面暫時無法顯示" in page


@pytest.mark.parametrize("host", [
    "evil.example", "evil.example:{port}", "127.0.0.1:{other}", "localhost:{other}", "127.0.0.1", "localhost",
    "localhost.:{port}", "127.0.0.2:{port}", "0.0.0.0:{port}", "[::1]", "127.0.0.1:{port}.evil.example",
    "user@127.0.0.1:{port}", "",
])
def test_foreign_host_header_is_rejected_before_reading_the_store(host):
    store = FakeStore()
    with running(store) as server:
        header = host.format(port=server.server_port, other=server.server_port + 1)
        response, page = fetch(server, headers={"Host": header})

    assert response.status == 421
    assert_security_headers(response)
    assert "市府公布預算" not in page and "gemini" not in page
    assert store.calls == 0


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "LOCALHOST:{port}", "[::1]:{port}", " localhost:{port} "])
def test_loopback_host_headers_are_accepted(host):
    with running(FakeStore()) as server:
        response, _ = fetch(server, headers={"Host": host.format(port=server.server_port)})

    assert response.status == 200


def test_missing_or_repeated_host_header_is_rejected():
    store = FakeStore()
    with running(store) as server:
        port = server.server_port
        missing = raw_request(port, b"GET / HTTP/1.1\r\nConnection: close\r\n\r\n")
        repeated = raw_request(
            port,
            f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nHost: evil.example\r\nConnection: close\r\n\r\n".encode(),
        )

    assert missing.startswith(b"HTTP/1.0 421") and repeated.startswith(b"HTTP/1.0 421")
    assert store.calls == 0


@pytest.mark.parametrize("path", ["/index.html", "/?q=1", "/favicon.ico", "/%2e%2e/", "/runtime", "/../"])
def test_non_root_paths_are_404_with_security_headers(path):
    store = FakeStore()
    with running(store) as server:
        response, page = fetch(server, path=path)

    assert response.status == 404
    assert_security_headers(response)
    assert "找不到這個頁面" in page
    assert store.calls == 0


def test_head_returns_headers_without_body():
    with running(FakeStore()) as server:
        get_response, page = fetch(server)
        head_response, body = fetch(server, method="HEAD")

    assert head_response.status == 200 and body == ""
    assert head_response.getheader("Content-Length") == get_response.getheader("Content-Length")
    assert_security_headers(head_response)
    assert page


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_other_methods_are_not_allowed(method):
    store = FakeStore()
    with running(store) as server:
        response, page = fetch(server, method=method)

    assert response.status == 405
    assert response.getheader("Allow") == "GET, HEAD"
    assert_security_headers(response)
    assert store.calls == 0 and "市府公布預算" not in page


def test_method_not_allowed_still_applies_host_guard():
    with running(FakeStore()) as server:
        response, _ = fetch(server, method="POST", headers={"Host": "evil.example"})

    assert response.status == 421


def test_unknown_method_and_malformed_request_still_send_security_headers():
    with running(FakeStore()) as server:
        port = server.server_port
        unknown = raw_request(port, f"BREW / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        malformed = raw_request(port, b"GET / extra HTTP/1.1\r\n\r\n")

    for reply in (unknown, malformed):
        assert reply.split(b" ", 2)[1] in {b"400", b"501"}
        assert f"Content-Security-Policy: {STATUS_PAGE_CSP}".encode() in reply
        assert b"X-Content-Type-Options: nosniff" in reply
        assert b"Cache-Control: no-store" in reply
        assert b"Python" not in reply


def test_is_loopback_host_requires_bound_port_except_default_http_port():
    assert is_loopback_host("127.0.0.1:8123", 8123)
    assert is_loopback_host("[::1]:8123", 8123)
    assert not is_loopback_host("127.0.0.1:8124", 8123)
    assert not is_loopback_host("127.0.0.1", 8123)
    assert is_loopback_host("localhost", 80) and is_loopback_host("localhost:80", 80)
    assert not is_loopback_host(None, 80)
    assert not is_loopback_host(b"localhost:80", 80)


def test_security_headers_are_complete():
    headers = dict(security_headers("default-src 'none'"))

    assert headers["Content-Security-Policy"] == "default-src 'none'"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "no-store"


class _StoppedServer:
    def __init__(self):
        self.closed = False

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        self.closed = True


def _capture_start_server(monkeypatch):
    captured = {}
    server = _StoppedServer()

    def fake_start_server(config, store, host="127.0.0.1", port=0):
        captured.update(settings=config, store=store, host=host, port=port)
        return server

    monkeypatch.setattr(web, "start_server", fake_start_server)
    return captured, server


def test_serve_builds_initialized_store_from_settings(tmp_path, monkeypatch):
    for name in ("DATA_DIR", "LLM_API_KEY", "LLM_MODEL", "LLM_PROVIDER", "GOOGLE_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    data_dir = tmp_path / "data"
    env_file.write_text(f"DATA_DIR={data_dir}\nLLM_API_KEY={SECRET_KEY}\n", encoding="utf-8")
    captured, server = _capture_start_server(monkeypatch)

    web.serve(host="127.0.0.1", port=8765, env_file=env_file)

    assert captured["host"] == "127.0.0.1" and captured["port"] == 8765
    assert captured["settings"].data_dir == data_dir
    assert captured["store"].path == data_dir / "gaohe.db"
    assert (data_dir / "gaohe.db").is_file()
    assert server.closed


def test_serve_keeps_serving_when_store_cannot_initialize(tmp_path, monkeypatch):
    monkeypatch.delenv("DATA_DIR", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATA_DIR={tmp_path / 'data'}\n", encoding="utf-8")
    captured, server = _capture_start_server(monkeypatch)

    def broken_initialize(self):
        raise sqlite3.DatabaseError("schema too new")

    monkeypatch.setattr(web.Store, "initialize", broken_initialize)

    web.serve(env_file=env_file)

    assert captured["store"] is not None and server.closed


def test_source_not_modified_status_has_a_zh_tw_label():
    from gaohe.web import render_status_page

    page = render_status_page({"sources": [{"id": 1, "name": "Example", "enabled": True, "status": "not_modified",
                                            "checked_at": "2026-09-29T00:00:00Z", "candidates_seen": 0, "error": None}]})
    assert "來源未變更" in page and "狀態不明" not in page


def test_start_server_binds_ipv6_loopback_when_available():
    import socket

    import pytest

    from gaohe.config import Settings
    from gaohe.web import start_server

    if not socket.has_ipv6:
        pytest.skip("IPv6 unavailable")
    try:
        server = start_server(Settings(), None, "::1", 0)
    except OSError:
        pytest.skip("IPv6 loopback not configured on this machine")
    try:
        assert server.address_family == socket.AF_INET6
    finally:
        server.server_close()
