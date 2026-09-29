"""The analysis queue: each pending revision runs as one isolated, budgeted job.

Per revision: group it with current peer revisions -> extract claims -> retrieve
and assess evidence for each provider candidate -> assess same-topic differences
against the peer text -> resolve findings -> save. Any stage error marks only
that revision failed (the store retries it up to max_attempts); reaching the
daily LLM call limit or a temporary AI service problem defers the revision as
skipped without using an attempt. A provider error that would fail every further
call the same way (rejected key, exhausted quota, unknown model, missing package,
incomplete setup) stops the whole run and hands the revision back as pending, so
nothing is marked failed because of the user's setup. Every request sent to a
provider is written to the LLM ledger; prompts and keys never are.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TypeVar

from .analysis import assess_evidence, extract_claims, resolve_finding, retrieve_evidence
from .domain import (
    AnalysisResult,
    ArticleRevision,
    Claim,
    Evidence,
    EvidenceAssessment,
    Finding,
    FindingCandidate,
    SearchHit,
    TopicGroup,
)
from .errors import ProviderError
from .providers import (
    ANALYSIS_PROMPT_VERSION,
    AnalysisProvider,
    EvidenceAssessor,
    EvidenceSearchProvider,
    PageFetcher,
)
from .safety import MAX_EVIDENCE_EXCERPT_CHARS, redact_text, redact_url, safe_error
from .storage import Store
from .topics import compare_topic, group_revision


CONTEXT_POOL_LIMIT = 100
# Same-topic peers passed to the analysis provider and compared per revision, and cross-media
# candidates assessed per revision: a story carried by many outlets must not use the day's budget.
MAX_RELATED_PEERS = 10
MAX_TOPIC_CANDIDATES = 5
COUNT_KEYS = (
    "claims", "candidates", "visible_findings", "pending", "retrieval_failures",
    "rejected_claims", "analyzed", "failed", "skipped",
)
# stopped is 1 when a run-stopping provider error halted the run; stop_code is that error's code.
SUMMARY_KEYS = COUNT_KEYS + ("stopped", "stop_code")
BUDGET_SKIP_REASON = "已達今日 AI 呼叫上限（DAILY_LLM_CALL_LIMIT），這篇會在之後的執行中自動分析。"
OVER_BUDGET_REASON = (
    "這篇文章需要的 AI 呼叫次數超過每日上限（DAILY_LLM_CALL_LIMIT），因此不再自動重試；"
    "提高上限後可重新查核。"
)
# Pending revisions grouped per pass over the peer pool. Two cached texts per revision plus
# the current peer's two stay inside gaohe.topics' 64-entry text cache, so each text is
# normalized once per pass instead of once per pair.
_GROUPING_CHUNK = 24
_MAX_RELATED_TITLE_CHARS = 500

Clock = Callable[[], datetime]
Summary = dict[str, int | str]
_Result = TypeVar("_Result")


class BudgetExhausted(Exception):
    """The daily LLM call limit was reached before a provider call."""


def utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def local_day_start(moment: datetime) -> str:
    """Return, as UTC ISO, the start of moment's local calendar day.

    An aware moment's own offset is its local time (Taiwan: +08:00); a naive one
    is read as this computer's local time.
    """
    local = moment if moment.tzinfo is not None else moment.astimezone()
    return utc_iso(local.replace(hour=0, minute=0, second=0, microsecond=0))


def empty_summary() -> Summary:
    summary: Summary = dict.fromkeys(COUNT_KEYS, 0)
    summary.update(stopped=0, stop_code="")
    return summary


def _clock(now: datetime | None) -> Clock:
    if now is None:
        return lambda: datetime.now().astimezone()
    if not isinstance(now, datetime):
        raise ValueError("now must be a datetime")
    fixed = now if now.tzinfo is not None else now.astimezone()
    return lambda: fixed


def _label(provider: object, name: str) -> str | None:
    value = getattr(provider, name, None)
    return value if isinstance(value, str) and value else None


def _counter(provider: object) -> int | None:
    value = getattr(provider, "requests_sent", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _chars(values: Iterable[object]) -> int:
    return sum(len(value) for value in values if isinstance(value, str))


def _analysis_output_chars(result: object) -> int:
    """Approximate answer size: the text fields of the structured result."""
    claims = getattr(result, "claims", ())
    candidates = getattr(result, "candidates", ())
    return _chars(getattr(claim, "text", None) for claim in claims) + _chars(
        value for candidate in candidates for value in (getattr(candidate, "summary", None), getattr(candidate, "query", None))
    )


def _assessment_output_chars(result: object) -> int:
    return _chars((getattr(result, "rationale", None), getattr(result, "evidence_quote", None)))


def _search_output_chars(hits: object) -> int:
    return _chars(value for hit in hits or () for value in (getattr(hit, "title", None), getattr(hit, "snippet", None)))


class _Meter:
    """Checks the daily budget before, and writes the LLM ledger after, every provider call.

    The ledger gets one row per HTTP request the adapter actually sent (its requests_sent
    counter, retries included; one request when an adapter has no counter), so the daily
    limit and `gaohe status` count what the user pays for. assess_evidence() and
    retrieve_evidence() treat any exception as "not assessed" / "nothing found", so an
    exception that must reach the pipeline (budget reached, ledger write failed, or a
    run-stopping or temporary provider error) is also kept in `interrupted` and re-raised
    by the pipeline once those helpers return.
    """

    def __init__(self, store: Store, clock: Clock, daily_limit: int | None) -> None:
        self._store = store
        self._clock = clock
        self._limit = daily_limit
        self.interrupted: Exception | None = None

    def exhausted(self) -> bool:
        if self._limit is None:
            return False
        return self._store.count_llm_calls_since(local_day_start(self._clock())) >= self._limit

    def used_by(self, revision_id: int) -> int:
        return self._store.count_llm_calls_since(local_day_start(self._clock()), revision_id=revision_id)

    def reset(self) -> None:
        self.interrupted = None

    def raise_if_interrupted(self) -> None:
        if self.interrupted is not None:
            raise self.interrupted

    def call(
        self,
        provider: object,
        purpose: str,
        revision_id: int,
        input_chars: int,
        run: Callable[[], _Result],
        output_chars: Callable[[_Result], int],
    ) -> _Result:
        self.raise_if_interrupted()
        if self.exhausted():
            self.interrupted = BudgetExhausted(BUDGET_SKIP_REASON)
            raise self.interrupted
        before = _counter(provider)
        try:
            result = run()
        except Exception as error:
            self._record(provider, purpose, revision_id, "failed", input_chars, 0, self._sent(provider, before))
            if isinstance(error, ProviderError) and (error.stops_run or error.transient):
                self.interrupted = error
            raise
        self._record(provider, purpose, revision_id, "ok", input_chars, output_chars(result), self._sent(provider, before))
        return result

    @staticmethod
    def _sent(provider: object, before: int | None) -> int:
        after = _counter(provider)
        if before is None or after is None or after < before:
            return 1
        return after - before

    def _record(
        self, provider: object, purpose: str, revision_id: int, status: str, input_chars: int, output_chars: int, sent: int
    ) -> None:
        # Retried requests carried the input too; only the last one produced the answer.
        rows = [("retry", input_chars, 0)] * max(0, sent - 1) + ([(status, input_chars, output_chars)] if sent else [])
        try:
            for row_status, row_input, row_output in rows:
                self._store.record_llm_call(
                    utc_iso(self._clock()), _label(provider, "provider_name"), _label(provider, "model"),
                    purpose, revision_id, row_status, row_input, row_output,
                )
        except Exception as error:
            self.interrupted = error
            raise


class _MeteredAnalysis:
    def __init__(self, inner: AnalysisProvider, meter: _Meter) -> None:
        self._inner = inner
        self._meter = meter

    def analyze(self, revision: ArticleRevision, related: Sequence[ArticleRevision]) -> AnalysisResult:
        size = _chars((revision.title, revision.text)) + _chars(value for peer in related for value in (peer.title, peer.text))
        return self._meter.call(
            self._inner, "analysis", revision.id, size,
            lambda: self._inner.analyze(revision, related), _analysis_output_chars,
        )


class _MeteredAssessor:
    def __init__(self, inner: EvidenceAssessor, meter: _Meter) -> None:
        self._inner = inner
        self._meter = meter

    def assess(
        self, claim_text: str, revision: ArticleRevision, evidence: Evidence, finding_type: str
    ) -> EvidenceAssessment | None:
        size = _chars((claim_text, evidence.title, evidence.excerpt))
        return self._meter.call(
            self._inner, "assessment", revision.id, size,
            lambda: self._inner.assess(claim_text, revision, evidence, finding_type), _assessment_output_chars,
        )


class _MeteredSearch:
    """Web search through an AI provider costs requests too, so it is budgeted and ledgered per revision."""

    def __init__(self, inner: EvidenceSearchProvider, meter: _Meter, revision_id: int) -> None:
        self._inner = inner
        self._meter = meter
        self._revision_id = revision_id

    def search(self, query: str, limit: int = 5) -> Sequence[SearchHit]:
        return self._meter.call(
            self._inner, "search", self._revision_id, len(query),
            lambda: self._inner.search(query, limit), _search_output_chars,
        )


def _metered_assessor(assessor: EvidenceAssessor | None, meter: _Meter) -> EvidenceAssessor | None:
    # The null assessor makes no LLM call, so it neither spends budget nor writes the ledger.
    if assessor is None or getattr(assessor, "provider_name", None) == "none":
        return assessor
    return _MeteredAssessor(assessor, meter)


def _metered_search(search: EvidenceSearchProvider, meter: _Meter, revision_id: int) -> EvidenceSearchProvider:
    # Only adapters that send requests count them; the null search and local fakes cost nothing.
    return _MeteredSearch(search, meter, revision_id) if _counter(search) is not None else search


# --- same-topic grouping --------------------------------------------------------------------------

_Signals = dict[int, list[tuple[ArticleRevision, TopicGroup]]]


def _peer_signals(
    revisions: Sequence[ArticleRevision], peers: Sequence[ArticleRevision], event_times: Mapping[int, str]
) -> _Signals:
    """Group each revision with each peer on its own; peer-major so each text is normalized once."""
    signals: _Signals = {revision.id: [] for revision in revisions}
    for peer in peers:
        for revision in revisions:
            if peer.id == revision.id:
                continue
            group = group_revision(revision, (peer,), event_times=event_times)
            if group is not None and group.confidence in {"high", "possible"}:
                signals[revision.id].append((peer, group))
    return signals


def _chunk_signals(
    revisions: Sequence[ArticleRevision], peers: Sequence[ArticleRevision], event_times: Mapping[int, str]
) -> _Signals:
    try:
        return _peer_signals(revisions, peers, event_times)
    except Exception:
        # Each revision of this chunk then groups itself inside its own job, isolating the bad text.
        return {}


def _related(
    store: Store, revision: ArticleRevision, signals: Sequence[tuple[ArticleRevision, TopicGroup]]
) -> tuple[ArticleRevision, ...]:
    """Persist every grouping signal; return the peers whose topic with this revision is confirmed.

    A topic is confirmed when the machine signal was high or a reviewer confirmed it; a
    dismissal (which holds for the two articles) keeps the peer out. High signals come first
    and at most MAX_RELATED_PEERS peers are returned.
    """
    high: list[ArticleRevision] = []
    confirmed: list[ArticleRevision] = []
    for peer, group in signals:
        topic_id = store.assign_topic(revision.id, peer.id, group.label, group.confidence)
        if store.topic_status(topic_id) == "active":
            (high if group.confidence == "high" else confirmed).append(peer)
    return tuple(high + confirmed)[:MAX_RELATED_PEERS]


# --- one revision ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Outcome:
    claims: tuple[Claim, ...]
    findings: tuple[Finding, ...]
    evidence: tuple[tuple[Evidence, ...], ...]
    rejected_claims: int


def _claim_text(candidate: FindingCandidate, claims: Sequence[Claim], revision: ArticleRevision) -> str:
    for claim in claims:
        if (claim.start, claim.end) == (candidate.start, candidate.end):
            return claim.text
    return revision.text[candidate.start:candidate.end]


def _related_evidence(peer: ArticleRevision) -> Evidence:
    """A same-topic peer's text as unassessed evidence; only an assessor can give it a relation."""
    return Evidence(
        None, None, redact_url(peer.url), redact_text(peer.title, _MAX_RELATED_TITLE_CHARS),
        redact_text(peer.text, MAX_EVIDENCE_EXCERPT_CHARS), "context", "retrieved",
        "related_article", peer.fetched_at, "related_revision", None, peer.content_hash,
    )


def _topic_candidates(
    revision: ArticleRevision, related: Sequence[ArticleRevision], event_times: Mapping[int, str]
) -> Iterable[tuple[ArticleRevision, FindingCandidate]]:
    """At most MAX_TOPIC_CANDIDATES differences, one per span of this revision, in peer order."""
    spans: set[tuple[int, int]] = set()
    for peer in related:
        for candidate in compare_topic((revision, peer), event_times=event_times):
            span = (candidate.start, candidate.end)
            if candidate.revision_id != revision.id or span in spans:
                continue
            spans.add(span)
            yield peer, candidate
            if len(spans) >= MAX_TOPIC_CANDIDATES:
                return


def _analyze(
    revision: ArticleRevision,
    related: tuple[ArticleRevision, ...],
    analysis: AnalysisProvider,
    search: EvidenceSearchProvider,
    fetcher: PageFetcher,
    assessor: EvidenceAssessor | None,
    meter: _Meter,
    event_times: Mapping[int, str],
) -> _Outcome:
    extracted = extract_claims(revision, analysis, related)
    findings: list[Finding] = []
    batches: list[tuple[Evidence, ...]] = []
    for candidate in extracted.candidates:
        if candidate.revision_id not in {None, revision.id}:
            raise ValueError("candidate revision_id does not match revision")
        if candidate.finding_type == "material_cross_media_difference":
            # Only the same-topic comparison below can verify one; web pages never can (gaohe.policy).
            continue
        candidate = replace(candidate, revision_id=revision.id)
        retrieved = retrieve_evidence(candidate, search, fetcher, revision=revision)
        meter.raise_if_interrupted()
        claim_text = _claim_text(candidate, extracted.claims, revision)
        assessed = tuple(assess_evidence(candidate, claim_text, revision, retrieved, assessor))
        meter.raise_if_interrupted()
        findings.append(resolve_finding(candidate, assessed, related, revision))
        batches.append(assessed)
    for peer, candidate in _topic_candidates(revision, related, event_times):
        # The evidence carries the peer's redacted URL, so match it against the same redacted form.
        verified_peer = (replace(peer, url=redact_url(peer.url)),)
        claim_text = revision.text[candidate.start:candidate.end]
        assessed = tuple(assess_evidence(candidate, claim_text, revision, (_related_evidence(peer),), assessor))
        meter.raise_if_interrupted()
        findings.append(resolve_finding(candidate, assessed, verified_peer, revision))
        batches.append(assessed)
    return _Outcome(extracted.claims, tuple(findings), tuple(batches), extracted.rejected_claims)


def _failure_reason(error: Exception) -> str:
    if isinstance(error, ProviderError):
        return error.user_message
    detail = safe_error(" ".join(str(error).split()))
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


def _count(summary: Summary, outcome: _Outcome) -> None:
    evidence = [item for batch in outcome.evidence for item in batch]
    counts = {
        "claims": len(outcome.claims),
        "candidates": len(outcome.findings),
        "visible_findings": sum(finding.visible for finding in outcome.findings),
        "pending": sum(not finding.visible for finding in outcome.findings),
        "retrieval_failures": sum(item.status == "retrieval_failed" for item in evidence),
        "rejected_claims": outcome.rejected_claims,
        "analyzed": 1,
    }
    for key, value in counts.items():
        summary[key] = int(summary[key]) + value


def _add(summary: Summary, key: str, changed: bool) -> None:
    summary[key] = int(summary[key]) + int(changed)


def _validate_limit(daily_llm_call_limit: int | None) -> None:
    if daily_llm_call_limit is not None and (
        not isinstance(daily_llm_call_limit, int) or isinstance(daily_llm_call_limit, bool) or daily_llm_call_limit < 1
    ):
        raise ValueError("daily_llm_call_limit must be a positive integer")


def run_pending_analysis(
    store: Store,
    analysis: AnalysisProvider,
    search: EvidenceSearchProvider,
    fetcher: PageFetcher,
    limit: int,
    *,
    assessor: EvidenceAssessor | None = None,
    now: datetime | None = None,
    daily_llm_call_limit: int | None = None,
    max_attempts: int = 3,
    revision_ids: Sequence[int] | None = None,
) -> Summary:
    """Analyze due revisions with injected providers; return counts only, never a verdict.

    now fixes the clock (its offset defines the local day for the budget); None uses
    the real local clock. revision_ids, when given, limits the run to those revisions
    (each still only when it is due). Without an assessor every page stays unassessed,
    so no finding becomes visible. The summary's stopped/stop_code report a run-stopping
    provider error; the revision it happened on is left pending and the rest untouched.
    """
    _validate_limit(daily_llm_call_limit)
    clock = _clock(now)
    summary = empty_summary()
    store.fail_abandoned_analyses(utc_iso(clock()), max_attempts=max_attempts)
    revisions = store.list_pending_revisions(
        limit, max_attempts=max_attempts, now=utc_iso(clock()), revision_ids=revision_ids,
    )
    if not revisions:
        return summary
    meter = _Meter(store, clock, daily_llm_call_limit)
    metered_analysis = _MeteredAnalysis(analysis, meter)
    metered_assessor = _metered_assessor(assessor, meter)
    peers = store.list_recent_revisions(CONTEXT_POOL_LIMIT, current_only=True)
    event_times = store.event_times(sorted({item.id for item in (*revisions, *peers)}))
    signals: _Signals = {}
    for index, revision in enumerate(revisions):
        if meter.exhausted():
            for item in revisions[index:]:
                _add(summary, "skipped", store.mark_analysis_skipped(item.id, utc_iso(clock()), BUDGET_SKIP_REASON))
            break
        if index % _GROUPING_CHUNK == 0:
            signals.update(_chunk_signals(revisions[index:index + _GROUPING_CHUNK], peers, event_times))
        claimed_at = utc_iso(clock())
        if not store.mark_analysis_running(revision.id, claimed_at, max_attempts=max_attempts):
            continue  # completed, taken by a concurrent run, or out of attempts since it was listed
        meter.reset()
        try:
            if revision.id not in signals:
                signals.update(_peer_signals((revision,), peers, event_times))
            related = _related(store, revision, signals[revision.id])
            outcome = _analyze(
                revision, related, metered_analysis, _metered_search(search, meter, revision.id), fetcher,
                metered_assessor, meter, event_times,
            )
            saved = store.save_analysis(
                revision.id, outcome.claims, outcome.findings, outcome.evidence,
                provider=_label(analysis, "provider_name"), model=_label(analysis, "model"),
                prompt_version=ANALYSIS_PROMPT_VERSION, completed_at=utc_iso(clock()), claimed_at=claimed_at,
            )
        except BudgetExhausted:
            at = utc_iso(clock())
            if daily_llm_call_limit is not None and meter.used_by(revision.id) >= daily_llm_call_limit:
                # Alone it used the whole day's budget: retrying tomorrow would only block the queue again.
                failed = store.mark_analysis_failed(
                    revision.id, at, OVER_BUDGET_REASON, claimed_at=claimed_at, final_attempts=max_attempts,
                )
                _add(summary, "failed", failed)
            else:
                _add(summary, "skipped", store.mark_analysis_skipped(revision.id, at, BUDGET_SKIP_REASON, claimed_at=claimed_at))
            continue
        except ProviderError as error:
            at = utc_iso(clock())
            if error.stops_run:
                store.mark_analysis_pending(revision.id, at, error.user_message, claimed_at=claimed_at)
                summary["stopped"], summary["stop_code"] = 1, error.code
                break
            if error.transient:
                _add(summary, "skipped", store.mark_analysis_skipped(revision.id, at, error.user_message, claimed_at=claimed_at))
                continue
            _add(summary, "failed", store.mark_analysis_failed(revision.id, at, _failure_reason(error), claimed_at=claimed_at))
            continue
        except Exception as error:  # one bad revision must never block the queue
            failed = store.mark_analysis_failed(revision.id, utc_iso(clock()), _failure_reason(error), claimed_at=claimed_at)
            _add(summary, "failed", failed)
            continue
        if saved:
            _count(summary, outcome)
    return summary
