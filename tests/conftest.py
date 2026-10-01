"""Shared deterministic fakes: no network, no real Gemini, no sleeping."""

import json

import pytest

from gaohe.domain import ArticleRevision, article_content_hash
from gaohe.sources import HttpResponse
from gaohe.storage import Store


ARTICLE_TEXT = "行政院今天宣布，自下月起開放外籍旅客入境觀光。官員表示，政策將使觀光收入增加三成。"


class FakeTransport:
    """url -> HttpResponse (or an Exception to raise); records every request."""

    def __init__(self, pages):
        self.pages = dict(pages)
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        page = self.pages.get(url)
        if isinstance(page, Exception):
            raise page
        return page or HttpResponse(404, url, {}, b"")


def html(title, *paragraphs):
    body = "".join(f"<p>{text}</p>" for text in paragraphs)
    return f"<html><head><title>{title}</title></head><body><article>{body}</article></body></html>".encode()


def html_response(url, title, *paragraphs):
    return HttpResponse(200, url, {"Content-Type": "text/html; charset=utf-8"}, html(title, *paragraphs))


def rss(*items):
    entries = "".join(
        f"<item><title>{title}</title><link>{link}</link>{f'<pubDate>{date}</pubDate>' if date else ''}</item>"
        for title, link, date in items
    )
    return f"<?xml version='1.0'?><rss><channel><title>Feed</title>{entries}</channel></rss>".encode()


def gemini_text(value):
    """A generateContent response whose single text part is value encoded as JSON."""
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(value, ensure_ascii=False)}]}}]}


class FakeGemini:
    """Routes generateContent payloads to canned answers by kind: analysis, assessment, search."""

    def __init__(self, analysis=None, assessment=None, search_uris=(), error=None):
        self.analysis = analysis or {"claims": [], "candidates": []}
        self.assessment = assessment
        self.search_uris = list(search_uris)
        self.error = error
        self.calls = []

    def __call__(self, model, payload, api_key):
        schema = json.dumps(payload.get("generationConfig", {}).get("responseSchema", {}))
        kind = "search" if "tools" in payload else ("assessment" if "evidence_quote" in schema else "analysis")
        self.calls.append((kind, model, api_key, payload))
        if self.error is not None:
            raise self.error
        if kind == "search":
            chunks = [{"web": {"uri": uri, "title": f"source {index}"}} for index, uri in enumerate(self.search_uris)]
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "groundingMetadata": {"groundingChunks": chunks}}]}
        if kind == "assessment":
            value = self.assessment(payload) if callable(self.assessment) else self.assessment
            return gemini_text(value or {"evidence_quote": "", "rationale": "無關", "relation": "irrelevant"})
        return gemini_text(self.analysis)


def revision(text=ARTICLE_TEXT, revision_id=1, url="https://news.example/a1", title="開放觀光"):
    return ArticleRevision(revision_id, 1, url, title, text, article_content_hash(title, text), "2026-10-01T00:00:00Z")


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "gaohe.db")
    instance.initialize()
    return instance


@pytest.fixture
def public_resolver():
    return lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))]
