import pytest

from gaohe.safety import canonical_url, is_credential_free_http_url, is_http_url, is_source_url, redact_text, redact_url, safe_error


FAKE_GOOGLE_KEY = "AIza" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r"


def test_redact_url_masks_bare_key_parameter_used_by_google_rest_apis():
    url = f"https://generativelanguage.googleapis.com/v1beta/models/m:generateContent?key={FAKE_GOOGLE_KEY}&alt=json"
    redacted = redact_url(url)
    assert FAKE_GOOGLE_KEY not in redacted
    assert "key=%2A%2A%2A" in redacted and "alt=json" in redacted


def test_redact_url_keeps_parameters_that_only_contain_key_as_a_substring():
    assert redact_url("https://news.test/a?keyword=abc&monkey=1") == "https://news.test/a?keyword=abc&monkey=1"


@pytest.mark.parametrize("text", [
    f"HTTP Error 400 for https://x.test/v1?key={FAKE_GOOGLE_KEY}&a=1",
    f"fragment https://x.test/v1#key={FAKE_GOOGLE_KEY}",
    f"a bare key {FAKE_GOOGLE_KEY} in prose",
])
def test_redact_text_masks_key_parameters_and_google_key_literals(text):
    assert FAKE_GOOGLE_KEY not in redact_text(text)
    assert FAKE_GOOGLE_KEY not in safe_error(text)


def test_redact_url_masks_google_key_literals_in_other_values_and_fragments():
    url = f"https://x.test/{FAKE_GOOGLE_KEY}/p?q={FAKE_GOOGLE_KEY}#key={FAKE_GOOGLE_KEY}"
    assert FAKE_GOOGLE_KEY not in redact_url(url)


def test_url_predicates():
    assert is_http_url("https://example.test/a") and not is_http_url("ftp://example.test") and not is_http_url("http://[")
    assert not is_credential_free_http_url("https://user:pass@example.test/")
    assert not is_source_url("https://example.test/a b") and not is_source_url("https://example.test:99999/")
    assert canonical_url("HTTPS://Example.TEST/Path?q=1#frag") == "https://example.test/Path?q=1"
    assert canonical_url("https://u:p@example.test/") is None


@pytest.mark.parametrize("url, secret", [
    ("https://wire.example.com/v1/rss?apiKey=WIRE-SECRET-123&lang=zh", "WIRE-SECRET-123"),
    ("https://x.test/a?access_key=AK-SECRET", "AK-SECRET"),
    ("https://x.test/a?sig=SIG-SECRET&auth=AUTH-SECRET", "SIG-SECRET"),
    ("https://x.test/a?a=1;token=SEMI-SECRET", "SEMI-SECRET"),
    ("https://x.test/a?next=https%3A%2F%2Fy.test%2F%3Fapi_key%3DNESTED-SECRET", "NESTED-SECRET"),
])
def test_redact_url_masks_compact_names_nested_and_semicolon_secrets(url, secret):
    assert secret not in redact_url(url)
    assert "AUTH-SECRET" not in redact_url("https://x.test/a?auth=AUTH-SECRET")


def test_header_rule_keeps_ordinary_prose_but_masks_header_lines():
    prose = "In the special session, the speaker said: the council approved 1000 new troops."
    assert redact_text(prose) == prose
    assert redact_text("立法院臨時會 session 中，院長表示：通過預算。") == "立法院臨時會 session 中，院長表示：通過預算。"
    assert "abc123" not in redact_text("Set-Cookie: sid=abc123\nX-Api-Key: abc123\nAuthorization: Bearer abc123")


def _resolver(mapping):
    def resolve(host, port):
        if host not in mapping:
            raise OSError("no such host")
        return [(2, 1, 6, "", (address, 0)) for address in mapping[host]]
    return resolve


@pytest.mark.parametrize("url, expected", [
    ("https://news.example/a", True),
    ("http://127.0.0.1:8000/", False),
    ("http://localhost/", False),
    ("http://[::1]/", False),
    ("http://169.254.169.254/latest/meta-data", False),
    ("http://10.0.0.5/", False),
    ("http://192.168.1.1/", False),
    ("http://[::ffff:127.0.0.1]/", False),
    ("http://internal.example/", False),
    ("http://missing.example/", False),
    ("https://u:p@news.example/", False),
    ("ftp://news.example/", False),
])
def test_is_public_http_url_refuses_local_private_and_unresolvable_hosts(url, expected):
    from gaohe.safety import is_public_http_url

    resolver = _resolver({"news.example": ["93.184.216.34"], "internal.example": ["93.184.216.34", "10.1.2.3"]})
    assert is_public_http_url(url, resolver=resolver) is expected
