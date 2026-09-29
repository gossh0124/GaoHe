"""Local, read-only GaoHe monitoring page (zh-TW UI) served on the loopback interface only.

Every value in a dashboard snapshot is treated as untrusted: text is escaped, links are
reduced to credential-free HTTP(S) URLs through gaohe.safety, and secrets never render.
"""

from collections.abc import Iterable, Mapping, Sequence
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import unquote, urlsplit

from .config import Settings, load_settings
from .domain import Finding
from .safety import is_credential_free_http_url, redact_text, redact_url
from .storage import Store


STATUS_PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")

FINDING_LABELS = {
    "factual_contradiction": "事實矛盾",
    "material_cross_media_difference": "實質跨媒體差異",
    "unsupported_inference": "推論超出證據",
}
EVIDENCE_STATUS_LABELS = {
    "pending": "待查證",
    "retrieved": "已取得證據",
    "retrieval_failed": "取回失敗",
    "insufficient_scope": "搜尋範圍不足",
}
EVIDENCE_RELATION_LABELS = {
    "supports": "來源一致",
    "contradicts": "找到反向證據",
    "context": "背景資料",
}
REVIEW_STATUS_LABELS = {
    "unreviewed": "待人工確認",
    "confirmed": "已人工確認",
    "dismissed": "已駁回",
}
TOPIC_CONFIDENCE_LABELS = {
    "high": "同題",
    "possible": "可能同題·待確認",
}
ANALYSIS_STATUS_LABELS = {
    "pending": "待分析",
    "running": "分析中",
    "completed": "已完成分析",
    "failed": "分析失敗",
    "skipped": "已略過",
    "unanalyzed": "尚未排入分析",
}
SOURCE_STATUS_LABELS = {
    "ok": "正常",
    "failed": "檢查失敗",
    "not_modified": "來源未變更",
    "not checked": "尚未檢查",
}
SOURCE_KIND_LABELS = {
    "direct": "直接抓取原始頁面",
    "firecrawl": "經 Firecrawl 抓取原始頁面",
    "related_article": "同題報導",
}
SEARCH_LEAD_LABEL = "僅找到搜尋線索，尚未取得原始頁面"

HELP_UNMARKED = "未標註的句子不代表已查證或已證實，只代表目前沒有形成可見發現。"
HELP_NO_VERDICT = "稿核不對整篇文章下判定，也不提供媒體總分、排名或排行榜。"
HELP_SOURCE_FAILURE = "來源檢查失敗（網站無法連線、需要登入或被阻擋）是監測端的狀況，不代表文章本身有問題。"
HELP_SEARCH_LEAD = "搜尋結果摘要只是線索，不是證據；找不到證據或取回失敗，也不代表文章有錯。"
HELP_COLORS = "螢光顏色代表問題類型（淡紅：事實矛盾；淡藍：實質跨媒體差異；淡紫：推論超出證據），證據狀態一律以文字標示。"
HELP_REVIEW = "所有發現都由機器提出；標示「待人工確認」者尚未經人檢視，「已駁回」者不會在原文上色。"
HELP_KEY = "API 金鑰只保存在本機 .env，不會寫入資料庫，也不會顯示在這個頁面。"
STORE_UNAVAILABLE_NOTICE = "無法讀取本機資料，目前只顯示本機設定。這不代表沒有新文章或發現事項。"
SOURCE_ERROR_TEXT = "來源檢查失敗，詳細錯誤已隱藏；這不代表文章有問題。"

# Low-saturation backgrounds with a stronger underline of the same hue (spec 7.3 / 7.5).
MARK_COLORS = {
    "factual_contradiction": ("#f8e0dd", "#b3392f"),
    "material_cross_media_difference": ("#dde9f6", "#2b61a3"),
    "unsupported_inference": ("#ebe2f5", "#6b48a8"),
}
PALETTE = {
    "text": "#1f1d1a",
    "muted": "#5a534b",
    "page": "#faf9f7",
    "surface": "#ffffff",
    "border": "#d9d3cc",
    "badge_border": "#6f675e",
    "link": "#1d5b8c",
    "notice": "#fdf1d6",
    "notice_border": "#9a7415",
}

_RUNTIME_FIELDS = (
    # (snapshot key, legacy English key, zh-TW label, .env name)
    ("llm_provider", "LLM provider", "AI 服務供應商", "LLM_PROVIDER"),
    ("llm_model", "LLM model", "AI 模型", "LLM_MODEL"),
    ("web_search_provider", "Web search provider", "搜尋服務", "WEB_SEARCH_PROVIDER"),
    ("data_dir", "Data directory", "資料目錄", "DATA_DIR"),
    ("poll_interval_minutes", "Poll interval minutes", "檢查間隔（分鐘）", "POLL_INTERVAL_MINUTES"),
    ("llm_api_key", "LLM API key", "AI API 金鑰", "LLM_API_KEY"),
    ("firecrawl_api_key", "Firecrawl API key", "Firecrawl API 金鑰", "FIRECRAWL_API_KEY"),
)
_SECRET_RUNTIME_KEYS = {"llm_api_key", "firecrawl_api_key"}
_KEY_STATE_LABELS = {"present": "已設定", "missing": "未設定"}
_ANALYSIS_ORDER = ("pending", "running", "completed", "failed", "skipped", "unanalyzed")
_RUN_FIELDS = (
    ("sources_checked", "檢查來源"),
    ("candidates_seen", "候選文章"),
    ("revisions_created", "新增版本"),
    ("failures", "失敗"),
)
_SENSITIVE_METADATA = re.compile(
    r"(?i)(?:bearer\s+\S+|[\"']?(?:authorization|cookie|token|secret|password|session|api[-_]key|access[_-]?token)[\"']?\s*[:=])"
)
_MAX_EVIDENCE_METADATA_CHARS = 200
_MAX_RATIONALE_CHARS = 600
_MAX_RUNTIME_CHARS = 300
_MIN_SECRET_MATCH_CHARS = 8


def _stylesheet() -> str:
    p = PALETTE
    marks = "".join(
        f".mark-{kind}{{background:{background};text-decoration-color:{underline}}}"
        for kind, (background, underline) in MARK_COLORS.items()
    )
    return "".join((
        f"body{{background:{p['page']};color:{p['text']};margin:0;",
        "font:16px/1.65 system-ui,'Microsoft JhengHei','PingFang TC','Noto Sans TC',sans-serif}",
        "main{margin:auto;max-width:980px;padding:1.5rem 1rem 3rem}",
        "h1{font-size:1.6rem;margin:.5rem 0}h2{font-size:1.25rem;margin:.2rem 0 .75rem}h3{font-size:1.05rem;margin:0 0 .25rem}",
        f"a{{color:{p['link']}}}a:focus-visible{{outline:3px solid {p['link']};outline-offset:2px}}",
        f"section,article{{background:{p['surface']};border:1px solid {p['border']};border-radius:.5rem;",
        "margin:1rem 0;padding:1rem 1.25rem}",
        f".en{{color:{p['muted']};font-size:.8em;font-weight:normal;margin-left:.4rem}}",
        f".meta,.empty,.rationale{{color:{p['muted']};font-size:.9rem}}",
        ".rationale{margin:.15rem 0 .35rem}",
        ".article-text{white-space:pre-wrap;overflow-wrap:anywhere}",
        ".annotation{background:transparent;color:inherit;border-radius:.15rem;padding:0 .08rem;",
        "text-decoration-line:underline;text-decoration-thickness:2px;text-underline-offset:.2em;",
        "text-decoration-skip-ink:none}",
        marks,
        ".annotation-multi{text-decoration-style:double}",
        f".annotation:focus,.annotation:hover{{outline:2px solid {p['link']};outline-offset:1px}}",
        ".annotation-detail{font-size:.82em;margin-left:.3rem}",
        ".chip{font-weight:600;white-space:nowrap}",
        f".badge{{background:{p['surface']};border:1px solid {p['badge_border']};border-radius:1rem;",
        f"color:{p['text']};font-size:.78em;padding:.05rem .45rem;white-space:nowrap}}",
        f".notice{{background:{p['notice']};border:1px solid {p['notice_border']};border-radius:.5rem;",
        "margin:1rem 0;padding:.75rem 1rem}",
        f".evidence{{border-top:1px dashed {p['border']};font-size:.92rem;margin-top:.6rem;padding-top:.5rem}}",
        ".evidence-heading{font-weight:600;margin:0}",
        "table{border-collapse:collapse;width:100%}",
        f"th,td{{border:1px solid {p['border']};padding:.4rem .6rem;text-align:left;vertical-align:top}}",
        "th{font-weight:600;width:15rem}code{font-family:Consolas,ui-monospace,monospace;font-size:.85em}",
        "ul{padding-left:1.25rem}li{margin:.35rem 0}nav a{margin-right:.8rem;white-space:nowrap}",
        "@media (max-width:600px){main{padding:1rem .75rem}th{width:auto}}",
    ))


_STYLESHEET = _stylesheet()


def _text(value: object, default: str = "") -> str:
    return escape(str(value)) if value is not None and value != "" else default


def _items(value: object) -> Sequence[object]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _value(item: object, name: str, default: object = "") -> object:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _safe_metadata(value: object) -> str:
    if not isinstance(value, str) or len(value) > _MAX_EVIDENCE_METADATA_CHARS or _SENSITIVE_METADATA.search(value):
        return ""
    redacted = redact_text(value, limit=_MAX_EVIDENCE_METADATA_CHARS)
    return redacted if redacted == value else ""


def _label(labels: Mapping[str, str], value: object, unknown: str) -> str:
    """Return the zh-TW label for a known enum value, else a bounded, secret-free echo or `unknown`."""
    if isinstance(value, str) and value in labels:
        return labels[value]
    return _safe_metadata(value)[:60] or unknown


def _topic_label(value: object) -> str:
    """Only an explicit high confidence reads as the same topic; anything else still needs a person."""
    if isinstance(value, str) and value in TOPIC_CONFIDENCE_LABELS:
        return TOPIC_CONFIDENCE_LABELS[value]
    return TOPIC_CONFIDENCE_LABELS["possible"]


def _badge(label: str, css_class: str = "") -> str:
    classes = f"badge {css_class}".strip()
    return f"<span class='{classes}'>{escape(label)}</span>"


def _english(text: str) -> str:
    return f"<span class='en' lang='en'>{escape(text)}</span>"


def _empty(zh: str, en: str) -> str:
    return f"<p class='empty'>{escape(zh)}{_english(en)}</p>"


def _decode_to_stable(value: str) -> str:
    while (decoded := unquote(value)) != value:
        value = decoded
    return value


def _safe_href(url: object) -> str:
    """Return a redacted, credential-free HTTP(S) URL, or "" when the URL must not become a link."""
    if not isinstance(url, str) or not is_credential_free_http_url(url):
        return ""
    parsed = urlsplit(url)
    if any(_SENSITIVE_METADATA.search(_decode_to_stable(value)) for value in (parsed.query, parsed.fragment)):
        return ""
    return redact_url(url)


def _link(href: str, label_html: str, css_class: str = "") -> str:
    class_attr = f" class='{css_class}'" if css_class else ""
    return f"<a{class_attr} href='{escape(href, quote=True)}' rel='noreferrer'>{label_html}</a>"


def _safe_rationale(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text or _SENSITIVE_METADATA.search(text) or redact_text(text, len(text)) != text:
        return ""
    return text if len(text) <= _MAX_RATIONALE_CHARS else text[: _MAX_RATIONALE_CHARS - 1] + "…"


def _evidence_items(item: object) -> Sequence[object]:
    evidence = _value(item, "evidence", _value(item, "evidences", ()))
    return (evidence,) if isinstance(evidence, Mapping) else _items(evidence)


def _evidence_entry(item: object) -> str:
    href = _safe_href(_value(item, "url", _value(item, "source_url", "")))
    if not href:
        return ""
    title = _safe_metadata(_value(item, "title", _value(item, "name", ""))) or "證據來源"
    provider = _safe_metadata(_value(item, "provider", _value(item, "source", "")))
    retrieved = _safe_metadata(_value(item, "retrieved_at", _value(item, "fetched_at", "")))
    source_kind = _value(item, "source_kind", "")
    kind_label = SOURCE_KIND_LABELS.get(source_kind, "") if isinstance(source_kind, str) else ""
    metadata = " · ".join(escape(value) for value in (provider, retrieved, kind_label) if value)
    details = f" <span class='meta'>{metadata}</span>" if metadata else ""
    badges: list[str] = []
    if source_kind == "search":
        badges.append(_badge(SEARCH_LEAD_LABEL))
    else:
        relation = _value(item, "relation", None)
        if isinstance(relation, str) and relation in EVIDENCE_RELATION_LABELS:
            badges.append(_badge(EVIDENCE_RELATION_LABELS[relation]))
    status = _value(item, "status", None)
    if isinstance(status, str) and status in EVIDENCE_STATUS_LABELS:
        badges.append(_badge(EVIDENCE_STATUS_LABELS[status]))
    badge_html = f" {' '.join(badges)}" if badges else ""
    rationale = _safe_rationale(_value(item, "rationale", None))
    rationale_html = f"<p class='rationale'>判讀理由：{escape(rationale)}</p>" if rationale else ""
    link = _link(href, escape(title), "evidence-link")
    return f"<li>{link}{details}{badge_html}{rationale_html}</li>"


def _evidence_links(item: object) -> str:
    entries = [entry for evidence in _evidence_items(item) if (entry := _evidence_entry(evidence))]
    if not entries:
        return ""
    return f"<div class='evidence'><p class='evidence-heading'>證據鏈</p><ul>{''.join(entries)}</ul></div>"


def _is_position(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _annotatable(finding: object) -> bool:
    """Only visible, non-dismissed findings of the three v0 kinds may ever colour article text."""
    return (
        isinstance(finding, Finding)
        and finding.visible in (True, 1)
        and isinstance(finding.finding_type, str)
        and finding.finding_type in FINDING_LABELS
        and finding.review_status != "dismissed"
        and _is_position(finding.start)
        and _is_position(finding.end)
    )


def _evidence_badge(status: object) -> str:
    known = isinstance(status, str) and status in EVIDENCE_STATUS_LABELS
    css_class = f"badge-{status}" if known else "badge-unknown"
    return _badge(_label(EVIDENCE_STATUS_LABELS, status, "證據狀態不明"), css_class)


def _annotation_label(finding: Finding) -> str:
    kind = FINDING_LABELS[finding.finding_type]
    status = _label(EVIDENCE_STATUS_LABELS, finding.evidence_status, "證據狀態不明")
    review = _label(REVIEW_STATUS_LABELS, finding.review_status, "待人工確認")
    summary = "" if finding.summary is None else str(finding.summary)
    return f"{kind}：{status}・{review} — {summary}"


def render_article(text: str, annotations: Sequence[Finding]) -> str:
    """Render original text once, with clipped, deterministic annotation intervals."""
    text = text if isinstance(text, str) else ""
    length = len(text)
    clipped: list[tuple[int, int, Finding]] = []
    for finding in annotations:
        if not _annotatable(finding):
            continue
        start = max(0, min(length, finding.start))
        end = max(0, min(length, finding.end))
        if start < end:
            clipped.append((start, end, finding))
    clipped.sort(key=lambda item: (item[0], item[1], item[2].finding_type, str(item[2].summary)))
    boundaries = sorted({0, length, *(point for start, end, _ in clipped for point in (start, end))})
    pieces: list[str] = []
    for start, end in zip(boundaries, boundaries[1:]):
        source = escape(text[start:end])
        active = [finding for left, right, finding in clipped if left <= start and end <= right]
        if not active:
            pieces.append(source)
            continue
        active.sort(key=lambda finding: (finding.finding_type, str(finding.evidence_status), str(finding.summary)))
        labels = "；".join(_annotation_label(finding) for finding in active)
        kinds = sorted({finding.finding_type for finding in active})
        classes = " ".join(f"mark-{kind}" for kind in kinds)
        if len(active) > 1:
            classes += " annotation-multi"
        badges = "".join(_evidence_badge(finding.evidence_status) for finding in active)
        pieces.append(
            f"<mark class='annotation {classes}' tabindex='0' title='{escape(labels, quote=True)}'>"
            f"{source}<span class='annotation-detail'>{escape(labels)} {badges}</span></mark>"
        )
    return "".join(pieces)


def _mask_secrets(value: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret and (value == secret or (len(secret) >= _MIN_SECRET_MATCH_CHARS and secret in value)):
            return "（已隱藏）"
    return value


def runtime_section(settings: Settings) -> dict[str, object]:
    """Describe the local configuration for display; keys are reported only as present or missing."""
    secrets = (settings.llm_api_key, settings.firecrawl_api_key)
    runtime: dict[str, object] = {
        "llm_provider": _mask_secrets(settings.llm_provider, secrets),
        "llm_model": _mask_secrets(settings.llm_model, secrets),
        "web_search_provider": _mask_secrets(settings.web_search_provider, secrets),
        "data_dir": _mask_secrets(str(settings.data_dir), secrets),
        "poll_interval_minutes": settings.poll_interval_minutes,
        "llm_api_key": "present" if settings.has_llm_key else "missing",
    }
    if settings.web_search_provider == "firecrawl":
        runtime["firecrawl_api_key"] = "present" if settings.has_firecrawl_key else "missing"
    return runtime


def _settings_snapshot(settings: Settings) -> dict[str, object]:
    return {
        "runtime": runtime_section(settings),
        "inbox": (), "findings": (), "comparisons": (), "sources": (),
    }


def build_snapshot(settings: Settings, store: Store | None) -> dict[str, object]:
    """Read one dashboard snapshot and merge the runtime section; unreadable data becomes a notice."""
    runtime = runtime_section(settings)
    try:
        data = store.dashboard_snapshot() if store is not None else None
    except (sqlite3.Error, OSError, ValueError):
        data = None
    if not isinstance(data, Mapping):
        return {"runtime": runtime, "data_unavailable": True}
    snapshot = dict(data)
    snapshot["runtime"] = runtime
    snapshot["data_unavailable"] = False
    return snapshot


def _topics_by_revision(comparisons: Sequence[object]) -> dict[int, list[tuple[str, str]]]:
    topics: dict[int, list[tuple[str, str]]] = {}
    for comparison in comparisons:
        label = _value(comparison, "label", None)
        confidence = _topic_label(_value(comparison, "confidence", None))
        for article in _items(_value(comparison, "articles", ())):
            revision_id = _value(article, "revision_id", None)
            if _is_position(revision_id):
                topics.setdefault(revision_id, []).append((str(label) if label else "未命名主題", confidence))
    return topics


def _article_card(article: object, topics: Mapping[int, list[tuple[str, str]]]) -> str:
    title = _text(_value(article, "title", None), "（無標題）")
    href = _safe_href(_value(article, "url", ""))
    heading = _link(href, title) if href else title
    source = _text(_value(article, "source", _value(article, "source_name", None)), "未知來源")
    meta = [source]
    published = _value(article, "published_at", None)
    updated = _value(article, "updated_at", _value(article, "fetched_at", None))
    if published:
        meta.append(f"發布：{_text(published)}")
    if updated:
        meta.append(f"更新：{_text(updated)}")
    badges: list[str] = []
    status = _value(article, "analysis_status", None)
    if status:
        badges.append(_badge(_label(ANALYSIS_STATUS_LABELS, status, "分析狀態不明")))
    pending = _count(_value(article, "pending_findings", None))
    if pending:
        badges.append(_badge(f"{pending} 項候選問題待補查"))
    revision_id = _value(article, "revision_id", None)
    article_topics = topics.get(revision_id, []) if _is_position(revision_id) else []
    for label, confidence in article_topics:
        badges.append(_badge(f"同題對照：{label}（{confidence}）"))
    badge_html = f"<p>{' '.join(badges)}</p>" if badges else ""
    findings = tuple(item for item in _items(_value(article, "annotations", ())) if isinstance(item, Finding))
    body = render_article(str(_value(article, "text", "") or ""), findings)
    return (
        f"<article><h3>{heading}</h3><p class='meta'>{' · '.join(meta)}</p>{badge_html}"
        f"<div class='article-text'>{body}</div>{_evidence_links(article)}</article>"
    )


def _finding_row(finding: object) -> str:
    kind = _value(finding, "finding_type", None)
    if _value(finding, "visible", True) is False or not isinstance(kind, str) or kind not in FINDING_LABELS:
        return ""
    review = _value(finding, "review_status", "unreviewed")
    badges = " ".join((
        _evidence_badge(_value(finding, "evidence_status", "pending")),
        _badge(_label(REVIEW_STATUS_LABELS, review, "待人工確認"), "review"),
    ))
    title = _text(_value(finding, "article_title", None), "（無標題）")
    href = _safe_href(_value(finding, "article_url", ""))
    context = [f"文章：{_link(href, title) if href else title}"]
    if source := _value(finding, "source", None):
        context.append(_text(source))
    reviewed_at = _value(finding, "reviewed_at", None)
    if reviewed_at and isinstance(review, str) and review in {"confirmed", "dismissed"}:
        context.append(f"人工確認時間：{_text(reviewed_at)}")
    summary = _text(_value(finding, "summary", None), "（沒有摘要）")
    return (
        f"<li class='finding'><span class='annotation chip mark-{kind}'>{FINDING_LABELS[kind]}</span> {badges}"
        f"<br>{summary}<br><span class='meta'>{' · '.join(context)}</span>{_evidence_links(finding)}</li>"
    )


def _comparison_row(item: object) -> str:
    label = _text(_value(item, "label", _value(item, "summary", None)), "（未命名主題）")
    confidence = _topic_label(_value(item, "confidence", None))
    articles: list[str] = []
    for article in _items(_value(item, "articles", ())):
        title = _text(_value(article, "title", None), "（無標題）")
        href = _safe_href(_value(article, "url", ""))
        source = _text(_value(article, "source", None), "未知來源")
        articles.append(f"<li>{_link(href, title) if href else title} <span class='meta'>{source}</span></li>")
    article_list = f"<ul>{''.join(articles)}</ul>" if articles else ""
    return f"<li><strong>{label}</strong> {_badge(confidence)}{article_list}</li>"


def _source_row(item: object) -> str:
    name = _text(_value(item, "name", None), "未知來源")
    status = _value(item, "status", None) or "not checked"
    badges = [_badge(_label(SOURCE_STATUS_LABELS, status, "狀態不明"))]
    if _value(item, "enabled", True) is False:
        badges.append(_badge("已停用"))
    checked = _text(_value(item, "checked_at", None), "尚未檢查")
    seen = _count(_value(item, "candidates_seen", None))
    details = [f"最近檢查：{checked}"]
    if seen is not None:
        details.append(f"候選文章 {seen} 篇")
    error = f"<br><span class='meta'>{escape(SOURCE_ERROR_TEXT)}</span>" if _value(item, "error", "") else ""
    return f"<li><strong>{name}</strong> {' '.join(badges)}<br><span class='meta'>{'；'.join(details)}</span>{error}</li>"


def _last_run(value: object) -> str:
    if not isinstance(value, Mapping):
        return "<p class='meta'>尚未執行過來源監測。</p>"
    started = _text(value.get("started_at"), "時間不明")
    finished = _text(value.get("finished_at"), "尚未完成")
    counts = "、".join(
        f"{label} {count}" for key, label in _RUN_FIELDS if (count := _count(value.get(key))) is not None
    )
    counts_html = f"；{counts}" if counts else ""
    return f"<p class='meta'>上次監測：開始 {started}，結束 {finished}{counts_html}。</p>"


def _analysis_table(value: object) -> str:
    if not isinstance(value, Mapping):
        return _empty("目前沒有分析佇列資料。", "No analysis queue data.")
    rows = "".join(
        f"<tr><th>{ANALYSIS_STATUS_LABELS[key]}</th><td>{_count(value.get(key)) or 0}</td></tr>"
        for key in _ANALYSIS_ORDER
    )
    return f"<table><tbody>{rows}</tbody></table>"


def _runtime_value(key: str, value: object) -> str:
    if key in _SECRET_RUNTIME_KEYS:
        state = value if isinstance(value, str) and value in _KEY_STATE_LABELS else None
        if state is None:
            return "狀態不明"
        return f"{_KEY_STATE_LABELS[state]} <span class='meta' lang='en'>{state}</span>"
    if value is None or value == "":
        return "未設定"
    return _text(redact_text(str(value), _MAX_RUNTIME_CHARS), "未設定")


def _runtime_table(runtime: Mapping[str, object]) -> str:
    rows = []
    for key, legacy, label, env_name in _RUNTIME_FIELDS:
        if key in runtime:
            value = runtime[key]
        elif legacy in runtime:
            value = runtime[legacy]
        else:
            continue
        rows.append(f"<tr><th>{label} <code>{env_name}</code></th><td>{_runtime_value(key, value)}</td></tr>")
    if not rows:
        return _empty("目前沒有本機設定資料。", "No local settings.")
    return f"<table><tbody>{''.join(rows)}</tbody></table>"


def _section(anchor: str, zh: str, en: str, body: str) -> str:
    return f"<section id='{anchor}' aria-labelledby='{anchor}-title'><h2 id='{anchor}-title'>{zh}{_english(en)}</h2>{body}</section>"


def _help_section() -> str:
    items = "".join(
        f"<li>{escape(text)}</li>"
        for text in (HELP_UNMARKED, HELP_NO_VERDICT, HELP_SOURCE_FAILURE, HELP_SEARCH_LEAD, HELP_COLORS, HELP_REVIEW, HELP_KEY)
    )
    return _section("help", "說明", "Help", f"<ul class='help'>{items}</ul>")


def _legend() -> str:
    chips = " ".join(
        f"<span class='annotation chip mark-{kind}'>{label}</span>" for kind, label in FINDING_LABELS.items()
    )
    return f"<p>{chips}</p><p class='meta'>螢光顏色代表問題類型，證據狀態以文字標示。<a href='#help'>閱讀說明</a></p>"


def _nav() -> str:
    links = (
        ("inbox", "新文章收件匣"), ("findings", "發現事項"), ("comparisons", "同題對照"), ("sources", "來源狀態"),
        ("analysis", "分析佇列"), ("runtime", "本機設定"), ("help", "說明"),
    )
    return "<nav aria-label='頁面區塊'>" + "".join(f"<a href='#{anchor}'>{label}</a>" for anchor, label in links) + "</nav>"


def _data_sections(snapshot: Mapping[str, Any]) -> str:
    comparisons = _items(snapshot.get("comparisons", ()))
    topics = _topics_by_revision(comparisons)
    inbox = "".join(_article_card(article, topics) for article in _items(snapshot.get("inbox", ())))
    findings = "".join(_finding_row(item) for item in _items(snapshot.get("findings", ())))
    comparison_rows = "".join(_comparison_row(item) for item in comparisons)
    sources = "".join(_source_row(item) for item in _items(snapshot.get("sources", ())))
    return "".join((
        _section("inbox", "新文章收件匣", "Inbox", inbox or _empty("目前沒有新文章。", "No new articles.")),
        _section(
            "findings", "發現事項", "Findings",
            f"<ul>{findings}</ul>" if findings else _empty("目前沒有可見的發現事項。", "No visible findings."),
        ),
        _section(
            "comparisons", "同題對照", "Same-topic comparison",
            f"<ul>{comparison_rows}</ul>" if comparison_rows
            else _empty("目前沒有至少兩篇文章的同題對照。", "No same-topic comparison."),
        ),
        _section(
            "sources", "來源狀態", "Source health",
            _last_run(snapshot.get("last_run"))
            + (f"<ul>{sources}</ul>" if sources else _empty("尚未加入或檢查任何來源。", "No source checks yet.")),
        ),
        _section("analysis", "分析佇列", "Analysis queue", _analysis_table(snapshot.get("analysis"))),
    ))


def render_status_page(snapshot: Mapping[str, object] | Settings) -> str:
    """Render a dependency-free local monitoring view from an untrusted snapshot."""
    if isinstance(snapshot, Settings):
        snapshot = _settings_snapshot(snapshot)
    if not isinstance(snapshot, Mapping):
        snapshot = {}
    runtime = snapshot.get("runtime", {})
    runtime = runtime if isinstance(runtime, Mapping) else {}
    if snapshot.get("data_unavailable") is True:
        data = f"<p class='notice' role='status'>{escape(STORE_UNAVAILABLE_NOTICE)}</p>"
    else:
        data = _data_sections(snapshot)
    runtime_html = _section("runtime", "本機設定", "Local settings", _runtime_table(runtime))
    return (
        '<!doctype html><html lang="zh-Hant-TW"><head><meta charset="utf-8">'
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>稿核本機監測</title><style>{_STYLESHEET}</style></head><body><main>"
        f"<header><h1>稿核 GaoHe 本機監測</h1>{_legend()}{_nav()}</header>"
        f"{data}{runtime_html}{_help_section()}</main></body></html>"
    )


def render_setup_page(state: Mapping[str, object]) -> str:
    configured = "已完成" if state.get("configured") else "尚未完成"
    source_count = _count(state.get("source_count", 0)) or 0
    key_state = "已設定" if state.get("has_llm_key") else "未設定"
    return (
        '<!doctype html><html lang="zh-Hant-TW"><meta charset="utf-8"><title>稿核設定</title><main>'
        f"<h1>稿核設定</h1><p>本機設定{configured}。</p><p>AI 金鑰：{key_state}。媒體來源：{source_count} 個。</p>"
        "<p>你的金鑰只保存在這台電腦，不會顯示在這個頁面。</p></main></html>"
    )


# --- loopback HTTP hardening shared with the setup wizard -------------------------------------------

def security_headers(content_security_policy: str) -> tuple[tuple[str, str], ...]:
    return (
        ("Content-Security-Policy", content_security_policy),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Cache-Control", "no-store"),
        ("X-Frame-Options", "DENY"),
    )


def is_loopback_host(value: object, port: int) -> bool:
    """Return whether a Host header names this loopback server (DNS-rebinding guard)."""
    if not isinstance(value, str):
        return False
    host = value.strip().lower()
    allowed = {f"{name}:{port}" for name in LOOPBACK_HOSTS}
    if port == 80:
        allowed.update(LOOPBACK_HOSTS)
    return host in allowed


def _message_page(message: str) -> str:
    return (
        '<!doctype html><html lang="zh-Hant-TW"><head><meta charset="utf-8"><title>稿核</title></head>'
        f"<body><main><p>{escape(message)}</p></main></body></html>"
    )


NOT_FOUND_PAGE = _message_page("找不到這個頁面。稿核本機頁面只有首頁。")
MISDIRECTED_PAGE = _message_page("請改用 http://127.0.0.1 開啟稿核本機頁面。")
METHOD_NOT_ALLOWED_PAGE = _message_page("稿核本機頁面不接受這種請求。")
INTERNAL_ERROR_PAGE = _message_page("稿核本機頁面暫時無法顯示，請稍後重新整理。")


class LoopbackHandler(BaseHTTPRequestHandler):
    """Base handler: Host guard, fixed zh-TW error pages, security headers on every response, no logs."""

    content_security_policy = STATUS_PAGE_CSP
    allowed_methods = ("GET", "HEAD")
    server_version = "GaoHe"
    timeout = 15
    error_content_type = "text/html; charset=utf-8"
    error_message_format = (
        '<!doctype html><html lang="zh-Hant-TW"><head><meta charset="utf-8"><title>%(code)d</title></head>'
        "<body><main><p>稿核本機頁面無法處理這個請求（%(code)d）。</p></main></body></html>"
    )

    def version_string(self) -> str:
        return self.server_version  # no Python/version fingerprint in the Server header

    def end_headers(self) -> None:
        for name, value in security_headers(self.content_security_policy):
            self.send_header(name, value)
        super().end_headers()

    def host_allowed(self) -> bool:
        values = self.headers.get_all("Host") or []
        return len(values) == 1 and is_loopback_host(values[0], self.server.server_port)

    def send_page(
        self, status: int, page: str, *, head_only: bool = False, headers: Iterable[tuple[str, str]] = ()
    ) -> None:
        payload = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def reject_host(self, head_only: bool = False) -> None:
        self.send_page(HTTPStatus.MISDIRECTED_REQUEST, MISDIRECTED_PAGE, head_only=head_only)

    def method_not_allowed(self) -> None:
        if not self.host_allowed():
            self.reject_host()
            return
        allow = (("Allow", ", ".join(self.allowed_methods)),)
        self.send_page(HTTPStatus.METHOD_NOT_ALLOWED, METHOD_NOT_ALLOWED_PAGE, headers=allow)

    def log_message(self, format: str, *args: object) -> None:
        return


def _status_handler(settings: Settings, store: Store | None) -> type[LoopbackHandler]:
    class StatusHandler(LoopbackHandler):
        def do_GET(self) -> None:
            self._respond(head_only=False)

        def do_HEAD(self) -> None:
            self._respond(head_only=True)

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = LoopbackHandler.method_not_allowed

        def _respond(self, head_only: bool) -> None:
            if not self.host_allowed():
                self.reject_host(head_only)
                return
            if self.path != "/":
                self.send_page(HTTPStatus.NOT_FOUND, NOT_FOUND_PAGE, head_only=head_only)
                return
            try:
                page = render_status_page(build_snapshot(settings, store))
            except Exception:  # never surface internals; the fixed page carries no exception text
                self.send_page(HTTPStatus.INTERNAL_SERVER_ERROR, INTERNAL_ERROR_PAGE, head_only=head_only)
                return
            self.send_page(HTTPStatus.OK, page, head_only=head_only)

    return StatusHandler


class _IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def start_server(settings: Settings, store: Store | None, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Create (not start) a loopback server that renders a fresh store snapshot on every GET /."""
    server_class = _IPv6ThreadingHTTPServer if ":" in host else ThreadingHTTPServer
    return server_class((host, port), _status_handler(settings, store))


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    env_file: Path = Path(".env"),
) -> None:
    settings = load_settings(env_file)
    store = Store(settings.database_path)
    try:
        store.initialize()
    except (sqlite3.Error, OSError, ValueError):
        pass  # every page then shows the local-data notice instead of crashing the server
    server = start_server(settings, store, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
