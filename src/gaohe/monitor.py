"""One short-lived monitoring pass over every enabled source (spec 5, 10).

Unchanged content must not cost repeated work, so requests are bounded four ways:
conditional GET on feeds and articles, an age-based recheck schedule for known
articles (``article_recheck_due``), at most ``MAX_ARTICLE_FETCHES_PER_SOURCE``
article requests per source per run, and a minimum delay between requests to the
same host. A failed request is only ever a source failure, and a skipped one is
work left due for a later run; neither says anything about an article's content.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import inspect
import time
from urllib.parse import urlsplit

from .config import Settings
from .domain import ArticleCandidate, FetchedArticle, RunSummary, Source, article_content_hash
from .sources import HttpResponse, HttpTransport, extract_article_text, parse_feed, parse_html_list, parse_sitemap
from .storage import Store


MAX_ARTICLE_FETCHES_PER_SOURCE = 30
MIN_HOST_DELAY_SECONDS = 1.0
# (age limit, minimum time between fetches) bands; articles 7 days or older are not rechecked by age.
RECHECK_SCHEDULE: tuple[tuple[timedelta, timedelta], ...] = (
    (timedelta(hours=6), timedelta(0)),
    (timedelta(hours=24), timedelta(hours=3)),
    (timedelta(hours=72), timedelta(hours=12)),
    (timedelta(days=7), timedelta(hours=48)),
)
_VALIDATOR_HEADERS = (("etag", "If-None-Match"), ("last_modified", "If-Modified-Since"))
_MARKER_KEY = "_article_marker"
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


def _timestamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    """Parse an ISO-8601 or RFC 822 timestamp to aware UTC; naive or unparseable values give None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def article_recheck_due(
    state: Mapping[str, object] | None,
    now: datetime,
    *,
    feed_marker_changed: bool | None = None,
) -> bool:
    """Return whether a listed article needs an HTTP request in this run.

    Spec 5.2 / 10: unchanged content must not cost repeated work, yet a story that
    is still being edited has to be seen again. ``state`` is
    ``Store.article_fetch_state``; ``feed_marker_changed`` is None when the listing
    has no per-article marker (article ETag or sitemap lastmod), else whether that
    marker differs from the stored one. In order:

    - never fetched (no state, or no stored revision yet): due, so a failed first
      fetch is retried every run;
    - the listing has a marker: due exactly when it changed;
    - no recorded fetch time, or one in the future: due;
    - otherwise by age since ``published_at`` (``discovered_at`` when it is
      missing or in the future), against the time of the last fetch:

      ===================  =======================
      article age          fetched again
      ===================  =======================
      under 6 hours        every run
      6 to 24 hours        at most every 3 hours
      24 to 72 hours       at most every 12 hours
      72 hours to 7 days   at most every 48 hours
      7 days or older      never (only a changed marker brings it back)
      ===================  =======================

    A boundary belongs to the older band: an article exactly 6 hours old is on the
    3-hour interval, and one exactly 7 days old is no longer rechecked.
    """
    if not state or not state.get("content_hash"):
        return True
    if feed_marker_changed is not None:
        return feed_marker_changed
    now = now.astimezone(timezone.utc)
    last_fetched = _parse_time(state.get("last_fetched_at"))
    if last_fetched is None or last_fetched > now:
        return True
    published = _parse_time(state.get("published_at"))
    if published is None or published > now:
        published = _parse_time(state.get("discovered_at"))
    if published is None:
        return True
    age = now - published
    for younger_than, interval in RECHECK_SCHEDULE:
        if age < younger_than:
            return now - last_fetched >= interval
    return False


def _accepts_headers(transport: object) -> bool:
    """Older transports and many test fakes implement fetch(url) only; those are never sent headers."""
    try:
        parameters = inspect.signature(transport.fetch).parameters.values()  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (parameter.name == "headers" and parameter.kind is not inspect.Parameter.POSITIONAL_ONLY)
        for parameter in parameters
    )


def _conditional_headers(validators: Mapping[str, object] | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, header in _VALIDATOR_HEADERS:
        value = (validators or {}).get(key)
        if isinstance(value, str) and value:
            headers[header] = value
    return headers


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return url


def _header(response: HttpResponse, name: str) -> str | None:
    value = response.headers.get(name)
    if value is None:
        lowered = name.lower()
        value = next((item for key, item in response.headers.items() if key.lower() == lowered), None)
    return value if isinstance(value, str) else None


class _PoliteFetcher:
    """Send every request of one run: spaced per host, conditional only when there are validators."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        min_delay: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._transport = transport
        self._sends_headers = _accepts_headers(transport)
        self._min_delay = min_delay
        self._clock = clock
        self._sleep = sleep
        self._last_request: dict[str, float] = {}

    def fetch(self, url: str, validators: Mapping[str, object] | None = None) -> HttpResponse:
        headers = _conditional_headers(validators) if self._sends_headers else {}
        host = _host(url)
        previous = self._last_request.get(host)
        if previous is not None:
            wait = self._min_delay - (self._clock() - previous)
            if wait > 0:
                self._sleep(wait)
        try:
            # With nothing to send, call fetch(url) exactly as transports without header support expect.
            response = self._transport.fetch(url, headers=headers) if headers else self._transport.fetch(url)
        finally:
            self._last_request[host] = self._clock()
        if response.status == 304 and not headers:
            raise ValueError("returned HTTP 304 to an unconditional request")
        return response


@dataclass(frozen=True)
class _Run:
    now: datetime
    started_at: str
    fetcher: _PoliteFetcher
    max_article_fetches: int
    listed_urls: set[str] = field(default_factory=set)


@dataclass
class _SourcePass:
    not_modified: bool = False
    seen: int = 0
    revisions_created: int = 0
    failures: int = 0
    deferred: int = 0
    error: str | None = None


def _is_xml_source(source_url: str, content_type: str) -> bool:
    return "xml" in content_type.lower() or source_url.lower().endswith((".xml", ".rss"))


def _candidates(body: bytes, source_url: str, content_type: str) -> list[ArticleCandidate]:
    if _is_xml_source(source_url, content_type):
        return parse_feed(body, source_url) or parse_sitemap(body, source_url)
    return parse_html_list(body, source_url)


def _marker(candidate: ArticleCandidate) -> str | None:
    article_etag = candidate.metadata.get("article_etag")
    if article_etag:
        return f"article_etag:{article_etag}"
    if candidate.metadata.get("discovery_type") == "sitemap" and candidate.published_at:
        return f"lastmod:{candidate.published_at}"
    return None


_DueArticle = tuple[ArticleCandidate, str | None, Mapping[str, object] | None]


def _feed_marker_changed(store: Store, url: str, marker: str | None, state: Mapping[str, object] | None) -> bool | None:
    """None when the listing has no per-article marker, else whether it differs from the stored one."""
    if marker is None:
        return None
    stored = store.latest_article_metadata(url) if state else None
    return (stored or {}).get(_MARKER_KEY) != marker


def _stored_candidate(candidate: ArticleCandidate, source_id: int, discovered_at: str) -> ArticleCandidate:
    """Bind a candidate to its source, first seen at this run; the listing marker is added only after a fetch."""
    published = _parse_time(candidate.published_at)
    metadata = {key: value for key, value in candidate.metadata.items() if key != _MARKER_KEY}
    return replace(
        candidate,
        source_id=source_id,
        published_at=_timestamp(published) if published else None,
        discovered_at=discovered_at,
        metadata=metadata,
    )


def _with_marker(candidate: ArticleCandidate, marker: str | None) -> ArticleCandidate:
    return replace(candidate, metadata={**candidate.metadata, _MARKER_KEY: marker}) if marker else candidate


def _fetch_priority(item: _DueArticle) -> tuple[bool, datetime]:
    state = item[2]
    fetched = bool(state and state.get("content_hash"))
    return fetched, (_parse_time(state.get("last_fetched_at")) if state else None) or _EARLIEST


def _fetch_article(
    candidate: ArticleCandidate,
    marker: str | None,
    state: Mapping[str, object] | None,
    store: Store,
    run: _Run,
) -> bool:
    """Fetch one due article and return whether it created a revision; any failure raises."""
    known = bool(state and state.get("content_hash"))
    if not known:
        # Metadata first (spec 5): a failed first fetch still leaves the article on record.
        store.save_candidate(candidate)
    response = run.fetcher.fetch(candidate.url, state if known else None)
    marked = _with_marker(candidate, marker)
    etag, last_modified = _header(response, "ETag"), _header(response, "Last-Modified")
    if response.status == 304:
        store.save_candidate(marked)
        store.record_article_fetch(candidate.url, run.started_at, changed=False, etag=etag, last_modified=last_modified)
        return False
    if not 200 <= response.status < 300:
        raise ValueError(f"article returned HTTP {response.status}")
    text = extract_article_text(response.body, _header(response, "Content-Type"))
    if not text:
        raise ValueError("article text could not be extracted")
    content_hash = article_content_hash(candidate.title, text)
    _, created = store.save_fetched_article(FetchedArticle(marked, text, run.started_at, content_hash))
    store.record_article_fetch(
        candidate.url, run.started_at, changed=created, etag=etag, last_modified=last_modified
    )
    return created


def _fetch_due_articles(
    candidates: list[ArticleCandidate], source_id: int, store: Store, run: _Run, result: _SourcePass
) -> None:
    due: list[_DueArticle] = []
    for candidate in candidates:
        if candidate.url in run.listed_urls:
            continue  # already handled this run, by this source or another one
        run.listed_urls.add(candidate.url)
        marker = _marker(candidate)
        state = store.article_fetch_state(candidate.url)
        marker_changed = _feed_marker_changed(store, candidate.url, marker, state)
        # Known articles that are not due are skipped without any request.
        if article_recheck_due(state, run.now, feed_marker_changed=marker_changed):
            due.append((candidate, marker, state))
    # New articles first, then the longest unchecked, so a capped run cannot starve unseen stories.
    due.sort(key=_fetch_priority)
    for index, (candidate, marker, state) in enumerate(due):
        stored = _stored_candidate(candidate, source_id, run.started_at)
        if index >= run.max_article_fetches:
            result.deferred += 1
            if state is None:
                store.save_candidate(stored)
            continue
        try:
            result.revisions_created += int(_fetch_article(stored, marker, state, store, run))
        except Exception as article_error:
            result.failures += 1
            result.error = str(article_error)


def _watch_source(source: Source, store: Store, run: _Run) -> _SourcePass:
    result = _SourcePass()
    feed: HttpResponse | None = None
    try:
        feed = run.fetcher.fetch(source.feed_url, store.source_fetch_state(source.id))
        if feed.status == 304:
            result.not_modified = True
            return result
        if not 200 <= feed.status < 300:
            raise ValueError(f"source returned HTTP {feed.status}")
        content_type = _header(feed, "Content-Type") or ""
        candidates = _candidates(feed.body, feed.url, content_type)
        if not candidates and feed.body.strip() and _is_xml_source(feed.url, content_type):
            raise ValueError("source feed could not be parsed")
        result.seen = len(candidates)
        _fetch_due_articles(candidates, source.id, store, run, result)
    except Exception as source_error:
        result.failures += 1
        result.error = str(source_error)
    if feed is not None and 200 <= feed.status < 300:
        # Keep the feed's validators only once its articles are fully handled; otherwise a 304 on the
        # next run would strand failed or deferred articles until the feed itself changes.
        complete = not result.failures and not result.deferred
        store.record_source_fetch_state(
            source.id,
            _header(feed, "ETag") if complete else None,
            _header(feed, "Last-Modified") if complete else None,
        )
    return result


def watch_once(
    settings: Settings,
    store: Store,
    transport: HttpTransport,
    now: datetime | None = None,
    *,
    max_article_fetches_per_source: int = MAX_ARTICLE_FETCHES_PER_SOURCE,
    min_host_delay_seconds: float = MIN_HOST_DELAY_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> RunSummary:
    del settings
    if max_article_fetches_per_source < 0:
        raise ValueError("max_article_fetches_per_source must not be negative")
    if min_host_delay_seconds < 0:
        raise ValueError("min_host_delay_seconds must not be negative")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    started_at = _timestamp(current)
    fetcher = _PoliteFetcher(transport, min_delay=min_host_delay_seconds, clock=clock, sleep=sleep)
    run = _Run(current, started_at, fetcher, max_article_fetches_per_source)
    sources_checked = candidates_seen = revisions_created = failures = 0

    for source in store.list_sources(enabled_only=True):
        sources_checked += 1
        result = _watch_source(source, store, run)
        candidates_seen += result.seen
        revisions_created += result.revisions_created
        failures += result.failures
        status = "not_modified" if result.not_modified else "failed" if result.failures else "ok"
        store.record_source_check(source.id, started_at, status, result.seen, result.error)

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
