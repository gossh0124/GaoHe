from collections.abc import Mapping
from dataclasses import dataclass
import hmac
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
from threading import Thread
from urllib.parse import parse_qs
import webbrowser

from .config import Settings
from .domain import Source
from .safety import is_source_url as _is_source_url
from .storage import Store
from .web import NOT_FOUND_PAGE, LoopbackHandler


@dataclass(frozen=True)
class SetupState:
    configured: bool
    has_llm_key: bool
    source_count: int
    warnings: tuple[str, ...]


_REQUIRED_FIELDS = (
    ("provider", "AI 服務供應商"),
    ("model", "AI 模型"),
    ("api_key", "AI API 金鑰"),
    ("media_name", "媒體名稱"),
    ("source_url", "來源網址"),
)
_SETUP_KEYS = {"LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY"}
SETUP_PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'self'"
_MAX_FORM_BYTES = 65536
_MAX_FORM_FIELDS = 32
_FORM_TOKEN_FIELD = "form_token"
# Fixed, secret-free messages: none of them ever echoes what the user typed.
_UNSUPPORTED_PROVIDER = "目前只支援 Gemini，請使用 Gemini 作為 AI 服務供應商。"
_INVALID_SOURCE_URL = "來源網址必須是有效的 HTTP(S) 網址。"
_SETUP_WARNING = "請填寫下方欄位以完成本機設定。"
_SAVE_FAILED = "無法儲存本機設定。"
_STALE_FORM = "這個設定表單已失效，請重新整理頁面後再送出。"
_BAD_REQUEST = "無法讀取送出的表單，請重新整理頁面後再試一次。"
_TOO_LARGE = "送出的表單太大，請確認欄位內容後再試一次。"
_STATE_UNAVAILABLE = "目前無法讀取本機資料，請關閉這個分頁後重新執行設定。"


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def validate_setup_form(form: Mapping[str, str]) -> list[str]:
    """Return fixed, secret-free errors for the local setup form."""
    errors: list[str] = []
    values: dict[str, str] = {}
    for field, label in _REQUIRED_FIELDS:
        value = form.get(field, "")
        if not isinstance(value, str) or not value.strip():
            errors.append(f"請填寫{label}。")
        elif _has_control(value):
            errors.append(f"{label}含有無效字元。")
        else:
            values[field] = value.strip()
    provider = values.get("provider")
    if provider is not None and provider != "gemini":
        errors.append(_UNSUPPORTED_PROVIDER)
    source_url = values.get("source_url")
    raw_source_url = form.get("source_url")
    if source_url is not None and (not isinstance(raw_source_url, str) or not _is_source_url(raw_source_url)):
        errors.append(_INVALID_SOURCE_URL)
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
    warnings = () if configured else (_SETUP_WARNING,)
    return SetupState(configured, settings.has_llm_key, source_count, warnings)


_SETUP_STYLE = (
    "body{background:#faf9f7;color:#1f1d1a;margin:0;"
    "font:16px/1.65 system-ui,'Microsoft JhengHei','PingFang TC','Noto Sans TC',sans-serif}"
    "main{margin:auto;max-width:720px;padding:1.5rem 1rem 3rem}"
    "section,form{background:#ffffff;border:1px solid #d9d3cc;border-radius:.5rem;margin:1rem 0;padding:1rem 1.25rem}"
    "label{display:block;font-weight:600;margin:.75rem 0 .2rem}"
    "input{box-sizing:border-box;font:inherit;padding:.35rem .5rem;width:100%}"
    ".hint{color:#5a534b;font-size:.9rem;font-weight:normal}"
    "button{font:inherit;margin-top:1rem;padding:.45rem 1rem}"
    "code{font-family:Consolas,ui-monospace,monospace;font-size:.9em}"
    ":focus-visible{outline:3px solid #1d5b8c;outline-offset:2px}"
)


def _document(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="zh-Hant-TW"><head><meta charset="utf-8">'
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><style>{_SETUP_STYLE}</style></head><body><main>{body}</main></body></html>"
    )


def _message(text: str) -> str:
    return _document("稿核設定", f"<h1>稿核設定</h1><p role='alert'>{escape(text)}</p>")


_DISCLOSURE = (
    "<section aria-labelledby='disclosure'><h2 id='disclosure'>外部傳送告知</h2><ul>"
    "<li>分析時，文章內容可能會送到你選擇的 AI 服務供應商（目前是 Gemini）。</li>"
    "<li>若另外啟用 Firecrawl，網址或頁面內容可能會送到 Firecrawl；它是外部服務，可能有額度、速率限制、登入牆或付費牆等限制。</li>"
    "<li>稿核不會繞過登入、付費牆、CAPTCHA 或網站安全控制。</li>"
    "<li>API 金鑰只寫入這台電腦的 <code>.env</code>，不會寫入資料庫，也不會顯示在頁面或錯誤訊息中。</li>"
    "</ul></section>"
)
_FORM_FIELDS = (
    "<label>AI 服務供應商 <span class='hint'>Gemini（目前唯一支援）</span>"
    "<input name='provider' value='gemini' readonly aria-readonly='true' required></label>"
    "<label>AI 模型 <span class='hint'>例如 gemini-2.5-flash-lite</span>"
    "<input name='model' required autocomplete='off'></label>"
    "<label>Gemini API 金鑰 <span class='hint'>請使用你自己的金鑰</span>"
    "<input name='api_key' type='password' required autocomplete='off'></label>"
    "<label>媒體名稱 <input name='media_name' required></label>"
    "<label>RSS 或新聞列表網址 <input name='source_url' type='url' required></label>"
    "<button type='submit'>儲存本機設定</button>"
)


def _page(state: SetupState, errors: tuple[str, ...] = (), complete: bool = False, form_token: str = "") -> str:
    if complete:
        return _document("稿核設定完成", (
            "<h1>稿核已完成設定</h1><p>你的本機設定與媒體來源已儲存，可以關閉這個分頁。</p>"
            "<p>之後可以用 <code>gaohe watch --once</code> 立即檢查來源，"
            "或用 <code>gaohe serve</code> 開啟本機監測頁。</p>"
        ))
    messages = "".join(f"<li>{escape(message)}</li>" for message in errors)
    error_html = f"<section role='alert'><h2>請檢查以下欄位</h2><ul>{messages}</ul></section>" if messages else ""
    key_state = "已設定" if state.has_llm_key else "未設定"
    token_html = ""
    if form_token:
        token_html = f"<input type='hidden' name='{_FORM_TOKEN_FIELD}' value='{escape(form_token, quote=True)}'>"
    byok = (
        "<p>每位下載稿核的人都要輸入自己的 Gemini API 金鑰（自備金鑰）。這個專案不提供共用的雲端帳號；"
        "Gemini 的額度、使用條款與速率限制都屬於你自己的帳號。其他 AI 服務供應商將在未來版本支援。</p>"
    )
    return _document("稿核設定", (
        f"<h1>在這台電腦設定稿核</h1>{byok}"
        f"<p>本機狀態：AI 金鑰{key_state}；媒體來源 {int(state.source_count)} 個。</p>{error_html}{_DISCLOSURE}"
        f"<form method='post' action='/'>{token_html}{_FORM_FIELDS}</form>"
    ))


def _content_length(handler: BaseHTTPRequestHandler) -> int | None:
    """Return the declared body length, or None when it is missing, repeated, malformed or negative."""
    values = handler.headers.get_all("Content-Length") or []
    if len(values) != 1:
        return None
    raw = values[0].strip()
    return int(raw) if raw.isascii() and raw.isdigit() else None


def _form_values(handler: BaseHTTPRequestHandler) -> Mapping[str, str]:
    length = _content_length(handler) or 0
    body = handler.rfile.read(min(length, _MAX_FORM_BYTES)).decode("utf-8", errors="replace")
    parsed = parse_qs(body, keep_blank_values=True, max_num_fields=_MAX_FORM_FIELDS)
    return {name: values[0] if len(values) == 1 else "" for name, values in parsed.items()}


def _add_source_once(store: Store, form: Mapping[str, str]) -> None:
    source_url = form["source_url"].strip()
    if any(source_url in (source.feed_url, source.article_url) for source in store.list_sources()):
        return
    store.add_source(Source(None, form["media_name"].strip(), source_url))


def _handler(settings: Settings, store: Store, env_path: Path):
    # One unguessable token per wizard run: a page on another site cannot read it, so it cannot
    # submit this form on the user's behalf (the Host guard already blocks DNS rebinding).
    form_token = secrets.token_urlsafe(32)

    class SetupHandler(LoopbackHandler):
        content_security_policy = SETUP_PAGE_CSP
        allowed_methods = ("GET", "HEAD", "POST")

        def _reply(self, status: int, page: str, head_only: bool = False) -> None:
            self.send_page(status, page, head_only=head_only)

        def _show_form(self, head_only: bool) -> None:
            if not self.host_allowed():
                self.reject_host(head_only)
                return
            if self.path != "/":
                self._reply(HTTPStatus.NOT_FOUND, NOT_FOUND_PAGE, head_only)
                return
            try:
                page = _page(setup_state(settings, store), form_token=form_token)
            except (OSError, ValueError, sqlite3.Error):
                self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, _message(_STATE_UNAVAILABLE), head_only)
                return
            self._reply(HTTPStatus.OK, page, head_only)

        def do_GET(self) -> None:
            self._show_form(head_only=False)

        def do_HEAD(self) -> None:
            self._show_form(head_only=True)

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = LoopbackHandler.method_not_allowed

        def do_POST(self) -> None:
            if not self.host_allowed():
                self.reject_host()
                return
            if self.path != "/":
                self._reply(HTTPStatus.NOT_FOUND, NOT_FOUND_PAGE)
                return
            length = _content_length(self)
            if length is None:
                self._reply(HTTPStatus.BAD_REQUEST, _message(_BAD_REQUEST))
                return
            if length > _MAX_FORM_BYTES:
                self._reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, _message(_TOO_LARGE))
                return
            try:
                form = _form_values(self)
            except ValueError:
                self._reply(HTTPStatus.BAD_REQUEST, _message(_BAD_REQUEST))
                return
            submitted = form.get(_FORM_TOKEN_FIELD, "")
            if not hmac.compare_digest(submitted.encode("utf-8"), form_token.encode("utf-8")):
                self._reply(HTTPStatus.FORBIDDEN, _message(_STALE_FORM))
                return
            try:
                errors = validate_setup_form(form)
                if errors:
                    page = _page(setup_state(settings, store), tuple(errors), form_token=form_token)
                    self._reply(HTTPStatus.BAD_REQUEST, page)
                    return
                write_setup_config(form, env_path)
                _add_source_once(store, form)
                done = _page(setup_state(Settings(llm_api_key="configured"), store), complete=True)
            except (OSError, ValueError, sqlite3.Error):
                self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, _message(_SAVE_FAILED))
                return
            self._reply(HTTPStatus.OK, done)
            Thread(target=self.server.shutdown, daemon=True).start()

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
