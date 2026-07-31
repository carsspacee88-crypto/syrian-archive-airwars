from __future__ import annotations

import asyncio
import json
import gzip
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .io_utils import sha256_bytes, utc_now


USER_AGENT = (
    "SyrianArchiveAirwars/1.0 "
    "(+https://github.com/carsspacee88-crypto/syrian-archive-airwars; archival research)"
)


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    content_type: str
    body: bytes
    retrieved_at: str
    elapsed_seconds: float
    response_content_encoding: str = ""
    body_decoded_from: str | None = None
    error: str | None = None
    attempts: int = 1
    waiting_seconds: float = 0.0
    attempt_history: list[dict[str, Any]] | None = None
    etag: str = ""
    last_modified: str = ""
    circuit_open_reason: str = ""
    circuit_open_status: int | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300 and bool(self.body)

    @property
    def content_hash(self) -> str | None:
        return sha256_bytes(self.body) if self.body else None

    def metadata(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("body", None)
        data["content_hash"] = self.content_hash
        data["bytes"] = len(self.body)
        return data


class RespectfulFetcher:
    def __init__(
        self,
        delay_seconds: float = 1.25,
        timeout_seconds: float = 45.0,
        retries: int = 3,
        max_bytes: int = 12 * 1024 * 1024,
    ):
        self.delay_seconds = max(0.0, delay_seconds)
        self.timeout_seconds = timeout_seconds
        self.retries = max(1, retries)
        self.max_bytes = max_bytes
        self._last_request_at = 0.0
        self.last_wayback_lookup: dict[str, Any] | None = None
        self.total_waiting_seconds = 0.0
        self.total_requests = 0
        self.total_retries = 0

    def _wait(self) -> float:
        remaining = self.delay_seconds - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)
            self.total_waiting_seconds += remaining
            return remaining
        return 0.0

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float:
        if not value:
            return 0.0
        try:
            return max(0.0, min(float(value), 120.0))
        except ValueError:
            try:
                target = parsedate_to_datetime(value).timestamp()
                return max(0.0, min(target - time.time(), 120.0))
            except (TypeError, ValueError, OverflowError):
                return 0.0

    def fetch(self, url: str, accept: str = "text/html,application/json;q=0.9,*/*;q=0.1") -> FetchResult:
        last: FetchResult | None = None
        history: list[dict[str, Any]] = []
        waiting = 0.0
        for attempt in range(1, self.retries + 1):
            waiting += self._wait()
            started = time.monotonic()
            self.total_requests += 1
            if attempt > 1:
                self.total_retries += 1
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": accept,
                    "Accept-Encoding": "identity",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read(self.max_bytes + 1)
                    if len(body) > self.max_bytes:
                        raise ValueError(f"response_exceeds_{self.max_bytes}_bytes")
                    content_encoding = response.headers.get("Content-Encoding", "")
                    decoded_from = None
                    if body.startswith(b"\x1f\x8b"):
                        body = gzip.decompress(body)
                        decoded_from = "gzip"
                        if len(body) > self.max_bytes:
                            raise ValueError(f"decoded_response_exceeds_{self.max_bytes}_bytes")
                    result = FetchResult(
                        url=url,
                        final_url=response.geturl(),
                        status=response.status,
                        content_type=response.headers.get("Content-Type", ""),
                        body=body,
                        retrieved_at=utc_now(),
                        elapsed_seconds=round(time.monotonic() - started, 3),
                        response_content_encoding=content_encoding,
                        body_decoded_from=decoded_from,
                        attempts=attempt,
                        waiting_seconds=round(waiting, 3),
                    )
                    history.append({
                        "attempt": attempt,
                        "retrieved_at": result.retrieved_at,
                        "status": result.status,
                        "elapsed_seconds": result.elapsed_seconds,
                        "result": "successful",
                    })
                    result.attempt_history = history
                    self._last_request_at = time.monotonic()
                    return result
            except urllib.error.HTTPError as error:
                body = error.read(min(self.max_bytes, 256 * 1024))
                last = FetchResult(
                    url=url,
                    final_url=error.geturl() or url,
                    status=error.code,
                    content_type=error.headers.get("Content-Type", "") if error.headers else "",
                    body=body,
                    retrieved_at=utc_now(),
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    error=f"http_{error.code}",
                    attempts=attempt,
                    waiting_seconds=round(waiting, 3),
                )
                history.append({
                    "attempt": attempt,
                    "retrieved_at": last.retrieved_at,
                    "status": last.status,
                    "elapsed_seconds": last.elapsed_seconds,
                    "result": "http_error",
                    "error": last.error,
                })
                last.attempt_history = list(history)
                self._last_request_at = time.monotonic()
                if error.code not in {429, 500, 502, 503, 504}:
                    return last
                retry_after = self._retry_after_seconds(error.headers.get("Retry-After") if error.headers else None)
                if retry_after:
                    time.sleep(retry_after)
                    waiting += retry_after
                    self.total_waiting_seconds += retry_after
            except Exception as error:  # network and parser-safe boundary
                last = FetchResult(
                    url=url,
                    final_url=url,
                    status=None,
                    content_type="",
                    body=b"",
                    retrieved_at=utc_now(),
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    error=f"{type(error).__name__}: {error}",
                    attempts=attempt,
                    waiting_seconds=round(waiting, 3),
                )
                history.append({
                    "attempt": attempt,
                    "retrieved_at": last.retrieved_at,
                    "status": None,
                    "elapsed_seconds": last.elapsed_seconds,
                    "result": "timed_out" if "timeout" in str(error).casefold() else "network_error",
                    "error": last.error,
                })
                last.attempt_history = list(history)
                self._last_request_at = time.monotonic()
            if attempt < self.retries:
                backoff = (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)
                time.sleep(backoff)
                waiting += backoff
                self.total_waiting_seconds += backoff
        assert last is not None
        last.waiting_seconds = round(waiting, 3)
        last.attempt_history = history
        return last

    def latest_wayback_capture(self, target_url: str) -> dict[str, str] | None:
        params = [
            ("url", target_url),
            ("output", "json"),
            ("fl", "timestamp,original,statuscode,digest,mimetype"),
            ("filter", "statuscode:200"),
            ("filter", "mimetype:text/html"),
            ("collapse", "digest"),
            ("limit", "-10"),
        ]
        cdx_url = "https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(params)
        result = self.fetch(cdx_url, accept="application/json")
        self.last_wayback_lookup = result.metadata()
        self.last_wayback_lookup["ok"] = result.ok
        if not result.ok:
            return None
        try:
            rows = json.loads(result.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if len(rows) < 2:
            self.last_wayback_lookup["lookup_result"] = "no_capture"
            return None
        headers = rows[0]
        captures = [dict(zip(headers, row)) for row in rows[1:] if len(row) == len(headers)]
        if not captures:
            self.last_wayback_lookup["lookup_result"] = "no_capture"
            return None
        capture = max(captures, key=lambda row: row.get("timestamp", ""))
        capture["cdx_url"] = cdx_url
        capture["replay_url"] = (
            f"https://web.archive.org/web/{capture['timestamp']}id_/" + target_url
        )
        self.last_wayback_lookup["lookup_result"] = "capture_found"
        return capture


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").casefold().removeprefix("www.")


def _circuit_failure(result: FetchResult) -> bool:
    if result.error and result.error.startswith("host_circuit_open"):
        return False
    return result.status is None or result.status in {401, 403, 408, 425, 429, 500, 502, 503, 504}


class HostCircuitBreaker:
    """Small serializable circuit breaker shared by sync and async collectors."""

    def __init__(
        self,
        threshold: int = 3,
        cooldown_seconds: float = 1800.0,
        reprobe_every: int = 100,
        state: dict[str, Any] | None = None,
    ):
        self.threshold = max(1, threshold)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self.reprobe_every = max(1, reprobe_every)
        self.state: dict[str, dict[str, Any]] = {
            str(host): dict(value)
            for host, value in (state or {}).items()
            if isinstance(value, dict)
        }

    def _entry(self, host: str) -> dict[str, Any]:
        return self.state.setdefault(host, {
            "consecutive_failures": 0,
            "opened_at_epoch": None,
            "suppressed_requests": 0,
            "opens": 0,
            "last_result": None,
            "open_reason": None,
            "open_status": None,
        })

    def open_context(self, url: str) -> tuple[str, int | None]:
        entry = self._entry(_host(url))
        reason = str(entry.get("open_reason") or "repeated_block_or_timeout")
        status = entry.get("open_status")
        return reason, int(status) if isinstance(status, int) else None

    def allow(self, url: str) -> bool:
        host = _host(url)
        if not host:
            return True
        entry = self._entry(host)
        opened_at = entry.get("opened_at_epoch")
        if opened_at is None:
            return True
        elapsed = time.time() - float(opened_at)
        suppressed = int(entry.get("suppressed_requests") or 0)
        if elapsed >= self.cooldown_seconds or suppressed >= self.reprobe_every:
            entry["opened_at_epoch"] = None
            entry["suppressed_requests"] = 0
            entry["last_result"] = "half_open_probe"
            return True
        entry["suppressed_requests"] = suppressed + 1
        entry["last_result"] = "suppressed"
        return False

    def note(self, url: str, failed: bool, result: str | None = None) -> None:
        host = _host(url)
        if not host:
            return
        entry = self._entry(host)
        entry["last_result"] = result or ("failed" if failed else "successful")
        entry["last_checked_at"] = utc_now()
        if not failed:
            entry["consecutive_failures"] = 0
            entry["opened_at_epoch"] = None
            entry["suppressed_requests"] = 0
            entry["open_reason"] = None
            entry["open_status"] = None
            return
        entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
        if entry["consecutive_failures"] >= self.threshold and entry.get("opened_at_epoch") is None:
            entry["opened_at_epoch"] = time.time()
            entry["suppressed_requests"] = 0
            entry["opens"] = int(entry.get("opens") or 0) + 1
            entry["open_reason"] = result or "repeated_failure"
            match = re.fullmatch(r"http_(\d{3})", result or "")
            entry["open_status"] = int(match.group(1)) if match else None

    def snapshot(self) -> dict[str, Any]:
        return {host: dict(value) for host, value in sorted(self.state.items())}


class CircuitBreakingFetcher:
    """Respectful synchronous fetcher that skips repeated failures by hostname."""

    def __init__(
        self,
        delay_seconds: float = 0.75,
        timeout_seconds: float = 10.0,
        retries: int = 1,
        max_bytes: int = 12 * 1024 * 1024,
        circuit_state: dict[str, Any] | None = None,
        circuit_threshold: int = 3,
        circuit_reprobe_every: int = 100,
    ):
        self.inner = RespectfulFetcher(delay_seconds, timeout_seconds, retries, max_bytes)
        self.circuit = HostCircuitBreaker(
            threshold=circuit_threshold,
            reprobe_every=circuit_reprobe_every,
            state=circuit_state,
        )
        self.last_wayback_lookup: dict[str, Any] | None = None
        self.circuit_skips = 0

    def fetch(self, url: str, accept: str = "text/html,application/json;q=0.9,*/*;q=0.1") -> FetchResult:
        if not self.circuit.allow(url):
            self.circuit_skips += 1
            reason, status = self.circuit.open_context(url)
            return FetchResult(
                url=url,
                final_url=url,
                status=None,
                content_type="",
                body=b"",
                retrieved_at=utc_now(),
                elapsed_seconds=0.0,
                error="host_circuit_open_after_repeated_block_or_timeout",
                attempts=0,
                attempt_history=[{"attempt": 0, "result": "host_circuit_open", "host": _host(url)}],
                circuit_open_reason=reason,
                circuit_open_status=status,
            )
        result = self.inner.fetch(url, accept=accept)
        self.circuit.note(url, _circuit_failure(result), result.error or str(result.status))
        return result

    def latest_wayback_capture(self, target_url: str) -> dict[str, str] | None:
        params = [
            ("url", target_url),
            ("output", "json"),
            ("fl", "timestamp,original,statuscode,digest,mimetype"),
            ("filter", "statuscode:200"),
            ("filter", "mimetype:text/html"),
            ("collapse", "digest"),
            ("limit", "-10"),
        ]
        cdx_url = "https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(params)
        result = self.fetch(cdx_url, accept="application/json")
        self.last_wayback_lookup = result.metadata()
        self.last_wayback_lookup["ok"] = result.ok
        if not result.ok:
            return None
        try:
            rows = json.loads(result.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.last_wayback_lookup["lookup_result"] = "invalid_response"
            return None
        if len(rows) < 2:
            self.last_wayback_lookup["lookup_result"] = "no_capture"
            return None
        headers = rows[0]
        captures = [dict(zip(headers, row)) for row in rows[1:] if len(row) == len(headers)]
        if not captures:
            self.last_wayback_lookup["lookup_result"] = "no_capture"
            return None
        capture = max(captures, key=lambda row: row.get("timestamp", ""))
        capture["cdx_url"] = cdx_url
        capture["replay_url"] = f"https://web.archive.org/web/{capture['timestamp']}id_/" + target_url
        self.last_wayback_lookup["lookup_result"] = "capture_found"
        return capture

    def stats(self) -> dict[str, Any]:
        return {
            "requests": self.inner.total_requests,
            "retries": self.inner.total_retries,
            "waiting_seconds": round(self.inner.total_waiting_seconds, 3),
            "circuit_skips": self.circuit_skips,
            "circuit_state": self.circuit.snapshot(),
        }


class AsyncHostFetcher:
    """Connection-pooled async fetcher with per-host pacing and persisted circuits."""

    def __init__(
        self,
        delay_seconds: float = 0.75,
        timeout_seconds: float = 10.0,
        retries: int = 1,
        max_bytes: int = 25 * 1024 * 1024,
        workers: int = 6,
        per_host_workers: int = 1,
        circuit_state: dict[str, Any] | None = None,
        circuit_threshold: int = 3,
        circuit_reprobe_every: int = 100,
    ):
        self.delay_seconds = max(0.0, delay_seconds)
        self.timeout_seconds = timeout_seconds
        self.retries = max(1, retries)
        self.max_bytes = max_bytes
        self.workers = max(1, workers)
        self.per_host_workers = max(1, per_host_workers)
        self.circuit = HostCircuitBreaker(
            threshold=circuit_threshold,
            reprobe_every=circuit_reprobe_every,
            state=circuit_state,
        )
        self._global = asyncio.Semaphore(self.workers)
        self._host_semaphores: dict[str, asyncio.Semaphore] = {}
        self._last_request_at: dict[str, float] = {}
        self._client: httpx.AsyncClient | None = None
        self.total_requests = 0
        self.total_retries = 0
        self.total_waiting_seconds = 0.0
        self.circuit_skips = 0

    async def __aenter__(self) -> "AsyncHostFetcher":
        limits = httpx.Limits(max_connections=self.workers * 2, max_keepalive_connections=self.workers)
        timeout = httpx.Timeout(
            connect=min(self.timeout_seconds, 10.0),
            read=self.timeout_seconds,
            write=self.timeout_seconds,
            pool=self.timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
            follow_redirects=True,
            limits=limits,
            timeout=timeout,
            trust_env=False,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _host_semaphore(self, host: str) -> asyncio.Semaphore:
        if host not in self._host_semaphores:
            self._host_semaphores[host] = asyncio.Semaphore(self.per_host_workers)
        return self._host_semaphores[host]

    async def _wait_for_host(self, host: str) -> float:
        remaining = self.delay_seconds - (time.monotonic() - self._last_request_at.get(host, 0.0))
        if remaining > 0:
            await asyncio.sleep(remaining)
            self.total_waiting_seconds += remaining
            return remaining
        return 0.0

    async def fetch(self, url: str, accept: str = "text/html,application/json;q=0.9,*/*;q=0.1") -> FetchResult:
        host = _host(url)
        if self._client is None:
            raise RuntimeError("AsyncHostFetcher must be used as an async context manager")
        last: FetchResult | None = None
        history: list[dict[str, Any]] = []
        waiting = 0.0
        # Acquire the host slot first so queued requests to one busy domain do not
        # consume every global worker. Re-check the circuit only after that slot is
        # acquired; otherwise hundreds of already-scheduled tasks can slip through.
        async with self._host_semaphore(host), self._global:
            if not self.circuit.allow(url):
                self.circuit_skips += 1
                reason, status = self.circuit.open_context(url)
                return FetchResult(
                    url=url,
                    final_url=url,
                    status=None,
                    content_type="",
                    body=b"",
                    retrieved_at=utc_now(),
                    elapsed_seconds=0.0,
                    error="host_circuit_open_after_repeated_block_or_timeout",
                    attempts=0,
                    attempt_history=[{"attempt": 0, "result": "host_circuit_open", "host": host}],
                    circuit_open_reason=reason,
                    circuit_open_status=status,
                )
            for attempt in range(1, self.retries + 1):
                waiting += await self._wait_for_host(host)
                started = time.monotonic()
                self.total_requests += 1
                if attempt > 1:
                    self.total_retries += 1
                try:
                    async with self._client.stream("GET", url, headers={"Accept": accept}) as response:
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > self.max_bytes:
                                raise ValueError(f"response_exceeds_{self.max_bytes}_bytes")
                        payload = bytes(body)
                        last = FetchResult(
                            url=url,
                            final_url=str(response.url),
                            status=response.status_code,
                            content_type=response.headers.get("Content-Type", ""),
                            body=payload,
                            retrieved_at=utc_now(),
                            elapsed_seconds=round(time.monotonic() - started, 3),
                            error=None if 200 <= response.status_code < 300 else f"http_{response.status_code}",
                            attempts=attempt,
                            waiting_seconds=round(waiting, 3),
                            etag=response.headers.get("ETag", ""),
                            last_modified=response.headers.get("Last-Modified", ""),
                        )
                        history.append({
                            "attempt": attempt,
                            "retrieved_at": last.retrieved_at,
                            "status": last.status,
                            "elapsed_seconds": last.elapsed_seconds,
                            "result": "successful" if last.ok else "http_error",
                            "error": last.error,
                        })
                        retry_after = RespectfulFetcher._retry_after_seconds(response.headers.get("Retry-After"))
                    self._last_request_at[host] = time.monotonic()
                    if last.ok or last.status not in {429, 500, 502, 503, 504}:
                        break
                    if retry_after:
                        await asyncio.sleep(retry_after)
                        waiting += retry_after
                        self.total_waiting_seconds += retry_after
                except Exception as error:
                    last = FetchResult(
                        url=url,
                        final_url=url,
                        status=None,
                        content_type="",
                        body=b"",
                        retrieved_at=utc_now(),
                        elapsed_seconds=round(time.monotonic() - started, 3),
                        error=f"{type(error).__name__}: {error}",
                        attempts=attempt,
                        waiting_seconds=round(waiting, 3),
                    )
                    history.append({
                        "attempt": attempt,
                        "retrieved_at": last.retrieved_at,
                        "status": None,
                        "elapsed_seconds": last.elapsed_seconds,
                        "result": "timed_out" if "timeout" in str(error).casefold() else "network_error",
                        "error": last.error,
                    })
                    self._last_request_at[host] = time.monotonic()
                if attempt < self.retries:
                    backoff = (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)
                    await asyncio.sleep(backoff)
                    waiting += backoff
                    self.total_waiting_seconds += backoff
        assert last is not None
        last.waiting_seconds = round(waiting, 3)
        last.attempt_history = history
        if _circuit_failure(last):
            self.circuit.note(url, True, last.error or str(last.status))
        return last

    def note_application_failure(self, url: str, failed: bool, result: str) -> None:
        self.circuit.note(url, failed, result)

    async def latest_wayback_capture(self, target_url: str) -> tuple[dict[str, str] | None, dict[str, Any]]:
        params = [
            ("url", target_url),
            ("output", "json"),
            ("fl", "timestamp,original,statuscode,digest,mimetype"),
            ("filter", "statuscode:200"),
            ("filter", "mimetype:text/html"),
            ("collapse", "digest"),
            ("limit", "-10"),
        ]
        cdx_url = "https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(params)
        result = await self.fetch(cdx_url, accept="application/json")
        metadata = result.metadata()
        metadata["ok"] = result.ok
        if not result.ok:
            return None, metadata
        self.circuit.note(cdx_url, False, "successful_cdx_response")
        try:
            rows = json.loads(result.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            metadata["lookup_result"] = "invalid_response"
            return None, metadata
        if len(rows) < 2:
            metadata["lookup_result"] = "no_capture"
            return None, metadata
        headers = rows[0]
        captures = [dict(zip(headers, row)) for row in rows[1:] if len(row) == len(headers)]
        if not captures:
            metadata["lookup_result"] = "no_capture"
            return None, metadata
        capture = max(captures, key=lambda row: row.get("timestamp", ""))
        capture["cdx_url"] = cdx_url
        capture["replay_url"] = f"https://web.archive.org/web/{capture['timestamp']}id_/" + target_url
        metadata["lookup_result"] = "capture_found"
        return capture, metadata

    def stats(self) -> dict[str, Any]:
        return {
            "requests": self.total_requests,
            "retries": self.total_retries,
            "waiting_seconds": round(self.total_waiting_seconds, 3),
            "circuit_skips": self.circuit_skips,
            "circuit_state": self.circuit.snapshot(),
        }


def airwars_endpoint_urls(airwars_id: str, slug: str) -> list[str]:
    fields = "id,slug,link,date,modified,status,title,content,excerpt,acf"
    query_by_id = urllib.parse.urlencode({"include": airwars_id, "_fields": fields})
    query_by_slug = urllib.parse.urlencode({"slug": slug, "_fields": fields})
    return [
        f"https://airwars.org/wp-json/wp/v2/civ?{query_by_id}",
        f"https://airwars.org/wp-json/wp/v2/civ?{query_by_slug}",
    ]
