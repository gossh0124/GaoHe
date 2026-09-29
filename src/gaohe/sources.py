from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import re
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from . import __version__
from .domain import ArticleCandidate
from .safety import is_http_url as _is_http_url


MAX_RESPONSE_BYTES = 1_000_000
MAX_CANDIDATES = 500
MAX_TIMEOUT_SECONDS = 20.0
MAX_REQUEST_HEADER_CHARS = 200
USER_AGENT = f"GaoHe/{__version__} source-monitor (stdlib)"
# Conditional-GET validators are the only request headers a caller may add.
_ALLOWED_REQUEST_HEADERS = {"if-none-match": "If-None-Match", "if-modified-since": "If-Modified-Since"}
_REMOVED_RESPONSE_HEADERS = {"authorization", "proxy-authorization"}
_DROP_TAGS = {"form", "footer", "header", "nav", "script", "style"}
_TEXT_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "li", "p"}
_CHARSET = re.compile(r"charset\s*=\s*[\"']?([^\s;\"']+)", re.IGNORECASE)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def fetch(
        self, url: str, timeout_seconds: float = 20.0, headers: Mapping[str, str] | None = None
    ) -> HttpResponse: ...


def _request_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Keep only If-None-Match / If-Modified-Since with short, printable ASCII values; drop everything else."""
    allowed: dict[str, str] = {}
    for name, value in (headers or {}).items():
        canonical = _ALLOWED_REQUEST_HEADERS.get(name.strip().lower()) if isinstance(name, str) else None
        if canonical is None or not isinstance(value, str):
            continue
        value = value.strip()
        if value and len(value) <= MAX_REQUEST_HEADER_CHARS and value.isascii() and value.isprintable():
            allowed[canonical] = value
    return allowed


def _response_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    return {key: value for key, value in (headers or {}).items() if key.lower() not in _REMOVED_RESPONSE_HEADERS}


class UrllibTransport:
    def fetch(
        self, url: str, timeout_seconds: float = 20.0, headers: Mapping[str, str] | None = None
    ) -> HttpResponse:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        timeout_seconds = min(float(timeout_seconds), MAX_TIMEOUT_SECONDS)
        request = Request(url, headers={**_request_headers(headers), "User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return self._response(response.status, response.geturl(), response.headers, response)
        except HTTPError as error:
            try:
                if error.code == 304:
                    # Not Modified carries validators but never a body; the caller keeps its stored copy.
                    return HttpResponse(304, error.geturl(), _response_headers(error.headers), b"")
                return self._response(error.code, error.geturl(), error.headers, error)
            finally:
                error.close()

    @staticmethod
    def _response(status: int, url: str, headers: Mapping[str, str], stream: object) -> HttpResponse:
        body = stream.read(MAX_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
        return HttpResponse(status, url, _response_headers(headers), body if len(body) <= MAX_RESPONSE_BYTES else b"")


def _text(value: str | None) -> str:
    return " ".join((value or "").split())


def _decoded(body: bytes, content_type: str | None = None) -> str:
    declared = _CHARSET.search(content_type or "")
    encodings = [declared.group(1)] if declared else []
    encodings.extend(["utf-8-sig", "utf-8", "latin-1"])
    for encoding in encodings:
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return ""


def _candidate(url: str, title: str, published_at: str | None, source_url: str, discovery_type: str, **metadata: str) -> ArticleCandidate:
    return ArticleCandidate(
        source_id=0,
        url=url,
        title=title,
        published_at=published_at,
        discovered_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        metadata={"discovery_type": discovery_type, "source_url": source_url, **metadata},
    )


def parse_feed(body: bytes, source_url: str) -> list[ArticleCandidate]:
    if len(body) > MAX_RESPONSE_BYTES:
        return []
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return []
    atom = root.tag == "{http://www.w3.org/2005/Atom}feed"
    if atom:
        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        source_title = _text(root.findtext("atom:title", namespaces=namespace))
        entries = root.findall("atom:entry", namespace)
        result: list[ArticleCandidate] = []
        for entry in entries:
            link = next((node.get("href", "") for node in entry.findall("atom:link", namespace) if node.get("rel", "alternate") == "alternate"), "")
            if _is_http_url(link):
                result.append(_candidate(link, _text(entry.findtext("atom:title", namespaces=namespace)), _text(entry.findtext("atom:published", namespaces=namespace) or entry.findtext("atom:updated", namespaces=namespace)) or None, source_url, "atom", source_title=source_title))
            if len(result) == MAX_CANDIDATES:
                break
        return result
    channel = next((node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "channel"), None)
    if channel is None:
        return []
    source_title = _text(next((node.text for node in channel if node.tag.rsplit("}", 1)[-1] == "title"), ""))
    result = []
    for item in (node for node in channel if node.tag.rsplit("}", 1)[-1] == "item"):
        values = {node.tag.rsplit("}", 1)[-1]: _text(node.text) for node in item}
        if _is_http_url(values.get("link", "")):
            result.append(_candidate(values["link"], values.get("title", ""), values.get("pubDate") or values.get("date") or None, source_url, "rss", source_title=source_title))
        if len(result) == MAX_CANDIDATES:
            break
    return result


def parse_sitemap(body: bytes, source_url: str) -> list[ArticleCandidate]:
    if len(body) > MAX_RESPONSE_BYTES:
        return []
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return []
    result: list[ArticleCandidate] = []
    for node in root:
        if node.tag.rsplit("}", 1)[-1] != "url":
            continue
        values = {child.tag.rsplit("}", 1)[-1]: _text(child.text) for child in node}
        if _is_http_url(values.get("loc", "")):
            result.append(_candidate(values["loc"], "", values.get("lastmod") or None, source_url, "sitemap"))
        if len(result) == MAX_CANDIDATES:
            break
    return result


class _ListParser(HTMLParser):
    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.ignored = 0
        self.anchor: dict[str, str] | None = None
        self.candidates: list[dict[str, str | None]] = []
        self.container_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self.ignored += 1
        if self.ignored:
            return
        if tag in {"article", "li"}:
            self.container_depth += 1
        values = dict(attrs)
        if tag == "a" and len(self.candidates) < MAX_CANDIDATES:
            self.anchor = {"url": urljoin(self.source_url, values.get("href", "")), "title": ""}
        elif tag == "time" and self.candidates and self.container_depth:
            self.candidates[-1]["published_at"] = values.get("datetime")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS and self.ignored:
            self.ignored -= 1
            return
        if self.ignored:
            return
        if tag == "a" and self.anchor is not None:
            if _is_http_url(self.anchor["url"]) and _text(self.anchor["title"]):
                self.candidates.append({"url": self.anchor["url"], "title": _text(self.anchor["title"]), "published_at": None})
            self.anchor = None
        elif tag in {"article", "li"} and self.container_depth:
            self.container_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored and self.anchor is not None:
            self.anchor["title"] += data


def parse_html_list(body: bytes, source_url: str) -> list[ArticleCandidate]:
    if len(body) > MAX_RESPONSE_BYTES:
        return []
    parser = _ListParser(source_url)
    try:
        parser.feed(_decoded(body))
        parser.close()
    except ValueError:
        return []
    return [_candidate(item["url"] or "", item["title"] or "", item["published_at"], source_url, "html_list") for item in parser.candidates]


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ignored = 0
        self.current: list[str] | None = None
        self.plain: list[str] = []
        self.parts: list[str] = []
        self.semantic_depth = 0

    def _flush_plain(self) -> None:
        text = _text("".join(self.plain))
        if text:
            self.parts.append(text)
        self.plain = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self.ignored += 1
        elif not self.ignored and tag in {"article", "main"}:
            self.semantic_depth += 1
        elif not self.ignored and tag in _TEXT_TAGS:
            self._flush_plain()
            self.current = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS and self.ignored:
            self.ignored -= 1
        elif not self.ignored and tag in _TEXT_TAGS and self.current is not None:
            text = _text("".join(self.current))
            if text:
                self.parts.append(text)
            self.current = None
        elif not self.ignored and tag in {"article", "main"} and self.semantic_depth:
            self._flush_plain()
            self.semantic_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored and self.current is not None:
            self.current.append(data)
        elif not self.ignored and self.semantic_depth:
            self.plain.append(data)


def extract_article_text(body: bytes, content_type: str | None = None) -> str:
    if len(body) > MAX_RESPONSE_BYTES or (content_type and "html" not in content_type.lower()):
        return ""
    parser = _TextParser()
    try:
        parser.feed(_decoded(body, content_type))
        parser.close()
    except ValueError:
        return ""
    return "\n".join(parser.parts)


_FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/feed+json", "application/xml", "text/xml"}


class _FeedLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        values = {name.lower(): (value or "") for name, value in attrs}
        rels = values.get("rel", "").lower().split()
        if "alternate" in rels and values.get("type", "").lower().split(";")[0].strip() in _FEED_TYPES and values.get("href"):
            self.links.append(values["href"])


def discover_feeds(body: bytes, base_url: str, content_type: str | None = None) -> list[str]:
    """Return absolute RSS/Atom URLs a web page advertises with <link rel="alternate">, in page order.

    Lets people paste a media homepage instead of hunting for its feed address.
    """
    if len(body) > MAX_RESPONSE_BYTES:
        return []
    parser = _FeedLinkParser()
    try:
        parser.feed(_decoded(body, content_type))
        parser.close()
    except ValueError:
        return []
    feeds: list[str] = []
    for href in parser.links:
        url = urljoin(base_url, href.strip())
        if _is_http_url(url) and url not in feeds:
            feeds.append(url)
    return feeds[:MAX_CANDIDATES]
