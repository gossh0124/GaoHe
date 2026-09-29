from email.message import Message
import json
from urllib.error import HTTPError, URLError

import pytest

from gaohe.config import Settings
from gaohe.domain import ArticleRevision, article_content_hash


KEY = "super-secret-key"


def settings(model="gemini-test", key=KEY):
    return Settings(llm_provider="gemini", llm_model=model, llm_api_key=key)


def revision(text="部長表示補助 1,000 萬元。大會邀集近 500 位\n國內外官員。"):
    content_hash = article_content_hash("補助新聞", text)
    return ArticleRevision(5, 2, "https://news.test/a", "補助新聞", text, content_hash, "2026-09-20T00:00:00Z")


def gemini_body(text):
    return json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]}).encode("utf-8")


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.body


class FakeUrlopen:
    """Replays a script of responses/exceptions and records every request."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return Response(step)


class FakeSleep:
    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError("https://generativelanguage.googleapis.com/v1beta/models/m:generateContent", code, "error", headers, None)


EMPTY = '{"claims":[],"candidates":[]}'


def provider_with(*script, sleep=None):
    from gaohe.providers import GeminiAnalysisProvider

    urlopen = FakeUrlopen(*script)
    fake_sleep = sleep or FakeSleep()
    return GeminiAnalysisProvider(settings(), urlopen_request=urlopen, sleep=fake_sleep), urlopen, fake_sleep


# --- response parsing -------------------------------------------------------------------------


def parse(response, item=None):
    from gaohe.providers import GeminiAnalysisProvider

    return GeminiAnalysisProvider(settings(), request=lambda *_args: response).analyze(item or revision(), ())


def claim_json(quote, kind="checkable", materiality="material", **extra):
    return {"quote": quote, "kind": kind, "materiality": materiality, **extra}


def candidate_json(claim_index, summary, finding_type="factual_contradiction", **extra):
    return {"claim_index": claim_index, "finding_type": finding_type, "summary": summary, "materiality": "material", **extra}


def response_json(claims=(), candidates=()):
    return json.dumps({"claims": list(claims), "candidates": list(candidates)}, ensure_ascii=False)


def test_claims_are_anchored_by_quote_and_candidates_take_their_claims_span():
    item = revision()
    response = response_json(
        [
            claim_json("部長表示補助 1,000 萬元", "attributed_statement"),
            claim_json("邀集近500位 國內外官員", "descriptive", "ordinary"),
        ],
        [candidate_json(0, "核對金額", query="補助 1,000 萬元")],
    )

    result = parse(response, item)

    first, second = result.claims
    assert (first.text, first.start, first.end) == ("部長表示補助 1,000 萬元", 0, len("部長表示補助 1,000 萬元"))
    assert second.text == "邀集近 500 位\n國內外官員"
    assert item.text[second.start:second.end] == second.text
    assert all(claim.id is None and claim.revision_id == item.id for claim in result.claims)
    assert all(claim.extraction_status == "extracted" for claim in result.claims)
    (candidate,) = result.candidates
    assert (candidate.claim_id, candidate.start, candidate.end) == (None, first.start, first.end)
    assert candidate.query == "補助 1,000 萬元"
    assert result.rejected_claims == 0


def test_unlocatable_ambiguous_and_malformed_claims_are_dropped_and_counted():
    item = revision("補助 100 萬元。另一項補助 100 萬元。部長出席。")
    response = response_json(
        [
            claim_json("部長出席"),
            claim_json("補助 100 萬元"),  # ambiguous: appears twice
            claim_json("補助 100 萬元", occurrence=2),
            claim_json("部長缺席"),  # not in the article
            claim_json(""),
            claim_json("部長出席", kind=3),
            {"kind": "checkable", "materiality": "material"},
            "not an object",
        ],
        [
            candidate_json(1, "ambiguous claim"),
            candidate_json(2, "second occurrence"),
            candidate_json(99, "out of range"),
            candidate_json(True, "bool index"),
            candidate_json("0", "string index"),
            candidate_json(0, "   "),
            candidate_json(0, "bad query", query=5),
            candidate_json(0, "no type", finding_type=None),
            ["not", "an", "object"],
        ],
    )

    result = parse(response, item)

    second = item.text.index("補助 100 萬元", 1)
    assert [(claim.text, claim.start) for claim in result.claims] == [
        ("部長出席", item.text.index("部長出席")),
        ("補助 100 萬元", second),
    ]
    assert [(candidate.summary, candidate.start) for candidate in result.candidates] == [("second occurrence", second)]
    assert result.rejected_claims == 6


def test_legacy_offsets_are_accepted_only_when_they_point_at_the_quote():
    item = revision("A checkable statement. Another statement.")
    exact = {"text": "Another statement.", "start": 23, "end": 41, "kind": "checkable", "materiality": "material"}
    miscounted = claim_json("Another statement.", start=20, end=30)
    bool_offsets = claim_json("Another statement.", start=False, end=True)

    for claim_item in (exact, miscounted, bool_offsets):
        (claim,) = parse(response_json([claim_item]), item).claims
        assert (claim.start, claim.end, claim.text) == (23, 41, "Another statement.")


def test_legacy_offsets_disambiguate_a_repeated_quote():
    item = revision("Twice. Twice.")

    (claim,) = parse(response_json([claim_json("Twice.", start=7, end=13)]), item).claims

    assert (claim.start, claim.end) == (7, 13)


def test_legacy_candidate_offsets_survive_parsing_for_extract_claims_to_validate():
    response = json.dumps({"claims": [], "candidates": [
        {"claim_id": None, "finding_type": "factual_contradiction", "summary": "legacy", "start": 0, "end": 4, "materiality": "material"},
        {"claim_id": True, "finding_type": "factual_contradiction", "summary": "bool id", "start": 0, "end": 4, "materiality": "material"},
        {"finding_type": "factual_contradiction", "summary": "no span", "materiality": "material"},
    ]})

    result = parse(response)

    assert [(candidate.summary, candidate.start, candidate.end) for candidate in result.candidates] == [("legacy", 0, 4)]


def test_quotes_are_located_only_in_the_text_the_model_saw():
    from gaohe.providers import MAX_ANALYSIS_CHARS

    head = "Unique quote. "
    item = revision(head + "x" * (MAX_ANALYSIS_CHARS - len(head)) + " Unique quote.")
    response = json.dumps({"claims": [{"quote": "Unique quote.", "kind": "checkable", "materiality": "material"}], "candidates": []})

    (claim,) = parse(response, item).claims

    assert (claim.start, claim.end) == (0, len("Unique quote."))


def test_long_candidate_summary_is_bounded():
    from gaohe.providers import MAX_CANDIDATE_SUMMARY_CHARS

    response = json.dumps({
        "claims": [{"quote": "部長表示補助 1,000 萬元", "kind": "checkable", "materiality": "material"}],
        "candidates": [{"claim_index": 0, "finding_type": "factual_contradiction", "summary": "s" * 5_000, "materiality": "material"}],
    }, ensure_ascii=False)

    assert len(parse(response).candidates[0].summary) == MAX_CANDIDATE_SUMMARY_CHARS


@pytest.mark.parametrize("response", ["[]", '"text"', "null", '{"claims":[]}', '{"claims":{},"candidates":[]}', "{", ""])
def test_malformed_top_level_response_is_invalid_without_leaking_the_body(response):
    from gaohe.providers import GeminiAnalysisProvider

    provider = GeminiAnalysisProvider(settings(), request=lambda *_args: response or " ")
    with pytest.raises(ValueError) as error:
        provider.analyze(revision(), ())

    assert str(error.value) in {"Gemini returned invalid analysis response", "Gemini returned no analysis content"}
    assert error.value.__context__ is None
    assert error.value.__cause__ is None


def test_invalid_json_error_does_not_carry_the_raw_response():
    from gaohe.providers import GeminiAnalysisProvider

    raw = '{"claims": [ Bearer leaked-token'
    provider = GeminiAnalysisProvider(settings(), request=lambda *_args: raw)
    with pytest.raises(ValueError, match="Gemini returned invalid analysis response") as error:
        provider.analyze(revision(), ())

    assert error.value.__context__ is None
    assert "leaked-token" not in repr(error.value)


def test_provider_metadata_and_prompt_versions_are_exposed():
    from gaohe.providers import ANALYSIS_PROMPT_VERSION, ASSESSMENT_PROMPT_VERSION, GeminiAnalysisProvider
    import re

    provider = GeminiAnalysisProvider(settings(model="gemini-2.5-flash"))

    assert provider.provider_name == "gemini"
    assert provider.model == "gemini-2.5-flash"
    assert provider.prompt_version == ANALYSIS_PROMPT_VERSION
    for version in (ANALYSIS_PROMPT_VERSION, ASSESSMENT_PROMPT_VERSION):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", version)


@pytest.mark.parametrize(("model", "key"), [("", KEY), ("gemini", "")])
def test_missing_model_or_key_fails_before_any_request(model, key):
    from gaohe.providers import GeminiAnalysisProvider

    def request(*_args):
        pytest.fail("no request without configuration")

    with pytest.raises(ValueError, match="requires LLM_MODEL and LLM_API_KEY"):
        GeminiAnalysisProvider(settings(model=model, key=key), request=request).analyze(revision(), ())


# --- request payload --------------------------------------------------------------------------


def test_post_sends_structured_output_request_with_key_only_in_header():
    provider, urlopen, _sleep = provider_with(gemini_body(EMPTY))
    related = ArticleRevision(6, 3, "https://other.test/a", "Other", "Other text", "hash", "2026-09-20T00:00:00Z")

    provider.analyze(revision(), (related,))

    ((request, timeout),) = urlopen.requests
    assert timeout == 60
    assert request.full_url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent"
    assert request.get_method() == "POST"
    assert dict(request.header_items())["X-goog-api-key"] == KEY
    assert KEY not in request.full_url
    body = request.data.decode("utf-8")
    assert KEY not in body
    payload = json.loads(body)
    assert set(payload) == {"systemInstruction", "contents", "generationConfig"}
    config = payload["generationConfig"]
    assert config["temperature"] == 0
    assert config["responseMimeType"] == "application/json"
    schema = config["responseSchema"]
    claim_properties = schema["properties"]["claims"]["items"]["properties"]
    candidate_properties = schema["properties"]["candidates"]["items"]["properties"]
    assert claim_properties["kind"]["enum"] == ["attributed_statement", "checkable", "descriptive", "inference", "opinion"]
    assert claim_properties["materiality"]["enum"] == ["material", "ordinary"]
    assert candidate_properties["finding_type"]["enum"] == [
        "factual_contradiction", "material_cross_media_difference", "unsupported_inference",
    ]
    assert candidate_properties["materiality"]["enum"] == ["material", "ordinary"]
    assert schema["required"] == ["claims", "candidates"]
    document = json.loads(payload["contents"][0]["parts"][0]["text"])
    assert payload["contents"][0]["role"] == "user"
    assert document["revision"] == {"id": 5, "title": "補助新聞", "text": revision().text}
    assert document["related"] == [{"id": 6, "title": "Other", "text": "Other text"}]
    assert "補助新聞" in body  # CJK is sent as-is rather than \\u-escaped


def test_post_strips_a_models_prefix_and_escapes_the_model_name():
    from gaohe.providers import GeminiAnalysisProvider

    urlopen = FakeUrlopen(gemini_body(EMPTY), gemini_body(EMPTY))
    GeminiAnalysisProvider(settings(model="models/gemini-x"), urlopen_request=urlopen).analyze(revision(), ())
    GeminiAnalysisProvider(settings(model="gemini?key=1"), urlopen_request=urlopen).analyze(revision(), ())

    assert urlopen.requests[0][0].full_url.endswith("/models/gemini-x:generateContent")
    assert "?" not in urlopen.requests[1][0].full_url


def test_post_joins_text_parts_and_skips_thought_parts():
    body = json.dumps({"candidates": [{"content": {"parts": [
        {"text": "internal reasoning", "thought": True},
        {"text": '{"claims":[],'},
        {"text": '"candidates":[]}'},
    ]}}]}).encode("utf-8")
    provider, _urlopen, _sleep = provider_with(body)

    assert provider.analyze(revision(), ()).claims == ()


def test_post_rejects_an_oversized_response():
    from gaohe.providers import MAX_PAGE_BYTES

    provider, _urlopen, _sleep = provider_with(b"x" * (MAX_PAGE_BYTES + 1))

    with pytest.raises(ValueError, match="^Gemini analysis request failed$"):
        provider.analyze(revision(), ())


@pytest.mark.parametrize(
    "response",
    [
        {"candidates": []},
        {"promptFeedback": {"blockReason": "SAFETY"}},
        {"candidates": [{"finishReason": "SAFETY"}]},
        {"candidates": [{"content": {"role": "model"}, "finishReason": "SAFETY"}]},
        {"candidates": [{"content": {"parts": []}}]},
        {"candidates": [{"content": {"parts": [{"text": "   "}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "only thinking", "thought": True}]}}]},
        {"candidates": [{"content": {"parts": [{"inlineData": {}}]}}]},
        [],
    ],
)
def test_response_without_content_is_reported_as_no_analysis_content(response):
    provider, urlopen, sleep = provider_with(json.dumps(response).encode("utf-8"))

    with pytest.raises(ValueError) as error:
        provider.analyze(revision(), ())

    assert str(error.value) == "Gemini returned no analysis content"
    assert error.value.__context__ is None
    assert len(urlopen.requests) == 1
    assert sleep.delays == []


def test_empty_text_from_the_request_seam_is_no_content():
    from gaohe.providers import GeminiAnalysisProvider

    with pytest.raises(ValueError, match="^Gemini returned no analysis content$"):
        GeminiAnalysisProvider(settings(), request=lambda *_args: "  ").analyze(revision(), ())


# --- bounded retry ----------------------------------------------------------------------------


def test_retries_a_429_then_succeeds_with_injected_backoff():
    provider, urlopen, sleep = provider_with(http_error(429), gemini_body(EMPTY))

    result = provider.analyze(revision(), ())

    assert result.claims == ()
    assert len(urlopen.requests) == 2
    assert sleep.delays == [1.0]


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_gives_up_after_two_retries_on_retryable_statuses(code):
    provider, urlopen, sleep = provider_with(http_error(code), http_error(code), http_error(code))

    with pytest.raises(ValueError) as error:
        provider.analyze(revision(), ())

    assert str(error.value) == f"Gemini analysis request failed (HTTP {code})"
    assert len(urlopen.requests) == 3
    assert sleep.delays == [1.0, 2.0]
    assert error.value.__context__ is None and error.value.__cause__ is None


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_does_not_retry_client_errors(code):
    provider, urlopen, sleep = provider_with(http_error(code))

    with pytest.raises(ValueError, match=rf"^Gemini analysis request failed \(HTTP {code}\)$"):
        provider.analyze(revision(), ())

    assert len(urlopen.requests) == 1
    assert sleep.delays == []


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (URLError("connection refused"), "network error"),
        (URLError(TimeoutError("timed out")), "timeout"),
        (TimeoutError("read timed out"), "timeout"),
    ],
)
def test_retries_network_errors_and_timeouts(error, reason):
    provider, urlopen, sleep = provider_with(error, error, gemini_body(EMPTY))

    assert provider.analyze(revision(), ()).claims == ()
    assert len(urlopen.requests) == 3
    assert sleep.delays == [1.0, 2.0]

    exhausted, _urlopen, _sleep = provider_with(error, error, error)
    with pytest.raises(ValueError, match=rf"^Gemini analysis request failed \({reason}\)$"):
        exhausted.analyze(revision(), ())


def test_retry_after_is_honoured_but_capped():
    provider, _urlopen, sleep = provider_with(http_error(429, "7"), http_error(503, "3600"), gemini_body(EMPTY))

    provider.analyze(revision(), ())

    assert sleep.delays == [7.0, 30.0]


def test_non_numeric_retry_after_falls_back_to_backoff():
    provider, _urlopen, sleep = provider_with(http_error(429, "Wed, 21 Oct 2026 07:28:00 GMT"), gemini_body(EMPTY))

    provider.analyze(revision(), ())

    assert sleep.delays == [1.0]


def test_unexpected_errors_are_not_retried_and_never_leak_the_key():
    from gaohe.providers import GeminiAnalysisProvider

    calls = []
    sleep = FakeSleep()

    def request(_model, _payload, api_key):
        calls.append(api_key)
        raise RuntimeError(f"x-goog-api-key: {api_key}")

    with pytest.raises(ValueError, match="^Gemini analysis request failed$") as error:
        GeminiAnalysisProvider(settings(), request=request, sleep=sleep).analyze(revision(), ())

    assert calls == [KEY]
    assert sleep.delays == []
    assert KEY not in repr(error.value)
    assert error.value.__context__ is None


def test_retry_also_wraps_the_injected_request_seam():
    from gaohe.providers import GeminiAnalysisProvider

    script = [http_error(503), EMPTY]
    sleep = FakeSleep()

    def request(*_args):
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    assert GeminiAnalysisProvider(settings(), request=request, sleep=sleep).analyze(revision(), ()).claims == ()
    assert sleep.delays == [1.0]
