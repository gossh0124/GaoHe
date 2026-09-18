from datetime import datetime, timezone
import sqlite3

from gaohe.config import Settings
from gaohe.domain import Source
from gaohe.sources import HttpResponse
from gaohe.storage import Store


NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch(self, url, timeout_seconds=20.0):
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def response(url, body, content_type="text/html"):
    return HttpResponse(200, url, {"Content-Type": content_type}, body)


def rss(url="https://example.test/story", title="Story"):
    return (
        b"<rss><channel><title>Example</title><item><title>"
        + title.encode()
        + b"</title><link>"
        + url.encode()
        + b"</link><pubDate>2026-09-18T10:00:00Z</pubDate></item></channel></rss>"
    )


def monitor(settings, store, transport):
    from gaohe.monitor import watch_once

    return watch_once(settings, store, transport, now=NOW)


def make_store(tmp_path):
    store = Store(tmp_path / "monitor.db")
    store.initialize()
    return store


def test_no_sources_records_an_empty_run(tmp_path):
    store = make_store(tmp_path)

    summary = monitor(Settings(data_dir=tmp_path), store, FakeTransport({}))

    assert summary.sources_checked == 0
    assert summary.candidates_seen == 0
    assert summary.revisions_created == 0
    assert summary.failures == 0
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT sources_checked, failures FROM runs").fetchall() == [(0, 0)]


def test_new_article_is_bound_to_persisted_source_before_its_first_revision(tmp_path):
    store = make_store(tmp_path)
    source_id = store.add_source(Source(None, "Example", "https://example.test/feed"))
    transport = FakeTransport(
        {
            "https://example.test/feed": response("https://example.test/feed", rss(), "application/rss+xml"),
            "https://example.test/story": response("https://example.test/story", b"<main><p>First body</p></main>"),
        }
    )

    summary = monitor(Settings(data_dir=tmp_path), store, transport)

    assert summary.revisions_created == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT source_id FROM articles").fetchall() == [(source_id,)]


def test_unchanged_article_creates_no_revision_on_a_second_run(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Example", "https://example.test/feed"))
    transport = FakeTransport(
        {
            "https://example.test/feed": response("https://example.test/feed", rss(), "application/rss+xml"),
            "https://example.test/story": response("https://example.test/story", b"<main><p>Same body</p></main>"),
        }
    )

    assert monitor(Settings(data_dir=tmp_path), store, transport).revisions_created == 1
    assert monitor(Settings(data_dir=tmp_path), store, transport).revisions_created == 0
    assert transport.calls.count("https://example.test/story") == 2


def test_sitemap_lastmod_date_does_not_break_revision_persistence(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Sitemap", "https://example.test/sitemap.xml"))
    transport = FakeTransport(
        {
            "https://example.test/sitemap.xml": response(
                "https://example.test/sitemap.xml",
                b"<urlset><url><loc>https://example.test/story</loc><lastmod>2026-09-18</lastmod></url></urlset>",
                "application/xml",
            ),
            "https://example.test/story": response("https://example.test/story", b"<main><p>Body</p></main>"),
        }
    )

    assert monitor(Settings(data_dir=tmp_path), store, transport).revisions_created == 1


def test_unchanged_sitemap_lastmod_skips_the_known_article_body_fetch(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Sitemap", "https://example.test/sitemap.xml"))
    transport = FakeTransport(
        {
            "https://example.test/sitemap.xml": response(
                "https://example.test/sitemap.xml",
                b"<urlset><url><loc>https://example.test/story</loc><lastmod>2026-09-18</lastmod></url></urlset>",
                "application/xml",
            ),
            "https://example.test/story": response("https://example.test/story", b"<main><p>Body</p></main>"),
        }
    )

    assert monitor(Settings(data_dir=tmp_path), store, transport).revisions_created == 1
    assert monitor(Settings(data_dir=tmp_path), store, transport).revisions_created == 0
    assert transport.calls.count("https://example.test/story") == 1


def test_unchanged_feed_etag_skips_the_known_article_body_fetch(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Feed", "https://example.test/feed"))
    transport = FakeTransport(
        {
            "https://example.test/feed": HttpResponse(
                200,
                "https://example.test/feed",
                {"Content-Type": "application/rss+xml", "ETag": "feed-v1"},
                rss(),
            ),
            "https://example.test/story": response("https://example.test/story", b"<main><p>Body</p></main>"),
        }
    )

    assert monitor(Settings(data_dir=tmp_path), store, transport).revisions_created == 1
    assert monitor(Settings(data_dir=tmp_path), store, transport).revisions_created == 0
    assert transport.calls.count("https://example.test/story") == 1


def test_changed_then_reverted_article_creates_each_current_state_transition(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Example", "https://example.test/feed"))
    transport = FakeTransport(
        {
            "https://example.test/feed": response("https://example.test/feed", rss(), "application/rss+xml"),
            "https://example.test/story": response("https://example.test/story", b"<main><p>A</p></main>"),
        }
    )

    assert monitor(Settings(data_dir=tmp_path), store, transport).revisions_created == 1
    transport.responses["https://example.test/story"] = response("https://example.test/story", b"<main><p>B</p></main>")
    assert monitor(Settings(data_dir=tmp_path), store, transport).revisions_created == 1
    transport.responses["https://example.test/story"] = response("https://example.test/story", b"<main><p>A</p></main>")
    assert monitor(Settings(data_dir=tmp_path), store, transport).revisions_created == 1


def test_a_failed_source_does_not_prevent_another_source_from_creating_a_revision(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Broken", "https://example.test/broken"))
    store.add_source(Source(None, "Working", "https://example.test/feed"))
    transport = FakeTransport(
        {
            "https://example.test/broken": OSError("offline"),
            "https://example.test/feed": response("https://example.test/feed", rss(), "application/rss+xml"),
            "https://example.test/story": response("https://example.test/story", b"<main><p>Working body</p></main>"),
        }
    )

    summary = monitor(Settings(data_dir=tmp_path), store, transport)

    assert (summary.sources_checked, summary.revisions_created, summary.failures) == (2, 1, 1)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT status FROM source_checks ORDER BY id").fetchall() == [("failed",), ("ok",)]


def test_malformed_feed_is_a_source_failure_and_empty_article_text_is_an_article_failure(tmp_path):
    store = make_store(tmp_path)
    store.add_source(Source(None, "Malformed", "https://example.test/malformed"))
    store.add_source(Source(None, "Empty article", "https://example.test/feed"))
    transport = FakeTransport(
        {
            "https://example.test/malformed": response("https://example.test/malformed", b"<rss>", "application/rss+xml"),
            "https://example.test/feed": response("https://example.test/feed", rss(), "application/rss+xml"),
            "https://example.test/story": response("https://example.test/story", b"<html><body>no semantic text</body></html>"),
        }
    )

    summary = monitor(Settings(data_dir=tmp_path), store, transport)

    assert (summary.candidates_seen, summary.revisions_created, summary.failures) == (1, 0, 2)


def test_watch_cli_prints_only_compact_counts_and_keeps_partial_failures_successful(tmp_path, monkeypatch, capsys):
    import gaohe.cli as cli
    from gaohe.domain import RunSummary

    monkeypatch.setattr(
        cli,
        "watch_once",
        lambda *_args: RunSummary("2026-09-18T12:00:00Z", "2026-09-18T12:00:00Z", 2, 3, 1, 1),
    )

    assert cli.main(["watch", "--once", "--env-file", str(tmp_path / ".env")]) == 0
    assert capsys.readouterr().out == "checked=2 candidates=3 revisions=1 failures=1\n"
