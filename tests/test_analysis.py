import pytest

from conftest import FakeGemini, FakeTransport, html_response, revision
from gaohe.analysis import MIN_EVIDENCE_QUOTE_CHARS, analyze_revision, assess_evidence, is_visible, retrieve_evidence
from gaohe.config import Settings
from gaohe.domain import Evidence, EvidenceAssessment, FindingCandidate, SearchHit
from gaohe.providers import DirectPageFetcher, GeminiAnalysisProvider, GeminiEvidenceAssessor, GeminiSearchProvider

SETTINGS = Settings(llm_provider="gemini", llm_model="m", llm_api_key="k")
CANDIDATE = FindingCandidate("factual_contradiction", "確認開放日期", 9, 19, "外籍旅客 入境 開放日期", 1)


class Search:
    def __init__(self, *urls):
        self.urls, self.queries = urls, []

    def search(self, query, limit=5):
        self.queries.append(query)
        return [SearchHit(url, "lead title", "test-search") for url in self.urls]


def evidence(url="https://gov.example/a", relation="contradicts", rationale="官方公告日期不同", status="retrieved", excerpt="公告：十二月起開放"):
    return Evidence(url, "官方", excerpt, relation, status, "2026-10-01T00:00:00Z", rationale=rationale)


# --- retrieval ---------------------------------------------------------------------------------

def test_retrieve_fetches_full_text_and_a_failed_lead_is_never_evidence():
    fetcher = DirectPageFetcher(FakeTransport({
        "https://gov.example/a": html_response("https://gov.example/a", "公告", "十二月起開放入境"),
        "https://blocked.example/b": OSError("refused"),
    }))
    items = retrieve_evidence(CANDIDATE, revision(), Search("https://gov.example/a", "https://blocked.example/b"), fetcher)
    assert [(item.url, item.status, item.excerpt) for item in items] == [
        ("https://gov.example/a", "retrieved", "十二月起開放入境"),
        ("https://blocked.example/b", "retrieval_failed", ""),  # the lead's title/snippet never becomes excerpt
    ]
    assert all(item.rationale is None for item in items)


@pytest.mark.parametrize("query", ["api_key=abc 開放", "行政院今天宣布", ""])
def test_retrieve_never_sends_secrets_or_the_article_itself(query):
    search = Search("https://gov.example/a")
    candidate = FindingCandidate("factual_contradiction", query or " ", 0, 5, query or None, 1)
    assert retrieve_evidence(candidate, revision(), search, DirectPageFetcher(FakeTransport({}))) == []
    assert search.queries == []


# --- assessment --------------------------------------------------------------------------------

class Assessor:
    def __init__(self, answer):
        self.answer = answer

    def assess(self, candidate, claim_text, revision, evidence):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.parametrize("answer, relation, rationale, status", [
    (EvidenceAssessment("contradicts", "日期不同", "十二月起開放"), "contradicts", "日期不同", "retrieved"),
    (EvidenceAssessment("contradicts", "日期不同", "十月起開放"), "context", None, "retrieved"),  # quote not in excerpt
    (EvidenceAssessment("contradicts", "日期不同", "開放"), "context", None, "retrieved"),  # too short to ground anything
    (EvidenceAssessment("contradicts", "  ", "十二月起開放"), "context", None, "retrieved"),  # no rationale
    (EvidenceAssessment("irrelevant", "無關", ""), "context", "無關", "insufficient_scope"),
    (ValueError("boom"), "context", None, "retrieved"),
])
def test_assessment_is_applied_only_when_it_holds_up(answer, relation, rationale, status):
    item = evidence(relation="context", rationale=None, excerpt="公告：自十二月起開放外籍旅客")
    [result] = assess_evidence(CANDIDATE, "下月起開放", revision(), [item], Assessor(answer))
    assert (result.relation, result.rationale, result.status) == (relation, rationale, status)
    assert MIN_EVIDENCE_QUOTE_CHARS > len("開放")


def test_failed_evidence_is_never_sent_to_the_assessor():
    failed = evidence(status="retrieval_failed", excerpt="", rationale=None, relation="context")
    assert assess_evidence(CANDIDATE, "x", revision(), [failed], Assessor(ValueError())) == [failed]


# --- visibility policy -------------------------------------------------------------------------

def test_factual_contradiction_needs_assessed_full_text_and_no_support():
    assert is_visible("factual_contradiction", [evidence()])
    assert not is_visible("factual_contradiction", [evidence(rationale=None)])
    assert not is_visible("factual_contradiction", [evidence(status="retrieval_failed")])
    assert not is_visible("factual_contradiction", [evidence(excerpt="")])
    assert not is_visible("factual_contradiction", [evidence(url="ftp://x/a")])
    assert not is_visible("factual_contradiction", [evidence(), evidence("https://b.example/", relation="supports")])
    assert not is_visible("factual_contradiction", [])


def test_unsupported_inference_needs_two_independent_sites_and_no_support():
    one = evidence("https://a.example/1", relation="context")
    same_site = evidence("https://a.example/2", relation="context")
    other_site = evidence("https://b.example/1", relation="context")
    assert not is_visible("unsupported_inference", [one])
    assert not is_visible("unsupported_inference", [one, same_site])
    assert is_visible("unsupported_inference", [one, other_site])
    assert not is_visible("unsupported_inference", [one, other_site, evidence("https://c.example/", relation="supports")])
    assert not is_visible("material_cross_media_difference", [one, other_site])


# --- one revision end to end -------------------------------------------------------------------

def test_analyze_revision_produces_one_visible_contradiction_and_one_pending_inference():
    gemini = FakeGemini(
        analysis={
            "claims": [
                {"quote": "開放外籍旅客入境觀光", "kind": "checkable", "materiality": "material"},
                {"quote": "政策將使觀光收入增加三成", "kind": "inference", "materiality": "material"},
            ],
            "candidates": [
                {"claim_index": 0, "finding_type": "factual_contradiction", "summary": "確認開放時間", "query": "外籍旅客 入境 開放時間"},
                {"claim_index": 1, "finding_type": "unsupported_inference", "summary": "增幅缺乏依據", "query": "觀光收入 預估"},
            ],
        },
        assessment=lambda payload: (
            {"evidence_quote": "明年三月起才開放", "rationale": "官方公告時間不同", "relation": "contradicts"}
            if "factual_contradiction" in payload["contents"][0]["parts"][0]["text"]
            else {"evidence_quote": "", "rationale": "無關", "relation": "irrelevant"}
        ),
        search_uris=["https://gov.example/notice"],
    )
    fetcher = DirectPageFetcher(FakeTransport({
        "https://gov.example/notice": html_response("https://gov.example/notice", "公告", "外籍旅客明年三月起才開放入境觀光。"),
    }))
    extracted, findings, batches = analyze_revision(
        revision(), GeminiAnalysisProvider(SETTINGS, gemini), GeminiSearchProvider(SETTINGS, gemini), fetcher,
        GeminiEvidenceAssessor(SETTINGS, gemini),
    )
    assert len(extracted.claims) == 2
    assert [(f.finding_type, f.visible, f.evidence_status) for f in findings] == [
        ("factual_contradiction", True, "retrieved"),
        ("unsupported_inference", False, "insufficient_scope"),
    ]
    assert batches[0][0].rationale == "官方公告時間不同"
