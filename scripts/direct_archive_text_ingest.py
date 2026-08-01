#!/usr/bin/env python3
"""One-off archived-page text ingestion for a bounded incident range.

This utility never requests ``original_url``.  It discovers third-party web
archive captures through CDX endpoints and downloads only archive replay URLs.
It checkpoints every completed source directly in ``data/sources``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html as html_stdlib
import json
import random
import re
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from lxml import html

from archive_pipeline.extractors import extract_payload
from archive_pipeline.io_utils import atomic_write_json, clean_text, load_json, sha256_text, utc_now
from archive_pipeline.pilot import detect_language


WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
ARQUIVO_CDX = "https://arquivo.pt/wayback/cdx"
ARCHIVE_HOSTS = {"web.archive.org", "arquivo.pt"}
ERROR_TEXT = {
    "site unavailable",
    "unable to access this site.",
    "page cannot be crawled or displayed due to robots.txt.",
}


def _in_scope(record: dict[str, Any], first: int, last: int) -> bool:
    return any(first <= int(value) <= last for value in record.get("incident_sequences") or [])


def _meta_text(body: bytes) -> tuple[str, str]:
    decoded = body.decode("utf-8", errors="replace")
    try:
        document = html.fromstring(decoded)
    except Exception:
        return "", ""
    title_values = document.xpath(
        '//meta[@property="og:title"]/@content | //meta[@name="twitter:title"]/@content | //title/text()'
    )
    description_values = document.xpath(
        '//meta[@name="description"]/@content | //meta[@property="og:description"]/@content | '
        '//meta[@name="twitter:description"]/@content'
    )
    title = clean_text(" ".join(html_stdlib.unescape(str(value)) for value in title_values))
    descriptions: list[str] = []
    for value in description_values:
        rendered = clean_text(html_stdlib.unescape(str(value)))
        if rendered and rendered not in descriptions:
            descriptions.append(rendered)
    return title, clean_text("\n\n".join(descriptions))


def _quality_text(extracted: dict[str, Any], body: bytes) -> tuple[str, str]:
    title, meta = _meta_text(body)
    body_text = clean_text(extracted.get("text") or "")
    candidates = [value for value in (body_text, meta) if value]
    text = max(candidates, key=len, default="")
    folded = text.casefold().strip()
    if not text or folded in ERROR_TEXT or any(marker in folded for marker in ERROR_TEXT):
        return title or clean_text(extracted.get("title") or ""), ""
    # Login shells and archive toolbar text are not source content.
    if len(text) < 25 or ("log in" in folded and len(text) < 100):
        return title or clean_text(extracted.get("title") or ""), ""
    return title or clean_text(extracted.get("title") or ""), text


def _capture_from_rows(payload: Any, provider: str) -> dict[str, str] | None:
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[0], list):
        return None
    headers = [str(value) for value in payload[0]]
    rows = [dict(zip(headers, row)) for row in payload[1:] if isinstance(row, list)]
    rows = [row for row in rows if str(row.get("statuscode")) == "200"]
    if not rows:
        return None
    row = rows[-1]
    timestamp = str(row.get("timestamp") or "")
    original = str(row.get("original") or "")
    if provider == "wayback":
        replay = f"https://web.archive.org/web/{timestamp}id_/{original}"
    else:
        replay = f"https://arquivo.pt/wayback/{timestamp}id_/{original}"
    return {"timestamp": timestamp, "original": original, "replay_url": replay, "provider": provider}


class DirectArchiveIngest:
    def __init__(self, root: Path, first: int, last: int, workers: int, timeout: float, shard_index: int, shard_count: int):
        self.root = root
        self.first = first
        self.last = last
        self.workers = workers
        self.timeout = timeout
        self.shard_index = shard_index
        self.shard_count = shard_count
        self.started = time.monotonic()
        shard_suffix = f"-shard-{shard_index:02d}-of-{shard_count:02d}" if shard_count > 1 else ""
        self.progress_path = root / "data" / "reports" / f"direct-archive-text-{first:04d}-{last:04d}{shard_suffix}.json"
        self.progress = load_json(self.progress_path, {}) or {}
        self.progress.setdefault("schema_version", "1.0.0")
        self.progress.setdefault("scope", {"first_sequence": first, "last_sequence": last})
        self.progress.setdefault("started_at", utc_now())
        self.progress.setdefault("completed_source_ids", [])
        self.progress.setdefault("outcomes", {})
        self.completed = set(self.progress["completed_source_ids"])
        self.counts = Counter(self.progress.get("outcomes") or {})
        limits = httpx.Limits(max_connections=max(workers * 2, 20), max_keepalive_connections=max(workers, 10))
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            limits=limits,
            headers={"User-Agent": "SyrianArchiveTextPreserver/1.0 (+archival research)"},
        )
        self.lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response | None:
        for attempt in range(4):
            try:
                response = await self.client.get(url, params=params)
                if response.status_code == 200:
                    return response
                if response.status_code not in {429, 500, 502, 503, 504}:
                    return response
            except httpx.HTTPError:
                pass
            await asyncio.sleep(min(8.0, (2**attempt) + random.random()))
        return None

    async def _discover(self, original_url: str) -> tuple[dict[str, str] | None, list[dict[str, Any]]]:
        attempts: list[dict[str, Any]] = []
        params = {
            "url": original_url,
            "output": "json",
            "fl": "timestamp,original,statuscode,mimetype,digest",
            "filter": "statuscode:200",
            "collapse": "digest",
            "limit": "-5",
        }
        response = await self._get(WAYBACK_CDX, params)
        attempts.append({"provider": "wayback", "status": response.status_code if response else None, "at": utc_now()})
        if response and response.status_code == 200:
            try:
                capture = _capture_from_rows(response.json(), "wayback")
            except Exception:
                capture = None
            if capture:
                return capture, attempts
        response = await self._get(ARQUIVO_CDX, params)
        attempts.append({"provider": "arquivo", "status": response.status_code if response else None, "at": utc_now()})
        if response and response.status_code == 200:
            try:
                capture = _capture_from_rows(response.json(), "arquivo")
            except Exception:
                capture = None
            if capture:
                return capture, attempts
        return None, attempts

    async def _process(self, path: Path, semaphore: asyncio.Semaphore) -> None:
        record = load_json(path, {}) or {}
        source_id = str(record.get("source_id") or path.stem)
        if source_id in self.completed:
            return
        async with semaphore:
            capture, attempts = await self._discover(str(record.get("original_url") or ""))
            outcome = "no_capture"
            if capture:
                response = await self._get(capture["replay_url"])
                attempts.append({
                    "provider": capture["provider"],
                    "role": "archive_replay",
                    "url": capture["replay_url"],
                    "status": response.status_code if response else None,
                    "at": utc_now(),
                })
                if response and response.status_code == 200:
                    final_host = (urllib.parse.urlsplit(str(response.url)).hostname or "").casefold()
                    if final_host in ARCHIVE_HOSTS:
                        try:
                            extracted = extract_payload(
                                response.content,
                                response.headers.get("content-type", ""),
                                str(response.url),
                                str(record.get("source_type") or ""),
                            )
                            title, text = _quality_text(extracted, response.content)
                        except Exception as error:
                            record.setdefault("review_flags", []).append(f"direct_archive_parser:{type(error).__name__}")
                            text = ""
                            title = ""
                        if text:
                            digest = sha256_text(text)
                            variant = {
                                "text": text,
                                "sha256": digest,
                                "bytes": len(text.encode("utf-8")),
                                "provenance": f"{capture['provider']}_capture",
                                "source_url": capture["replay_url"],
                                "retrieved_at": utc_now(),
                            }
                            variants = record.setdefault("content_variants", [])
                            if not any(item.get("sha256") == digest for item in variants if isinstance(item, dict)):
                                variants.append(variant)
                            archives = record.setdefault("archived_urls", [])
                            if capture["replay_url"] not in archives:
                                archives.append(capture["replay_url"])
                            record.update({
                                "text_original": text,
                                "text_original_language": detect_language(text, ""),
                                "content_hash": digest,
                                "page_title": title or record.get("page_title") or "",
                                "preservation_status": "archived_text_preserved",
                                "extraction_status": "text_extracted",
                                "retrieval_status": "successful",
                                "failure_reason": None,
                                "final_redirected_url": capture["replay_url"],
                                "retrieved_at": variant["retrieved_at"],
                            })
                            outcome = "text_extracted"
                        else:
                            outcome = "capture_without_text"
                    else:
                        outcome = "archive_redirect_rejected"
                else:
                    outcome = "capture_fetch_failed"
            record.setdefault("direct_archive_attempts", []).extend(attempts)
            if outcome != "text_extracted":
                record["retrieval_status"] = outcome
                record["failure_reason"] = outcome
            atomic_write_json(path, record)
        async with self.lock:
            self.completed.add(source_id)
            self.counts[outcome] += 1
            self.progress["completed_source_ids"] = sorted(self.completed)
            self.progress["outcomes"] = dict(sorted(self.counts.items()))
            self.progress["updated_at"] = utc_now()
            self.progress["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
            self.progress["completed_count"] = len(self.completed)
            atomic_write_json(self.progress_path, self.progress)
            if len(self.completed) % 100 == 0:
                print(json.dumps({"completed": len(self.completed), "outcomes": dict(self.counts), "elapsed_seconds": self.progress["elapsed_seconds"]}, ensure_ascii=False), flush=True)

    async def run(self) -> dict[str, Any]:
        paths = []
        for path in sorted((self.root / "data" / "sources").glob("*.json")):
            record = load_json(path, {}) or {}
            source_id = str(record.get("source_id") or path.stem)
            shard = int(hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:16], 16) % self.shard_count
            if shard == self.shard_index and _in_scope(record, self.first, self.last) and not (record.get("text_original") or "").strip():
                paths.append(path)
        semaphore = asyncio.Semaphore(self.workers)
        await asyncio.gather(*(self._process(path, semaphore) for path in paths))
        self.progress["finished_at"] = utc_now()
        self.progress["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.progress["target_count"] = len(paths)
        self.progress["done"] = True
        atomic_write_json(self.progress_path, self.progress)
        return self.progress


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--project-root", default=".")
    result.add_argument("--first-sequence", type=int, required=True)
    result.add_argument("--last-sequence", type=int, required=True)
    result.add_argument("--workers", type=int, default=24)
    result.add_argument("--timeout", type=float, default=30.0)
    result.add_argument("--shard-index", type=int, default=0)
    result.add_argument("--shard-count", type=int, default=1)
    return result


async def async_main(args: argparse.Namespace) -> None:
    if not 0 <= args.shard_index < args.shard_count:
        raise ValueError("shard_index must be between zero and shard_count - 1")
    runner = DirectArchiveIngest(
        Path(args.project_root).resolve(), args.first_sequence, args.last_sequence,
        args.workers, args.timeout, args.shard_index, args.shard_count,
    )
    try:
        result = await runner.run()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await runner.close()


if __name__ == "__main__":
    asyncio.run(async_main(parser().parse_args()))
