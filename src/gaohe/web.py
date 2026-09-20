from collections.abc import Mapping, Sequence
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .config import Settings, load_settings
from .domain import Finding

if TYPE_CHECKING:
    from .storage import Store


_FINDING_LABELS = {
    "factual_contradiction": "Factual contradiction",
    "material_cross_media_difference": "Material cross-media difference",
    "unsupported_inference": "Unsupported inference",
}
_STATUS_LABELS = {
    "pending": "Pending check",
    "retrieval_failed": "Retrieval failed",
    "insufficient_scope": "Insufficient scope",
    "retrieved": "Evidence retrieved",
}
_RUNTIME_FIELDS = (
    ("LLM provider", "LLM provider"),
    ("LLM model", "LLM model"),
    ("Web search provider", "Web search provider"),
    ("Data directory", "Data directory"),
    ("LLM API key", "LLM API key"),
)


def _text(value: object, default: str = "") -> str:
    return escape(str(value)) if value is not None else default


def _items(value: object) -> Sequence[object]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _value(item: object, name: str, default: object = "") -> object:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _status_badge(status: object) -> str:
    raw = str(status or "pending")
    label = _STATUS_LABELS.get(raw, raw.replace("_", " ").title())
    return f"<span class='badge badge-{escape(raw, quote=True)}'>{escape(label)}</span>"


def _safe_error(value: object) -> str:
    return "Source check failed; sensitive details hidden." if value else ""


def _evidence_items(item: object) -> Sequence[object]:
    evidence = _value(item, "evidence", _value(item, "evidences", ()))
    return (evidence,) if isinstance(evidence, Mapping) else _items(evidence)


def _evidence_link(item: object) -> str:
    url = _value(item, "url", _value(item, "source_url", ""))
    if not isinstance(url, str):
        return ""
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
    except ValueError:
        valid = False
    if not valid:
        return ""
    title = _text(_value(item, "title", _value(item, "name", "Evidence")))
    provider = _text(_value(item, "provider", _value(item, "source", "")))
    retrieved = _text(_value(item, "retrieved_at", _value(item, "fetched_at", "")))
    metadata = " · ".join(value for value in (provider, retrieved) if value)
    details = f" <span class='meta'>{metadata}</span>" if metadata else ""
    return f"<a class='evidence-link' href='{escape(url, quote=True)}' rel='noreferrer'>{title}</a>{details}"


def _evidence_links(item: object) -> str:
    links = [link for evidence in _evidence_items(item) if (link := _evidence_link(evidence))]
    return f"<p class='evidence'>Evidence: {'; '.join(links)}</p>" if links else ""


def _finding_label(finding: Finding) -> str:
    return _FINDING_LABELS.get(finding.finding_type, finding.finding_type.replace("_", " ").title())


def render_article(text: str, annotations: Sequence[Finding]) -> str:
    """Render original text once, with clipped, deterministic annotation intervals."""
    length = len(text)
    clipped: list[tuple[int, int, Finding]] = []
    for finding in annotations:
        start = max(0, min(length, finding.start))
        end = max(0, min(length, finding.end))
        if start < end:
            clipped.append((start, end, finding))
    clipped.sort(key=lambda item: (item[0], item[1], item[2].finding_type, item[2].summary))
    boundaries = sorted({0, length, *(point for start, end, _ in clipped for point in (start, end))})
    pieces: list[str] = []
    for start, end in zip(boundaries, boundaries[1:]):
        source = escape(text[start:end])
        active = [finding for left, right, finding in clipped if left <= start and end <= right]
        if not active:
            pieces.append(source)
            continue
        active.sort(key=lambda finding: (finding.finding_type, finding.evidence_status, finding.summary))
        labels = " · ".join(
            f"{_finding_label(finding)}: {_STATUS_LABELS.get(finding.evidence_status, finding.evidence_status)} — {finding.summary}"
            for finding in active
        )
        classes = " ".join(sorted({f"mark-{finding.finding_type}" for finding in active}))
        badges = "".join(_status_badge(finding.evidence_status) for finding in active)
        pieces.append(
            f"<mark class='annotation {classes}' tabindex='0' title='{escape(labels, quote=True)}'>"
            f"{source}<span class='annotation-detail'>{escape(labels)} {badges}</span></mark>"
        )
    return "".join(pieces)


def _runtime_snapshot(settings: Settings) -> Mapping[str, object]:
    return {
        "runtime": {
            "LLM provider": settings.llm_provider,
            "LLM model": settings.llm_model,
            "Web search provider": settings.web_search_provider,
            "Data directory": str(settings.data_dir),
            "LLM API key": "present" if settings.has_llm_key else "missing",
        },
        "inbox": (), "findings": (), "comparisons": (), "sources": (),
    }


def _article_card(article: object) -> str:
    title = _text(_value(article, "title", "Untitled"))
    source = _text(_value(article, "source", _value(article, "source_name", "Unknown source")))
    updated = _text(_value(article, "updated_at", _value(article, "fetched_at", "Unknown time")))
    body = str(_value(article, "text", ""))
    findings = tuple(item for item in _items(_value(article, "annotations", ())) if isinstance(item, Finding))
    return f"<article><h3>{title}</h3><p class='meta'>{source} · {updated}</p><div class='article-text'>{render_article(body, findings)}</div>{_evidence_links(article)}</article>"


def _finding_row(finding: object) -> str:
    kind = _value(finding, "finding_type", "Finding")
    label = _text(kind.replace("_", " ").title() if isinstance(kind, str) else "Finding")
    return f"<li><strong>{label}</strong> {_status_badge(_value(finding, 'evidence_status', 'pending'))}<br>{_text(_value(finding, 'summary', 'No summary'))}{_evidence_links(finding)}</li>"


def _comparison_row(item: object) -> str:
    return f"<li>{_text(_value(item, 'label', _value(item, 'summary', 'No confirmed comparison')))} <span class='badge'>{_text(_value(item, 'confidence', 'pending'))}</span></li>"


def _source_row(item: object) -> str:
    error = _text(_safe_error(_value(item, "error", "")))
    error_html = f"<br><span class='error'>{error}</span>" if error else ""
    return f"<li><strong>{_text(_value(item, 'name', 'Unknown source'))}</strong> · {_text(_value(item, 'status', 'pending'))} · {_text(_value(item, 'checked_at', 'Not checked'))} · {_text(_value(item, 'candidates_seen', 0))} candidates{error_html}</li>"


def render_status_page(snapshot: Mapping[str, object] | Settings) -> str:
    """Render a dependency-free local monitoring view from an untrusted snapshot."""
    if isinstance(snapshot, Settings):
        snapshot = _runtime_snapshot(snapshot)
    runtime = snapshot.get("runtime", {}) if isinstance(snapshot.get("runtime", {}), Mapping) else {}
    runtime_rows = "".join(
        f"<tr><th>{_text(label)}</th><td>{_text(runtime[key])}</td></tr>"
        for label, key in _RUNTIME_FIELDS
        if key in runtime
    )
    inbox = "".join(_article_card(article) for article in _items(snapshot.get("inbox", ()))) or "<p>No new articles.</p>"
    findings = "".join(_finding_row(item) for item in _items(snapshot.get("findings", ()))) or "<li>No visible findings.</li>"
    comparisons = "".join(_comparison_row(item) for item in _items(snapshot.get("comparisons", ()))) or "<li>No high-confidence comparison.</li>"
    sources = "".join(_source_row(item) for item in _items(snapshot.get("sources", ()))) or "<li>No source checks yet.</li>"
    return f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>GaoHe local monitoring</title><style>body{{background:#faf9f7;color:#252525;font:16px system-ui,sans-serif;line-height:1.5;margin:0}}main{{margin:auto;max-width:960px;padding:2rem}}section,article{{background:#fff;border:1px solid #ddd8d2;border-radius:.5rem;margin:1rem 0;padding:1rem}}.meta,.error{{color:#665f58;font-size:.9rem}}.error{{color:#8a4945}}.annotation{{border-radius:.2rem;color:inherit;padding:0 .08rem}}.mark-factual_contradiction{{background:#f2d8d2}}.mark-material_cross_media_difference{{background:#f3e5bd}}.mark-unsupported_inference{{background:#d9e7ef}}.annotation-detail{{font-size:.82em;margin-left:.3rem}}.badge{{border:1px solid #777;border-radius:1rem;font-size:.78em;padding:.05rem .4rem;white-space:nowrap}}.annotation:focus,.annotation:hover{{outline:3px solid #3b6f91;outline-offset:2px}}.evidence{{font-size:.9rem}}table{{border-collapse:collapse}}th,td{{border:1px solid #ddd8d2;padding:.35rem;text-align:left}}ul{{padding-left:1.25rem}}</style></head><body><main><h1>GaoHe local monitoring</h1><p><span class='annotation mark-factual_contradiction'>Factual contradiction</span> <span class='annotation mark-material_cross_media_difference'>Material cross-media difference</span> <span class='annotation mark-unsupported_inference'>Unsupported inference</span> — labels and evidence badges explain every mark.</p><section><h2>Inbox</h2>{inbox}</section><section><h2>Findings</h2><ul>{findings}</ul></section><section><h2>Same-topic comparison</h2><ul>{comparisons}</ul></section><section><h2>Source health</h2><ul>{sources}</ul></section><section><h2>GaoHe local runtime</h2><table><tbody>{runtime_rows}</tbody></table></section></main></body></html>"


def render_setup_page(state: Mapping[str, object]) -> str:
    configured = "ready" if state.get("configured") else "not configured"
    source_count = _text(state.get("source_count", 0))
    key_state = "present" if state.get("has_llm_key") else "missing"
    return f"<!doctype html><html lang='en'><meta charset='utf-8'><title>GaoHe setup</title><main><h1>GaoHe setup</h1><p>Setup is {configured}.</p><p>AI key: {key_state}. Sources: {source_count}.</p><p>Your key stays on this computer and is never shown here.</p></main></html>"


def _handler_for(snapshot: Mapping[str, object] | Settings):
    class StatusHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/":
                self.send_error(404)
                return
            payload = render_status_page(snapshot).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            return

    return StatusHandler


def start_server(settings: Settings, store: "Store", host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Create a loopback server; richer Store snapshots are added by the UI integration task."""
    del store
    return ThreadingHTTPServer((host, port), _handler_for(settings))


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    env_file: Path = Path(".env"),
) -> None:
    settings = load_settings(env_file)
    server = ThreadingHTTPServer((host, port), _handler_for(settings))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
