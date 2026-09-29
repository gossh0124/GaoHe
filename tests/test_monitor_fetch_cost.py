"""Fetch-cost controls of watch_once: conditional GET, recheck schedule, per-host delay, per-source cap."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

import gaohe.monitor as monitor_module
from gaohe.config import Settings
from gaohe.domain import ArticleCandidate, Source
from gaohe.monitor import watch_once
from gaohe.sources import HttpResponse
from gaohe.storage import Store


T0 = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
FEED = "https://news.example.test/feed.xml"
NOT_SENT = object()


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def html(text: str) -> bytes:
    return f"<main><p>{text}</p></main>".encode()


def rss(*items: tuple[str, str, str]) -> bytes:
    entries = "".join(
        f"<item><title>{title}</title><link>{url}</link><pubDate>{published}</pubDate></item>"
        for url, title, published in items
    )
    return f"<rss><channel><title>Example</title>{entries}</channel></rss>".encode()


@dataclass
class Page:
    body: bytes
    content_type: str = "text/html"
    etag: str | None = None
    last_modified: str | None = None
    status: int = 200
    latency: float = 0.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Site:
    """A fake web that honors If-None-Match / If-Modified-Since the way a real server does."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.pages: dict[str, Page | Exception] = {}
        self.calls: list[tuple[str, dict[str, str] | None]] = []
        self.clock = clock

    def fetch(self, url, timeout_seconds=20.0, headers=NOT_SENT):
        self.calls.append((url, None if headers is NOT_SENT else dict(headers)))
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        if self.clock is not None:
            self.clock.now += page.latency
        sent = {} if headers is NOT_SENT else headers
        if (page.etag and sent.get("If-None-Match") == page.etag) or (
            page.last_modified and sent.get("If-Modified-Since") == page.last_modified
        ):
            return HttpResponse(304, url, {"ETag": page.etag} if page.etag else {}, b"")
        response_headers = {"Content-Type": page.content_type}
        if page.etag:
            response_headers["ETag"] = page.etag
        if page.last_modified:
            response_headers["Last-Modified"] = page.last_modified
        return HttpResponse(page.status, url, response_headers, page.body)

    def urls(self) -> list[str]:
        return [url for url, _headers in self.calls]

    def take(self) -> list[str]:
        urls = self.urls()
        self.calls.clear()
        return urls


class UrlOnlyTransport:
    """The narrow fetch(url) shape many existing fakes implement."""

    def __init__(self, site: Site) -> None:
        self.site = site

    def fetch(self, url):
        return self.site.fetch(url)


def make_store(tmp_path) -> Store:
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    return store


def run(tmp_path, store, transport, at, clock=None, **kwargs):
    clock = clock or FakeClock()
    return watch_once(Settings(data_dir=tmp_path), store, transport, at, clock=clock, sleep=clock.sleep, **kwargs)


def checks(store) -> list[tuple[str, int, str | None]]:
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT status, candidates_seen, error FROM source_checks ORDER BY id").fetchall()
    connection.close()
    return rows


def revision_count(store) -> int:
    with sqlite3.connect(store.path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM article_revisions").fetchone()[0]
    connection.close()
    return count


def test_rss_items_without_markers_are_not_refetched_within_their_interval(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Example", FEED))
    fresh, older = "https://news.example.test/fresh", "https://news.example.test/older"
    site = Site()
    site.pages[FEED] = Page(
        rss((fresh, "Fresh", iso(T0 - HOUR)), (older, "Older", iso(T0 - 10 * HOUR))), "application/rss+xml"
    )
    site.pages[fresh] = Page(html("Fresh body"))
    site.pages[older] = Page(html("Older body"))

    first = run(tmp_path, store, site, T0)
    assert site.take() == [FEED, fresh, older]
    assert (first.candidates_seen, first.revisions_created, first.failures) == (2, 2, 0)

    # The 1-hour-old story is rechecked every run; the 10-hour-old one at most every 3 hours.
    for hours in (1, 2):
        summary = run(tmp_path, store, site, T0 + hours * HOUR)
        assert site.take() == [FEED, fresh]
        assert (summary.candidates_seen, summary.revisions_created, summary.failures) == (2, 0, 0)

    run(tmp_path, store, site, T0 + 3 * HOUR)
    assert site.take() == [FEED, older, fresh]  # the longest-unchecked article goes first
    assert revision_count(store) == 2


def test_hourly_runs_over_a_stable_feed_fetch_each_old_article_about_once(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Example", FEED))
    site = Site()
    items = [(f"https://news.example.test/story-{number}", f"Story {number}", iso(T0 - 48 * HOUR)) for number in range(50)]
    site.pages[FEED] = Page(rss(*items), "application/rss+xml")
    for url, _title, _published in items:
        site.pages[url] = Page(html(f"Body of {url}"))

    for hour in range(12):
        run(tmp_path, store, site, T0 + hour * HOUR)

    article_requests = [url for url in site.urls() if url != FEED]
    # Previously every run fetched all 50 articles: 600 requests in 12 hours.
    assert len(article_requests) == 50
    assert len(set(article_requests)) == 50
    assert revision_count(store) == 50


def test_feed_validators_are_sent_and_a_304_does_no_article_work(tmp_path):
    store = make_store(tmp_path)
    source_id = store.add_source(Source(None, "Example", FEED))
    story = "https://news.example.test/story"
    site = Site()
    site.pages[FEED] = Page(
        rss((story, "Story", iso(T0 - HOUR))), "application/rss+xml", '"feed-v1"', "Fri, 18 Sep 2026 11:00:00 GMT"
    )
    site.pages[story] = Page(html("Body"))

    run(tmp_path, store, site, T0)
    assert site.calls[0] == (FEED, None)
    assert store.source_fetch_state(source_id) == {"etag": '"feed-v1"', "last_modified": "Fri, 18 Sep 2026 11:00:00 GMT"}
    site.calls.clear()

    summary = run(tmp_path, store, site, T0 + HOUR)

    assert site.calls == [
        (FEED, {"If-None-Match": '"feed-v1"', "If-Modified-Since": "Fri, 18 Sep 2026 11:00:00 GMT"})
    ]
    assert (summary.sources_checked, summary.candidates_seen, summary.revisions_created, summary.failures) == (1, 0, 0, 0)
    assert checks(store)[-1] == ("not_modified", 0, None)


def test_a_200_feed_replaces_or_clears_its_validators(tmp_path):
    store = make_store(tmp_path)
    source_id = store.add_source(Source(None, "Example", FEED))
    story = "https://news.example.test/story"
    site = Site()
    site.pages[FEED] = Page(rss((story, "Story", iso(T0 - HOUR))), "application/rss+xml", '"feed-v1"')
    site.pages[story] = Page(html("Body"))

    run(tmp_path, store, site, T0)
    site.pages[FEED].etag = '"feed-v2"'
    run(tmp_path, store, site, T0 + HOUR)
    assert store.source_fetch_state(source_id) == {"etag": '"feed-v2"', "last_modified": None}

    site.pages[FEED].etag = None
    run(tmp_path, store, site, T0 + 2 * HOUR)
    assert store.source_fetch_state(source_id) == {"etag": None, "last_modified": None}


def test_feed_validators_are_withheld_while_article_work_remains(tmp_path):
    store = make_store(tmp_path)
    source_id = store.add_source(Source(None, "Example", FEED))
    story = "https://news.example.test/story"
    site = Site()
    site.pages[FEED] = Page(rss((story, "Story", iso(T0 - HOUR))), "application/rss+xml", '"feed-v1"')
    site.pages[story] = OSError("offline")

    first = run(tmp_path, store, site, T0)
    assert (first.revisions_created, first.failures) == (0, 1)
    assert store.source_fetch_state(source_id) == {"etag": None, "last_modified": None}

    # The unchanged feed is fetched in full again, so the failed article is retried instead of hidden by a 304.
    site.pages[story] = Page(html("Recovered"))
    site.calls.clear()
    second = run(tmp_path, store, site, T0 + HOUR)
    assert site.calls == [(FEED, None), (story, None)]
    assert (second.revisions_created, second.failures) == (1, 0)
    assert store.source_fetch_state(source_id)["etag"] == '"feed-v1"'


def test_an_unsolicited_304_is_a_source_failure_not_an_unchanged_feed(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Example", FEED))
    site = Site()
    site.pages[FEED] = Page(b"", "application/rss+xml", status=304)

    summary = run(tmp_path, store, site, T0)

    assert (summary.candidates_seen, summary.failures) == (0, 1)
    assert checks(store)[-1][0] == "failed"


def test_article_304_keeps_the_revision_and_counts_an_unchanged_fetch(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Example", FEED))
    story = "https://news.example.test/story"
    site = Site()
    site.pages[FEED] = Page(rss((story, "Story", iso(T0 - HOUR))), "application/rss+xml")
    site.pages[story] = Page(html("Body"), etag='"a1"', last_modified="Fri, 18 Sep 2026 10:30:00 GMT")

    assert run(tmp_path, store, site, T0).revisions_created == 1
    state = store.article_fetch_state(story)
    assert (state["etag"], state["last_modified"], state["fetch_count"], state["unchanged_count"]) == (
        '"a1"', "Fri, 18 Sep 2026 10:30:00 GMT", 1, 0
    )
    site.calls.clear()

    summary = run(tmp_path, store, site, T0 + HOUR)

    assert site.calls[1] == (story, {"If-None-Match": '"a1"', "If-Modified-Since": "Fri, 18 Sep 2026 10:30:00 GMT"})
    assert (summary.revisions_created, summary.failures) == (0, 0)
    state = store.article_fetch_state(story)
    assert (state["last_fetched_at"], state["fetch_count"], state["unchanged_count"]) == (iso(T0 + HOUR), 2, 1)
    assert revision_count(store) == 1
    assert checks(store)[-1] == ("ok", 1, None)


def test_article_etag_round_trip_follows_the_server_validator(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Example", FEED))
    story = "https://news.example.test/story"
    site = Site()
    site.pages[FEED] = Page(rss((story, "Story", iso(T0 - HOUR))), "application/rss+xml")
    site.pages[story] = Page(html("First"), etag='"a1"')

    run(tmp_path, store, site, T0)
    site.pages[story] = Page(html("Second"), etag='"a2"')
    site.calls.clear()

    assert run(tmp_path, store, site, T0 + HOUR).revisions_created == 1
    assert site.calls[1] == (story, {"If-None-Match": '"a1"'})
    state = store.article_fetch_state(story)
    assert (state["etag"], state["fetch_count"], state["unchanged_count"]) == ('"a2"', 2, 0)
    site.calls.clear()

    assert run(tmp_path, store, site, T0 + 2 * HOUR).revisions_created == 0
    assert site.calls[1] == (story, {"If-None-Match": '"a2"'})
    assert store.article_fetch_state(story)["unchanged_count"] == 1


def test_identical_content_without_validators_is_an_unchanged_fetch(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Example", FEED))
    story = "https://news.example.test/story"
    site = Site()
    site.pages[FEED] = Page(rss((story, "Story", iso(T0 - HOUR))), "application/rss+xml")
    site.pages[story] = Page(html("Same"))

    run(tmp_path, store, site, T0)
    assert run(tmp_path, store, site, T0 + HOUR).revisions_created == 0

    state = store.article_fetch_state(story)
    assert (state["fetch_count"], state["unchanged_count"]) == (2, 1)
    assert site.calls[-1] == (story, None)


def test_headers_are_omitted_without_validators_and_url_only_transports_still_work(tmp_path):
    store = make_store(tmp_path)
    source_id = store.add_source(Source(None, "Example", FEED))
    story = "https://news.example.test/story"
    site = Site()
    site.pages[FEED] = Page(rss((story, "Story", iso(T0 - HOUR))), "application/rss+xml", '"feed-v1"')
    site.pages[story] = Page(html("A"), etag='"a1"')
    transport = UrlOnlyTransport(site)

    assert run(tmp_path, store, transport, T0).revisions_created == 1
    assert store.source_fetch_state(source_id)["etag"] == '"feed-v1"'
    site.pages[story] = Page(html("B"), etag='"a2"')

    # Validators are stored, but a fetch(url)-only transport is never handed a headers argument.
    assert run(tmp_path, store, transport, T0 + HOUR).revisions_created == 1
    assert site.calls == [(FEED, None), (story, None), (FEED, None), (story, None)]


def test_same_host_requests_are_spaced_by_the_minimum_delay(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Example", FEED))
    first, other_host, second = (
        "https://news.example.test/a", "https://cdn.example.test/b", "https://news.example.test/c"
    )
    clock = FakeClock()
    site = Site(clock)
    site.pages[FEED] = Page(
        rss(*((url, url[-1], iso(T0 - HOUR)) for url in (first, other_host, second))), "application/rss+xml"
    )
    site.pages[first] = Page(html("A"))
    site.pages[other_host] = Page(html("B"), latency=0.7)
    site.pages[second] = Page(html("C"))

    run(tmp_path, store, site, T0, clock)

    assert site.urls() == [FEED, first, other_host, second]
    # feed -> a waits the full second; b is another host; c only waits what b's 0.7 s did not cover.
    assert clock.sleeps == [pytest.approx(1.0), pytest.approx(0.3)]


def test_host_delay_is_configurable_and_never_sleeps_for_distinct_hosts(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Example", FEED))
    story = "https://other.example.test/story"
    clock = FakeClock()
    site = Site(clock)
    site.pages[FEED] = Page(rss((story, "Story", iso(T0 - HOUR))), "application/rss+xml")
    site.pages[story] = Page(html("Body"))

    run(tmp_path, store, site, T0, clock)
    assert clock.sleeps == []

    store.add_source(Source(None, "Second feed on the same host", "https://news.example.test/second.xml"))
    site.pages["https://news.example.test/second.xml"] = Page(rss(), "application/rss+xml")
    run(tmp_path, store, site, T0 + HOUR, clock, min_host_delay_seconds=2.5)
    assert clock.sleeps == [pytest.approx(2.5)]


def test_per_source_cap_defers_the_rest_to_the_next_run(tmp_path):
    store = make_store(tmp_path)
    source_id = store.add_source(Source(None, "Busy", FEED))
    other_feed, other_story = "https://other.example.test/feed.xml", "https://other.example.test/story"
    store.add_source(Source(None, "Quiet", other_feed))
    stories = [f"https://news.example.test/story-{number}" for number in range(1, 6)]
    site = Site()
    site.pages[FEED] = Page(
        rss(*((url, url[-7:], iso(T0 - HOUR)) for url in stories)), "application/rss+xml", '"feed-v1"'
    )
    site.pages[other_feed] = Page(rss((other_story, "Other", iso(T0 - HOUR))), "application/rss+xml")
    site.pages[other_story] = Page(html("Other body"))
    for url in stories:
        site.pages[url] = Page(html(f"Body {url[-1]}"))

    first = run(tmp_path, store, site, T0, max_article_fetches_per_source=2)

    assert site.take() == [FEED, stories[0], stories[1], other_feed, other_story]
    assert (first.candidates_seen, first.revisions_created, first.failures) == (6, 3, 0)
    assert checks(store) == [("ok", 5, None), ("ok", 1, None)]
    # Deferred new stories are still on record (metadata first), and the feed stays unconditional.
    assert all(store.article_fetch_state(url) is not None for url in stories)
    assert store.source_fetch_state(source_id)["etag"] is None

    run(tmp_path, store, site, T0 + HOUR, max_article_fetches_per_source=2)
    assert site.take()[:3] == [FEED, stories[2], stories[3]]

    run(tmp_path, store, site, T0 + 2 * HOUR, max_article_fetches_per_source=2)
    assert site.take()[:3] == [FEED, stories[4], stories[0]]
    assert revision_count(store) == 6


def test_zero_cap_records_metadata_without_article_requests(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Example", FEED))
    story = "https://news.example.test/story"
    site = Site()
    site.pages[FEED] = Page(rss((story, "Story", iso(T0 - HOUR))), "application/rss+xml")

    summary = run(tmp_path, store, site, T0, max_article_fetches_per_source=0)

    assert site.urls() == [FEED]
    assert (summary.candidates_seen, summary.revisions_created, summary.failures) == (1, 0, 0)
    assert store.article_fetch_state(story)["discovered_at"] == iso(T0)


@pytest.mark.parametrize(
    "kwargs", [{"max_article_fetches_per_source": -1}, {"min_host_delay_seconds": -0.5}]
)
def test_negative_bounds_are_rejected(tmp_path, kwargs):
    with pytest.raises(ValueError):
        run(tmp_path, make_store(tmp_path), Site(), T0, **kwargs)


def test_an_article_listed_twice_is_fetched_once_per_run(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Main", FEED))
    syndicator = "https://syndicator.example.test/feed.xml"
    store.add_source(Source(None, "Syndicator", syndicator))
    story = "https://news.example.test/story"
    site = Site()
    site.pages[FEED] = Page(rss((story, "Story", iso(T0 - HOUR)), (story, "Story", iso(T0 - HOUR))), "application/rss+xml")
    site.pages[syndicator] = Page(rss((story, "Story", iso(T0 - HOUR))), "application/rss+xml")
    site.pages[story] = Page(html("Body"))

    summary = run(tmp_path, store, site, T0)

    assert site.urls().count(story) == 1
    assert (summary.candidates_seen, summary.revisions_created) == (3, 1)


def test_offset_and_rfc822_publication_times_are_stored_in_utc(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Atom", "https://atom.example.test/feed.xml"))
    store.add_source(Source(None, "RSS", FEED))
    atom_story, rss_story = "https://atom.example.test/story", "https://news.example.test/story"
    site = Site()
    site.pages["https://atom.example.test/feed.xml"] = Page(
        b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title><entry><title>Taipei</title>'
        b'<link href="https://atom.example.test/story"/><updated>2026-09-18T18:00:00+08:00</updated></entry></feed>',
        "application/atom+xml",
    )
    site.pages[FEED] = Page(rss((rss_story, "RFC", "Fri, 18 Sep 2026 10:30:00 +0000")), "application/rss+xml")
    site.pages[atom_story] = Page(html("Atom body"))
    site.pages[rss_story] = Page(html("RSS body"))

    summary = run(tmp_path, store, site, T0)

    # A +08:00 timestamp used to be rejected by the store, failing every article of such a feed.
    assert (summary.revisions_created, summary.failures) == (2, 0)
    assert store.article_fetch_state(atom_story)["published_at"] == "2026-09-18T10:00:00Z"
    assert store.article_fetch_state(rss_story)["published_at"] == "2026-09-18T10:30:00Z"


def test_a_changed_marker_survives_a_failed_or_deferred_fetch(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Feed", FEED))
    story = "https://news.example.test/story"
    marker = {"value": "v1"}

    def candidates(*_args):
        return [ArticleCandidate(0, story, "Story", iso(T0 - 30 * 24 * HOUR), iso(T0), {"article_etag": marker["value"]})]

    monkeypatch.setattr(monitor_module, "_candidates", candidates)
    site = Site()
    site.pages[FEED] = Page(rss(), "application/rss+xml")
    site.pages[story] = Page(html("First"))
    assert run(tmp_path, store, site, T0).revisions_created == 1

    marker["value"] = "v2"
    site.pages[story] = OSError("offline")
    assert run(tmp_path, store, site, T0 + HOUR).failures == 1
    assert run(tmp_path, store, site, T0 + 2 * HOUR, max_article_fetches_per_source=0).failures == 0

    # Neither the failure nor the deferral recorded v2 as seen, so the edit is still picked up.
    site.pages[story] = Page(html("Second"))
    assert run(tmp_path, store, site, T0 + 3 * HOUR).revisions_created == 1
    assert store.latest_article_metadata(story)["_article_marker"] == "article_etag:v2"
    site.calls.clear()
    assert run(tmp_path, store, site, T0 + 4 * HOUR).revisions_created == 0
    assert site.urls() == [FEED]
