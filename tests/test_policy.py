from dataclasses import replace

import pytest

from gaohe.domain import Evidence
from gaohe.policy import FINDING_TYPES, FULL_TEXT_SOURCE_KINDS, is_visible, usable


PEER = "https://peer.test/story"


def ev(
    relation="contradicts",
    source_kind="direct",
    *,
    url="https://record.test/a",
    status="retrieved",
    excerpt="Official record: 200 units.",
    rationale="The record says 200, not 100.",
):
    return Evidence(
        None, None, url, "Record", excerpt, relation, status, source_kind,
        "2026-09-20T01:00:00Z", "official", None, "hash", rationale,
    )


def peer(relation="contradicts", **overrides):
    return ev(relation, "related_article", url=overrides.pop("url", PEER), **overrides)


def test_constants_match_the_v0_spec():
    assert FINDING_TYPES == {"factual_contradiction", "material_cross_media_difference", "unsupported_inference"}
    assert FULL_TEXT_SOURCE_KINDS == {"direct", "firecrawl"}


# --- usable -----------------------------------------------------------------------------------


def test_usable_requires_every_condition():
    assert usable(ev()) is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "retrieval_failed"},
        {"status": "insufficient_scope"},
        {"status": "pending"},
        {"url": "https://user:secret@record.test/a"},
        {"url": "ftp://record.test/a"},
        {"url": ""},
        {"excerpt": ""},
        {"excerpt": "   \n"},
        {"rationale": None},
        {"rationale": ""},
        {"rationale": "  "},
    ],
)
def test_usable_rejects_each_missing_condition(overrides):
    assert usable(replace(ev(), **overrides)) is False


# --- factual_contradiction --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ([ev("contradicts", "direct")], True),
        ([ev("contradicts", "firecrawl")], True),
        ([ev("contradicts", "search")], False),  # spec 2.3: a search lead is never evidence
        ([ev("contradicts", "related_article")], False),  # peers are cross-media, not factual, evidence
        ([ev("contradicts", "direct", rationale=None)], False),  # unassessed
        ([ev("contradicts", "direct", status="retrieval_failed")], False),  # spec 2.4
        ([ev("contradicts", "direct", excerpt="")], False),
        ([ev("context", "direct")], False),
        ([ev("supports", "direct")], False),
        ([], False),  # spec 2.4: no evidence is not proof of error
        # Conflicting sources stay pending for human reading, whichever kind supports.
        ([ev("contradicts", "direct"), ev("supports", "direct", url="https://other.test/b")], False),
        ([ev("contradicts", "direct"), ev("supports", "firecrawl", url="https://other.test/b")], False),
        ([ev("contradicts", "direct"), ev("supports", "search", url="https://other.test/b")], False),
        # An unassessed or failed "supports" does not count against the contradiction.
        ([ev("contradicts", "direct"), ev("supports", "direct", rationale=None)], True),
        ([ev("contradicts", "direct"), ev("supports", "direct", status="retrieval_failed")], True),
        ([ev("contradicts", "search"), ev("contradicts", "firecrawl"), ev("context", "direct")], True),
    ],
)
def test_factual_contradiction_truth_table(evidence, expected):
    assert is_visible("factual_contradiction", evidence, ()) is expected


# --- unsupported_inference --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ([ev("context", "direct")], True),
        ([ev("contradicts", "direct")], True),
        ([ev("context", "firecrawl")], True),
        ([ev("context", "search")], False),
        ([ev("context", "related_article")], False),
        ([ev("context", "direct", rationale=None)], False),  # retrieval alone is not an assessment
        ([ev("context", "direct", status="insufficient_scope")], False),
        ([ev("supports", "direct")], False),
        ([ev("context", "direct"), ev("supports", "direct", url="https://other.test/b")], False),
        ([ev("contradicts", "firecrawl"), ev("supports", "search", url="https://other.test/b")], False),
        ([ev("context", "direct"), ev("supports", "direct", rationale="")], True),
        ([], False),
    ],
)
def test_unsupported_inference_truth_table(evidence, expected):
    assert is_visible("unsupported_inference", evidence, ()) is expected


# --- material_cross_media_difference -------------------------------------------------------------


@pytest.mark.parametrize(
    ("evidence", "related_urls", "expected"),
    [
        ([peer()], [PEER], True),
        ([peer()], [], False),  # a single article never has cross-media differences (spec 8)
        ([peer()], ["https://someone-else.test/story"], False),
        ([peer(url="https://PEER.test/story#part-2")], ["https://peer.test/story"], True),
        ([peer()], ["HTTPS://Peer.Test/story#top"], True),
        ([peer()], [None, 42, "not a url", PEER], True),
        ([peer()], ["https://user:pw@peer.test/story"], False),
        ([peer("context")], [PEER], False),
        ([peer("supports")], [PEER], False),
        ([peer(rationale=None)], [PEER], False),
        ([peer(excerpt="")], [PEER], False),
        ([peer(status="retrieval_failed")], [PEER], False),
        ([ev("contradicts", "direct", url=PEER)], [PEER], False),  # must come from the peer revision itself
        ([ev("contradicts", "search", url=PEER)], [PEER], False),
    ],
)
def test_material_cross_media_difference_truth_table(evidence, related_urls, expected):
    assert is_visible("material_cross_media_difference", evidence, related_urls) is expected


# --- everything else ---------------------------------------------------------------------------


@pytest.mark.parametrize("finding_type", ["opinion", "important_omission", "", "Factual_contradiction", None])
def test_other_finding_types_are_never_visible(finding_type):
    strong = [ev("contradicts", "direct"), ev("context", "firecrawl"), peer()]

    assert is_visible(finding_type, strong, [PEER]) is False


def test_is_visible_accepts_one_shot_iterables():
    assert is_visible("factual_contradiction", iter([ev()]), iter(())) is True
    assert is_visible("material_cross_media_difference", (item for item in [peer()]), (url for url in [PEER])) is True


def test_resolve_finding_delegates_to_policy(monkeypatch):
    from gaohe import analysis
    from gaohe.domain import FindingCandidate

    calls = []

    def fake_is_visible(finding_type, evidence, related_urls):
        calls.append((finding_type, tuple(evidence), tuple(related_urls)))
        return True

    monkeypatch.setattr(analysis, "is_visible", fake_is_visible)
    proposal = FindingCandidate(None, "factual_contradiction", "Check", 0, 4, "material", None, 7)

    finding = analysis.resolve_finding(proposal, [ev()], ())

    assert (finding.visible, finding.status, finding.evidence_status) == (True, "resolved", "retrieved")
    assert calls == [("factual_contradiction", (ev(),), ())]
