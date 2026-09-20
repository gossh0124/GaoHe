from datetime import datetime, timezone
from pathlib import Path

from gaohe.cli import run_pending_analysis
from gaohe.config import load_settings
from gaohe.domain import Claim, Source
from gaohe.monitor import watch_once
from gaohe.providers import AnalysisResult
from gaohe.setup_flow import validate_setup_form, write_setup_config
from gaohe.sources import HttpResponse
from gaohe.storage import Store
from gaohe.web import render_status_page


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses

    def fetch(self, url, timeout_seconds=20.0):
        del timeout_seconds
        return self.responses[url]


class FakeAnalysis:
    def analyze(self, revision, related):
        del related
        text = "reported figure"
        start = revision.text.index(text)
        claim = Claim(None, revision.id, text, start, start + len(text), "checkable", "material", "extracted")
        return AnalysisResult(revision.id, (claim,), ())


class NoSearch:
    def search(self, query, limit=5):
        del query, limit
        return ()


class NoFetcher:
    def fetch(self, url):
        raise AssertionError(f"unexpected evidence fetch: {url}")


def _response(url: str, body: bytes, content_type: str = "text/html") -> HttpResponse:
    return HttpResponse(200, url, {"Content-Type": content_type}, body)


def _rss() -> bytes:
    return (
        b"<rss><channel><title>Example</title><item><title>Local article</title>"
        b"<link>https://example.test/article</link><pubDate>2026-09-21T10:00:00Z</pubDate>"
        b"</item></channel></rss>"
    )


def test_offline_windows_user_flow_keeps_key_local_and_skips_unchanged_content(tmp_path):
    env_path = tmp_path / ".env"
    data_dir = tmp_path / "data"
    env_path.write_text(f"DATA_DIR={data_dir}\n", encoding="utf-8")
    form = {
        "provider": "gemini",
        "model": "gemini-2.5-flash-lite",
        "api_key": "sample-user-key",
        "media_name": "Example News",
        "source_url": "https://example.test/feed.xml",
    }

    assert validate_setup_form(form) == []
    write_setup_config(form, env_path)
    settings = load_settings(env_path, environ={})
    store = Store(settings.database_path)
    store.initialize()
    store.add_source(Source(None, form["media_name"], form["source_url"]))

    transport = FakeTransport({
        form["source_url"]: _response(form["source_url"], _rss(), "application/rss+xml"),
        "https://example.test/article": _response(
            "https://example.test/article", b"<main>Local report contains a reported figure.</main>"
        ),
    })
    assert watch_once(settings, store, transport, now=NOW).revisions_created == 1
    assert watch_once(settings, store, transport, now=NOW).revisions_created == 0

    summary = run_pending_analysis(store, FakeAnalysis(), NoSearch(), NoFetcher(), 10)
    assert summary == {"claims": 1, "candidates": 0, "visible_findings": 0, "pending": 0, "retrieval_failures": 0}
    revision = store.list_recent_revisions()[0]
    page = render_status_page({
        "runtime": {"LLM provider": settings.llm_provider, "LLM API key": "present"},
        "inbox": ({"title": revision.title, "source": form["media_name"], "text": revision.text},),
        "findings": (),
        "comparisons": (),
        "sources": ({"name": form["media_name"], "status": "ok", "checked_at": NOW.isoformat(), "candidates_seen": 1},),
    })
    assert "Inbox" in page and "No visible findings." in page and "Source health" in page
    assert revision.text in page and "sample-user-key" not in page

    uninstall_script = (Path(__file__).parents[1] / "scripts" / "uninstall.ps1").read_text(encoding="utf-8")
    assert '$RequiredConfirmation = "DELETE GAOHE DATA"' in uninstall_script
    assert "if ($DeleteConfig -or $DeleteData)" in uninstall_script
