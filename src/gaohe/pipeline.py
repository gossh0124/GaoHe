"""The analysis queue: each pending revision is analyzed on its own.

A revision that fails is marked failed and retried on later runs (up to max_attempts). A
provider problem that would fail every further call the same way (bad key, exhausted quota,
unknown model, rate limit, no network) stops the run instead and leaves the revision queued,
so nothing is marked failed because of the user's setup or connection.
"""

from collections.abc import Sequence
from datetime import datetime, timezone

from .analysis import analyze_revision
from .errors import ProviderError
from .providers import PROMPT_VERSION, AnalysisProvider, EvidenceAssessor, PageFetcher, SearchProvider
from .safety import safe_error
from .storage import Store


SUMMARY_KEYS = ("analyzed", "failed", "claims", "candidates", "visible_findings", "pending_findings", "stopped")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_pending_analysis(
    store: Store,
    analysis: AnalysisProvider,
    search: SearchProvider,
    fetcher: PageFetcher,
    assessor: EvidenceAssessor | None,
    limit: int = 20,
    *,
    revision_ids: Sequence[int] | None = None,
    max_attempts: int = 3,
    now: str | None = None,
) -> dict[str, object]:
    """Analyze up to limit pending revisions (or just revision_ids) and return a non-verdict summary."""
    summary: dict[str, object] = dict.fromkeys(SUMMARY_KEYS, 0)
    summary["stop_code"] = ""
    for revision in store.list_pending_revisions(limit, max_attempts=max_attempts, revision_ids=revision_ids):
        at = now or utc_now()
        try:
            extracted, findings, evidence = analyze_revision(revision, analysis, search, fetcher, assessor)
        except ProviderError as error:
            if error.stops_run:
                summary.update(stopped=1, stop_code=error.code)
                break
            store.mark_analysis_failed(revision.id, at, error.code)
            summary["failed"] += 1
            continue
        except Exception as error:
            store.mark_analysis_failed(revision.id, at, f"{type(error).__name__}: {safe_error(str(error))}")
            summary["failed"] += 1
            continue
        store.save_analysis(
            revision.id, extracted.claims, findings, evidence,
            completed_at=at, model=getattr(analysis, "model", None), prompt_version=PROMPT_VERSION,
        )
        summary["analyzed"] += 1
        summary["claims"] += len(extracted.claims)
        summary["candidates"] += len(findings)
        summary["visible_findings"] += sum(finding.visible for finding in findings)
        summary["pending_findings"] += sum(not finding.visible for finding in findings)
    return summary
