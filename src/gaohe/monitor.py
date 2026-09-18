from dataclasses import replace
from datetime import datetime, timezone

from .config import Settings
from .domain import ArticleCandidate, FetchedArticle, RunSummary, article_content_hash
from .sources import HttpTransport, extract_article_text, parse_feed, parse_html_list, parse_sitemap
from .storage import Store


def _timestamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_xml_source(source_url: str, content_type: str) -> bool:
    return "xml" in content_type.lower() or source_url.lower().endswith((".xml", ".rss"))


def _candidates(body: bytes, source_url: str, content_type: str) -> list[ArticleCandidate]:
    if _is_xml_source(source_url, content_type):
        return parse_feed(body, source_url) or parse_sitemap(body, source_url)
    return parse_html_list(body, source_url)


def _marker(candidate: ArticleCandidate, source_etag: str | None) -> str | None:
    if source_etag:
        return f"etag:{source_etag}"
    if candidate.metadata.get("discovery_type") == "sitemap" and candidate.published_at:
        return f"lastmod:{candidate.published_at}"
    return candidate.metadata.get("etag")


def _stored_candidate(candidate: ArticleCandidate, source_id: int, marker: str | None) -> ArticleCandidate:
    published_at = candidate.published_at
    if published_at:
        try:
            parsed = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                published_at = None
        except ValueError:
            published_at = None
    metadata = dict(candidate.metadata)
    if marker:
        metadata["_monitor_marker"] = marker
    return replace(candidate, source_id=source_id, published_at=published_at, metadata=metadata)


def watch_once(
    settings: Settings,
    store: Store,
    transport: HttpTransport,
    now: datetime | None = None,
) -> RunSummary:
    del settings
    current = now or datetime.now(timezone.utc)
    started_at = _timestamp(current)
    sources_checked = candidates_seen = revisions_created = failures = 0

    for source in store.list_sources(enabled_only=True):
        sources_checked += 1
        source_failures = 0
        seen = 0
        error = None
        try:
            feed = transport.fetch(source.feed_url)
            if not 200 <= feed.status < 300:
                raise ValueError(f"source returned HTTP {feed.status}")
            candidates = _candidates(feed.body, feed.url, feed.headers.get("Content-Type", ""))
            if not candidates and feed.body.strip() and _is_xml_source(feed.url, feed.headers.get("Content-Type", "")):
                raise ValueError("source feed could not be parsed")
            seen = len(candidates)
            candidates_seen += seen
            source_etag = next((value for name, value in feed.headers.items() if name.lower() == "etag"), None)
            for candidate in candidates:
                marker = _marker(candidate, source_etag)
                known_hash = store.latest_content_hash(candidate.url)
                metadata = store.latest_article_metadata(candidate.url) if known_hash and marker else None
                if metadata and metadata.get("_monitor_marker") == marker:
                    continue
                candidate = _stored_candidate(candidate, source.id, marker)
                try:
                    article = transport.fetch(candidate.url)
                    if not 200 <= article.status < 300:
                        raise ValueError(f"article returned HTTP {article.status}")
                    text = extract_article_text(article.body, article.headers.get("Content-Type"))
                    if not text:
                        raise ValueError("article text could not be extracted")
                    content_hash = article_content_hash(candidate.title, text)
                    if known_hash == content_hash:
                        continue
                    _, created = store.save_fetched_article(
                        FetchedArticle(candidate, text, started_at, content_hash)
                    )
                    revisions_created += int(created)
                except Exception as article_error:
                    source_failures += 1
                    error = str(article_error)
        except Exception as source_error:
            source_failures += 1
            error = str(source_error)

        failures += source_failures
        store.record_source_check(source.id, started_at, "failed" if source_failures else "ok", seen, error)

    summary = RunSummary(
        started_at,
        _timestamp(now or datetime.now(timezone.utc)),
        sources_checked,
        candidates_seen,
        revisions_created,
        failures,
    )
    store.record_run(summary)
    return summary
