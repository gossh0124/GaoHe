from dataclasses import dataclass

import pytest

from gaohe.domain import ArticleRevision, Claim, article_content_hash
from gaohe.providers import AnalysisResult, FindingCandidate


def revision(text: str) -> ArticleRevision:
    return ArticleRevision(7, 3, "https://news.test/article", "Article", text, article_content_hash("Article", text), "2026-09-20T00:00:00Z")


def claim(item: ArticleRevision, text: str, kind: str = "checkable", materiality: str = "material", *, claim_id: int | None = None) -> Claim:
    start = item.text.index(text)
    return Claim(claim_id, item.id, text, start, start + len(text), kind, materiality, "extracted")


def candidate(item: ArticleRevision, text: str, finding_type: str = "factual_contradiction", materiality: str = "material", *, claim_id: int | None = None, summary: str = "Conflicts with the cited record") -> FindingCandidate:
    start = item.text.index(text)
    return FindingCandidate(claim_id, finding_type, summary, start, start + len(text), materiality, None)


@dataclass
class FakeAnalysisProvider:
    result: AnalysisResult
    calls: int = 0

    def analyze(self, item: ArticleRevision, related: tuple[ArticleRevision, ...]) -> AnalysisResult:
        assert related == ()
        self.calls += 1
        return self.result


def test_extract_claims_keeps_nearly_500_officials_as_background_without_candidate():
    from gaohe.analysis import extract_claims

    item = revision("邀集近 500 位國內外官員與專家。")
    ordinary = claim(item, "邀集近 500 位國內外官員與專家", kind="descriptive", materiality="ordinary")
    provider = FakeAnalysisProvider(AnalysisResult(item.id, (ordinary,), (candidate(item, ordinary.text),)))

    result = extract_claims(item, provider)

    assert provider.calls == 1
    assert result.claims == (ordinary,)
    assert result.candidates == ()


def test_extract_claims_keeps_only_an_explicit_material_contradiction():
    from gaohe.analysis import extract_claims

    item = revision("該計畫在 2026 年提供 1,000 萬元補助。")
    stated = claim(item, "該計畫在 2026 年提供 1,000 萬元補助")
    valid = candidate(item, stated.text)
    provider = FakeAnalysisProvider(AnalysisResult(item.id, (stated,), (valid,)))

    assert extract_claims(item, provider).candidates == (valid,)


@pytest.mark.parametrize(
    ("kind", "materiality", "finding_type", "expected"),
    [
        ("attributed_statement", "ordinary", "factual_contradiction", False),
        ("attributed_statement", "material", "factual_contradiction", True),
        ("inference", "material", "unsupported_inference", True),
        ("opinion", "ordinary", "unsupported_inference", False),
        ("descriptive", "ordinary", "material_cross_media_difference", False),
    ],
)
def test_material_gate_distinguishes_attribution_inference_opinion_and_description(kind, materiality, finding_type, expected):
    from gaohe.analysis import is_material_candidate

    item = revision("Minister says the policy will work.")
    stated = claim(item, "Minister says the policy will work", kind, materiality)

    assert is_material_candidate(stated, candidate(item, stated.text, finding_type), item) is expected


def test_extract_claims_rejects_invalid_candidate_shapes_but_keeps_valid_claims():
    from gaohe.analysis import extract_claims

    item = revision("The record says 100 units.")
    stated = claim(item, "The record says 100 units")
    invalid = (
        FindingCandidate(None, "factual_contradiction", "Bad span", 0, 1, "material", None),
        FindingCandidate(None, "factual_contradiction", "   ", stated.start, stated.end, "material", None),
        FindingCandidate(None, "opinion", "Wrong type", stated.start, stated.end, "material", None),
        FindingCandidate(None, "factual_contradiction", "No claim", 4, 10, "material", None),
    )

    result = extract_claims(item, FakeAnalysisProvider(AnalysisResult(item.id, (stated,), invalid)))

    assert result.claims == (stated,)
    assert result.candidates == ()


@pytest.mark.parametrize(
    "value",
    ["factual_contradiction", "material_cross_media_difference", "unsupported_inference"],
)
def test_allowed_finding_type_has_an_exact_allowlist(value):
    from gaohe.analysis import allowed_finding_type

    assert allowed_finding_type(value) is True


@pytest.mark.parametrize("value", ["Factual_contradiction", "opinion", "", " factual_contradiction", None])
def test_allowed_finding_type_rejects_everything_else(value):
    from gaohe.analysis import allowed_finding_type

    assert allowed_finding_type(value) is False


def test_extract_claims_rejects_provider_revision_or_claim_text_mismatch():
    from gaohe.analysis import extract_claims

    item = revision("The verified wording.")
    mismatch = Claim(None, item.id, "Different wording", 0, len("The verified wording"), "checkable", "material", "extracted")

    with pytest.raises(ValueError, match="revision_id"):
        extract_claims(item, FakeAnalysisProvider(AnalysisResult(8, (), ())))
    with pytest.raises(ValueError, match="claim text"):
        extract_claims(item, FakeAnalysisProvider(AnalysisResult(item.id, (mismatch,), ())))


def test_material_gate_requires_exact_claim_association_and_never_makes_no_evidence_visible():
    from gaohe.analysis import is_material_candidate

    item = revision("The verified wording.")
    stated = claim(item, "The verified wording", claim_id=11)
    mismatched_id = candidate(item, stated.text, claim_id=12)
    keyword_only = FindingCandidate(None, "factual_contradiction", "Suspicious tone", 0, len("The verified wording"), "material", None)

    assert is_material_candidate(stated, mismatched_id, item) is False
    assert is_material_candidate(stated, keyword_only, item) is True
    assert not hasattr(keyword_only, "visible")


def test_extract_claims_requires_a_unique_span_match_without_claim_id():
    from gaohe.analysis import extract_claims

    item = revision("The verified wording.")
    first = claim(item, "The verified wording", claim_id=11)
    duplicate = claim(item, "The verified wording", claim_id=12)
    unlinked = candidate(item, first.text)
    linked = candidate(item, first.text, claim_id=11)

    result = extract_claims(item, FakeAnalysisProvider(AnalysisResult(item.id, (first, duplicate), (unlinked, linked))))

    assert result.candidates == (linked,)
