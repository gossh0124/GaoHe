import gaohe.sources as sources
from gaohe.sources import (
    MAX_CANDIDATES,
    MAX_RESPONSE_BYTES,
    extract_article_text,
    parse_feed,
    parse_html_list,
    parse_sitemap,
)


def test_urllib_transport_uses_bounded_request_and_removes_authorization_headers(monkeypatch):
    seen: dict[str, object] = {}

    class Response:
        status = 201
        headers = {"Authorization": "removed", "Content-Type": "text/plain; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://example.test/final"

        def read(self, size):
            seen["read_size"] = size
            return b"ok"

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["agent"] = request.get_header("User-agent")
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(sources, "urlopen", fake_urlopen)

    response = sources.UrllibTransport().fetch("https://example.test/start", timeout_seconds=3.5)

    assert response.status == 201
    assert response.url == "https://example.test/final"
    assert response.headers == {"Content-Type": "text/plain; charset=utf-8"}
    assert response.body == b"ok"
    assert seen == {
        "agent": "GaoHe/0.1 source-monitor (stdlib)",
        "read_size": MAX_RESPONSE_BYTES + 1,
        "timeout": 3.5,
        "url": "https://example.test/start",
    }


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
    assert parse_html_list(b"<a href='/story'>Story</a>", "https://example.test/")
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


def test_extract_article_text_returns_empty_for_oversized_or_non_html_content():
    assert extract_article_text(b"<p>text</p>" * (MAX_RESPONSE_BYTES + 1)) == ""
    assert extract_article_text(b"not an html article", "application/pdf") == ""
