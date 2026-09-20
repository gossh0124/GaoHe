from pathlib import Path
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlencode

import pytest

import gaohe.setup_flow as setup_flow
from gaohe.config import Settings
from gaohe.domain import Source
from gaohe.setup_flow import SetupState, _handler, setup_state, validate_setup_form, write_setup_config
from gaohe.storage import Store


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


@pytest.mark.parametrize(("field", "value", "error"), [
    ("provider", "   ", "Provider is required."),
    ("model", "\t", "Model is required."),
    ("api_key", "", "AI API key is required."),
    ("media_name", "\n", "Media name is required."),
    ("source_url", "", "Source URL is required."),
])
def test_setup_validation_rejects_required_or_whitespace_fields(field, value, error):
    assert error in validate_setup_form(valid_form(**{field: value}))


@pytest.mark.parametrize("url", [
    "not a url", "ftp://example.test/feed", "https://user:secret@example.test/feed", "https://@example.test/feed",
    "https://example.test/a b", "https://example.test:abc/feed", "https://example.test:99999/feed",
    "https://example.test/feed\nnext",
])
def test_setup_validation_rejects_unsafe_urls_without_echoing_secrets(url):
    errors = validate_setup_form(valid_form(source_url=url, api_key="private-key"))

    assert "Source URL must be a valid HTTP(S) URL." in errors or "Source URL contains invalid characters." in errors
    assert "private-key" not in " ".join(errors)
    assert "secret" not in " ".join(errors)


def test_setup_validation_rejects_unsupported_provider_without_echoing_input():
    errors = validate_setup_form(valid_form(provider="unknown-private"))

    assert errors == ["AI provider is not supported."]
    assert "unknown-private" not in " ".join(errors)


def test_setup_validation_never_echoes_key():
    errors = validate_setup_form(valid_form(api_key="key\nprivate"))

    assert errors == ["AI API key contains invalid characters."]
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
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    store.add_source(Source(None, "Existing", "https://example.test/list", "https://example.test/feed.xml"))
    env_path = tmp_path / ".env"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(Settings(), store, env_path))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/")
        page = connection.getresponse().read().decode("utf-8")
        assert "private-key" not in page and "AI key missing" in page

        body = urlencode(valid_form()).encode("utf-8")
        connection.request("POST", "/", body, {"Content-Type": "application/x-www-form-urlencoded"})
        response = connection.getresponse()
        assert response.status == 200
        assert "GaoHe is ready" in response.read().decode("utf-8")
    finally:
        connection.close()
        thread.join(timeout=2)
        server.server_close()

    assert len(store.list_sources()) == 1
    assert "private-key" in env_path.read_text(encoding="utf-8")


def test_setup_state_masks_key_and_source_registration_is_idempotent(tmp_path):
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    store.add_source(Source(None, "Example", "https://example.test/feed.xml"))

    state = setup_state(Settings(llm_api_key="private-key"), store)

    assert state == SetupState(True, True, 1, ())
    assert "private-key" not in repr(state)
