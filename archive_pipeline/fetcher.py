from __future__ import annotations

import json
import gzip
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from typing import Any

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


def airwars_endpoint_urls(airwars_id: str, slug: str) -> list[str]:
    fields = "id,slug,link,date,modified,status,title,content,excerpt,acf"
    query_by_id = urllib.parse.urlencode({"include": airwars_id, "_fields": fields})
    query_by_slug = urllib.parse.urlencode({"slug": slug, "_fields": fields})
    return [
        f"https://airwars.org/wp-json/wp/v2/civ?{query_by_id}",
        f"https://airwars.org/wp-json/wp/v2/civ?{query_by_slug}",
    ]
