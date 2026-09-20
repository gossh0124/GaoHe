from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
from threading import Thread
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .config import Settings
from .domain import Source
from .storage import Store


@dataclass(frozen=True)
class SetupState:
    configured: bool
    has_llm_key: bool
    source_count: int
    warnings: tuple[str, ...]


_REQUIRED_FIELDS = (
    ("provider", "Provider"),
    ("model", "Model"),
    ("api_key", "AI API key"),
    ("media_name", "Media name"),
    ("source_url", "Source URL"),
)
_SETUP_KEYS = {"LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY"}


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _is_source_url(value: str) -> bool:
    if any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def validate_setup_form(form: Mapping[str, str]) -> list[str]:
    """Return fixed, secret-free errors for the local setup form."""
    errors: list[str] = []
    values: dict[str, str] = {}
    for field, label in _REQUIRED_FIELDS:
        value = form.get(field, "")
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label} is required.")
        elif _has_control(value):
            errors.append(f"{label} contains invalid characters.")
        else:
            values[field] = value.strip()
    provider = values.get("provider")
    if provider is not None and provider != "gemini":
        errors.append("AI provider is not supported.")
    source_url = values.get("source_url")
    raw_source_url = form.get("source_url")
    if source_url is not None and (not isinstance(raw_source_url, str) or not _is_source_url(raw_source_url)):
        errors.append("Source URL must be a valid HTTP(S) URL.")
    return errors


def _updated_env_text(existing: str, values: Mapping[str, str]) -> str:
    lines = existing.splitlines()
    replacements = {
        "LLM_PROVIDER": values["provider"].strip(),
        "LLM_MODEL": values["model"].strip(),
        "LLM_API_KEY": values["api_key"].strip(),
    }
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        key, separator, _ = line.partition("=")
        normalized = key.strip()
        if separator and normalized in _SETUP_KEYS:
            if normalized not in seen:
                output.append(f"{normalized}={replacements[normalized]}")
                seen.add(normalized)
        else:
            output.append(line)
    output.extend(f"{name}={value}" for name, value in replacements.items() if name not in seen)
    return "\n".join(output) + "\n"


def write_setup_config(form: Mapping[str, str], env_path: Path) -> None:
    errors = validate_setup_form(form)
    if errors:
        raise ValueError("invalid setup form")
    env_path = Path(env_path)
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=env_path.parent, prefix=f"{env_path.name}.tmp-",
        ) as handle:
            temporary = Path(handle.name)
            handle.write(_updated_env_text(existing, form))
        os.replace(temporary, env_path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def setup_state(settings: Settings, store: Store) -> SetupState:
    source_count = len(store.list_sources())
    configured = settings.has_llm_key and source_count > 0
    warnings = () if configured else ("Complete the fields below to finish local setup.",)
    return SetupState(configured, settings.has_llm_key, source_count, warnings)


def _page(state: SetupState, errors: tuple[str, ...] = (), complete: bool = False) -> str:
    messages = "".join(f"<li>{escape(message)}</li>" for message in errors)
    error_html = f"<section aria-live='polite'><h2>Check these fields</h2><ul>{messages}</ul></section>" if messages else ""
    key_state = "present" if state.has_llm_key else "missing"
    if complete:
        return "<!doctype html><meta charset='utf-8'><title>GaoHe setup complete</title><main><h1>GaoHe is ready</h1><p>Your local settings and media source were saved. You can close this tab.</p></main>"
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><title>GaoHe setup</title></head><body><main>
<h1>Set up GaoHe on this computer</h1>
<p>Each person who downloads GaoHe enters their own AI key. This repository does not share a cloud account. Provider quota, terms, and rate limits belong to that person's account.</p>
<p>Local readiness: AI key {key_state}; media sources {state.source_count}.</p>{error_html}
<form method='post'><label>AI provider <input name='provider' required></label><br><label>Model <input name='model' required></label><br><label>AI API key <input name='api_key' type='password' required autocomplete='off'></label><br><label>Media name <input name='media_name' required></label><br><label>RSS or list URL <input name='source_url' type='url' required></label><br><button type='submit'>Save local setup</button></form>
</main></body></html>"""


def _form_values(handler: BaseHTTPRequestHandler) -> Mapping[str, str]:
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(min(length, 65536)).decode("utf-8", errors="replace")
    parsed = parse_qs(body, keep_blank_values=True)
    return {name: values[0] if len(values) == 1 else "" for name, values in parsed.items()}


def _add_source_once(store: Store, form: Mapping[str, str]) -> None:
    source_url = form["source_url"].strip()
    if any(source_url in (source.feed_url, source.article_url) for source in store.list_sources()):
        return
    store.add_source(Source(None, form["media_name"].strip(), source_url))


def _handler(settings: Settings, store: Store, env_path: Path):
    class SetupHandler(BaseHTTPRequestHandler):
        def _reply(self, status: int, page: str) -> None:
            payload = page.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            self._reply(200, _page(setup_state(settings, store))) if self.path == "/" else self.send_error(404)

        def do_POST(self) -> None:
            if self.path != "/":
                self.send_error(404)
                return
            form = _form_values(self)
            errors = validate_setup_form(form)
            if errors:
                self._reply(400, _page(setup_state(settings, store), tuple(errors)))
                return
            try:
                write_setup_config(form, env_path)
                _add_source_once(store, form)
            except (OSError, ValueError):
                self._reply(500, _page(setup_state(settings, store), ("Local setup could not be saved.",)))
                return
            self._reply(200, _page(setup_state(Settings(llm_api_key="configured"), store), complete=True))
            Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, format: str, *args: object) -> None:
            return

    return SetupHandler


def run_setup_wizard(settings: Settings, store: Store, open_browser: bool = True, env_path: Path = Path(".env")) -> None:
    """Run a one-shot loopback setup page until a valid local form is submitted."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(settings, store, env_path))
    try:
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{server.server_port}/")
        server.serve_forever()
    finally:
        server.server_close()
