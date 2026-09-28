"""The one place that decides whether a finding may become a visible annotation.

Every rule here is deliberately conservative: a finding stays pending (for human
reading) unless assessed, retrievable full-text evidence explicitly carries it.
Missing, failed, snippet-only, or unassessed evidence never makes a finding
visible, and a visible finding is never a verdict on the article or the outlet.
"""

from collections.abc import Iterable

from .domain import Evidence
from .safety import canonical_url


# Spec 7.3: v0 has exactly these three visible annotation types.
FINDING_TYPES = frozenset({
    "factual_contradiction",
    "material_cross_media_difference",
    "unsupported_inference",
})
# Evidence whose full text GaoHe fetched itself, as opposed to a search hit or a peer revision.
FULL_TEXT_SOURCE_KINDS = frozenset({"direct", "firecrawl"})


def usable(evidence: Evidence) -> bool:
    """Return whether one evidence record may count towards visibility at all."""
    return (
        evidence.status == "retrieved"  # spec 2.4: failed or blocked retrieval is never evidence
        and canonical_url(evidence.url) is not None  # spec 6.3: the source must be traceable
        and isinstance(evidence.excerpt, str)
        and bool(evidence.excerpt.strip())  # spec 2.3: a citable page text, never a bare snippet
        and isinstance(evidence.rationale, str)
        and bool(evidence.rationale.strip())  # spec 7.1: only assessed evidence has a relation
    )


def is_visible(finding_type: str, evidence: Iterable[Evidence], related_urls: Iterable[object]) -> bool:
    """Return whether a finding of finding_type may be shown as a visible annotation."""
    items = tuple(item for item in evidence if usable(item))
    supported = any(item.relation == "supports" for item in items)
    if finding_type == "factual_contradiction":
        # Spec 7.3 + 6.4: a fetched full-text source must directly conflict; any
        # supporting source means the sources disagree, which stays 待人工判讀.
        conflicting = any(
            item.relation == "contradicts" and item.source_kind in FULL_TEXT_SOURCE_KINDS
            for item in items
        )
        return conflicting and not supported
    if finding_type == "unsupported_inference":
        # Spec 7.3: full-text sources cover the facts but not the inferred conclusion;
        # a source that supports the conclusion removes the gap.
        limited = any(
            item.relation in {"context", "contradicts"} and item.source_kind in FULL_TEXT_SOURCE_KINDS
            for item in items
        )
        return limited and not supported
    if finding_type == "material_cross_media_difference":
        # Spec 8: only a verified same-topic peer article that conflicts on an important claim.
        peers = {canonical_url(url) for url in related_urls}
        peers.discard(None)
        return any(
            item.relation == "contradicts"
            and item.source_kind == "related_article"
            and canonical_url(item.url) in peers
            for item in items
        )
    # Spec 7.3: nothing outside the three v0 types is ever visible.
    return False
