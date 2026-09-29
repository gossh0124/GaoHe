"""The analysis queue: each pending revision runs as one isolated, budgeted job.

Per revision: group it with current peer revisions -> extract claims -> retrieve
and assess evidence for each provider candidate -> assess same-topic differences
against the peer text -> resolve findings -> save. Any stage error marks only
that revision failed (the store retries it up to max_attempts); reaching the
daily LLM call limit defers the revision as skipped without using an attempt.
Every provider call is written to the LLM ledger; prompts and keys never are.
"""

from collections.abc import Callable, Iterable, Sequence
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
    TopicGroup,
)
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
SUMMARY_KEYS = (
    "claims", "candidates", "visible_findings", "pending", "retrieval_failures",
    "rejected_claims", "analyzed", "failed", "skipped",
)
BUDGET_SKIP_REASON = "daily LLM call limit reached; the revision will be analyzed on a later run"
# Pending revisions grouped per pass over the peer pool. Two cached texts per revision plus
# the current peer's two stay inside gaohe.topics' 64-entry text cache, so each text is
# normalized once per pass instead of once per pair.
_GROUPING_CHUNK = 24
_MAX_RELATED_TITLE_CHARS = 500

Clock = Callable[[], datetime]
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


class _Meter:
    """Checks the daily budget before, and writes the LLM ledger after, every provider call.

    assess_evidence() treats any assessor exception as "not assessed", so an exception
    raised by the meter itself (budget reached, ledger write failed) is also kept in
    `interrupted` for the pipeline to re-raise once assess_evidence() returns.
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
        try:
            result = run()
        except Exception:
            self._record(provider, purpose, revision_id, "failed", input_chars, 0)
            raise
        self._record(provider, purpose, revision_id, "ok", input_chars, output_chars(result))
        return result

    def _record(self, provider: object, purpose: str, revision_id: int, status: str, input_chars: int, output_chars: int) -> None:
        try:
            self._store.record_llm_call(
                utc_iso(self._clock()), _label(provider, "provider_name"), _label(provider, "model"),
                purpose, revision_id, status, input_chars, output_chars,
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


def _metered_assessor(assessor: EvidenceAssessor | None, meter: _Meter) -> EvidenceAssessor | None:
    # The null assessor makes no LLM call, so it neither spends budget nor writes the ledger.
    if assessor is None or getattr(assessor, "provider_name", None) == "none":
        return assessor
    return _MeteredAssessor(assessor, meter)


# --- same-topic grouping --------------------------------------------------------------------------

_Signals = dict[int, list[tuple[ArticleRevision, TopicGroup]]]


def _peer_signals(revisions: Sequence[ArticleRevision], peers: Sequence[ArticleRevision]) -> _Signals:
    """Group each revision with each peer on its own; peer-major so each text is normalized once."""
    signals: _Signals = {revision.id: [] for revision in revisions}
    for peer in peers:
        for revision in revisions:
            if peer.id == revision.id:
                continue
            group = group_revision(revision, (peer,))
            if group is not None and group.confidence in {"high", "possible"}:
                signals[revision.id].append((peer, group))
    return signals


def _chunk_signals(revisions: Sequence[ArticleRevision], peers: Sequence[ArticleRevision]) -> _Signals:
    try:
        return _peer_signals(revisions, peers)
    except Exception:
        # Each revision of this chunk then groups itself inside its own job, isolating the bad text.
        return {}


def _related(
    store: Store, revision: ArticleRevision, signals: Sequence[tuple[ArticleRevision, TopicGroup]]
) -> tuple[ArticleRevision, ...]:
    """Persist every grouping signal; return the high-confidence peers a reviewer has not dismissed."""
    related = []
    for peer, group in signals:
        topic_id = store.assign_topic(revision.id, peer.id, group.label, group.confidence)
        if group.confidence == "high" and store.topic_status(topic_id) != "dismissed":
            related.append(peer)
    return tuple(related)


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


def _analyze(
    revision: ArticleRevision,
    related: tuple[ArticleRevision, ...],
    analysis: AnalysisProvider,
    search: EvidenceSearchProvider,
    fetcher: PageFetcher,
    assessor: EvidenceAssessor | None,
    meter: _Meter,
) -> _Outcome:
    extracted = extract_claims(revision, analysis, related)
    findings: list[Finding] = []
    batches: list[tuple[Evidence, ...]] = []
    for candidate in extracted.candidates:
        if candidate.revision_id not in {None, revision.id}:
            raise ValueError("candidate revision_id does not match revision")
        candidate = replace(candidate, revision_id=revision.id)
        retrieved = retrieve_evidence(candidate, search, fetcher, revision=revision)
        claim_text = _claim_text(candidate, extracted.claims, revision)
        assessed = tuple(assess_evidence(candidate, claim_text, revision, retrieved, assessor))
        meter.raise_if_interrupted()
        findings.append(resolve_finding(candidate, assessed, related, revision))
        batches.append(assessed)
    for peer in related:
        # The evidence carries the peer's redacted URL, so match it against the same redacted form.
        verified_peer = (replace(peer, url=redact_url(peer.url)),)
        for candidate in compare_topic((revision, peer)):
            if candidate.revision_id != revision.id:
                continue
            claim_text = revision.text[candidate.start:candidate.end]
            assessed = tuple(assess_evidence(candidate, claim_text, revision, (_related_evidence(peer),), assessor))
            meter.raise_if_interrupted()
            findings.append(resolve_finding(candidate, assessed, verified_peer, revision))
            batches.append(assessed)
    return _Outcome(extracted.claims, tuple(findings), tuple(batches), extracted.rejected_claims)


def _failure_reason(error: Exception) -> str:
    detail = safe_error(" ".join(str(error).split()))
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


def _count(summary: dict[str, int], outcome: _Outcome) -> None:
    evidence = [item for batch in outcome.evidence for item in batch]
    summary["claims"] += len(outcome.claims)
    summary["candidates"] += len(outcome.findings)
    summary["visible_findings"] += sum(finding.visible for finding in outcome.findings)
    summary["pending"] += sum(not finding.visible for finding in outcome.findings)
    summary["retrieval_failures"] += sum(item.status == "retrieval_failed" for item in evidence)
    summary["rejected_claims"] += outcome.rejected_claims
    summary["analyzed"] += 1


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
) -> dict[str, int]:
    """Analyze due revisions with injected providers; return counts only, never a verdict.

    now fixes the clock (its offset defines the local day for the budget); None uses
    the real local clock. Without an assessor every page stays unassessed, so no
    finding becomes visible.
    """
    if daily_llm_call_limit is not None and (
        not isinstance(daily_llm_call_limit, int) or isinstance(daily_llm_call_limit, bool) or daily_llm_call_limit < 1
    ):
        raise ValueError("daily_llm_call_limit must be a positive integer")
    clock = _clock(now)
    summary = dict.fromkeys(SUMMARY_KEYS, 0)
    revisions = store.list_pending_revisions(limit, max_attempts=max_attempts, now=utc_iso(clock()))
    if not revisions:
        return summary
    meter = _Meter(store, clock, daily_llm_call_limit)
    metered_analysis = _MeteredAnalysis(analysis, meter)
    metered_assessor = _metered_assessor(assessor, meter)
    peers = store.list_recent_revisions(CONTEXT_POOL_LIMIT, current_only=True)
    signals: _Signals = {}
    for index, revision in enumerate(revisions):
        if meter.exhausted():
            summary["skipped"] += sum(
                store.mark_analysis_skipped(item.id, utc_iso(clock()), BUDGET_SKIP_REASON) for item in revisions[index:]
            )
            break
        if index % _GROUPING_CHUNK == 0:
            signals.update(_chunk_signals(revisions[index:index + _GROUPING_CHUNK], peers))
        if not store.mark_analysis_running(revision.id, utc_iso(clock())):
            continue  # completed by a concurrent run since it was listed
        meter.reset()
        try:
            own_signals = signals[revision.id] if revision.id in signals else _peer_signals((revision,), peers)[revision.id]
            related = _related(store, revision, own_signals)
            outcome = _analyze(revision, related, metered_analysis, search, fetcher, metered_assessor, meter)
            store.save_analysis(
                revision.id, outcome.claims, outcome.findings, outcome.evidence,
                provider=_label(analysis, "provider_name"), model=_label(analysis, "model"),
                prompt_version=ANALYSIS_PROMPT_VERSION, completed_at=utc_iso(clock()),
            )
        except BudgetExhausted:
            store.mark_analysis_skipped(revision.id, utc_iso(clock()), BUDGET_SKIP_REASON)
            summary["skipped"] += 1
            continue
        except Exception as error:  # one bad revision must never block the queue
            store.mark_analysis_failed(revision.id, utc_iso(clock()), _failure_reason(error))
            summary["failed"] += 1
            continue
        _count(summary, outcome)
    return summary
