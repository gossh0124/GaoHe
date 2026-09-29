"""Shared contracts added before the second parallel round (errors, pause state, manual source, feed discovery)."""

from gaohe.domain import CheckOutcome, Source
from gaohe.errors import PROVIDER_ERROR_MESSAGES, LiveCheckItem, ProviderError
from gaohe.sources import discover_feeds
from gaohe.storage import MANUAL_SOURCE_FEED_URL, SCHEMA_VERSION, Store


def test_provider_error_is_classified_secret_free_and_still_a_value_error():
    error = ProviderError("auth", provider="gemini")
    assert isinstance(error, ValueError) and error.stops_run and not error.transient
    assert str(error) == PROVIDER_ERROR_MESSAGES["auth"]
    assert ProviderError("made-up").code == "unavailable"
    assert ProviderError("rate_limit").transient and not ProviderError("rate_limit").stops_run
    assert LiveCheckItem("analysis", True, "ok", "可以使用").ok


def test_check_outcome_defaults():
    outcome = CheckOutcome("invalid_url", "請貼上以 http 或 https 開頭的新聞網址。")
    assert outcome.revision_id is None and outcome.article_id is None


def test_pause_state_round_trip_and_default(tmp_path):
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    assert SCHEMA_VERSION >= 5
    assert store.monitoring_paused() is False
    store.set_monitoring_paused(True, "2026-09-29T01:00:00Z")
    assert store.monitoring_paused() is True
    store.set_monitoring_paused(False, "2026-09-29T02:00:00Z")
    assert store.monitoring_paused() is False


def test_manual_source_is_hidden_never_polled_and_cannot_be_enabled(tmp_path):
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    feed_id = store.add_source(Source(None, "Example", "https://example.test/feed.xml"))
    manual_id = store.ensure_manual_source()
    assert store.ensure_manual_source() == manual_id
    assert [source.id for source in store.list_sources()] == [feed_id]
    assert [source.id for source in store.list_sources(enabled_only=True)] == [feed_id]
    manual = [source for source in store.list_sources(include_manual=True) if source.id == manual_id][0]
    assert (manual.kind, manual.enabled, manual.feed_url) == ("manual", False, MANUAL_SOURCE_FEED_URL)
    assert store.set_source_enabled(manual_id, True) is False
    assert store.list_sources(enabled_only=True, include_manual=True)[0].id == feed_id


def test_discover_feeds_finds_alternate_links_in_order_and_resolves_relative_urls():
    page = (
        "<html><head>"
        "<link rel='stylesheet' href='/a.css'>"
        "<link rel='alternate' type='application/rss+xml' title='即時' href='/rss/realtime.xml'>"
        "<link rel='Alternate' type='application/atom+xml; charset=utf-8' href='https://news.example/atom'>"
        "<link rel='alternate' type='application/rss+xml' href='/rss/realtime.xml'>"
        "<link rel='alternate' type='text/html' href='/en'>"
        "<link rel='alternate' type='application/rss+xml' href='javascript:alert(1)'>"
        "</head><body>新聞</body></html>"
    ).encode("utf-8")
    assert discover_feeds(page, "https://news.example/home") == [
        "https://news.example/rss/realtime.xml",
        "https://news.example/atom",
    ]
    assert discover_feeds(b"<html><body>no feeds</body></html>", "https://news.example/") == []
