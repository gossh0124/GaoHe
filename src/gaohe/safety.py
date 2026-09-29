"""Single home for URL validation, canonicalization, and secret redaction.

Every module that accepts, stores, or renders a URL or free-form text that may
carry credentials goes through these helpers, so a redaction fix lands once.
"""

import ipaddress
import re
import socket
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


MAX_EVIDENCE_EXCERPT_CHARS = 2_000
MAX_ERROR_CHARS = 500

SENSITIVE_NAME = r"(?:authorization|cookie|token|secret|password|session|api[-_]?key|access[-_]?key)"
# Only header-shaped lines ("Set-Cookie: ...", "X-Api-Key: ..."): a single name token right before the colon,
# so ordinary prose such as "In the special session, the speaker said: ..." survives.
_SENSITIVE_HEADER = re.compile(rf"(?im)^[ \t]*[A-Za-z0-9_-]*{SENSITIVE_NAME}[A-Za-z0-9_-]*[ \t]*:[^\r\n]*")
_SENSITIVE_QUERY = re.compile(rf"(?i)([?&][^=&#\s]*{SENSITIVE_NAME}[^=&#\s]*=)[^&#\s]*")
_SENSITIVE_FRAGMENT = re.compile(rf"(?i)(^|[?&])([^=&#\s]*{SENSITIVE_NAME}[^=&#\s]*=)[^&#\s]*")
_SENSITIVE_VALUE = re.compile(
    rf"(?i)\b(?:{SENSITIVE_NAME}|(?:access|refresh|client)[_-]?(?:token|secret)|passwd|pwd|session(?:[_-]?id)?)\s*=\s*[^\s,;&]+"
)
_BEARER_TOKEN = re.compile(r"(?i)bearer\s+[^\s,;]+")
# Google's REST APIs accept the key as a bare `key=` query parameter.
_KEY_PARAMETER = re.compile(r"(?i)([?&#]key=)[^&#\s]*")
# Google API keys have a fixed, recognizable shape; mask them wherever they appear.
_GOOGLE_API_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{35}")


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
    redacted = _KEY_PARAMETER.sub(r"\1[redacted]", redacted)
    redacted = _GOOGLE_API_KEY.sub("[redacted]", redacted)
    redacted = _SENSITIVE_VALUE.sub(lambda match: match.group(0).split("=", 1)[0] + "=[redacted]", redacted)
    return _BEARER_TOKEN.sub("Bearer [redacted]", redacted)[:limit]


def safe_error(error: str | None) -> str | None:
    """Redact and bound an error message before it is stored or displayed."""
    if error is None:
        return None
    return redact_text(error, MAX_ERROR_CHARS)


# Exact parameter names that carry credentials but are too short to match as substrings.
_EXACT_SENSITIVE_PARAMETERS = frozenset({"key", "auth", "sig", "signature", "x-amz-signature", "x-goog-signature"})


def _is_sensitive_parameter(name: str) -> bool:
    return name.lower() in _EXACT_SENSITIVE_PARAMETERS or re.search(SENSITIVE_NAME, name, re.IGNORECASE) is not None


def _sensitive_value(value: str) -> bool:
    """A decoded value that itself carries a credential (nested URL, `;token=`, key literal)."""
    return redact_text(value, len(value) + 1) != value


def redact_url(value: str) -> str:
    """Mask sensitive query/fragment parameters and strip userinfo from a URL."""
    parsed = urlsplit(value)
    query = urlencode([
        (key, "***" if _is_sensitive_parameter(key) or _sensitive_value(query_value) else query_value)
        for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)
    ])
    fragment = _SENSITIVE_FRAGMENT.sub(r"\1\2***", parsed.fragment)
    fragment = re.sub(r"(?i)(^|[?&])(key=)[^&#\s]*", r"\1\2***", fragment)
    path = _GOOGLE_API_KEY.sub("***", parsed.path)
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], path, query, fragment))


Resolver = Callable[..., list]


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def is_public_http_url(url: object, *, resolver: Resolver = socket.getaddrinfo) -> bool:
    """Whether url is a credential-free HTTP(S) URL whose host resolves only to public addresses.

    GaoHe fetches URLs that come from feeds, search results and redirects; refusing loopback,
    private, link-local and reserved destinations keeps those fetches from reaching the local
    dashboard, the router or cloud metadata services. Resolution failures count as not public.
    """
    if not is_credential_free_http_url(url):
        return False
    host = urlsplit(url).hostname or ""  # type: ignore[arg-type]
    if host.endswith(".") and host != ".":
        host = host[:-1]
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return False
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return _is_public_address(host)
    except ValueError:
        pass
    try:
        infos = resolver(host, None)
    except (OSError, UnicodeError, ValueError):
        return False
    addresses = {info[4][0] for info in infos if info and len(info) > 4 and info[4]}
    return bool(addresses) and all(_is_public_address(address) for address in addresses)
