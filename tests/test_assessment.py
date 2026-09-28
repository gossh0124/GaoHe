from dataclasses import dataclass, field
import json
from urllib.error import HTTPError

import pytest

from gaohe.config import Settings
from gaohe.domain import ArticleRevision, Evidence, EvidenceAssessment, FindingCandidate, article_content_hash


KEY = "assessor-secret-key"
EXCERPT = "官方紀錄：本次論壇共 200\n名官員出席。"


def settings(provider="gemini", model="gemini-test", key=KEY):
    return Settings(llm_provider=provider, llm_model=model, llm_api_key=key)


def revision():
    text = "報導指出，本次論壇共 2,000 名官員出席。"
    content_hash = article_content_hash("論壇報導", text)
    return ArticleRevision(9, 4, "https://news.test/forum", "論壇報導", text, content_hash, "2026-09-20T00:00:00Z")


def candidate(finding_type="factual_contradiction"):
    item = revision()
    text = "本次論壇共 2,000 名官員出席"
    start = item.text.index(text)
    return FindingCandidate(None, finding_type, "核對出席人數", start, start + len(text), "material", "論壇 出席 官員", item.id)


def evidence(excerpt=EXCERPT, status="retrieved", relation="context", rationale=None, source_kind="direct"):
    return Evidence(
        None, None, "https://record.test/forum", "官方紀錄", excerpt, relation, status, source_kind,
        "2026-09-20T01:00:00Z", "official", "2026-09-19T00:00:00Z", "hash", rationale,
    )


@dataclass
class FakeAssessor:
    answer: object = None
    error: Exception | None = None
    calls: list = field(default_factory=list)

    def assess(self, claim_text, item, record, finding_type):
        self.calls.append((claim_text, item.id, record.url, finding_type))
        if self.error is not None:
            raise self.error
        return self.answer


def assess(answer=None, *, batch=None, error=None, finding_type="factual_contradiction"):
    from gaohe.analysis import assess_evidence

    assessor = FakeAssessor(answer, error)
    result = assess_evidence(candidate(finding_type), "本次論壇共 2,000 名官員出席", revision(), batch or [evidence()], assessor)
    return result, assessor


# --- assess_evidence ----------------------------------------------------------------------------


@pytest.mark.parametrize("relation", ["supports", "contradicts", "context"])
def test_valid_assessment_sets_relation_and_rationale(relation):
    result, assessor = assess(EvidenceAssessment(relation, "  紀錄顯示 200 名官員出席。 ", "共 200\n名官員出席"))

    (record,) = result
    assert (record.relation, record.status, record.rationale) == (relation, "retrieved", "紀錄顯示 200 名官員出席。")
    assert (record.url, record.excerpt, record.source_kind, record.provider) == (
        "https://record.test/forum", EXCERPT, "direct", "official",
    )
    assert assessor.calls == [("本次論壇共 2,000 名官員出席", 9, "https://record.test/forum", "factual_contradiction")]


def test_irrelevant_assessment_becomes_insufficient_scope_context_with_rationale():
    (record,), _assessor = assess(EvidenceAssessment("irrelevant", "頁面談的是另一場活動。", ""))

    assert (record.relation, record.status, record.rationale) == ("context", "insufficient_scope", "頁面談的是另一場活動。")


def test_irrelevant_assessment_with_a_quote_must_still_quote_verbatim():
    (kept,), _ = assess(EvidenceAssessment("irrelevant", "Different event.", "官方紀錄"))
    (fabricated,), _ = assess(EvidenceAssessment("irrelevant", "Different event.", "invented words"))

    assert (kept.status, kept.rationale) == ("insufficient_scope", "Different event.")
    assert (fabricated.status, fabricated.relation, fabricated.rationale) == ("retrieved", "context", None)


def test_whitespace_insensitive_evidence_quote_is_accepted():
    (record,), _ = assess(EvidenceAssessment("contradicts", "人數不同。", "共200名官員出席"))

    assert record.relation == "contradicts"


@pytest.mark.parametrize(
    "answer",
    [
        EvidenceAssessment("contradicts", "人數不同。", "共 300 名官員出席"),  # not in the excerpt
        EvidenceAssessment("contradicts", "人數不同。", ""),  # a contradiction must be quotable
        EvidenceAssessment("supports", "一致。", "   "),
        EvidenceAssessment("refutes", "人數不同。", "共 200"),  # not an assessment relation
        EvidenceAssessment("contradicts", "   ", "共 200"),  # empty rationale
        EvidenceAssessment("contradicts", None, "共 200"),
        EvidenceAssessment("contradicts", "人數不同。", None),
        EvidenceAssessment(None, "人數不同。", "共 200"),
        {"relation": "contradicts", "rationale": "x", "evidence_quote": "共 200"},  # not an EvidenceAssessment
        None,  # NullEvidenceAssessor-style "no assessment"
    ],
)
def test_invalid_assessments_leave_the_evidence_unassessed(answer):
    (record,), _ = assess(answer)

    assert (record.relation, record.rationale, record.status) == ("context", None, "retrieved")
    assert record.excerpt == EXCERPT


def test_assessor_errors_leave_the_evidence_unassessed():
    (record,), assessor = assess(error=RuntimeError(f"Authorization: Bearer {KEY}"))

    assert (record.relation, record.rationale) == ("context", None)
    assert len(assessor.calls) == 1


def test_only_retrieved_evidence_with_an_excerpt_is_assessed():
    failed = evidence(excerpt="", status="retrieval_failed", source_kind="search")
    scoped = evidence(excerpt="", status="insufficient_scope", source_kind="search")
    blank = evidence(excerpt="   ")
    readable = evidence()

    result, assessor = assess(EvidenceAssessment("contradicts", "人數不同。", "共 200"), batch=[failed, scoped, blank, readable])

    assert result[:3] == [failed, scoped, blank]
    assert result[3].relation == "contradicts"
    assert len(assessor.calls) == 1


def test_missing_assessor_resets_any_prior_relation_to_unassessed_context():
    from gaohe.analysis import assess_evidence

    stale = evidence(relation="contradicts", rationale=None)

    (record,) = assess_evidence(candidate(), "claim", revision(), [stale], None)

    assert (record.relation, record.rationale) == ("context", None)


def test_rationale_is_redacted_and_bounded():
    from gaohe.analysis import MAX_RATIONALE_CHARS

    rationale = f"token={KEY} " + "理" * 3_000
    (record,), _ = assess(EvidenceAssessment("contradicts", rationale, "共 200"))

    assert KEY not in record.rationale
    assert "token=[redacted]" in record.rationale
    assert len(record.rationale) <= MAX_RATIONALE_CHARS


def test_null_evidence_assessor_assesses_nothing():
    from gaohe.analysis import assess_evidence
    from gaohe.providers import NullEvidenceAssessor

    assessor = NullEvidenceAssessor()
    assert assessor.assess("claim", revision(), evidence(), "factual_contradiction") is None
    (record,) = assess_evidence(candidate(), "claim", revision(), [evidence()], assessor)
    assert (record.relation, record.rationale) == ("context", None)


# --- builders -----------------------------------------------------------------------------------


def test_build_evidence_assessor_supports_gemini_only():
    from gaohe.providers import GeminiEvidenceAssessor, build_evidence_assessor

    assessor = build_evidence_assessor(settings(model="gemini-2.5-flash"))
    assert isinstance(assessor, GeminiEvidenceAssessor)
    assert (assessor.provider_name, assessor.model) == ("gemini", "gemini-2.5-flash")
    for provider in ("", "openai", "none"):
        with pytest.raises(ValueError, match=f"Unsupported LLM_PROVIDER: {provider}"):
            build_evidence_assessor(settings(provider=provider))


# --- GeminiEvidenceAssessor ---------------------------------------------------------------------


def gemini_assessor(response=None, *, request=None, sleep=None):
    from gaohe.providers import GeminiEvidenceAssessor

    captured = []

    def default_request(model, payload, api_key):
        captured.append((model, payload, api_key))
        return response

    assessor = GeminiEvidenceAssessor(settings(), request=request or default_request, sleep=sleep or (lambda _seconds: None))
    return assessor, captured


def test_gemini_assessor_sends_structured_request_and_parses_the_answer():
    from gaohe.providers import ASSESSMENT_PROMPT_VERSION

    answer = {"evidence_quote": "共 200\n名官員出席", "rationale": "官方紀錄為 200 名。", "relation": "contradicts"}
    assessor, captured = gemini_assessor(json.dumps(answer, ensure_ascii=False))

    result = assessor.assess("本次論壇共 2,000 名官員出席", revision(), evidence(), "factual_contradiction")

    assert result == EvidenceAssessment("contradicts", "官方紀錄為 200 名。", "共 200\n名官員出席")
    assert assessor.prompt_version == ASSESSMENT_PROMPT_VERSION
    ((model, payload, api_key),) = captured
    assert (model, api_key) == ("gemini-test", KEY)
    assert KEY not in json.dumps(payload, ensure_ascii=False)
    config = payload["generationConfig"]
    assert (config["temperature"], config["responseMimeType"]) == (0, "application/json")
    schema = config["responseSchema"]
    assert schema["properties"]["relation"]["enum"] == ["context", "contradicts", "irrelevant", "supports"]
    assert schema["required"] == ["evidence_quote", "rationale", "relation"]
    instructions = payload["systemInstruction"]["parts"][0]["text"]
    assert "verbatim" in instructions
    assert "Missing information is never a contradiction" in instructions
    assert "at most 500 characters" in instructions
    document = json.loads(payload["contents"][0]["parts"][0]["text"])
    assert document["finding_type"] == "factual_contradiction"
    assert document["claim"] == "本次論壇共 2,000 名官員出席"
    assert document["evidence"]["excerpt"] == EXCERPT
    assert document["evidence"]["url"] == "https://record.test/forum"
    assert "本次論壇共 2,000 名官員出席" in document["article"]["context"]


def test_gemini_assessor_sends_only_a_window_of_a_long_article():
    from gaohe.providers import GeminiEvidenceAssessor

    text = "前" * 5_000 + "關鍵主張" + "後" * 5_000
    item = ArticleRevision(9, 4, "https://news.test/a", "T", text, article_content_hash("T", text), "2026-09-20T00:00:00Z")
    captured = []
    answer = '{"evidence_quote":"","rationale":"無關。","relation":"irrelevant"}'
    assessor = GeminiEvidenceAssessor(settings(), request=lambda _m, payload, _k: captured.append(payload) or answer)

    assessor.assess("關鍵主張", item, evidence(), "unsupported_inference")

    context = json.loads(captured[0]["contents"][0]["parts"][0]["text"])["article"]["context"]
    assert "關鍵主張" in context
    assert len(context) < 2_000


def test_gemini_assessor_bounds_the_rationale_to_500_characters():
    from gaohe.providers import MAX_ASSESSOR_RATIONALE_CHARS

    assessor, _ = gemini_assessor(json.dumps({"evidence_quote": "共 200", "rationale": "r" * 900, "relation": "supports"}))

    result = assessor.assess("claim", revision(), evidence(), "factual_contradiction")

    assert len(result.rationale) == MAX_ASSESSOR_RATIONALE_CHARS == 500


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        "[]",
        json.dumps({"evidence_quote": "q", "rationale": "r", "relation": "refutes"}),
        json.dumps({"evidence_quote": "q", "rationale": "", "relation": "supports"}),
        json.dumps({"evidence_quote": None, "rationale": "r", "relation": "supports"}),
        json.dumps({"rationale": "r", "relation": "supports"}),
    ],
)
def test_gemini_assessor_rejects_invalid_answers(response):
    assessor, _ = gemini_assessor(response)

    with pytest.raises(ValueError, match="^Gemini returned invalid assessment response$") as error:
        assessor.assess("claim", revision(), evidence(), "factual_contradiction")
    assert error.value.__context__ is None


def test_gemini_assessor_reports_failures_and_empty_answers_without_the_key():
    def failing(*_args):
        raise RuntimeError(f"key={KEY}")

    failed, _ = gemini_assessor(request=failing)
    empty, _ = gemini_assessor("")
    with pytest.raises(ValueError, match="^Gemini assessment request failed$") as error:
        failed.assess("claim", revision(), evidence(), "factual_contradiction")
    assert KEY not in repr(error.value)
    assert error.value.__context__ is None
    with pytest.raises(ValueError, match="^Gemini returned no assessment content$"):
        empty.assess("claim", revision(), evidence(), "factual_contradiction")


def test_gemini_assessor_requires_configuration():
    from gaohe.providers import GeminiEvidenceAssessor

    with pytest.raises(ValueError, match="requires LLM_MODEL and LLM_API_KEY"):
        GeminiEvidenceAssessor(settings(key="")).assess("claim", revision(), evidence(), "factual_contradiction")


def test_gemini_assessor_shares_the_bounded_retry_plumbing():
    answer = json.dumps({"evidence_quote": "共 200", "rationale": "不同。", "relation": "contradicts"})
    script = [HTTPError("https://example.test", 429, "busy", None, None), answer]
    delays = []

    def request(*_args):
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    assessor, _ = gemini_assessor(request=request, sleep=delays.append)

    assert assessor.assess("claim", revision(), evidence(), "factual_contradiction").relation == "contradicts"
    assert delays == [1.0]


def test_gemini_assessor_output_flows_through_assess_evidence_validation():
    from gaohe.analysis import assess_evidence

    fabricated = json.dumps({"evidence_quote": "共 3,000 名官員", "rationale": "不同。", "relation": "contradicts"})
    grounded = json.dumps({"evidence_quote": "共 200 名官員出席", "rationale": "不同。", "relation": "contradicts"})

    for response, relation in ((fabricated, "context"), (grounded, "contradicts")):
        assessor, _ = gemini_assessor(response)
        (record,) = assess_evidence(candidate(), "本次論壇共 2,000 名官員出席", revision(), [evidence()], assessor)
        assert record.relation == relation
