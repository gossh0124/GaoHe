from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from typing import Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import Settings
from .domain import ArticleRevision, Claim, RetrievedPage, SearchHit, article_content_hash
from .sources import HttpTransport, UrllibTransport, extract_article_text


MAX_QUERY_CHARS = 500
MAX_SEARCH_LIMIT = 10
MAX_ANALYSIS_CHARS = 20_000
MAX_PAGE_BYTES = 1_000_000
MAX_PAGE_TEXT_CHARS = 200_000
MAX_PAGE_TITLE_CHARS = 500


@dataclass(frozen=True)
class FindingCandidate:
    claim_id: int | None
    finding_type: str
    summary: str
    start: int
    end: int
    materiality: str
    query: str | None


@dataclass(frozen=True)
class AnalysisResult:
    revision_id: int
    claims: tuple[Claim, ...]
    candidates: tuple[FindingCandidate, ...]


class AnalysisProvider(Protocol):
    def analyze(self, revision: ArticleRevision, related: Sequence[ArticleRevision]) -> AnalysisResult: ...


class EvidenceSearchProvider(Protocol):
    def search(self, query: str, limit: int = 5) -> Sequence[SearchHit]: ...


class PageFetcher(Protocol):
    def fetch(self, url: str) -> RetrievedPage: ...


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _failed_page(url: str, status: str) -> RetrievedPage:
    return RetrievedPage(url if _is_http_url(url) else "", "", "", _now(), status, None)


class NullSearchProvider:
    def search(self, query: str, limit: int = 5) -> Sequence[SearchHit]:
        del query
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("Search limit must be positive")
        limit = min(limit, MAX_SEARCH_LIMIT)
        del limit
        return ()


def build_search_provider(settings: Settings) -> EvidenceSearchProvider:
    if settings.web_search_provider == "none":
        return NullSearchProvider()
    raise ValueError(f"Unsupported WEB_SEARCH_PROVIDER: {settings.web_search_provider}")


def build_analysis_provider(settings: Settings) -> AnalysisProvider:
    if settings.llm_provider == "gemini":
        return GeminiAnalysisProvider(settings)
    raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")


GeminiRequest = Callable[[str, Mapping[str, object], str], str]


class GeminiAnalysisProvider:
    def __init__(self, settings: Settings, request: GeminiRequest | None = None, *, urlopen_request: Callable[..., object] = urlopen) -> None:
        self._model = settings.llm_model
        self._api_key = settings.llm_api_key
        self._urlopen = urlopen_request
        self._request = request or self._post

    def analyze(self, revision: ArticleRevision, related: Sequence[ArticleRevision]) -> AnalysisResult:
        if not self._model or not self._api_key:
            raise ValueError("Gemini analysis requires LLM_MODEL and LLM_API_KEY")
        prompt = {
            "revision": {"id": revision.id, "title": revision.title[:MAX_PAGE_TITLE_CHARS], "text": revision.text[:MAX_ANALYSIS_CHARS]},
            "related": [{"id": item.id, "title": item.title[:MAX_PAGE_TITLE_CHARS], "text": item.text[:MAX_ANALYSIS_CHARS]} for item in related[:10]],
            "response_schema": {"claims": "list", "candidates": "list"},
        }
        try:
            raw = self._request(self._model, prompt, self._api_key)
            return self._parse(revision, raw)
        except ValueError as error:
            if str(error) == "Gemini returned invalid analysis response":
                raise
            raise ValueError("Gemini analysis request failed") from None
        except Exception:
            raise ValueError("Gemini analysis request failed") from None

    @staticmethod
    def _parse(revision: ArticleRevision, raw: str) -> AnalysisResult:
        try:
            value = json.loads(raw)
            claims_raw = value["claims"]
            candidates_raw = value["candidates"]
            if not isinstance(value, dict) or not isinstance(claims_raw, list) or not isinstance(candidates_raw, list):
                raise TypeError
            claims = tuple(
                Claim(None, revision.id, _string(item, "text"), _integer(item, "start"), _integer(item, "end"), _string(item, "kind"), _string(item, "materiality"), "extracted")
                for item in claims_raw
            )
            candidates = tuple(
                FindingCandidate(_optional_integer(item, "claim_id"), _string(item, "finding_type"), _string(item, "summary"), _integer(item, "start"), _integer(item, "end"), _string(item, "materiality"), _bounded_optional_string(item, "query", MAX_QUERY_CHARS))
                for item in candidates_raw
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("Gemini returned invalid analysis response") from None
        return AnalysisResult(revision.id, claims, candidates)

    def _post(self, model: str, payload: Mapping[str, object], api_key: str) -> str:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        request = Request(endpoint, data=json.dumps({"contents": [{"parts": [{"text": json.dumps(payload)}]}]}).encode("utf-8"), headers={"Content-Type": "application/json", "x-goog-api-key": api_key}, method="POST")
        with self._urlopen(request, timeout=20) as response:
            body = response.read(MAX_PAGE_BYTES + 1)
        if len(body) > MAX_PAGE_BYTES:
            raise ValueError("response too large")
        value = json.loads(body.decode("utf-8"))
        return value["candidates"][0]["content"]["parts"][0]["text"]


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise TypeError
    return value


def _string(value: object, key: str) -> str:
    item = _mapping(value).get(key)
    if not isinstance(item, str) or not item:
        raise TypeError
    return item


def _optional_string(value: object, key: str) -> str | None:
    item = _mapping(value).get(key)
    if item is not None and not isinstance(item, str):
        raise TypeError
    return item


def _bounded_optional_string(value: object, key: str, maximum: int) -> str | None:
    item = _optional_string(value, key)
    return item[:maximum] if item is not None else None


def _integer(value: object, key: str) -> int:
    item = _mapping(value).get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise TypeError
    return item


def _optional_integer(value: object, key: str) -> int | None:
    item = _mapping(value).get(key)
    if item is not None and (not isinstance(item, int) or isinstance(item, bool)):
        raise TypeError
    return item


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._in_title = tag == "title"

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.parts.append(data)


def _page_title(body: bytes) -> str:
    parser = _TitleParser()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
        parser.close()
    except ValueError:
        return ""
    return " ".join("".join(parser.parts).split())[:MAX_PAGE_TITLE_CHARS]


Fallback = PageFetcher | Callable[[str], RetrievedPage | tuple[str, str]]


def _normalized_fallback_page(url: str, value: object) -> RetrievedPage | None:
    if isinstance(value, RetrievedPage):
        if value.status != "retrieved":
            return None
        page_url, title, text = value.url, value.title, value.text
    elif isinstance(value, tuple) and len(value) == 2:
        page_url, (title, text) = url, value
    else:
        return None
    if not _is_http_url(page_url) or not isinstance(title, str) or not isinstance(text, str):
        return None
    title = title[:MAX_PAGE_TITLE_CHARS]
    text = text[:MAX_PAGE_TEXT_CHARS]
    if not title.strip() or not text:
        return None
    return RetrievedPage(page_url, title, text, _now(), "retrieved", article_content_hash(title, text))


class DirectPageFetcher:
    def __init__(self, transport: HttpTransport | None = None, *, fallback: Fallback | None = None, firecrawl_api_key: str = "") -> None:
        self._transport = transport or UrllibTransport()
        self._fallback = fallback
        self._firecrawl_api_key = firecrawl_api_key

    def fetch(self, url: str) -> RetrievedPage:
        if not _is_http_url(url):
            return _failed_page(url, "invalid_url")
        try:
            response = self._transport.fetch(url)
        except TimeoutError:
            return self._fallback_or(url, "timeout")
        except Exception:
            return self._fallback_or(url, "retrieval_failed")
        if not 200 <= response.status < 300:
            return self._fallback_or(url, "http_error")
        if len(response.body) > MAX_PAGE_BYTES:
            return self._fallback_or(url, "oversized")
        text = extract_article_text(response.body, response.headers.get("Content-Type"))[:MAX_PAGE_TEXT_CHARS]
        if not text:
            return self._fallback_or(url, "parse_error")
        title = _page_title(response.body)
        return RetrievedPage(response.url if _is_http_url(response.url) else url, title, text, _now(), "retrieved", article_content_hash(title, text))

    def _fallback_or(self, url: str, status: str) -> RetrievedPage:
        if not self._firecrawl_api_key or self._fallback is None:
            return _failed_page(url, status)
        try:
            page = self._fallback.fetch(url) if hasattr(self._fallback, "fetch") else self._fallback(url)
            normalized = _normalized_fallback_page(url, page)
            return normalized or _failed_page(url, status)
        except Exception:
            return _failed_page(url, status)


class FirecrawlPageFetcher:
    def __init__(self, api_key: str, fetch: Callable[[str, str], tuple[str, str]]) -> None:
        self._api_key = api_key
        self._fetch = fetch

    def fetch(self, url: str) -> RetrievedPage:
        if not _is_http_url(url) or not self._api_key:
            return _failed_page(url, "invalid_url" if not _is_http_url(url) else "unavailable")
        try:
            page = _normalized_fallback_page(url, self._fetch(url, self._api_key))
        except Exception:
            return _failed_page(url, "retrieval_failed")
        return page or _failed_page(url, "retrieval_failed")
