from dataclasses import dataclass
from pathlib import Path

import pytest

from gaohe.domain import (
    ANALYSIS_STATUSES,
    ArticleCandidate,
    Claim,
    Evidence,
    FetchedArticle,
    Finding,
    RunSummary,
    Source,
    TopicGroup,
    article_content_hash,
)
from gaohe.storage import Store


SECRETS = ("SECRET-A", "SECRET-B", "SECRET-C", "SECRET-D", "SECRET-E", "SECRET-F", "hunter2", "user:pass")
INBOX_KEYS = {
    "article_id", "revision_id", "title", "url", "source", "manual", "published_at", "updated_at", "text",
    "analysis_status", "annotations", "evidence", "pending_findings",
}
FINDING_KEYS = {
    "id", "revision_id", "article_id", "article_title", "article_url", "source", "finding_type", "summary",
    "start", "end", "status", "evidence_status", "visible", "review_status", "reviewed_at", "is_current", "evidence",
}
EVIDENCE_KEYS = {"url", "title", "provider", "retrieved_at", "relation", "status", "source_kind", "rationale"}
SOURCE_KEYS = {"id", "name", "enabled", "status", "checked_at", "candidates_seen", "error"}
RUN_KEYS = {"started_at", "finished_at", "sources_checked", "candidates_seen", "revisions_created", "failures"}


@dataclass
class Fixture:
    store: Store
    alpha: int
    bravo: int
    charlie: int
    old_alpha_revision: int
    alpha_revision: int
    bravo_revision: int
    unanalyzed_revision: int
    failed_revision: int
    visible_finding: int
    pending_finding: int
    bravo_finding: int
    topic: int


def save(store: Store, source_id: int, url: str, title: str, text: str, fetched_at: str, published_at: str | None = None) -> int:
    item = ArticleCandidate(source_id, url, title, published_at, "2026-09-18T01:00:00Z", {"section": "news"})
    return store.save_fetched_article(FetchedArticle(item, text, fetched_at, article_content_hash(title, text)))[0]


def span(text: str, part: str) -> tuple[int, int]:
    start = text.index(part)
    return start, start + len(part)


ALPHA_URL = "https://alpha.test/harbor?session=SECRET-A&page=2"
BRAVO_URL = "https://user:pass@bravo.test/harbor#access_token=SECRET-B"
ALPHA_TEXT = "The port authority closed the harbor on Monday after 120 ships were delayed."
BRAVO_TEXT = "The harbor stayed open on Monday while 12 ships were delayed."


def build_fixture(tmp_path: Path) -> Fixture:
    store = Store(tmp_path / "dashboard.db")
    store.initialize()
    alpha = store.add_source(Source(None, "Alpha Daily", "https://alpha.test/feed"))
    bravo = store.add_source(Source(None, "Bravo News", "https://bravo.test/feed"))
    charlie = store.add_source(Source(None, "Charlie Wire", "https://charlie.test/feed", enabled=False))

    old_alpha = save(store, alpha, ALPHA_URL, "Harbor closed", "Draft text.", "2026-09-18T03:00:00Z", "2026-09-18T02:50:00Z")
    bravo_revision = save(store, bravo, BRAVO_URL, "Harbor open", BRAVO_TEXT, "2026-09-18T04:00:00Z")
    failed = save(store, bravo, "https://bravo.test/weather", "Weather", "Rain expected.", "2026-09-18T04:15:00Z")
    unanalyzed = save(store, alpha, "https://alpha.test/sports", "Sports", "Team won.", "2026-09-18T04:30:00Z")
    alpha_revision = save(store, alpha, ALPHA_URL, "Harbor closed", ALPHA_TEXT, "2026-09-18T05:00:00Z", "2026-09-18T02:50:00Z")

    closed = span(ALPHA_TEXT, "closed the harbor")
    ships = span(ALPHA_TEXT, "120 ships")
    store.save_analysis(
        alpha_revision,
        (
            Claim(None, alpha_revision, "closed the harbor", *closed, "checkable", "material", "extracted"),
            Claim(None, alpha_revision, "120 ships", *ships, "checkable", "material", "extracted"),
        ),
        (
            Finding(None, alpha_revision, None, "factual_contradiction", "Port notice says the harbor stayed open",
                    *closed, "resolved", "retrieved", True),
            Finding(None, alpha_revision, None, "factual_contradiction", "Ship count not yet checked",
                    *ships, "pending", "retrieval_failed", False),
        ),
        (
            (Evidence(None, None, "https://evidence.test/notice?token=SECRET-C&lang=en", "Port notice", "Harbor open.",
                      "contradicts", "retrieved", "direct", "2026-09-18T05:10:00Z", "direct-http", "2026-09-18T01:00:00Z",
                      "hash-notice", "Notice says open; password=hunter2 was in the page"),),
            (Evidence(None, None, "https://evidence.test/ships?api_key=SECRET-D", "Ships", "", "context",
                      "retrieval_failed", "direct", "2026-09-18T05:11:00Z", rationale="Page timed out"),),
        ),
        provider="gemini", model="gemini-2.5-flash", prompt_version="v1", completed_at="2026-09-18T05:12:00Z",
    )
    harbor = span(BRAVO_TEXT, "harbor stayed open")
    store.save_analysis(
        bravo_revision,
        (Claim(None, bravo_revision, "harbor stayed open", *harbor, "checkable", "material", "extracted"),),
        (Finding(None, bravo_revision, None, "material_cross_media_difference", "Alpha reports a closure",
                 *harbor, "resolved", "retrieved", True),),
        ((Evidence(None, None, ALPHA_URL, "Harbor closed", ALPHA_TEXT, "contradicts", "retrieved", "related_article",
                   "2026-09-18T05:00:00Z", "related_revision"),),),
        completed_at="2026-09-18T05:13:00Z",
    )
    store.mark_analysis_running(failed, "2026-09-18T05:20:00Z")
    store.mark_analysis_failed(failed, "2026-09-18T05:21:00Z", "HTTP 401 key=SECRET-E")
    visible_finding, pending_finding, bravo_finding = 1, 2, 3
    store.review_finding(visible_finding, "confirmed", "2026-09-18T06:00:00Z", "Matches the port notice")

    topic = store.assign_topic(bravo_revision, alpha_revision, "Harbor closure", "high")
    single_article = store.save_topic(TopicGroup(None, "Alpha rewrite", "possible", "possible"))
    store.link_revision_to_topic(old_alpha, single_article)
    store.link_revision_to_topic(alpha_revision, single_article)
    dismissed = store.save_topic(TopicGroup(None, "Rejected pairing", "possible", "dismissed"))
    store.link_revision_to_topic(unanalyzed, dismissed)
    store.link_revision_to_topic(failed, dismissed)

    store.record_source_check(alpha, "2026-09-18T03:00:00Z", "failed", 0, "old failure")
    store.record_source_check(alpha, "2026-09-18T05:00:00Z", "ok", 3, None)
    store.record_source_check(bravo, "2026-09-18T03:00:00Z", "ok", 2, None)
    store.record_source_check(
        bravo, "2026-09-18T05:00:00Z", "failed", 0,
        "Authorization: Bearer SECRET-F\nGET https://bravo.test/feed?api_key=SECRET-F&kind=rss",
    )
    store.record_run(RunSummary("2026-09-18T03:00:00Z", "2026-09-18T03:01:00Z", 2, 2, 2, 1))
    store.record_run(RunSummary("2026-09-18T05:00:00Z", "2026-09-18T05:02:00Z", 2, 3, 1, 1))
    return Fixture(
        store, alpha, bravo, charlie, old_alpha, alpha_revision, bravo_revision, unanalyzed, failed,
        visible_finding, pending_finding, bravo_finding, topic,
    )


def url_fields(snapshot: dict) -> list[str]:
    urls = [item["url"] for item in snapshot["inbox"]]
    urls += [evidence["url"] for item in snapshot["inbox"] for evidence in item["evidence"]]
    urls += [item["article_url"] for item in snapshot["findings"]]
    urls += [evidence["url"] for item in snapshot["findings"] for evidence in item["evidence"]]
    urls += [article["url"] for item in snapshot["comparisons"] for article in item["articles"]]
    return urls


def test_dashboard_snapshot_has_exactly_the_contract_keys(tmp_path: Path):
    snapshot = build_fixture(tmp_path).store.dashboard_snapshot()

    assert set(snapshot) == {"inbox", "findings", "comparisons", "sources", "last_run", "analysis", "monitoring_paused"}
    assert all(set(item) == INBOX_KEYS for item in snapshot["inbox"])
    assert all(set(item) == FINDING_KEYS for item in snapshot["findings"])
    assert all(set(evidence) == EVIDENCE_KEYS for item in snapshot["findings"] for evidence in item["evidence"])
    assert all(set(evidence) == EVIDENCE_KEYS for item in snapshot["inbox"] for evidence in item["evidence"])
    assert all(set(item) == {"id", "label", "confidence", "status", "articles"} for item in snapshot["comparisons"])
    assert all(
        set(article) == {"title", "url", "source", "revision_id"}
        for item in snapshot["comparisons"] for article in item["articles"]
    )
    assert all(set(item) == SOURCE_KEYS for item in snapshot["sources"])
    assert set(snapshot["last_run"]) == RUN_KEYS


def test_dashboard_inbox_lists_current_revisions_newest_first_with_annotations(tmp_path: Path):
    fixture = build_fixture(tmp_path)

    inbox = fixture.store.dashboard_snapshot()["inbox"]

    assert [item["revision_id"] for item in inbox] == [
        fixture.alpha_revision, fixture.unanalyzed_revision, fixture.failed_revision, fixture.bravo_revision,
    ]
    alpha, unanalyzed, failed, bravo = inbox
    closed = span(ALPHA_TEXT, "closed the harbor")
    assert alpha | {"annotations": None, "evidence": None} == {
        "article_id": 1, "revision_id": fixture.alpha_revision, "title": "Harbor closed",
        "url": "https://alpha.test/harbor?session=%2A%2A%2A&page=2", "source": "Alpha Daily", "manual": False,
        "published_at": "2026-09-18T02:50:00Z", "updated_at": "2026-09-18T05:00:00Z", "text": ALPHA_TEXT,
        "analysis_status": "completed", "annotations": None, "evidence": None, "pending_findings": 1,
    }
    assert alpha["annotations"] == (
        Finding(fixture.visible_finding, fixture.alpha_revision, 1, "factual_contradiction",
                "Port notice says the harbor stayed open", *closed, "resolved", "retrieved", True, "confirmed"),
    )
    assert alpha["evidence"] == [{
        "url": "https://evidence.test/notice?token=%2A%2A%2A&lang=en", "title": "Port notice", "provider": "direct-http",
        "retrieved_at": "2026-09-18T05:10:00Z", "relation": "contradicts", "status": "retrieved", "source_kind": "direct",
        "rationale": "Notice says open; password=[redacted] was in the page",
    }]
    assert (unanalyzed["analysis_status"], unanalyzed["annotations"], unanalyzed["evidence"], unanalyzed["pending_findings"]) == (
        "unanalyzed", (), [], 0,
    )
    assert failed["analysis_status"] == "failed"
    assert bravo["url"] == "https://bravo.test/harbor#access_token=***"
    assert [finding.finding_type for finding in bravo["annotations"]] == ["material_cross_media_difference"]
    assert bravo["evidence"][0]["source_kind"] == "related_article"
    assert all(isinstance(finding, Finding) and finding.visible for item in inbox for finding in item["annotations"])


def test_list_findings_returns_newest_first_with_evidence_chain(tmp_path: Path):
    fixture = build_fixture(tmp_path)

    visible = fixture.store.list_findings()
    everything = fixture.store.list_findings(visible_only=False)

    assert [item["id"] for item in visible] == [fixture.bravo_finding, fixture.visible_finding]
    assert [item["id"] for item in everything] == [fixture.bravo_finding, fixture.pending_finding, fixture.visible_finding]
    assert fixture.store.list_findings(1) == visible[:1]
    confirmed = visible[1]
    assert confirmed | {"evidence": None} == {
        "id": fixture.visible_finding, "revision_id": fixture.alpha_revision, "article_id": 1,
        "article_title": "Harbor closed", "article_url": "https://alpha.test/harbor?session=%2A%2A%2A&page=2",
        "source": "Alpha Daily", "finding_type": "factual_contradiction",
        "summary": "Port notice says the harbor stayed open", "start": span(ALPHA_TEXT, "closed the harbor")[0],
        "end": span(ALPHA_TEXT, "closed the harbor")[1], "status": "resolved", "evidence_status": "retrieved",
        "visible": True, "review_status": "confirmed", "reviewed_at": "2026-09-18T06:00:00Z", "is_current": True,
        "evidence": None,
    }
    pending = everything[1]
    assert (pending["visible"], pending["review_status"], pending["reviewed_at"]) == (False, "unreviewed", None)
    assert pending["evidence"] == [{
        "url": "https://evidence.test/ships?api_key=%2A%2A%2A", "title": "Evidence retrieval failed", "provider": None,
        "retrieved_at": "2026-09-18T05:11:00Z", "relation": "context", "status": "retrieval_failed",
        "source_kind": "direct", "rationale": "Page timed out",
    }]


def test_list_findings_validates_limit_and_handles_findings_without_evidence(tmp_path: Path):
    fixture = build_fixture(tmp_path)
    finding_id = fixture.store.save_finding(
        Finding(None, fixture.unanalyzed_revision, None, "unsupported_inference", "No evidence yet", 0, 4, "pending", "pending", True)
    )

    assert fixture.store.list_findings(1)[0]["id"] == finding_id
    assert fixture.store.list_findings(1)[0]["evidence"] == []
    with pytest.raises(ValueError, match="positive"):
        fixture.store.list_findings(0)


def test_dashboard_comparisons_need_two_distinct_articles_in_an_open_topic(tmp_path: Path):
    fixture = build_fixture(tmp_path)

    comparisons = fixture.store.dashboard_snapshot()["comparisons"]

    assert comparisons == [{
        "id": fixture.topic, "label": "Harbor closure", "confidence": "high", "status": "active",
        "articles": [
            {"title": "Harbor open", "url": "https://bravo.test/harbor#access_token=***", "source": "Bravo News",
             "revision_id": fixture.bravo_revision},
            {"title": "Harbor closed", "url": "https://alpha.test/harbor?session=%2A%2A%2A&page=2", "source": "Alpha Daily",
             "revision_id": fixture.alpha_revision},
        ],
    }]


def test_comparison_lists_each_article_once_with_its_newest_linked_revision(tmp_path: Path):
    fixture = build_fixture(tmp_path)
    fixture.store.link_revision_to_topic(fixture.old_alpha_revision, fixture.topic)

    articles = fixture.store.dashboard_snapshot()["comparisons"][0]["articles"]

    assert [article["revision_id"] for article in articles] == [fixture.bravo_revision, fixture.alpha_revision]


def test_dashboard_sources_use_latest_check_and_redacted_errors(tmp_path: Path):
    fixture = build_fixture(tmp_path)

    sources = fixture.store.dashboard_snapshot()["sources"]

    assert [item["id"] for item in sources] == [fixture.alpha, fixture.bravo, fixture.charlie]
    assert sources[0] == {
        "id": fixture.alpha, "name": "Alpha Daily", "enabled": True, "status": "ok",
        "checked_at": "2026-09-18T05:00:00Z", "candidates_seen": 3, "error": None,
    }
    assert sources[1] | {"error": None} == {
        "id": fixture.bravo, "name": "Bravo News", "enabled": True, "status": "failed",
        "checked_at": "2026-09-18T05:00:00Z", "candidates_seen": 0, "error": None,
    }
    assert "SECRET-F" not in sources[1]["error"]
    assert "kind=rss" in sources[1]["error"]
    assert sources[2] == {
        "id": fixture.charlie, "name": "Charlie Wire", "enabled": False, "status": "not checked",
        "checked_at": None, "candidates_seen": None, "error": None,
    }


def test_dashboard_reports_latest_run_and_analysis_counts(tmp_path: Path):
    snapshot = build_fixture(tmp_path).store.dashboard_snapshot()

    assert snapshot["last_run"] == {
        "started_at": "2026-09-18T05:00:00Z", "finished_at": "2026-09-18T05:02:00Z", "sources_checked": 2,
        "candidates_seen": 3, "revisions_created": 1, "failures": 1,
    }
    assert snapshot["analysis"] == {
        "pending": 0, "running": 0, "completed": 2, "failed": 1, "skipped": 0, "unanalyzed": 1,
    }


def test_dashboard_never_exposes_secrets_in_any_field(tmp_path: Path):
    fixture = build_fixture(tmp_path)

    snapshot = fixture.store.dashboard_snapshot()
    findings = fixture.store.list_findings(visible_only=False)

    rendered = repr(snapshot) + repr(findings)
    assert all(secret not in rendered for secret in SECRETS)
    urls = url_fields(snapshot) + [item["article_url"] for item in findings]
    urls += [evidence["url"] for item in findings for evidence in item["evidence"]]
    assert len(urls) == 18
    assert all("SECRET" not in url and "@" not in url for url in urls)
    assert "Harbor closed" in rendered


def test_dashboard_limit_bounds_inbox_findings_and_comparisons(tmp_path: Path):
    fixture = build_fixture(tmp_path)

    snapshot = fixture.store.dashboard_snapshot(limit=1)

    assert [item["revision_id"] for item in snapshot["inbox"]] == [fixture.alpha_revision]
    assert [item["id"] for item in snapshot["findings"]] == [fixture.bravo_finding]
    assert len(snapshot["comparisons"]) == 1
    assert len(snapshot["sources"]) == 3
    with pytest.raises(ValueError, match="positive"):
        fixture.store.dashboard_snapshot(0)


def test_empty_dashboard_snapshot(tmp_path: Path):
    store = Store(tmp_path / "empty.db")
    store.initialize()

    assert store.dashboard_snapshot() == {
        "inbox": [], "findings": [], "comparisons": [], "sources": [], "last_run": None,
        "analysis": {status: 0 for status in ANALYSIS_STATUSES} | {"unanalyzed": 0}, "monitoring_paused": False,
    }


def test_dashboard_blanks_unparseable_article_urls_instead_of_failing(tmp_path: Path):
    store = Store(tmp_path / "odd.db")
    store.initialize()
    source_id = store.add_source(Source(None, "Odd", "https://odd.test/feed"))
    save(store, source_id, "https://[broken/story", "Odd story", "Body.", "2026-09-18T03:00:00Z")

    assert store.dashboard_snapshot()["inbox"][0]["url"] == ""
