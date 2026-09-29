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
