from pathlib import Path
import sqlite3

import pytest

from gaohe.domain import ArticleCandidate, FetchedArticle, Finding, Source, TopicGroup, article_content_hash
from gaohe.storage import Store


def store_with_revisions(tmp_path: Path, count: int = 4) -> tuple[Store, list[int]]:
    store = Store(tmp_path / "topics.db")
    store.initialize()
    revision_ids = []
    for index in range(count):
        source_id = store.add_source(Source(None, f"Media {index}", f"https://media-{index}.test/feed"))
        item = ArticleCandidate(source_id, f"https://media-{index}.test/story", "Harbor closure", None, "2026-09-18T02:00:00Z", {})
        text = f"Port authority closed the harbor, report {index}."
        revision_ids.append(
            store.save_fetched_article(FetchedArticle(item, text, "2026-09-18T03:00:00Z", article_content_hash(item.title, text)))[0]
        )
    return store, revision_ids


def topics(store: Store) -> list[tuple]:
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT id, label, confidence, status FROM topics ORDER BY id").fetchall()
    connection.close()
    return rows


def links(store: Store) -> list[tuple]:
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT topic_id, revision_id FROM topic_articles ORDER BY topic_id, revision_id").fetchall()
    connection.close()
    return rows


def test_topic_id_for_revision_ignores_dismissed_topics_and_prefers_active(tmp_path: Path):
    store, (first, *_) = store_with_revisions(tmp_path, 1)
    assert store.topic_id_for_revision(first) is None

    dismissed = store.save_topic(TopicGroup(None, "Dismissed", "high", "dismissed"))
    possible = store.save_topic(TopicGroup(None, "Possible", "possible", "possible"))
    active = store.save_topic(TopicGroup(None, "Active", "high", "active"))
    store.link_revision_to_topic(first, dismissed)
    assert store.topic_id_for_revision(first) is None
    store.link_revision_to_topic(first, possible)
    assert store.topic_id_for_revision(first) == possible
    store.link_revision_to_topic(first, active)
    assert store.topic_id_for_revision(first) == active


@pytest.mark.parametrize(("confidence", "status"), [("high", "active"), ("possible", "possible"), ("low", "possible")])
def test_assign_topic_creates_a_topic_linking_both_revisions(tmp_path: Path, confidence: str, status: str):
    store, (first, second, *_) = store_with_revisions(tmp_path)

    topic_id = store.assign_topic(second, first, "Harbor closure", confidence)

    assert topics(store) == [(topic_id, "Harbor closure", confidence, status)]
    assert links(store) == [(topic_id, first), (topic_id, second)]
    assert store.topic_id_for_revision(first) == store.topic_id_for_revision(second) == topic_id


def test_a_high_signal_upgrades_only_a_possible_topic_of_exactly_that_pair(tmp_path: Path):
    store, (first, second, third, _) = store_with_revisions(tmp_path)
    possible = store.assign_topic(second, first, "Harbor closure", "possible")

    # second was only possibly related to first, so a high first-third pair must not vouch for it.
    confirmed = store.assign_topic(third, first, "Different label", "high")

    assert confirmed != possible
    assert topics(store) == [(possible, "Harbor closure", "possible", "possible"), (confirmed, "Different label", "high", "active")]
    assert links(store) == [(possible, first), (possible, second), (confirmed, first), (confirmed, third)]
    assert store.assign_topic(second, first, "Harbor closure again", "high") == possible
    assert topics(store)[0] == (possible, "Harbor closure again", "high", "active")


def test_assign_topic_never_downgrades_or_widens_a_confirmed_topic(tmp_path: Path):
    store, (first, second, third, _) = store_with_revisions(tmp_path)
    topic_id = store.assign_topic(second, first, "Harbor closure", "high")

    possible = store.assign_topic(third, first, "Harbor closure", "possible")
    assert possible != topic_id
    assert store.assign_topic(third, second, "Harbor closure", "low") == possible
    assert store.assign_topic(first, second, "Harbor closure", "possible") == topic_id

    assert topics(store) == [(topic_id, "Harbor closure", "high", "active"), (possible, "Harbor closure", "possible", "possible")]
    assert [link for link in links(store) if link[0] == topic_id] == [(topic_id, first), (topic_id, second)]


def test_assign_topic_uses_the_revision_topic_when_the_peer_has_none(tmp_path: Path):
    store, (first, second, third, _) = store_with_revisions(tmp_path)
    topic_id = store.assign_topic(second, first, "Harbor closure", "possible")

    assert store.assign_topic(first, third, "Harbor closure", "possible") == topic_id
    assert links(store) == [(topic_id, first), (topic_id, second), (topic_id, third)]
    assert topics(store)[0][2:] == ("possible", "possible")


def test_assign_topic_is_idempotent_for_the_same_pair(tmp_path: Path):
    store, (first, second, *_) = store_with_revisions(tmp_path)

    topic_id = store.assign_topic(second, first, "Harbor closure", "high")

    assert store.assign_topic(second, first, "Harbor closure", "high") == topic_id
    assert store.assign_topic(first, second, "Harbor closure", "high") == topic_id
    assert len(topics(store)) == 1
    assert links(store) == [(topic_id, first), (topic_id, second)]


def test_assign_topic_respects_a_reviewer_dismissal_of_the_same_pair(tmp_path: Path):
    store, (first, second, third, _) = store_with_revisions(tmp_path)
    topic_id = store.assign_topic(second, first, "Harbor closure", "possible")
    assert store.set_topic_status(topic_id, "dismissed") is True

    assert store.assign_topic(second, first, "Harbor closure", "high") == topic_id
    assert topics(store) == [(topic_id, "Harbor closure", "possible", "dismissed")]

    fresh_topic = store.assign_topic(third, first, "Harbor closure", "high")
    assert fresh_topic != topic_id
    assert topics(store)[-1] == (fresh_topic, "Harbor closure", "high", "active")
    assert (fresh_topic, first) in links(store) and (fresh_topic, third) in links(store)


def test_assign_topic_redacts_and_bounds_labels(tmp_path: Path):
    store, (first, second, *_) = store_with_revisions(tmp_path)

    topic_id = store.assign_topic(second, first, "Harbor api_key=AIzaSECRET " + "x" * 700, "high")

    label = topics(store)[0][1]
    assert topics(store)[0][0] == topic_id
    assert "AIzaSECRET" not in label
    assert label.startswith("Harbor api_key=[redacted]")
    assert len(label) <= 500


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (("self", "Harbor", "high"), "own topic peer"),
        (("unknown", "Harbor", "high"), "unknown revision"),
        (("peer", "   ", "high"), "label"),
        (("peer", "Harbor", "certain"), "confidence"),
    ],
)
def test_assign_topic_rejects_invalid_requests_without_writing(tmp_path: Path, arguments: tuple, error: str):
    store, (first, second, *_) = store_with_revisions(tmp_path)
    peer = {"self": second, "unknown": 999, "peer": first}[arguments[0]]

    with pytest.raises(ValueError, match=error):
        store.assign_topic(second, peer, arguments[1], arguments[2])

    assert topics(store) == []
    assert links(store) == []


def test_set_topic_status_validates_status_and_reports_unknown_topics(tmp_path: Path):
    store, (first, second, *_) = store_with_revisions(tmp_path)
    topic_id = store.assign_topic(second, first, "Harbor closure", "possible")

    assert store.set_topic_status(topic_id, "active") is True
    assert store.set_topic_status(999, "active") is False
    with pytest.raises(ValueError, match="topic status"):
        store.set_topic_status(topic_id, "merged")
    assert topics(store)[0][3] == "active"


def finding_store(tmp_path: Path) -> tuple[Store, int, int]:
    store, (revision_id, *_) = store_with_revisions(tmp_path, 1)
    finding_id = store.save_finding(
        Finding(None, revision_id, None, "factual_contradiction", "Closure date differs", 0, 14, "resolved", "retrieved", True)
    )
    return store, revision_id, finding_id


def review_row(store: Store, finding_id: int) -> tuple:
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT review_status, reviewed_at, review_note, status, visible FROM findings WHERE id = ?", (finding_id,)
        ).fetchone()
    connection.close()
    return row


def test_new_findings_start_unreviewed(tmp_path: Path):
    store, _, finding_id = finding_store(tmp_path)

    assert review_row(store, finding_id) == ("unreviewed", None, None, "resolved", 1)


def test_review_finding_records_decision_without_touching_machine_state(tmp_path: Path):
    store, _, finding_id = finding_store(tmp_path)

    assert store.review_finding(finding_id, "dismissed", "2026-09-18T09:00:00+00:00", "Source was a satire page") is True

    assert review_row(store, finding_id) == ("dismissed", "2026-09-18T09:00:00Z", "Source was a satire page", "resolved", 1)
    assert store.review_finding(finding_id, "confirmed", "2026-09-18T10:00:00Z") is True
    assert review_row(store, finding_id)[:3] == ("confirmed", "2026-09-18T10:00:00Z", None)


def test_review_note_is_redacted_bounded_and_blank_notes_are_dropped(tmp_path: Path):
    store, _, finding_id = finding_store(tmp_path)

    store.review_finding(finding_id, "confirmed", "2026-09-18T09:00:00Z", "Cookie: session=abc\nchecked " + "y" * 800)
    note = review_row(store, finding_id)[2]
    assert "session=abc" not in note
    assert len(note) <= 500

    store.review_finding(finding_id, "confirmed", "2026-09-18T09:00:00Z", "  \n ")
    assert review_row(store, finding_id)[2] is None


def test_review_finding_validates_input_and_reports_unknown_findings(tmp_path: Path):
    store, _, finding_id = finding_store(tmp_path)

    assert store.review_finding(999, "confirmed", "2026-09-18T09:00:00Z") is False
    with pytest.raises(ValueError, match="review_status"):
        store.review_finding(finding_id, "approved", "2026-09-18T09:00:00Z")
    with pytest.raises(ValueError, match="UTC"):
        store.review_finding(finding_id, "confirmed", "2026-09-18T09:00:00")
    assert review_row(store, finding_id)[:3] == ("unreviewed", None, None)


def test_save_finding_rejects_unknown_review_status(tmp_path: Path):
    store, revision_id, _ = finding_store(tmp_path)

    with pytest.raises(ValueError, match="review_status"):
        store.save_finding(Finding(None, revision_id, None, "factual_contradiction", "x", 0, 1, "pending", "pending", False, "maybe"))
