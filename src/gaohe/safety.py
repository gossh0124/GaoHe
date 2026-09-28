"""Single home for URL validation, canonicalization, and secret redaction.

Every module that accepts, stores, or renders a URL or free-form text that may
carry credentials goes through these helpers, so a redaction fix lands once.
"""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


MAX_EVIDENCE_EXCERPT_CHARS = 2_000
MAX_ERROR_CHARS = 500

SENSITIVE_NAME = r"(?:authorization|cookie|token|secret|password|session|api[-_]key)"
_SENSITIVE_HEADER = re.compile(rf"(?im)^[^\r\n:]*?{SENSITIVE_NAME}[^\r\n:]*:\s*[^\r\n]*")
_SENSITIVE_QUERY = re.compile(rf"(?i)([?&][^=&#\s]*{SENSITIVE_NAME}[^=&#\s]*=)[^&#\s]*")
_SENSITIVE_FRAGMENT = re.compile(rf"(?i)(^|[?&])([^=&#\s]*{SENSITIVE_NAME}[^=&#\s]*=)[^&#\s]*")
_SENSITIVE_VALUE = re.compile(
    rf"(?i)\b(?:{SENSITIVE_NAME}|(?:access|refresh|client)[_-]?(?:token|secret)|passwd|pwd|session(?:[_-]?id)?)\s*=\s*[^\s,;&]+"
)
_BEARER_TOKEN = re.compile(r"(?i)bearer\s+[^\s,;]+")


def is_http_url(value: object) -> bool:
    """Return whether value is an absolute HTTP(S) URL with a network location."""
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_credential_free_http_url(value: object) -> bool:
    """Return whether value is an HTTP(S) URL with a hostname and no userinfo."""
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def is_source_url(value: object) -> bool:
    """Stricter check for user-entered media sources: no whitespace, valid port, no userinfo."""
    if not isinstance(value, str) or any(character.isspace() for character in value):
        return False
    try:
        _ = urlsplit(value).port
    except ValueError:
        return False
    return is_credential_free_http_url(value)


def canonical_url(url: object) -> str | None:
    """Lower-case scheme/host and drop the fragment; None for non-HTTP(S) or credentialed URLs."""
    if not is_credential_free_http_url(url):
        return None
    parsed = urlsplit(url)  # type: ignore[arg-type]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def redact_text(value: object, limit: int = MAX_EVIDENCE_EXCERPT_CHARS) -> str:
    """Keep text bounded without persisting common credential forms."""
    if not isinstance(value, str) or limit < 1:
        return ""
    redacted = _SENSITIVE_HEADER.sub("[redacted]", value)
    redacted = _SENSITIVE_QUERY.sub(r"\1[redacted]", redacted)
    redacted = _SENSITIVE_VALUE.sub(lambda match: match.group(0).split("=", 1)[0] + "=[redacted]", redacted)
    return _BEARER_TOKEN.sub("Bearer [redacted]", redacted)[:limit]


def safe_error(error: str | None) -> str | None:
    """Redact and bound an error message before it is stored or displayed."""
    if error is None:
        return None
    return redact_text(error, MAX_ERROR_CHARS)


def redact_url(value: str) -> str:
    """Mask sensitive query/fragment parameters and strip userinfo from a URL."""
    parsed = urlsplit(value)
    query = urlencode([
        (key, "***" if re.search(SENSITIVE_NAME, key, re.IGNORECASE) else query_value)
        for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)
    ])
    fragment = _SENSITIVE_FRAGMENT.sub(r"\1\2***", parsed.fragment)
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, query, fragment))
