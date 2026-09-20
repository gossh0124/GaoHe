"""Deterministic, conservative same-topic comparison helpers."""

from collections.abc import Sequence
from datetime import datetime, timezone
import math
import re
from urllib.parse import urlsplit, urlunsplit

from .domain import ArticleRevision, TopicGroup
from .providers import FindingCandidate, MAX_QUERY_CHARS


TOPIC_WINDOW_HOURS = 72
MAX_SUMMARY_CHARS = 500
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'-]{2,}|[\u4e00-\u9fff]{2,}|\d[\d,]*")
_ENTITY = re.compile(r"\b[A-Z][a-z]{2,}\b|[\u4e00-\u9fff]{1,7}(?:部|局|會|院|署|市|縣|國|公司|大學|銀行|醫院)")
_NUMBER = re.compile(r"(?<![\w,])(\d[\d,]*)(?:\s*)([%A-Za-z]+|[\u4e00-\u9fff]{1,4})?")
_APPROXIMATE = re.compile(r"(?:about|around|approximately|roughly|nearly|over|under|約|近|逾|超過)\s*$", re.IGNORECASE)
_OPPOSITES = (("approved", "rejected"), ("opened", "closed"), ("confirmed", "denied"), ("arrested", "released"), ("批准", "否決"), ("開放", "關閉"), ("確認", "否認"), ("逮捕", "釋放"))
_STOPWORDS = {"a", "an", "and", "after", "at", "by", "for", "from", "in", "of", "on", "or", "the", "that", "this", "to", "with", "reported", "reports", "said", "says"}
_DATE_UNITS = {"am", "pm", "day", "days", "hour", "hours", "month", "months", "year", "years"}


def _tokens(value: str) -> set[str]:
    return {item.casefold() for item in _WORD.findall(value)}


def _entities(value: str) -> set[str]:
    return {item.casefold() for item in _ENTITY.findall(value)}


def _event_tokens(value: str, *, units: set[str] = set(), opposites: set[str] = set()) -> set[str]:
    tokens = _tokens(value)
    return tokens - _entities(value) - _STOPWORDS - units - opposites - {item for item in tokens if item.replace(",", "").isdigit()}


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return ""


def _safe_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, "", ""))[:200]


def _when(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _within_window(left: ArticleRevision, right: ArticleRevision) -> bool:
    left_when, right_when = _when(left.fetched_at), _when(right.fetched_at)
    return left_when is not None and right_when is not None and abs((left_when - right_when).total_seconds()) <= TOPIC_WINDOW_HOURS * 3600


def _pair_confidence(left: ArticleRevision, right: ArticleRevision) -> str | None:
    left_url, right_url = _safe_url(left.url), _safe_url(right.url)
    if not left_url or not right_url or left_url == right_url or not _within_window(left, right):
        return None
    shared_entities = (_entities(left.title + " " + left.text) - _STOPWORDS) & (_entities(right.title + " " + right.text) - _STOPWORDS)
    shared_events = _event_tokens(left.title + " " + left.text) & _event_tokens(right.title + " " + right.text)
    shared_body_events = _event_tokens(left.text) & _event_tokens(right.text)
    if _host(left_url) != _host(right_url) and shared_entities and len(shared_events) >= 2 and shared_body_events:
        return "high"
    if shared_events:
        return "possible"
    return None


def _label(left: ArticleRevision, right: ArticleRevision) -> str:
    shared = sorted((_tokens(left.title) & _tokens(right.title)) | (_entities(left.title + " " + left.text) & _entities(right.title + " " + right.text)))
    return " ".join(shared[:5]) or "possible topic"


def group_revision(revision: ArticleRevision, existing: Sequence[ArticleRevision]) -> TopicGroup | None:
    """Return the best deterministic grouping signal without forcing uncertain peers."""
    possible: ArticleRevision | None = None
    for item in existing:
        confidence = _pair_confidence(revision, item)
        if confidence == "high":
            return TopicGroup(None, _label(revision, item), "high", "active")
        if confidence == "possible" and possible is None:
            possible = item
    return TopicGroup(None, _label(revision, possible), "possible", "possible") if possible else None


def _sentence(text: str, start: int, end: int) -> str:
    left = max(text.rfind(mark, 0, start) for mark in ".!?。！？") + 1
    right_positions = [position for mark in ".!?。！？" if (position := text.find(mark, end)) != -1]
    return text[left:min(right_positions) if right_positions else len(text)]


def _numeric_claims(text: str) -> list[tuple[int, int, int, str, str, str]]:
    claims: list[tuple[int, int, int, str, str, str]] = []
    for match in _NUMBER.finditer(text):
        before = text[max(0, match.start() - 20):match.start()]
        if _APPROXIMATE.search(before):
            continue
        value = int(match.group(1).replace(",", ""))
        phrase = match.group(0).strip()
        unit = (match.group(2) or "").casefold()
        if not value or not unit or unit in _DATE_UNITS:
            continue
        claims.append((match.start(), match.end(), value, phrase, unit, _sentence(text, match.start(), match.end())))
    return claims


def _numeric_difference(left: ArticleRevision, right: ArticleRevision) -> tuple[int, int, str, str] | None:
    for start, end, value, phrase, unit, context in _numeric_claims(left.text):
        left_events = _event_tokens(context, units={unit})
        left_entities = _entities(context) - _STOPWORDS
        for _, _, other_value, other_phrase, other_unit, other_context in _numeric_claims(right.text):
            if value == other_value or unit != other_unit:
                continue
            right_events = _event_tokens(other_context, units={other_unit})
            right_entities = _entities(other_context) - _STOPWORDS
            if left_entities & right_entities and len(left_events & right_events) >= 2 and int(math.log10(value)) != int(math.log10(other_value)):
                return start, end, phrase, other_phrase
    return None


def _opposite_difference(left: ArticleRevision, right: ArticleRevision) -> tuple[int, int, str, str] | None:
    left_text, right_text = left.text.casefold(), right.text.casefold()
    opposite_words = {word for pair in _OPPOSITES for word in pair}
    for first, second in _OPPOSITES:
        for left_word, right_word in ((first, second), (second, first)):
            start = left_text.find(left_word)
            while start != -1:
                left_context = _sentence(left.text, start, start + len(left_word))
                left_entities = _entities(left_context) - _STOPWORDS
                left_events = _event_tokens(left_context, opposites=opposite_words)
                right_start = right_text.find(right_word)
                while right_start != -1:
                    right_context = _sentence(right.text, right_start, right_start + len(right_word))
                    if (left_entities & (_entities(right_context) - _STOPWORDS)) and (left_events & _event_tokens(right_context, opposites=opposite_words)):
                        return start, start + len(left_word), left_word, right_word
                    right_start = right_text.find(right_word, right_start + len(right_word))
                start = left_text.find(left_word, start + len(left_word))
    return None


def _candidate(left: ArticleRevision, right: ArticleRevision, difference: tuple[int, int, str, str]) -> FindingCandidate:
    start, end, left_claim, right_claim = difference
    left_url, right_url = _safe_url(left.url), _safe_url(right.url)
    detail = f"{left_claim} versus {right_claim}"
    sources = f"{left_url} ({_host(left.url)}, {left.fetched_at[:32]}) vs {right_url} ({_host(right.url)}, {right.fetched_at[:32]})"
    summary = f"Material cross-media difference: {detail}; sources: {sources}"[:MAX_SUMMARY_CHARS]
    query = f"cross-media difference {detail}; sources: {sources}"[:MAX_QUERY_CHARS]
    return FindingCandidate(None, "material_cross_media_difference", summary, start, end, "material", query, left.id)


def compare_topic(revisions: Sequence[ArticleRevision]) -> list[FindingCandidate]:
    """Return only material, checkable candidates between high-confidence peers."""
    candidates: list[FindingCandidate] = []
    seen: set[tuple[int, int, int]] = set()
    for index, left in enumerate(revisions):
        for right in revisions[index + 1:]:
            if _pair_confidence(left, right) != "high":
                continue
            difference = _numeric_difference(left, right) or _opposite_difference(left, right)
            if difference is None:
                continue
            key = (left.id, difference[0], right.id)
            if key not in seen:
                seen.add(key)
                candidates.append(_candidate(left, right, difference))
    return candidates
