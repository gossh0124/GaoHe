from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import gaohe.sources as sources
import pytest
from gaohe.sources import (
    MAX_CANDIDATES,
    MAX_RESPONSE_BYTES,
    extract_article_text,
    parse_feed,
    parse_html_list,
    parse_sitemap,
)


class _LocalSite:
    """A loopback HTTP server: GET /feed -> 200, /redirect -> 302 to a private address, /secret -> 200."""

    def __enter__(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://10.0.0.1/internal")
                    self.end_headers()
                    return
                body = b"<rss></rss>" if self.path == "/feed" else b"x" * (sources.MAX_RESPONSE_BYTES + 1)
                self.send_response(200)
                self.send_header("Content-Type", "application/rss+xml")
                self.send_header("Authorization", "Bearer leaked")
                self.send_header("Set-Cookie", "sid=leaked")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def test_urllib_transport_refuses_private_destinations_by_default():
    with pytest.raises(sources.BlockedDestination):
        sources.UrllibTransport().fetch("http://127.0.0.1:9/feed")
    with pytest.raises(sources.BlockedDestination):
        sources.UrllibTransport().fetch("http://169.254.169.254/latest/meta-data")


def test_urllib_transport_strips_credential_headers_and_bounds_bodies():
    with _LocalSite() as site:
        transport = sources.UrllibTransport(allow_private=True)
        feed = transport.fetch(site.base + "/feed")
        big = transport.fetch(site.base + "/big")
    assert feed.status == 200 and feed.body == b"<rss></rss>"
    assert {"authorization", "set-cookie"}.isdisjoint(key.lower() for key in feed.headers)
    assert big.status == 200 and big.body == b""
    assert "GaoHe/" in sources.USER_AGENT


def test_urllib_transport_refuses_redirects_to_private_addresses(monkeypatch):
    with _LocalSite() as site:
        # allow the loopback test server itself, but still check where its redirect points
        monkeypatch.setattr(sources, "is_public_http_url", lambda url: url.startswith(site.base))
        with pytest.raises(sources.BlockedDestination):
            sources.UrllibTransport().fetch(site.base + "/redirect")


def test_parse_rss_preserves_article_metadata_and_rejects_relative_links():
    candidates = parse_feed(
        b'''<?xml version="1.0"?><rss><channel><title>Example News</title>
        <item><title>First story</title><link>https://example.test/news/first</link>
        <pubDate>Thu, 18 Sep 2026 10:00:00 +0000</pubDate></item>
        <item><title>Relative</title><link>/news/relative</link></item></channel></rss>''',
        "https://example.test/feed.xml",
    )

    assert [(item.url, item.title, item.published_at) for item in candidates] == [
        ("https://example.test/news/first", "First story", "Thu, 18 Sep 2026 10:00:00 +0000")
    ]
    assert candidates[0].source_id == 0
    assert candidates[0].metadata == {
        "discovery_type": "rss",
        "source_title": "Example News",
        "source_url": "https://example.test/feed.xml",
    }


def test_parse_atom_uses_link_href_and_explicit_namespaces():
    candidates = parse_feed(
        b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
        <title>Atom Press</title><entry><title>Atom story</title>
        <link rel="alternate" href="https://example.test/atom-story" />
        <updated>2026-09-18T10:00:00Z</updated></entry></feed>''',
        "https://example.test/atom.xml",
    )

    assert [(item.url, item.title, item.published_at) for item in candidates] == [
        ("https://example.test/atom-story", "Atom story", "2026-09-18T10:00:00Z")
    ]
    assert candidates[0].metadata["discovery_type"] == "atom"
    assert candidates[0].metadata["source_title"] == "Atom Press"


def test_parse_sitemap_keeps_lastmod_and_ignores_non_http_urls():
    candidates = parse_sitemap(
        b'''<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://example.test/story</loc><lastmod>2026-09-18</lastmod></url>
        <url><loc>mailto:editor@example.test</loc></url></urlset>''',
        "https://example.test/sitemap.xml",
    )

    assert [(item.url, item.title, item.published_at) for item in candidates] == [
        ("https://example.test/story", "", "2026-09-18")
    ]
    assert candidates[0].metadata == {
        "discovery_type": "sitemap",
        "source_url": "https://example.test/sitemap.xml",
    }


def test_parse_html_list_resolves_relative_links_and_ignores_navigation():
    candidates = parse_html_list(
        b'''<html><body><nav><a href="/about">About</a></nav><main>
        <article><a href="/story">Local story</a><time datetime="2026-09-18">today</time></article>
        <li><a href="https://example.test/second">Second story</a></li>
        </main></body></html>''',
        "https://example.test/news/",
    )

    assert [(item.url, item.title, item.published_at) for item in candidates] == [
        ("https://example.test/story", "Local story", "2026-09-18"),
        ("https://example.test/second", "Second story", None),
    ]
    assert all(item.metadata["discovery_type"] == "html_list" for item in candidates)


def test_invalid_or_oversized_discovery_payloads_return_no_candidates():
    assert parse_feed(b"<rss>", "https://example.test/feed") == []
    assert parse_sitemap(b"<urlset>", "https://example.test/sitemap") == []
    assert parse_html_list(b"<a href='mailto:editor@example.test'>Story</a>", "https://example.test/") == []
    assert parse_feed(b"x" * (MAX_RESPONSE_BYTES + 1), "https://example.test/feed") == []


def test_candidate_limit_is_enforced_for_large_html_lists():
    body = b"".join(
        f'<a href="/story-{number}">Story {number}</a>'.encode()
        for number in range(MAX_CANDIDATES + 1)
    )

    candidates = parse_html_list(body, "https://example.test/")

    assert len(candidates) == MAX_CANDIDATES
    assert candidates[-1].url == f"https://example.test/story-{MAX_CANDIDATES - 1}"


def test_extract_article_text_decodes_charset_and_drops_hidden_chrome():
    body = (
        '<html><head><meta charset="iso-8859-1"><style>ignore me</style></head><body>'
        '<nav>Navigation</nav><main><h1>Headline</h1><p>Caf\xe9   story</p>'
        '<ul><li>First point</li><li>Second point</li></ul><script>secret()</script>'
        '<form>Search</form></main></body></html>'
    ).encode("iso-8859-1")

    assert extract_article_text(body, "text/html; charset=iso-8859-1") == (
        "Headline\nCaf\xe9 story\nFirst point\nSecond point"
    )


def test_extract_article_text_keeps_plain_semantic_container_text_without_duplicate_children():
    body = b"<main>Plain main text <span>with inline text</span><p>Child paragraph</p></main>"

    assert extract_article_text(body) == "Plain main text with inline text\nChild paragraph"


def test_extract_article_text_keeps_article_div_text_before_structured_child():
    body = b"<article><div>Article div text</div><p>Child paragraph</p></article>"

    assert extract_article_text(body) == "Article div text\nChild paragraph"


def test_extract_article_text_returns_empty_for_oversized_or_non_html_content():
    assert extract_article_text(b"<p>text</p>" * (MAX_RESPONSE_BYTES + 1)) == ""
    assert extract_article_text(b"not an html article", "application/pdf") == ""
