"""The local, read-only result page (zh-TW), served on the loopback interface only.

Every snapshot value is untrusted: text is escaped, links are limited to credential-free
HTTP(S) URLs, and the API key is never part of the page.
"""

from collections.abc import Iterable, Mapping, Sequence
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sqlite3

from .config import Settings, load_settings
from .domain import Finding
from .safety import is_credential_free_http_url, redact_text, redact_url
from .storage import Store


STATUS_PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")

FINDING_LABELS = {"factual_contradiction": "事實矛盾", "unsupported_inference": "推論超出證據"}
EVIDENCE_STATUS_LABELS = {
    "pending": "待查證", "retrieved": "已取得證據", "retrieval_failed": "取回失敗", "insufficient_scope": "搜尋範圍不足",
}
RELATION_LABELS = {"supports": "來源一致", "contradicts": "找到反向證據", "context": "背景資料"}
ANALYSIS_LABELS = {"pending": "待分析", "completed": "已分析", "failed": "分析失敗（會自動重試）"}
SOURCE_STATUS_LABELS = {"ok": "正常", "failed": "檢查失敗", "not checked": "尚未檢查"}
HELP_ITEMS = (
    "淡紅底線＝事實矛盾：可取得的原始頁面直接與文章說法衝突。",
    "淡紫底線＝推論超出證據：文章把推測或因果寫成定論，至少兩個獨立網站都只支持背後的事實。",
    "未標註的句子不代表已查證或已證實，只代表目前沒有形成標註。「待查證」很常見，代表還沒找到足夠證據。",
    "稿核不對整篇文章下判定，也不提供媒體分數；來源檢查失敗是連線狀況，不代表文章有問題。",
    "文章內容與查核問題會送到你設定的 Gemini（含 Google 搜尋）；金鑰只保存在本機 .env。",
)
_MARK_COLORS = {"factual_contradiction": ("#fbe3e1", "#b3261e"), "unsupported_inference": ("#efe5f7", "#6a3d9a")}
_STYLE = (
    "body{background:#faf9f7;color:#1f1f1f;margin:0;font:16px/1.65 system-ui,'Microsoft JhengHei','Noto Sans TC',sans-serif}"
    "main{margin:auto;max-width:960px;padding:1.5rem 1rem 3rem}"
    "section,article{background:#fff;border:1px solid #ddd8d2;border-radius:.5rem;margin:1rem 0;padding:1rem 1.25rem}"
    "h1{font-size:1.5rem}h2{font-size:1.2rem;margin-top:0}h3{font-size:1.05rem;margin:0}"
    ".meta,.empty{color:#5f5953;font-size:.9rem}.article-text{white-space:pre-wrap;overflow-wrap:anywhere}"
    ".annotation{background:transparent;color:inherit;text-decoration-line:underline;text-decoration-thickness:2px;"
    "text-underline-offset:.2em}.detail{font-size:.82em;margin-left:.3rem}"
    + "".join(f".mark-{kind}{{background:{bg};text-decoration-color:{line}}}" for kind, (bg, line) in _MARK_COLORS.items())
    + ".evidence{border-top:1px dashed #ddd8d2;font-size:.92rem;margin-top:.6rem;padding-top:.5rem}"
    "a{color:#1d4f91}ul{padding-left:1.25rem}"
)


def _text(value: object) -> str:
    return escape(str(value)) if value is not None else ""


def _href(url: object) -> str:
    """A link target only for credential-free HTTP(S) URLs, with sensitive parameters masked."""
    return escape(redact_url(url), quote=True) if is_credential_free_http_url(url) else ""


def render_article(text: str, annotations: Sequence[Finding]) -> str:
    """Render the original text once, marking visible findings' spans (clipped to the text)."""
    length = len(text)
    spans = [
        (max(0, f.start), min(length, f.end), f)
        for f in annotations
        if isinstance(f, Finding) and f.visible and f.finding_type in FINDING_LABELS and f.start < f.end
    ]
    boundaries = sorted({0, length, *(point for start, end, _ in spans for point in (start, end) if 0 <= point <= length)})
    pieces: list[str] = []
    for start, end in zip(boundaries, boundaries[1:]):
        source = escape(text[start:end])
        active = sorted((f for left, right, f in spans if left <= start and end <= right), key=lambda f: f.finding_type)
        if not active:
            pieces.append(source)
            continue
        label = "；".join(f"{FINDING_LABELS[f.finding_type]}：{f.summary}" for f in active)
        classes = " ".join(sorted({f"mark-{f.finding_type}" for f in active}))
        pieces.append(
            f"<mark class='annotation {classes}' title='{escape(label, quote=True)}'>{source}"
            f"<span class='detail'>［{escape(label)}］</span></mark>"
        )
    return "".join(pieces)


def _evidence(items: Iterable[Mapping[str, object]]) -> str:
    rows = []
    for item in items:
        href = _href(item.get("url"))
        title = _text(item.get("title") or item.get("url") or "來源")
        link = f"<a href='{href}' rel='noreferrer'>{title}</a>" if href else title
        relation = RELATION_LABELS.get(str(item.get("relation")), "") if item.get("rationale") else "尚未判讀"
        status = EVIDENCE_STATUS_LABELS.get(str(item.get("status")), "")
        rationale = _text(redact_text(item.get("rationale") or "", 1000))
        rows.append(f"<li>{link}（{escape(status)}・{escape(relation)}）<br><span class='meta'>{rationale}</span></li>")
    return f"<div class='evidence'><ul>{''.join(rows)}</ul></div>" if rows else ""


def _article_card(article: Mapping[str, object]) -> str:
    href = _href(article.get("url"))
    title = _text(article.get("title") or "（無標題）")
    heading = f"<a href='{href}' rel='noreferrer'>{title}</a>" if href else title
    status = ANALYSIS_LABELS.get(str(article.get("analysis_status")), "待分析")
    pending = int(article.get("pending_findings") or 0)
    extra = f"・另有 {pending} 項待查證" if pending else ""
    text = str(article.get("text") or "")
    annotations = [f for f in article.get("annotations") or () if isinstance(f, Finding)]
    return (
        f"<article><h3>{heading}</h3><p class='meta'>{_text(article.get('source'))}・{_text(article.get('updated_at'))}"
        f"・{escape(status)}{escape(extra)}</p><div class='article-text'>{render_article(text, annotations)}</div>"
        f"{_evidence(article.get('evidence') or ())}</article>"
    )


def _source_row(source: Mapping[str, object]) -> str:
    status = SOURCE_STATUS_LABELS.get(str(source.get("status")), "狀態不明")
    paused = "" if source.get("enabled", True) else "（已停用）"
    error = "<br><span class='meta'>詳細錯誤已隱藏；這不代表文章有問題。</span>" if source.get("error") else ""
    return (
        f"<li><strong>{_text(source.get('name'))}</strong>{paused}・{escape(status)}・{_text(source.get('checked_at') or '尚未檢查')}"
        f"・{_text(source.get('candidates_seen') or 0)} 篇{error}</li>"
    )


def render_status_page(snapshot: Mapping[str, object], settings: Settings | None = None) -> str:
    inbox = "".join(_article_card(a) for a in snapshot.get("inbox") or () if isinstance(a, Mapping))
    sources = "".join(_source_row(s) for s in snapshot.get("sources") or () if isinstance(s, Mapping))
    run = snapshot.get("last_run") if isinstance(snapshot.get("last_run"), Mapping) else None
    run_text = (
        f"上次檢查：{_text(run.get('finished_at'))}，檢查 {_text(run.get('sources_checked'))} 個來源，"
        f"新增 {_text(run.get('revisions_created'))} 個文章版本。" if run else "尚未執行過監測；第一次檢查通常需要幾分鐘。"
    )
    notice = "" if snapshot.get("available", True) else "<section><p>無法讀取本機資料；這不代表沒有新文章或發現。</p></section>"
    key = "已設定" if settings is not None and settings.has_llm_key else "未設定"
    help_items = "".join(f"<li>{escape(item)}</li>" for item in HELP_ITEMS)
    return (
        "<!doctype html><html lang='zh-Hant-TW'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'><title>稿核 GaoHe</title>"
        f"<style>{_STYLE}</style></head><body><main><h1>稿核 GaoHe</h1>"
        f"<p class='meta'>{escape(run_text)}　AI 金鑰：{key}</p>{notice}"
        f"<section><h2>怎麼看結果</h2><ul>{help_items}</ul></section>"
        f"<section><h2>最新文章</h2>{inbox or '<p class=empty>目前還沒有文章。加入來源或執行 gaohe check 後會出現在這裡。</p>'}</section>"
        f"<section><h2>來源狀態</h2><ul>{sources or '<li class=empty>尚未加入來源。</li>'}</ul></section>"
        "</main></body></html>"
    )


# --- loopback HTTP hardening shared with the setup wizard ------------------------------------

def security_headers(content_security_policy: str) -> tuple[tuple[str, str], ...]:
    return (
        ("Content-Security-Policy", content_security_policy),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Cache-Control", "no-store"),
        ("X-Frame-Options", "DENY"),
    )


def is_loopback_host(value: object, port: int) -> bool:
    """Whether a Host header names this loopback server (DNS-rebinding guard)."""
    if not isinstance(value, str):
        return False
    allowed = {f"{name}:{port}" for name in LOOPBACK_HOSTS} | (set(LOOPBACK_HOSTS) if port == 80 else set())
    return value.strip().lower() in allowed


def _message_page(message: str) -> str:
    return (
        "<!doctype html><html lang='zh-Hant-TW'><head><meta charset='utf-8'><title>稿核</title></head>"
        f"<body><main><p>{escape(message)}</p></main></body></html>"
    )


NOT_FOUND_PAGE = _message_page("找不到這個頁面。稿核本機頁面只有首頁。")
MISDIRECTED_PAGE = _message_page("請改用 http://127.0.0.1 開啟稿核本機頁面。")
ERROR_PAGE = _message_page("稿核本機頁面暫時無法顯示，請稍後重新整理。")


class LoopbackHandler(BaseHTTPRequestHandler):
    """Host guard, security headers on every response, fixed zh-TW error pages, no request logs."""

    content_security_policy = STATUS_PAGE_CSP
    server_version = "GaoHe"
    timeout = 15

    def version_string(self) -> str:
        return self.server_version

    def end_headers(self) -> None:
        for name, value in security_headers(self.content_security_policy):
            self.send_header(name, value)
        super().end_headers()

    def host_allowed(self) -> bool:
        values = self.headers.get_all("Host") or []
        return len(values) == 1 and is_loopback_host(values[0], self.server.server_port)

    def send_page(self, status: int, page: str, *, head_only: bool = False) -> None:
        payload = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def _status_handler(settings: Settings, store: Store | None) -> type[LoopbackHandler]:
    class StatusHandler(LoopbackHandler):
        def do_GET(self) -> None:
            self._respond(head_only=False)

        def do_HEAD(self) -> None:
            self._respond(head_only=True)

        def _respond(self, head_only: bool) -> None:
            if not self.host_allowed():
                self.send_page(HTTPStatus.MISDIRECTED_REQUEST, MISDIRECTED_PAGE, head_only=head_only)
                return
            if self.path != "/":
                self.send_page(HTTPStatus.NOT_FOUND, NOT_FOUND_PAGE, head_only=head_only)
                return
            try:
                snapshot = store.dashboard_snapshot() if store is not None else {"available": False}
            except (sqlite3.Error, OSError, ValueError):
                snapshot = {"available": False}
            try:
                page = render_status_page(snapshot, settings)
            except Exception:  # the fixed page carries no exception text
                self.send_page(HTTPStatus.INTERNAL_SERVER_ERROR, ERROR_PAGE, head_only=head_only)
                return
            self.send_page(HTTPStatus.OK, page, head_only=head_only)

    return StatusHandler


class _IPv6Server(ThreadingHTTPServer):
    allow_reuse_address = False
    address_family = socket.AF_INET6


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server silently share a busy port; fail loudly instead.
    allow_reuse_address = False


def start_server(settings: Settings, store: Store | None, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Create (not start) the loopback server; each GET / renders a fresh snapshot."""
    server_class = _IPv6Server if ":" in host else _Server
    return server_class((host, port), _status_handler(settings, store))


def serve(host: str = "127.0.0.1", port: int = 8000, env_file: Path = Path(".env")) -> None:
    settings = load_settings(env_file)
    store: Store | None = Store(settings.database_path)
    try:
        store.initialize()
    except (sqlite3.Error, OSError, ValueError):
        store = None  # the page then says local data could not be read
    server = start_server(settings, store, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
