from __future__ import annotations

import urllib.parse
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NormalizedURL:
    raw_value: str
    normalized_value: str
    normalization_status: str
    normalization_reason: str | None
    domain: str


def normalize_url(raw_url: str) -> NormalizedURL:
    """Conservative, evidence-preserving HTTP URL normalization."""
    raw = str(raw_url or "")
    value = raw.strip()
    if not value:
        return NormalizedURL(raw, "", "missing", "empty_url", "")
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme.casefold() not in {"http", "https"}:
            return NormalizedURL(raw, value, "malformed", "unsupported_or_missing_scheme", "")
        if not parsed.hostname:
            return NormalizedURL(raw, value, "malformed", "missing_hostname", "")
        scheme = parsed.scheme.casefold()
        host = parsed.hostname.casefold().rstrip(".")
        port = parsed.port
        netloc = host
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            netloc = f"{host}:{port}"
        path = parsed.path or "/"
        normalized = urllib.parse.urlunsplit((scheme, netloc, path, parsed.query, ""))
        status = "unchanged" if normalized == value else "normalized"
        reason = None if status == "unchanged" else "transport_syntax_normalized"
        return NormalizedURL(raw, normalized, status, reason, host.removeprefix("www."))
    except (ValueError, UnicodeError) as error:
        return NormalizedURL(raw, value, "malformed", f"{type(error).__name__}:{error}", "")
