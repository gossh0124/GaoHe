"""One short-lived monitoring pass over every enabled source (spec 5).

A listed article is fetched when it is new, or when it is still fresh (under 48 hours old)
and was last fetched 3 or more hours ago, so edits to new stories are caught without
re-downloading every item every hour. At most 30 article requests go to one source per run,
with a 1-second gap per host. A failed request is only a source failure, never a statement
about the article.
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import time
from urllib.parse import urlsplit

from .config import Settings
from .domain import ArticleCandidate, FetchedArticle, RunSummary, Source, article_content_hash
from .sources import HttpResponse, HttpTransport, extract_article_text, parse_feed, parse_html_list, parse_sitemap
from .storage import Store


FRESH_FOR = timedelta(hours=48)
RECHECK_EVERY = timedelta(hours=3)
MAX_ARTICLE_FETCHES_PER_SOURCE = 30
MIN_HOST_DELAY_SECONDS = 1.0
# Taiwan feeds often write CST meaning China Standard Time (+08:00), not US Central.
_TZ_ALIASES = {" CST": " +0800"}


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: object) -> datetime | None:
    """ISO-8601 or RFC 822 to aware UTC; naive or unparseable values give None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for alias, offset in _TZ_ALIASES.items():
            if text.endswith(alias):
                text = text[: -len(alias)] + offset
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def needs_fetch(state: dict | None, now: datetime) -> bool:
    """New or never successfully fetched -> yes; fresh and not fetched recently -> yes; else no."""
    if not state or not state.get("content_hash"):
        return True
    published = parse_time(state.get("published_at")) or parse_time(state.get("discovered_at"))
    last_fetched = parse_time(state.get("last_fetched_at"))
    if published is None or last_fetched is None:
        return True
    return now - published < FRESH_FOR and now - last_fetched >= RECHECK_EVERY


def _candidates(feed: HttpResponse) -> list[ArticleCandidate]:
    content_type = feed.headers.get("Content-Type", "")
    looks_xml = "xml" in content_type.lower() or feed.url.lower().endswith((".xml", ".rss")) or feed.body.lstrip()[:5] == b"<?xml"
    if looks_xml:
        return parse_feed(feed.body, feed.url) or parse_sitemap(feed.body, feed.url)
    return parse_html_list(feed.body, feed.url)


class _PoliteFetcher:
    def __init__(self, transport: HttpTransport, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self._transport, self._clock, self._sleep = transport, clock, sleep
        self._last: dict[str, float] = {}

    def fetch(self, url: str) -> HttpResponse:
        host = (urlsplit(url).hostname or "").lower()
        if host in self._last:
            wait = MIN_HOST_DELAY_SECONDS - (self._clock() - self._last[host])
            if wait > 0:
                self._sleep(wait)
        try:
            return self._transport.fetch(url)
        finally:
            self._last[host] = self._clock()


def _fetch_article(candidate: ArticleCandidate, store: Store, fetcher: _PoliteFetcher, fetched_at: str) -> bool:
    """Fetch one article; return whether it created a revision. Any failure raises."""
    store.save_candidate(candidate)  # metadata first: a failed fetch still leaves the article on record
    response = fetcher.fetch(candidate.url)
    if not 200 <= response.status < 300:
        raise ValueError(f"article returned HTTP {response.status}")
    text = extract_article_text(response.body, response.headers.get("Content-Type"))
    if not text:
        raise ValueError("article text could not be extracted")
    article = FetchedArticle(candidate, text, fetched_at, article_content_hash(candidate.title, text))
    return store.save_fetched_article(article)[1]


def _watch_source(source: Source, store: Store, fetcher: _PoliteFetcher, now: datetime, seen_urls: set[str]):
    started_at = _timestamp(now)
    seen = created = failures = 0
    error: str | None = None
    try:
        feed = fetcher.fetch(source.feed_url)
        if not 200 <= feed.status < 300:
            raise ValueError(f"source returned HTTP {feed.status}")
        if not feed.body:
            raise ValueError("source response was empty or larger than 1 MB")
        candidates = _candidates(feed)
        seen = len(candidates)
        fetches = 0
        for candidate in candidates:
            if candidate.url in seen_urls:
                continue  # listed by another source this run
            seen_urls.add(candidate.url)
            published = parse_time(candidate.published_at)
            candidate = replace(
                candidate, source_id=source.id, discovered_at=started_at,
                published_at=_timestamp(published) if published else None,
            )
            if not needs_fetch(store.article_state(candidate.url), now) or fetches >= MAX_ARTICLE_FETCHES_PER_SOURCE:
                continue
            fetches += 1
            try:
                created += int(_fetch_article(candidate, store, fetcher, started_at))
            except Exception as article_error:
                failures += 1
                error = str(article_error)
    except Exception as source_error:
        failures += 1
        error = str(source_error)
    store.record_source_check(source.id, started_at, "failed" if failures else "ok", seen, error)
    return seen, created, failures


def watch_once(
    settings: Settings,
    store: Store,
    transport: HttpTransport,
    now: datetime | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> RunSummary:
    del settings
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    fetcher = _PoliteFetcher(transport, clock, sleep)
    seen_urls: set[str] = set()
    totals = [0, 0, 0, 0]  # sources, candidates, revisions, failures
    for source in store.list_sources(enabled_only=True):
        seen, created, failures = _watch_source(source, store, fetcher, current, seen_urls)
        for index, value in enumerate((1, seen, created, failures)):
            totals[index] += value
    summary = RunSummary(_timestamp(current), _timestamp(datetime.now(timezone.utc)), *totals)
    store.record_run(summary)
    return summary
