"""Round-2 storage behaviour: atomic job claims, article-level topic review, stale findings, read models."""

from pathlib import Path
import sqlite3

from gaohe.domain import ArticleCandidate, Claim, Evidence, FetchedArticle, Finding, Source, article_content_hash
from gaohe.storage import ABANDONED_RUN_ERROR, MANUAL_SOURCE_NAME, SCHEMA_VERSION, Store


T0 = "2026-09-18T04:00:00Z"
TEN_MINUTES_LATER = "2026-09-18T04:10:00Z"
TWO_HOURS_LATER = "2026-09-18T06:00:00Z"
TROOPS = "Taipei forum opens with 100 troops."


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "gaohe.db")
    store.initialize()
    return store


def _save(store: Store, name: str, text: str = TROOPS, fetched_at: str = "2026-09-18T03:00:00Z", *, source_id=None) -> int:
    if source_id is None:
        source_id = store.add_source(Source(None, name, f"https://{name}.test/feed"))
    candidate = ArticleCandidate(source_id, f"https://{name}.test/story", "Forum", "2026-09-18T02:00:00Z", fetched_at, {})
    return store.save_fetched_article(FetchedArticle(candidate, text, fetched_at, article_content_hash("Forum", text)))[0]


def _claim(revision_id: int) -> Claim:
    return Claim(None, revision_id, "100 troops", 23, 33, "checkable", "material", "extracted")


def _difference(revision_id: int) -> Finding:
    return Finding(None, revision_id, None, "material_cross_media_difference", "100 troops versus 1000 troops", 23, 33,
                   "resolved", "retrieved", True)


def _peer_evidence(url: str, content_hash: str, rationale: str | None = "The peer reports 1000 troops.") -> Evidence:
    return Evidence(None, None, url, "Forum", "Taipei forum opens with 1000 troops.", "contradicts", "retrieved",
                    "related_article", "2026-09-18T03:00:00Z", "related_revision", None, content_hash, rationale)


def _save_difference(store: Store, revision_id: int, peer_revision_id: int) -> None:
    peer = next(item for item in store.list_recent_revisions() if item.id == peer_revision_id)
    assert store.save_analysis(
        revision_id, (_claim(revision_id),), (_difference(revision_id),),
        ((_peer_evidence(peer.url, peer.content_hash),),), completed_at=T0,
    )


def _count(store: Store, sql: str) -> int:
    with sqlite3.connect(store.path) as connection:
        return connection.execute(sql).fetchone()[0]


# --- finding 0: one claim per job, and a claim that was taken over cannot write ----------------------------------

def test_a_fresh_running_job_cannot_be_claimed_by_a_second_run(tmp_path):
    store = _store(tmp_path)
    revision_id = _save(store, "alpha")

    assert store.mark_analysis_running(revision_id, T0) is True
    assert store.mark_analysis_running(revision_id, TEN_MINUTES_LATER) is False

    status = store.analysis_status(revision_id)
    assert (status["status"], status["attempts"], status["last_error"], status["updated_at"]) == ("running", 0, None, T0)


def test_a_worker_whose_claim_was_taken_over_writes_nothing(tmp_path):
    store = _store(tmp_path)
    revision_id = _save(store, "alpha")
    assert store.mark_analysis_running(revision_id, T0) is True
    assert store.mark_analysis_running(revision_id, TWO_HOURS_LATER) is True  # the first run looked crashed

    assert store.save_analysis(revision_id, (_claim(revision_id),), (), (), completed_at=TWO_HOURS_LATER, claimed_at=T0) is False
    assert store.mark_analysis_failed(revision_id, TWO_HOURS_LATER, "late", claimed_at=T0) is False
    assert store.mark_analysis_skipped(revision_id, TWO_HOURS_LATER, "late", claimed_at=T0) is False
    assert _count(store, "SELECT COUNT(*) FROM claims") == 0

    assert store.save_analysis(
        revision_id, (_claim(revision_id),), (), (), completed_at=TWO_HOURS_LATER, claimed_at=TWO_HOURS_LATER,
    ) is True
    assert store.analysis_status(revision_id)["status"] == "completed"


def test_save_analysis_refuses_an_already_completed_job_instead_of_duplicating_findings(tmp_path):
    store = _store(tmp_path)
    alpha, bravo = _save(store, "alpha"), _save(store, "bravo", "Taipei forum opens with 1000 troops.")
    _save_difference(store, alpha, bravo)

    peer = next(item for item in store.list_recent_revisions() if item.id == bravo)
    again = store.save_analysis(
        alpha, (), (_difference(alpha),), ((_peer_evidence(peer.url, peer.content_hash),),), completed_at=TWO_HOURS_LATER,
    )

    assert again is False
    assert _count(store, "SELECT COUNT(*) FROM findings") == 1
    assert _count(store, "SELECT COUNT(*) FROM evidence") == 1


def test_a_superseded_revision_is_never_claimed(tmp_path):
    store = _store(tmp_path)
    old = _save(store, "alpha")
    source_id = store.list_sources()[0].id
    new = _save(store, "alpha", "Taipei forum opens with 120 troops.", "2026-09-18T05:00:00Z", source_id=source_id)

    assert store.mark_analysis_running(old, TWO_HOURS_LATER) is False
    assert store.mark_analysis_running(new, TWO_HOURS_LATER) is True


def test_unclaimed_skip_or_pending_never_overwrites_another_runs_live_job(tmp_path):
    store = _store(tmp_path)
    revision_id = _save(store, "alpha")
    store.mark_analysis_running(revision_id, T0)

    assert store.mark_analysis_skipped(revision_id, TEN_MINUTES_LATER, "budget") is False
    assert store.mark_analysis_pending(revision_id, TEN_MINUTES_LATER, "stopped") is False
    assert store.analysis_status(revision_id)["status"] == "running"
    assert store.mark_analysis_pending(revision_id, TEN_MINUTES_LATER, "金鑰被拒", claimed_at=T0) is True
    status = store.analysis_status(revision_id)
    assert (status["status"], status["attempts"], status["last_error"]) == ("pending", 0, "金鑰被拒")


# --- finding 2: crashes count like failures and a job never stays running forever ---------------------------------

def test_abandoned_job_without_attempts_left_is_failed_by_the_sweep(tmp_path):
    store = _store(tmp_path)
    revision_id = _save(store, "alpha")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO revision_analysis (revision_id, status, attempts, updated_at) VALUES (?, 'running', 2, ?)",
            (revision_id, T0),
        )

    assert store.fail_abandoned_analyses(TEN_MINUTES_LATER) == 0  # still fresh
    assert store.fail_abandoned_analyses(TWO_HOURS_LATER) == 1

    status = store.analysis_status(revision_id)
    assert (status["status"], status["attempts"], status["last_error"]) == ("failed", 3, ABANDONED_RUN_ERROR)
    assert store.analysis_counts()["running"] == 0


def test_requeue_gives_failed_jobs_fresh_attempts(tmp_path):
    store = _store(tmp_path)
    first, second = _save(store, "alpha"), _save(store, "bravo")
    for revision_id in (first, second):
        store.mark_analysis_failed(revision_id, T0, "金鑰被拒", final_attempts=3)
    assert store.list_pending_revisions(now=TWO_HOURS_LATER) == []

    assert store.requeue_failed_analyses(TWO_HOURS_LATER, [second]) == 1

    assert [item.id for item in store.list_pending_revisions(now=TWO_HOURS_LATER)] == [second]
    assert store.requeue_failed_analyses(TWO_HOURS_LATER) == 1


def test_list_pending_can_be_restricted_to_given_revisions(tmp_path):
    store = _store(tmp_path)
    first, second, third = (_save(store, name) for name in ("alpha", "bravo", "charlie"))

    assert [item.id for item in store.list_pending_revisions(now=T0, revision_ids=[third, first])] == [first, third]
    assert store.list_pending_revisions(now=T0, revision_ids=[]) == []
    assert second not in [item.id for item in store.list_pending_revisions(now=T0, revision_ids=[first])]


def test_llm_calls_can_be_counted_for_one_revision(tmp_path):
    store = _store(tmp_path)
    first, second = _save(store, "alpha"), _save(store, "bravo")
    for revision_id in (first, first, second, None):
        store.record_llm_call(T0, "fake", "model", "analysis", revision_id, "ok")

    assert store.count_llm_calls_since(T0) == 4
    assert store.count_llm_calls_since(T0, revision_id=first) == 2


# --- findings 1, 4, 5: topic decisions hold at the article level ------------------------------------------------

def _new_revision(store: Store, name: str, text: str) -> int:
    source_id = next(source.id for source in store.list_sources() if source.name == name)
    return _save(store, name, text, "2026-09-18T05:00:00Z", source_id=source_id)


def test_dismissal_survives_a_new_revision_of_either_article(tmp_path):
    store = _store(tmp_path)
    alpha, bravo = _save(store, "alpha"), _save(store, "bravo")
    topic_id = store.assign_topic(alpha, bravo, "forum", "high")
    assert store.set_topic_status(topic_id, "dismissed")

    bravo_update = _new_revision(store, "bravo", TROOPS + " One more sentence.")
    alpha_update = _new_revision(store, "alpha", TROOPS.replace(".", "!"))

    assert store.assign_topic(bravo_update, alpha, "forum", "high") == topic_id
    assert store.assign_topic(alpha_update, bravo_update, "forum", "high") == topic_id
    assert store.topic_status(topic_id) == "dismissed"
    assert store.dashboard_snapshot()["comparisons"] == []


def test_a_third_article_cannot_pull_a_dismissed_pair_back_together(tmp_path):
    store = _store(tmp_path)
    r, p, s = _save(store, "rho"), _save(store, "pi"), _save(store, "sigma")
    dismissed = store.assign_topic(r, p, "forum", "high")
    store.set_topic_status(dismissed, "dismissed")

    with_p = store.assign_topic(s, p, "forum", "high")
    with_r = store.assign_topic(s, r, "forum", "high")

    assert with_p != with_r
    assert {item.id for item in store.list_topic_revisions(with_p)} == {s, p}
    assert {item.id for item in store.list_topic_revisions(with_r)} == {s, r}


def test_possible_pairing_is_never_shown_inside_a_confirmed_topic(tmp_path):
    store = _store(tmp_path)
    a, b, c = _save(store, "alpha"), _save(store, "bravo"), _save(store, "charlie")
    store.assign_topic(a, b, "forum", "high")
    store.assign_topic(c, b, "forum?", "possible")

    comparisons = {item["confidence"]: [article["revision_id"] for article in item["articles"]]
                   for item in store.dashboard_snapshot()["comparisons"]}
    assert comparisons == {"high": [a, b], "possible": [b, c]}


def test_confirming_a_possible_topic_makes_it_a_high_confidence_topic(tmp_path):
    store = _store(tmp_path)
    a, b = _save(store, "alpha"), _save(store, "bravo")
    topic_id = store.assign_topic(a, b, "forum", "possible")

    assert store.set_topic_status(topic_id, "active")

    [comparison] = store.dashboard_snapshot()["comparisons"]
    assert (comparison["confidence"], comparison["status"]) == ("high", "active")
    # A later possible signal for a new revision of a confirmed pair joins the confirmed topic.
    b_update = _new_revision(store, "bravo", TROOPS + " Update.")
    assert store.assign_topic(b_update, a, "forum", "possible") == topic_id


# --- finding 9: dismissing a topic withdraws the differences that rested on it ----------------------------------

def test_dismissing_a_topic_withdraws_its_cross_media_differences(tmp_path):
    store = _store(tmp_path)
    alpha, bravo = _save(store, "alpha"), _save(store, "bravo", "Taipei forum opens with 1000 troops.")
    topic_id = store.assign_topic(alpha, bravo, "forum", "high")
    _save_difference(store, alpha, bravo)
    assert [item["id"] for item in store.list_findings()] == [1]

    store.set_topic_status(topic_id, "dismissed")

    assert store.list_findings() == []
    [inbox_alpha] = [item for item in store.dashboard_snapshot()["inbox"] if item["revision_id"] == alpha]
    assert inbox_alpha["annotations"] == () and inbox_alpha["pending_findings"] == 0
    [withdrawn] = store.list_findings(visible_only=False)
    assert (withdrawn["status"], withdrawn["visible"]) == ("dismissed", False)


# --- finding 6: superseded text is never presented as current ---------------------------------------------------

def test_findings_on_superseded_revisions_are_left_out_by_default(tmp_path):
    store = _store(tmp_path)
    old = _save(store, "alpha")
    store.save_analysis(old, (), (Finding(None, old, None, "unsupported_inference", "gap", 0, 6, "resolved", "retrieved", True),),
                        ((),), completed_at=T0)
    _new_revision(store, "alpha", "Corrected text.")

    assert store.list_findings() == []
    assert store.dashboard_snapshot()["findings"] == []
    [superseded] = store.list_findings(current_only=False)
    assert superseded["is_current"] is False


def test_a_peer_correction_hides_differences_assessed_against_its_old_text(tmp_path):
    store = _store(tmp_path)
    alpha, bravo = _save(store, "alpha"), _save(store, "bravo", "Taipei forum opens with 1000 troops.")
    store.assign_topic(alpha, bravo, "forum", "high")
    _save_difference(store, alpha, bravo)

    _new_revision(store, "bravo", TROOPS)  # Bravo corrects 1000 to 100

    assert store.list_findings() == []
    [hidden] = store.list_findings(visible_only=False)
    assert (hidden["status"], hidden["visible"], hidden["revision_id"]) == ("pending", False, alpha)


# --- finding 8: v0.1 unassessed findings are hidden by migration 6 ------------------------------------------------

def test_migration_6_hides_unassessed_findings_and_keeps_assessed_ones(tmp_path):
    store = _store(tmp_path)
    alpha, bravo = _save(store, "alpha"), _save(store, "bravo", "Taipei forum opens with 1000 troops.")
    peer = next(item for item in store.list_recent_revisions() if item.id == bravo)
    store.save_analysis(
        alpha, (), (_difference(alpha), _difference(alpha)),
        ((_peer_evidence(peer.url, peer.content_hash, rationale=None),), (_peer_evidence(peer.url, peer.content_hash),)),
        completed_at=T0,
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version = 5")

    store.initialize()

    assert SCHEMA_VERSION == 6
    visible = {item["id"]: item["visible"] for item in store.list_findings(visible_only=False)}
    assert visible == {1: False, 2: True}


# --- contract C2 read models --------------------------------------------------------------------------------------

def test_manual_checks_are_flagged_in_the_inbox_and_hidden_from_source_health(tmp_path):
    store = _store(tmp_path)
    _save(store, "alpha")
    manual = store.ensure_manual_source()
    checked = _save(store, "manual-check", source_id=manual)
    store.set_monitoring_paused(True, T0)

    snapshot = store.dashboard_snapshot()

    assert [item["name"] for item in snapshot["sources"]] == ["alpha"]
    by_revision = {item["revision_id"]: item for item in snapshot["inbox"]}
    assert (by_revision[checked]["source"], by_revision[checked]["manual"]) == (MANUAL_SOURCE_NAME, True)
    assert by_revision[1]["manual"] is False
    assert snapshot["monitoring_paused"] is True


def test_article_detail_returns_the_card_all_findings_and_currency(tmp_path):
    store = _store(tmp_path)
    old = _save(store, "alpha")
    visible = Finding(None, old, None, "unsupported_inference", "gap", 0, 6, "resolved", "retrieved", True)
    pending = Finding(None, old, None, "factual_contradiction", "check", 7, 12, "pending", "pending", False)
    store.save_analysis(old, (), (visible, pending), ((), ()), completed_at=T0)
    new = _new_revision(store, "alpha", "Corrected text.")

    detail = store.article_detail(old)

    assert detail is not None and detail["is_current"] is False
    assert (detail["revision_id"], detail["article_id"], detail["text"], detail["manual"]) == (old, 1, TROOPS, False)
    assert len(detail["annotations"]) == 1 and detail["pending_findings"] == 1
    assert sorted(item["finding_type"] for item in detail["findings"]) == ["factual_contradiction", "unsupported_inference"]
    assert all(item["is_current"] is False for item in detail["findings"])
    current = store.article_detail(new)
    assert current is not None and current["is_current"] is True and current["findings"] == []
    assert store.article_detail(999) is None


def test_event_times_return_known_publication_times(tmp_path):
    store = _store(tmp_path)
    revision_id = _save(store, "alpha")

    assert store.event_times([revision_id, 999]) == {revision_id: "2026-09-18T02:00:00Z"}
