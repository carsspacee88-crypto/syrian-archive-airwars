from __future__ import annotations

import hashlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from archive_engine.models import CaptureAttempt, utc_now
from archive_engine.statuses import CollectionStatus


@dataclass(slots=True)
class FetchResponse:
    requested_url: str
    final_url: str
    status_code: int | None
    content_type: str
    body: bytes
    outcome: CollectionStatus
    attempts: list[CaptureAttempt] = field(default_factory=list)
    error: str | None = None


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchResponse: ...


def classify_http_result(status: int | None, error: str | None = None) -> CollectionStatus:
    if status is not None and 200 <= status < 300:
        return CollectionStatus.FETCHED
    if status in {401, 403, 429, 451}:
        return CollectionStatus.BLOCKED
    if status in {404, 410}:
        return CollectionStatus.DEAD
    if status is not None and status >= 500:
        return CollectionStatus.RETRYABLE_FAILURE
    if error:
        folded = error.casefold()
        if "timeout" in folded or "tempor" in folded:
            return CollectionStatus.RETRYABLE_FAILURE
    return CollectionStatus.FINAL_FAILURE


class HttpFetcher:
    """Small generic fetcher with per-host pacing, retries, and exact attempts."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        retries: int = 2,
        per_host_delay: float = 0.2,
        user_agent: str = "TextualArchiveEngine/1.0 (+public archival research)",
        allowed_domains: set[str] | None = None,
    ):
        self.timeout = timeout
        self.retries = retries
        self.per_host_delay = per_host_delay
        self.user_agent = user_agent
        self.allowed_domains = {item.casefold().removeprefix("www.") for item in (allowed_domains or set())}
        self._lock = threading.Lock()
        self._last_request: dict[str, float] = {}

    def _assert_allowed(self, url: str) -> None:
        host = (urllib.parse.urlsplit(url).hostname or "").casefold().removeprefix("www.")
        if not host:
            raise ValueError("malformed_target_url")
        if self.allowed_domains and not any(host == allowed or host.endswith("." + allowed) for allowed in self.allowed_domains):
            raise PermissionError(f"domain_not_allowed:{host}")

    def _pace(self, host: str) -> None:
        with self._lock:
            wait = self.per_host_delay - (time.monotonic() - self._last_request.get(host, 0.0))
            if wait > 0:
                time.sleep(wait)
            self._last_request[host] = time.monotonic()

    def fetch(self, url: str) -> FetchResponse:
        self._assert_allowed(url)
        host = (urllib.parse.urlsplit(url).hostname or "").casefold()
        attempts: list[CaptureAttempt] = []
        last_error: str | None = None
        for attempt_number in range(1, self.retries + 2):
            self._pace(host)
            started = time.monotonic()
            status: int | None = None
            final_url = url
            content_type = ""
            body = b""
            try:
                request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    status = int(response.status)
                    final_url = response.geturl()
                    content_type = response.headers.get("Content-Type", "")
                    body = response.read()
                error = None
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                final_url = exc.geturl() or url
                content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
                body = exc.read(64 * 1024)
                error = f"HTTPError:{status}"
            except Exception as exc:  # noqa: BLE001 - exact failure is captured as evidence
                error = f"{type(exc).__name__}:{exc}"
            elapsed = round(time.monotonic() - started, 6)
            outcome = classify_http_result(status, error)
            digest = hashlib.sha256(body).hexdigest() if body else None
            attempts.append(CaptureAttempt(
                url=url,
                attempted_at=utc_now(),
                status_code=status,
                outcome=outcome,
                elapsed_seconds=elapsed,
                error=error,
                response_hash=digest,
                final_url=final_url,
            ))
            if outcome in {CollectionStatus.FETCHED, CollectionStatus.BLOCKED, CollectionStatus.DEAD, CollectionStatus.FINAL_FAILURE}:
                return FetchResponse(url, final_url, status, content_type, body, outcome, attempts, error)
            last_error = error
            if attempt_number <= self.retries:
                time.sleep(min(2 ** (attempt_number - 1), 8))
        return FetchResponse(url, url, None, "", b"", CollectionStatus.RETRYABLE_FAILURE, attempts, last_error)
