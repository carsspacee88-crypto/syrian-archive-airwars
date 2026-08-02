from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
import urllib.parse
import uuid
from collections import Counter, defaultdict
from collections import deque
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from . import PILOT_PARSER_VERSION, PILOT_SCHEMA_VERSION, TRANSLATION_VERSION
from .collector import collect_one
from .content_extractor import extract_public_text
from .fetcher import AsyncHostFetcher, CircuitBreakingFetcher, FetchResult
from .io_utils import atomic_write_json, clean_text, load_json, sha256_bytes, sha256_text, utc_now
from .legacy import LegacyArchive
from .normalize import finalize_status
from .normalize import build_legacy_record
from .normalized_archive import NormalizedArchive
from .pilot import (
    DIRECT_AUDIO_SUFFIXES,
    DIRECT_IMAGE_SUFFIXES,
    DIRECT_VIDEO_SUFFIXES,
    LOGIN_WALL_MARKERS,
    _append_unique,
    _classify_fetch_failure,
    _extract_html,
    _extract_pdf,
    _legacy_source_seed,
    _normalized_source_seed,
    _record_exact_url_observations,
    _source_variant,
    classify_source_type,
    detect_language,
    normalize_source_url,
    stable_source_id,
)
from .source_cache import SourceCacheStore


DEFAULT_FIRST_SEQUENCE = 101
DEFAULT_LAST_SEQUENCE = 150
ENGINE_VERSION = "4.0.0"
RECENT_TIMING_LIMIT = 5_000
RESOLVED_SOURCE_STATUSES = {
    "successful", "successful_partial", "cached", "embedded_text_preserved"
}
POLICY_COMPLETE_SOURCE_STATUSES = {"media_metadata_preserved"}
DEFERRED_SOURCE_STATUSES = {"recovery_deferred"}
OPERATIONAL_FAILURE_STATUSES = {"failed", "internal_error"}
# A deferred source is terminal for the foreground job, but remains explicitly
# queued for later recovery.  It must never be presented as extracted content.
COMPLETE_SOURCE_STATUSES = RESOLVED_SOURCE_STATUSES | POLICY_COMPLETE_SOURCE_STATUSES
SUCCESS_SOURCE_STATUSES = COMPLETE_SOURCE_STATUSES | DEFERRED_SOURCE_STATUSES
LEGACY_RETRYABLE_SOURCE_STATUSES = {
    "archive_lookup_failed",
    "no_archive_capture",
    "low_quality_content",
    "login_required",
    "blocked",
    "timed_out",
    "unavailable",
    "unsupported_content_type",
    "empty_response",
}
RETRYABLE_SOURCE_STATUSES = (
    DEFERRED_SOURCE_STATUSES
    | OPERATIONAL_FAILURE_STATUSES
    | LEGACY_RETRYABLE_SOURCE_STATUSES
)

SHELL_MARKERS = (
    "log in to facebook",
    "see more on facebook",
    "create new account",
    "something went wrong",
    "this browser is no longer supported",
    "don’t miss what’s happening",
    "don't miss what's happening",
    "people on x are the first to know",
    "javascript is not available",
    "enable javascript and cookies to continue",
    "just a moment",
    "checking your browser",
    "verify you are human",
    "attention required",
    "access denied",
    "captcha",
    "يرجى التحقق من أنك إنسان",
    "تم رفض الوصول",
)


def canonical_fetch_key(raw_url: str) -> str:
    """Cross-job cache key without changing the evidence source identity."""
    normalized = normalize_source_url(raw_url)
    parsed = urllib.parse.urlsplit(normalized)
    if not parsed.hostname:
        return normalized
    host = parsed.hostname.casefold().removeprefix("www.")
    if host == "twitter.com" or host.endswith(".twitter.com"):
        host = "x.com"
    removable = {
        "fbclid", "gclid", "mc_cid", "mc_eid",
        "utm_campaign", "utm_content", "utm_medium", "utm_source", "utm_term",
    }
    if host in {"x.com", "twitter.com"}:
        removable.update({"s", "t", "ref_src"})
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key.casefold() not in removable and not key.casefold().startswith("utm_")]
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path or "/", urllib.parse.urlencode(query), ""))


def assess_content_quality(text: str, source_type: str, title: str = "") -> dict[str, Any]:
    """Reject obvious access shells while retaining short, legitimate social posts."""
    cleaned = clean_text(text)
    folded = f"{title}\n{cleaned}".casefold()
    minimum = 16 if source_type.startswith("public_") or source_type == "youtube_page" else 40
    reasons: list[str] = []
    if len(cleaned) < minimum:
        reasons.append("text_too_short")
    matched = [marker for marker in SHELL_MARKERS if marker in folded]
    if matched and len(cleaned) < 1500:
        reasons.append("access_or_platform_shell")
    words = re.findall(r"[^\W_]+", cleaned, flags=re.UNICODE)
    if len(words) >= 30 and len({word.casefold() for word in words}) / len(words) < 0.18:
        reasons.append("highly_repetitive_text")
    accepted = not reasons
    score = 100
    if "text_too_short" in reasons:
        score -= 55
    if "access_or_platform_shell" in reasons:
        score -= 70
    if "highly_repetitive_text" in reasons:
        score -= 45
    return {
        "accepted": accepted,
        "score": max(0, score),
        "characters": len(cleaned),
        "words": len(words),
        "reasons": reasons,
        "validator_version": ENGINE_VERSION,
    }


def default_host_policies(
    per_host_workers: int,
    social_workers: int,
    archive_workers: int,
    base_delay: float = 0.05,
) -> dict[str, dict[str, float | int]]:
    social_limit = max(2, social_workers)
    archive_limit = max(2, archive_workers)
    return {
        "facebook.com": {"workers": social_limit, "rate": 24, "burst": social_limit, "delay": 0},
        "x.com": {"workers": social_limit, "rate": 20, "burst": social_limit, "delay": 0},
        "twitter.com": {"workers": social_limit, "rate": 20, "burst": social_limit, "delay": 0},
        "publish.twitter.com": {"workers": social_limit, "rate": 24, "burst": social_limit, "delay": 0},
        "web.archive.org": {"workers": archive_limit, "rate": 8, "burst": min(archive_limit, 8), "delay": 0},
        "archive.is": {"workers": archive_limit, "rate": 6, "burst": min(archive_limit, 6), "delay": 0},
        "archive.ph": {"workers": archive_limit, "rate": 6, "burst": min(archive_limit, 6), "delay": 0},
        "archive.fo": {"workers": archive_limit, "rate": 6, "burst": min(archive_limit, 6), "delay": 0},
        "archive.md": {"workers": archive_limit, "rate": 6, "burst": min(archive_limit, 6), "delay": 0},
    }


def source_route_key(record: dict[str, Any]) -> str:
    source_type = str(record.get("source_type") or "")
    if source_type == "public_facebook_post":
        return "social:facebook_embed"
    if source_type == "public_x_post":
        return "social:x_oembed"
    host = (urllib.parse.urlsplit(record.get("original_url") or "").hostname or "unknown").casefold().removeprefix("www.")
    return "general:" + host


def fair_source_order(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round-robin routes so one dominant platform cannot starve rare hosts."""
    buckets: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for record in records:
        buckets[source_route_key(record)].append(record)
    active = deque(sorted(buckets, key=lambda key: (len(buckets[key]), key)))
    ordered: list[dict[str, Any]] = []
    while active:
        key = active.popleft()
        ordered.append(buckets[key].popleft())
        if buckets[key]:
            active.append(key)
    return ordered


def _scope_key(first_sequence: int, last_sequence: int) -> str:
    return f"{first_sequence:04d}-{last_sequence:04d}"


def _scope_sequences(first_sequence: int, last_sequence: int) -> tuple[int, ...]:
    if first_sequence < 1 or last_sequence < first_sequence or last_sequence > 8114:
        raise ValueError(f"invalid_speed_pilot_scope:{first_sequence}-{last_sequence}")
    return tuple(range(first_sequence, last_sequence + 1))


def should_defer_archive(record: dict[str, Any]) -> bool:
    """Archived lookup is low priority when a complete original text is already preserved."""
    return bool(clean_text(record.get("text_original") or ""))


def resolved_status(quality: dict[str, Any]) -> str:
    return "successful_partial" if quality.get("completeness") == "partial" else "successful"


def _merge_stats(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = dict(previous or {})
    for key in ("requests", "retries", "circuit_skips"):
        merged[key] = int(merged.get(key) or 0) + int(current.get(key) or 0)
    for key in (
        "waiting_seconds",
        "scheduler_waiting_seconds",
        "pacing_waiting_seconds",
        "retry_waiting_seconds",
    ):
        merged[key] = round(
            float(merged.get(key) or 0) + float(current.get(key) or 0), 3
        )
    merged["circuit_state"] = deepcopy(current.get("circuit_state") or merged.get("circuit_state") or {})
    merged["adaptive_host_delays"] = deepcopy(current.get("adaptive_host_delays") or merged.get("adaptive_host_delays") or {})
    merged["host_rate_state"] = deepcopy(
        current.get("host_rate_state") or merged.get("host_rate_state") or {}
    )
    host_counts = deepcopy(merged.get("host_result_counts") or {})
    for host, counts in (current.get("host_result_counts") or {}).items():
        target = host_counts.setdefault(host, {})
        for status, count in counts.items():
            target[status] = int(target.get(status) or 0) + int(count or 0)
    merged["host_result_counts"] = host_counts
    return merged


def _inherit_airwars_circuit_classification(records: list[dict[str, Any]]) -> int:
    """Annotate skipped Airwars requests from explicit same-batch 403 evidence."""
    retrieval_keys = ("airwars_endpoint", "live_page")
    observed_403 = any(
        isinstance((record.get("retrieval_status") or {}).get(key), dict)
        and ((record.get("retrieval_status") or {}).get(key) or {}).get("status") == 403
        for record in records
        for key in retrieval_keys
    )
    if not observed_403:
        return 0
    changed = 0
    for record in records:
        retrieval = record.get("retrieval_status") or {}
        annotated = False
        for key in retrieval_keys:
            attempt = retrieval.get(key)
            if not isinstance(attempt, dict):
                continue
            error = str(attempt.get("error") or "")
            if error.startswith("host_circuit_open") and attempt.get("circuit_open_status") is None:
                attempt["circuit_open_reason"] = "http_403_observed_for_same_airwars_host_in_batch"
                attempt["circuit_open_status"] = 403
                annotated = True
        if not annotated:
            continue
        old_status = record.get("completeness_status")
        finalize_status(record)
        if old_status != record.get("completeness_status"):
            _append_unique(record.setdefault("review_flags", []), "batch_circuit_status_inherited_from_observed_airwars_403")
        changed += 1
    return changed


def _record_exact_content_duplicate_groups(source_root: Path) -> tuple[int, int]:
    """Record identical content without merging distinct source identities."""
    records: dict[Path, dict[str, Any]] = {}
    by_hash: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(source_root.glob("*.json")):
        record = load_json(path, {}) or {}
        records[path] = record
        digest = str(record.get("content_hash") or "")
        if digest:
            by_hash[digest].append(path)
    groups = 0
    updated = 0
    for digest, paths in by_hash.items():
        if len(paths) < 2:
            continue
        groups += 1
        group_id = f"sha256-{digest[:24]}"
        for path in paths:
            record = records[path]
            if record.get("exact_content_duplicate_group") == group_id:
                continue
            record["exact_content_duplicate_group"] = group_id
            atomic_write_json(path, record)
            updated += 1
    return groups, updated


def _new_source_record(seed: dict[str, Any], source_id: str) -> dict[str, Any]:
    original_url = seed["original_url"]
    record = {
        "schema_version": PILOT_SCHEMA_VERSION,
        "parser_version": PILOT_PARSER_VERSION,
        "translation_version": TRANSLATION_VERSION,
        "source_id": source_id,
        "stable_id_basis": normalize_source_url(original_url),
        "incident_ids": [],
        "incident_sequences": [],
        "airwars_source_ids": [],
        "publisher": seed["publisher"],
        "publisher_ar": seed["publisher_ar"],
        "source_type": classify_source_type(original_url, seed["publisher"]),
        "original_url": original_url,
        "normalized_url": normalize_source_url(original_url),
        "observed_original_urls": [],
        "final_redirected_url": "",
        "archived_urls": [],
        "page_title": "",
        "author": seed["author"],
        "publication_date": seed["publication_date"],
        "text_original": "",
        "text_original_language": detect_language(seed["content"], seed["declared_language"]),
        "text_ar": "",
        "content_hash": None,
        "content_variants": [],
        "captions": [],
        "descriptions": [],
        "retrieval_status": "pending",
        "extraction_status": "pending",
        "translation": {
            "status": "disabled_by_user",
            "version": TRANSLATION_VERSION,
            "provider": "none",
            "review_required": False,
            "generated_in_pilot": False,
            "chunks": [],
        },
        "preservation_status": "metadata_only",
        "retrieved_at": None,
        "attempt_history": [],
        "provenance": [],
        "failure_reason": None,
        "review_flags": [],
        "pdf": None,
    }
    return record


def _merge_previous_source(seed_record: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    if not previous:
        return seed_record
    merged = deepcopy(seed_record)
    for field in (
        "final_redirected_url", "page_title", "author", "publication_date", "text_original",
        "text_original_language", "text_ar", "content_hash", "retrieval_status",
        "extraction_status", "translation", "preservation_status", "retrieved_at",
        "failure_reason", "pdf", "timing", "content_quality", "collection_checkpoint",
    ):
        if previous.get(field) not in (None, "", [], {}):
            merged[field] = deepcopy(previous[field])
    for field in (
        "incident_ids", "incident_sequences", "airwars_source_ids", "observed_original_urls",
        "archived_urls", "content_variants", "captions", "descriptions", "attempt_history",
        "provenance", "review_flags",
    ):
        values: list[Any] = []
        for value in list(previous.get(field) or []) + list(seed_record.get(field) or []):
            if value not in values:
                values.append(deepcopy(value))
        merged[field] = values
    return merged


class SpeedPilotRunner:
    def __init__(
        self,
        root: Path,
        legacy_zip: Path | None,
        first_sequence: int = DEFAULT_FIRST_SEQUENCE,
        last_sequence: int = DEFAULT_LAST_SEQUENCE,
        delay: float = 0.75,
        timeout: float = 10.0,
        retries: int = 1,
        workers: int = 6,
        per_host_workers: int = 1,
        archive_workers: int = 4,
        social_workers: int = 8,
        fast_timeout: float = 4.0,
        incident_mode: str = "snapshot_first",
        inline_wayback: bool = False,
        checkpoint_every: int = 5000,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.root = root.resolve()
        self.legacy_zip = legacy_zip.resolve() if legacy_zip else None
        self.first_sequence = first_sequence
        self.last_sequence = last_sequence
        self.sequences = _scope_sequences(first_sequence, last_sequence)
        self.key = _scope_key(first_sequence, last_sequence)
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.workers = workers
        self.per_host_workers = per_host_workers
        self.archive_workers = max(1, min(archive_workers, workers))
        self.social_workers = max(1, min(social_workers, workers))
        self.fast_timeout = max(1.0, min(fast_timeout, timeout))
        if incident_mode not in {"snapshot_first", "network_refresh"}:
            raise ValueError(f"unknown_incident_mode:{incident_mode}")
        self.incident_mode = incident_mode
        self.inline_wayback = bool(inline_wayback)
        self.checkpoint_every = max(1, checkpoint_every)
        self.progress_callback = progress_callback
        self.run_id = f"v4-{uuid.uuid4().hex}"
        self.progress_path = self.root / "data" / "pilot" / f"speed-pilot-{self.key}-progress.json"
        self.manifest_path = self.root / "data" / "pilot" / f"speed-pilot-{self.key}-manifest.json"
        self.result_path = self.root / "data" / "pilot" / f"speed-pilot-{self.key}-stage-result.json"
        self.cache_path = self.root / "data" / "cache" / "source-url-index.json"
        self.cache_database_path = self.root / "data" / "cache" / "source-cache-v3.sqlite3"
        self.report_path = self.root / "data" / "reports" / f"speed-pilot-{self.key}.json"
        self.report_markdown_path = self.report_path.with_suffix(".md")
        self.relationship_path = self.root / "data" / "relationships" / f"incident-sources-{self.key}.json"
        self.source_catalog_path = self.root / "data" / "catalog" / f"source-catalog-{self.key}.json"
        self.recovery_path = self.root / "data" / "recovery" / f"source-recovery-{self.key}.json"
        self.progress = load_json(self.progress_path, {}) or {}
        previous_engine_version = str(self.progress.get("engine_version") or "legacy")
        try:
            previous_version_tuple = tuple(
                int(part) for part in previous_engine_version.split(".")[:3]
            )
        except ValueError:
            previous_version_tuple = (0, 0, 0)
        if previous_version_tuple > (4, 0, 0):
            raise RuntimeError(
                f"unsafe_engine_downgrade:{previous_engine_version}_to_{ENGINE_VERSION}"
            )
        self.progress.setdefault("pilot", f"speed-pilot-{self.key}")
        self.progress.setdefault("scope", {
            "first_sequence": first_sequence,
            "last_sequence": last_sequence,
            "count": len(self.sequences),
        })
        self.progress.setdefault("started_at", utc_now())
        self.progress.setdefault("incident_completed_sequences", [])
        self.progress.setdefault("source_completed_ids", [])
        self.progress.setdefault("incident_timings", [])
        self.progress.setdefault("source_timings", [])
        if len(self.progress["source_timings"]) > RECENT_TIMING_LIMIT:
            self.progress["source_timings"] = self.progress["source_timings"][-RECENT_TIMING_LIMIT:]
        self.progress.setdefault("source_timing_aggregate", {
            "count": len(self.progress["source_timings"]),
            "total_seconds": round(sum(
                float(row.get("duration_seconds") or 0)
                for row in self.progress["source_timings"]
            ), 3),
        })
        self.progress.setdefault("source_outcomes", {})
        self.progress.setdefault("stage_runs", [])
        self.progress.setdefault("incident_fetch_stats", {})
        self.progress.setdefault("source_fetch_stats", {})
        self.cache = SourceCacheStore(self.cache_database_path, self.cache_path)
        self.recovery = load_json(self.recovery_path, {}) or {}
        self.recovery["schema_version"] = ENGINE_VERSION
        self.recovery.setdefault("scope", self.key)
        self.recovery.setdefault("items", {})
        migrated_source_ids: list[str] = []
        if previous_engine_version != ENGINE_VERSION:
            outcomes = self.progress.get("source_outcomes") or {}
            preserved_completed_ids: list[str] = []
            for source_id in self.progress.get("source_completed_ids") or []:
                outcome = outcomes.get(source_id) or {}
                status = str(outcome.get("status") or "")
                source_record = load_json(
                    self.root / "data" / "sources" / f"{source_id}.json", {}
                ) or {}
                resolved_content = (
                    status in RESOLVED_SOURCE_STATUSES
                    and bool(source_record.get("text_original"))
                    and bool(source_record.get("content_hash"))
                )
                policy_complete = status == "media_metadata_preserved"
                if resolved_content or policy_complete:
                    preserved_completed_ids.append(source_id)
                    continue
                outcomes.pop(source_id, None)
                migrated_source_ids.append(source_id)
            self.progress["source_completed_ids"] = preserved_completed_ids
            self.progress["source_outcomes"] = outcomes
            self.progress.setdefault("engine_migrations", []).append({
                "from": previous_engine_version,
                "to": ENGINE_VERSION,
                "migrated_at": utc_now(),
                "preserved_completed_sources": len(preserved_completed_ids),
                "requeued_unresolved_sources": len(migrated_source_ids),
            })
        self.progress["engine_version"] = ENGINE_VERSION
        self._source_catalog_cache: tuple[dict[str, dict[str, Any]], dict[str, list[str]], dict[str, list[str]]] | None = None
        self._source_completed_set = set(self.progress["source_completed_ids"])
        self._incident_completed_set = {int(value) for value in self.progress["incident_completed_sequences"]}
        self._source_timing_index = {
            str(row.get("source_id")): index
            for index, row in enumerate(self.progress["source_timings"])
            if row.get("source_id")
        }
        self._incident_timing_index = {
            int(row.get("sequence")): index
            for index, row in enumerate(self.progress["incident_timings"])
            if row.get("sequence") is not None
        }
        self._dirty_checkpoints = 0
        if migrated_source_ids:
            self.save_progress()

    def save_progress(self) -> None:
        self.progress["updated_at"] = utc_now()
        atomic_write_json(self.progress_path, self.progress)

    def save_cache(self) -> None:
        self.cache.flush()

    def save_recovery(self) -> None:
        self.recovery["updated_at"] = utc_now()
        atomic_write_json(self.recovery_path, self.recovery)

    def close(self) -> None:
        self.cache.close()

    def _open_archive(self) -> Any:
        if self.legacy_zip is not None and self.legacy_zip.is_file():
            return LegacyArchive(self.legacy_zip)
        return NormalizedArchive(self.root)

    def flush_checkpoints(self, force: bool = False) -> None:
        if not force and self._dirty_checkpoints < self.checkpoint_every:
            return
        self.save_progress()
        self.save_cache()
        self.save_recovery()
        self._dirty_checkpoints = 0

    def _emit_progress(self, payload: dict[str, Any]) -> None:
        if not self.progress_callback:
            return
        try:
            self.progress_callback({"engine_version": ENGINE_VERSION, "scope": self.key, **payload})
        except Exception as error:
            # A dashboard/database outage must never discard the evidence item.
            print(f"progress callback failed: {type(error).__name__}:{error}", flush=True)

    def _stop_requested(self) -> bool:
        return bool(getattr(self.progress_callback, "stop_requested", False))

    def _stage(self, name: str, function: Callable[[], Any]) -> Any:
        started_at = utc_now()
        started = time.monotonic()
        result = "complete"
        try:
            return function()
        except Exception:
            result = "failed"
            raise
        finally:
            self.progress["stage_runs"].append({
                "stage": name,
                "started_at": started_at,
                "finished_at": utc_now(),
                "duration_seconds": round(time.monotonic() - started, 3),
                "result": result,
            })
            self.save_progress()

    def build_manifest(self) -> dict[str, Any]:
        incidents: list[dict[str, Any]] = []
        with self._open_archive() as archive:
            for sequence in self.sequences:
                summary = archive.summary_by_sequence(sequence)
                legacy = archive.case_data(sequence)
                airwars_id = str(summary.get("airwars_id") or legacy.get("case", {}).get("airwars_id") or "")
                canonical_url = summary.get("airwars_url") or legacy.get("incident", {}).get("رابط الحادثة") or ""
                if not airwars_id.isdigit() or not canonical_url.startswith("https://airwars.org/"):
                    raise ValueError(f"invalid_speed_pilot_identifier:{sequence:04d}")
                incidents.append({
                    "sequence": sequence,
                    "sequence_padded": f"{sequence:04d}",
                    "internal_id": f"airwars-{airwars_id}",
                    "airwars_id": airwars_id,
                    "incident_code": summary.get("code") or legacy.get("incident", {}).get("رمز الحادثة"),
                    "canonical_url": canonical_url,
                })
        if [row["sequence"] for row in incidents] != list(self.sequences):
            raise ValueError("speed_pilot_manifest_scope_mismatch")
        manifest = {
            "schema_version": PILOT_SCHEMA_VERSION,
            "pilot": f"speed-pilot-{self.key}",
            "scope": self.progress["scope"],
            "created_at": utc_now(),
            "incidents": incidents,
        }
        atomic_write_json(self.manifest_path, manifest)
        return {"done": True, "total": len(incidents)}

    def _manifest_items(self) -> dict[int, dict[str, Any]]:
        manifest = load_json(self.manifest_path, {}) or {}
        return {int(row["sequence"]): row for row in manifest.get("incidents") or []}

    def _snapshot_incident(
        self,
        archive: Any,
        sequence: int,
        item: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Create the incident record locally and defer the blocked Airwars refresh.

        Source discovery only needs the immutable identity and source rows already
        present in the historical snapshot.  Waiting on the same blocked Airwars
        host for every incident added minutes without improving that input.
        """
        summary = archive.summary_by_sequence(sequence)
        legacy = archive.case_data(sequence)
        candidate = deepcopy(legacy["_normalized_record"]) if legacy.get("_normalized_record") else build_legacy_record(summary, legacy)
        path = self.root / "data" / "incidents" / f"{item['internal_id']}.json"
        previous = load_json(path, {}) or {}
        previous_verified = bool(previous.get("page_extraction") or previous.get("api_extraction"))
        if previous_verified:
            record = previous
            status = "verified_record_reused"
        else:
            record = candidate
            record["retrieval_status"]["overall"] = "network_refresh_deferred"
            record["extraction_status"] = "historical_snapshot_preserved"
            record["completeness_status"] = str(
                record.get("legacy_completeness_status") or "partial"
            )
            record["review_flags"].append("airwars_network_refresh_deferred_by_v3_fast_mode")
            record["refresh_queue"] = {
                "status": "deferred_background",
                "reason": "airwars_host_repeatedly_blocked_in_measured_pilots",
                "queued_at": utc_now(),
            }
            status = "snapshot_preserved"
        return record, legacy | {"_v3_status": status}

    @staticmethod
    def _decorate_incident(record: dict[str, Any], legacy: dict[str, Any], sequence: int, key: str) -> None:
        record["pilot"] = {
            "name": f"speed-pilot-{key}",
            "in_scope": True,
            "sequence": sequence,
            "parser_version": PILOT_PARSER_VERSION,
            "translation_policy": "disabled_by_user",
            "engine_version": ENGINE_VERSION,
        }
        if not legacy.get("_normalized_record"):
            record["legacy_incident_fields"] = legacy.get("incident", {})
            record["legacy_page_fields"] = legacy.get("page_fields", [])
            record["legacy_page_sections"] = legacy.get("page_sections", [])
            record["additional_notes"] = legacy.get("incident", {}).get("ملاحظات تصنيفية — الأصل") or ""
        record["narrative_original"] = record.get("narrative") or ""
        record["narrative_original_language"] = detect_language(record["narrative_original"])
        record.setdefault("narrative_ar", "")
        record["translation"] = {
            "status": "disabled_by_user",
            "version": TRANSLATION_VERSION,
            "provider": "none",
            "generated_in_pilot": False,
            "review_required": False,
            "chunks": [],
        }

    def collect_incidents(self, max_items: int | None = None) -> dict[str, Any]:
        items = self._manifest_items()
        if set(items) != set(self.sequences):
            raise ValueError("speed_pilot_manifest_missing_or_invalid")
        completed = self._incident_completed_set
        fetcher = CircuitBreakingFetcher(
            delay_seconds=self.delay,
            timeout_seconds=self.timeout,
            retries=self.retries,
            circuit_state=self.progress.get("incident_fetch_stats", {}).get("circuit_state"),
            circuit_threshold=3,
            circuit_reprobe_every=max(10, len(self.sequences) * 2),
        )
        processed = 0
        with self._open_archive() as archive:
            for sequence in self.sequences:
                item = items[sequence]
                path = self.root / "data" / "incidents" / f"{item['internal_id']}.json"
                if sequence in completed and path.is_file():
                    continue
                started = time.monotonic()
                status = "failed"
                error_text = ""
                try:
                    if self.incident_mode == "snapshot_first":
                        record, legacy = self._snapshot_incident(archive, sequence, item)
                        status = str(legacy.pop("_v3_status"))
                    else:
                        record = collect_one(archive, sequence, self.root, fetcher)
                        legacy = archive.case_data(sequence)
                        status = str(record.get("completeness_status") or "partial")
                    self._decorate_incident(record, legacy, sequence, self.key)
                    atomic_write_json(path, record)
                except Exception as error:
                    error_text = f"{type(error).__name__}:{error}"
                duration = round(time.monotonic() - started, 3)
                timing_row = {
                    "sequence": sequence,
                    "internal_id": item["internal_id"],
                    "duration_seconds": duration,
                    "status": status,
                    "error": error_text or None,
                    "finished_at": utc_now(),
                }
                timing_index = self._incident_timing_index.get(sequence)
                if timing_index is None:
                    self._incident_timing_index[sequence] = len(self.progress["incident_timings"])
                    self.progress["incident_timings"].append(timing_row)
                else:
                    self.progress["incident_timings"][timing_index] = timing_row
                if sequence not in self._incident_completed_set:
                    self._incident_completed_set.add(sequence)
                    self.progress["incident_completed_sequences"].append(sequence)
                    self.progress["incident_completed_sequences"].sort()
                self.progress["incident_fetch_stats"] = _merge_stats(self.progress.get("incident_fetch_stats", {}), fetcher.stats())
                # Numeric counters are already merged; reset the current fetcher counters before the next checkpoint.
                fetcher.inner.total_requests = 0
                fetcher.inner.total_retries = 0
                fetcher.inner.total_waiting_seconds = 0.0
                fetcher.circuit_skips = 0
                self._dirty_checkpoints += 1
                self.flush_checkpoints()
                self._emit_progress({
                    "kind": "incident",
                    "identity": f"{sequence:04d}",
                    "status": status,
                    "duration_seconds": duration,
                    "error": error_text or None,
                    "detail": {"internal_id": item["internal_id"]},
                })
                print(f"speed incident [{processed + 1}/{len(self.sequences)}] {sequence:04d} -> {status}", flush=True)
                processed += 1
                if self._stop_requested() or (max_items is not None and processed >= max_items):
                    break
        incident_records: list[dict[str, Any]] = []
        incident_paths: list[Path] = []
        for sequence in self.sequences:
            item = items[sequence]
            path = self.root / "data" / "incidents" / f"{item['internal_id']}.json"
            record = load_json(path, {}) or {}
            if record:
                incident_paths.append(path)
                incident_records.append(record)
        repaired = _inherit_airwars_circuit_classification(incident_records)
        if repaired:
            for path, record in zip(incident_paths, incident_records):
                atomic_write_json(path, record)
            self.save_progress()
        self.flush_checkpoints(force=True)
        complete = len(set(self.progress["incident_completed_sequences"]) & set(self.sequences))
        return {
            "done": complete == len(self.sequences),
            "processed": processed,
            "completed": complete,
            "total": len(self.sequences),
            "reclassified_circuit_records": repaired,
        }

    def _discover_source_seeds(self) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], dict[str, list[str]]]:
        items = self._manifest_items()
        records: dict[str, dict[str, Any]] = {}
        incident_sources: dict[str, list[str]] = defaultdict(list)
        exact_urls: dict[str, list[str]] = defaultdict(list)
        with self._open_archive() as archive:
            for sequence in self.sequences:
                manifest_item = items[sequence]
                incident_id = manifest_item["internal_id"]
                normalized = load_json(self.root / "data" / "incidents" / f"{incident_id}.json", {}) or {}
                archive_case = archive.case_data(sequence)
                legacy_seeds = [] if archive_case.get("_normalized_record") else [_legacy_source_seed(raw) for raw in archive_case.get("sources", [])]
                normalized_seeds = [_normalized_source_seed(raw) for raw in normalized.get("sources") or []]
                source_seeds = _record_exact_url_observations(exact_urls, legacy_seeds, normalized_seeds)
                for seed in source_seeds:
                    original_url = seed["original_url"]
                    if not original_url:
                        continue
                    source_id = stable_source_id(original_url)
                    if source_id not in records:
                        records[source_id] = _new_source_record(seed, source_id)
                    record = records[source_id]
                    _append_unique(record["incident_ids"], incident_id)
                    _append_unique(record["incident_sequences"], sequence)
                    if seed["airwars_source_id"]:
                        _append_unique(record["airwars_source_ids"], seed["airwars_source_id"])
                    _append_unique(record["observed_original_urls"], original_url)
                    for archive_url in seed["archived_urls"]:
                        _append_unique(record["archived_urls"], archive_url)
                    content = seed["content"]
                    if content:
                        variant = _source_variant(content, seed["provenance"], manifest_item["canonical_url"], normalized.get("retrieved_at"))
                        _append_unique(record["content_variants"], variant, key=lambda row: row["sha256"])
                        if not record["text_original"]:
                            record["text_original"] = content
                            record["content_hash"] = variant["sha256"]
                            record["preservation_status"] = "preserved_in_airwars_incident_page"
                            record["extraction_status"] = "airwars_embedded_text"
                    _append_unique(record["provenance"], {
                        "source_type": seed["provenance"],
                        "incident_id": incident_id,
                        "incident_sequence": sequence,
                        "airwars_source_id": seed["airwars_source_id"] or None,
                        "observed_original_url": original_url,
                        "airwars_incident_url": manifest_item["canonical_url"],
                    }, key=lambda row: (row["incident_id"], row["source_type"], row.get("airwars_source_id"), row.get("observed_original_url")))
                    _append_unique(incident_sources[incident_id], source_id)
        return records, incident_sources, exact_urls

    def _source_seeds(self) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], dict[str, list[str]]]:
        """Build the expensive legacy/incident join once, then reuse it for every chunk."""
        if self._source_catalog_cache is not None:
            return self._source_catalog_cache
        payload = load_json(self.source_catalog_path, {}) or {}
        scope = payload.get("scope") or {}
        valid = (
            payload.get("schema_version") == "2.0.0"
            and int(scope.get("first_sequence") or 0) == self.first_sequence
            and int(scope.get("last_sequence") or 0) == self.last_sequence
            and isinstance(payload.get("records"), dict)
            and isinstance(payload.get("incident_sources"), dict)
            and isinstance(payload.get("exact_urls"), dict)
        )
        if valid:
            records = payload["records"]
            incident_sources = payload["incident_sources"]
            exact_urls = payload["exact_urls"]
        else:
            started = time.monotonic()
            records, incident_sources, exact_urls = self._discover_source_seeds()
            atomic_write_json(self.source_catalog_path, {
                "schema_version": "2.0.0",
                "engine_version": ENGINE_VERSION,
                "created_at": utc_now(),
                "scope": self.progress["scope"],
                "record_count": len(records),
                "build_seconds": round(time.monotonic() - started, 3),
                "records": records,
                "incident_sources": dict(incident_sources),
                "exact_urls": dict(exact_urls),
            })
        self._source_catalog_cache = (records, incident_sources, exact_urls)
        return self._source_catalog_cache

    def _attempt(self, record: dict[str, Any], result: FetchResult, role: str, **extra: Any) -> None:
        metadata = result.metadata()
        metadata["ok"] = result.ok
        metadata["attempt_role"] = role
        metadata["engine_version"] = ENGINE_VERSION
        metadata["run_id"] = self.run_id
        metadata.update(extra)
        record.setdefault("attempt_history", []).append(metadata)

    @staticmethod
    def _extract_into(record: dict[str, Any], result: FetchResult, provenance: str) -> dict[str, Any]:
        content_type = result.content_type.casefold()
        source_type = record["source_type"]
        extraction_started = time.monotonic()
        try:
            is_pdf = result.body.startswith(b"%PDF-") or (
                ("pdf" in content_type or source_type == "pdf_document")
                and not result.body.lstrip().startswith((b"<!doctype", b"<html", b"<HTML"))
            )
            if is_pdf:
                extracted = _extract_pdf(result.body)
                extracted["method"] = "pypdf"
                extracted["alternate_urls"] = []
                extracted["candidate_count"] = int(bool(extracted.get("text")))
                record["pdf"] = {
                    "byte_size": len(result.body),
                    "sha256": sha256_bytes(result.body),
                    "page_count": extracted.get("page_count"),
                    "ocr_pending": extracted.get("ocr_pending", False),
                    "binary_committed": False,
                }
                record["extraction_status"] = "ocr_pending" if extracted.get("ocr_pending") else "text_extracted"
            elif content_type.startswith(("image/", "video/", "audio/")):
                record["retrieval_status"] = "media_metadata_preserved"
                record["extraction_status"] = "media_metadata_only"
                record["failure_reason"] = None
                quality = {
                    "accepted": False, "score": 0, "characters": 0, "words": 0,
                    "reasons": ["binary_media_policy_complete"],
                    "validator_version": ENGINE_VERSION,
                    "provenance": provenance,
                    "completeness": "policy_complete",
                }
                record["content_quality"] = quality
                return quality
            else:
                extracted = extract_public_text(result.body, result.content_type, result.final_url)
                record["extraction_status"] = "text_extracted" if extracted.get("text") else "parsing_failed"
        except Exception as error:
            record["extraction_status"] = "parsing_failed"
            record["failure_reason"] = f"{type(error).__name__}:{error}"
            _append_unique(record["review_flags"], "source_parser_failed")
            record.setdefault("timing", {})["text_extraction_seconds"] = round(time.monotonic() - extraction_started, 6)
            quality = {
                "accepted": False, "score": 0, "characters": 0, "words": 0,
                "reasons": ["parser_failed"], "validator_version": ENGINE_VERSION,
            }
            record["content_quality"] = quality
            return quality
        record.setdefault("timing", {})["text_extraction_seconds"] = round(time.monotonic() - extraction_started, 6)
        record["page_title"] = extracted.get("title") or record.get("page_title") or ""
        record["author"] = extracted.get("author") or record.get("author") or ""
        record["publication_date"] = extracted.get("publication_date") or record.get("publication_date") or ""
        record["extraction_method"] = extracted.get("method") or "unknown"
        record["discovered_alternates"] = list(extracted.get("alternate_urls") or [])
        text = clean_text(extracted.get("text") or "")
        quality = assess_content_quality(text, source_type, record["page_title"])
        quality["provenance"] = provenance
        quality["extraction_method"] = record["extraction_method"]
        quality["candidate_count"] = int(extracted.get("candidate_count") or 0)
        method = str(record.get("extraction_method") or "")
        summary_only = method == "meta_description" or method.endswith(":description") or method.endswith(":summary")
        quality["response_truncated"] = bool(result.response_truncated)
        quality["completeness"] = "partial" if result.response_truncated or summary_only else "full"
        if result.response_truncated or summary_only:
            quality["score"] = min(int(quality.get("score") or 0), 80)
        if result.response_truncated:
            _append_unique(record["review_flags"], "source_response_truncated_content_partial")
        if summary_only:
            _append_unique(record["review_flags"], "source_summary_only_content_partial")
        record["content_quality"] = quality
        if text and quality["accepted"]:
            variant = _source_variant(text, provenance, result.final_url, result.retrieved_at)
            _append_unique(record["content_variants"], variant, key=lambda row: row["sha256"])
            existing_text = clean_text(record.get("text_original") or "")
            existing_quality = assess_content_quality(
                existing_text,
                source_type,
                record.get("page_title") or "",
            ) if existing_text else {"accepted": False, "score": 0}
            should_promote = (
                not existing_text
                or not existing_quality.get("accepted")
                or (
                    int(quality.get("score") or 0) > int(existing_quality.get("score") or 0)
                    and len(text) >= len(existing_text)
                )
                or (
                    not source_type.startswith("public_")
                    and len(text) >= max(400, int(len(existing_text) * 1.5))
                )
            )
            if should_promote:
                record["text_original"] = text
                record["content_hash"] = variant["sha256"]
                record["preservation_status"] = "live_text_preserved" if provenance == "source_live" else "archived_text_preserved"
            elif record.get("content_hash") != variant["sha256"]:
                _append_unique(record["review_flags"], "content_variants_require_review")
        elif text:
            record["extraction_status"] = "quality_rejected"
            _append_unique(record["review_flags"], "source_content_failed_quality_validation")
        if record.get("text_original"):
            record["text_original_language"] = detect_language(record["text_original"], record.get("text_original_language") or "")
        return quality

    async def _extract_into_async(
        self,
        record: dict[str, Any],
        result: FetchResult,
        provenance: str,
    ) -> dict[str, Any]:
        """Keep HTML/PDF parsing and decompression off the network loop."""
        return await asyncio.to_thread(self._extract_into, record, result, provenance)

    async def _x_oembed(self, fetcher: AsyncHostFetcher, record: dict[str, Any]) -> bool:
        if record.get("source_type") != "public_x_post":
            return False
        query = urllib.parse.urlencode({
            "url": record["original_url"], "omit_script": "true", "dnt": "true",
        })
        endpoint = f"https://publish.twitter.com/oembed?{query}"
        result = await fetcher.fetch(
            endpoint,
            accept="application/json",
            timeout_seconds=self.fast_timeout,
            max_bytes=512 * 1024,
        )
        self._attempt(record, result, "x_oembed", requested_source_url=record["original_url"])
        if not result.ok:
            return False
        try:
            payload = json.loads(result.body.decode("utf-8"))
            fragment = str(payload.get("html") or "")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return False
        if not fragment:
            return False
        synthetic = FetchResult(
            url=record["original_url"],
            final_url=record["original_url"],
            status=200,
            content_type="text/html; charset=utf-8",
            body=("<main>" + fragment + "</main>").encode("utf-8"),
            retrieved_at=result.retrieved_at,
            elapsed_seconds=result.elapsed_seconds,
        )
        quality = await self._extract_into_async(record, synthetic, "x_oembed")
        if not quality["accepted"]:
            return False
        fetcher.note_application_failure(endpoint, False, "successful_oembed_response")
        record["author"] = clean_text(payload.get("author_name") or record.get("author") or "")
        record["retrieval_status"] = resolved_status(quality)
        record["retrieved_at"] = result.retrieved_at
        record["final_redirected_url"] = record["original_url"]
        record["failure_reason"] = None
        return True

    async def _facebook_embed(self, fetcher: AsyncHostFetcher, record: dict[str, Any]) -> bool:
        """Use Facebook's small public embed surface instead of the full SPA page."""
        if record.get("source_type") != "public_facebook_post":
            return False
        original = record["original_url"]
        parsed = urllib.parse.urlsplit(original)
        path = parsed.path.casefold()
        plugin = "video.php" if any(token in path for token in ("/watch", "/videos/", "/reel/")) or "v=" in parsed.query else "post.php"
        query = urllib.parse.urlencode({
            "href": original,
            "show_text": "true",
            "width": "500",
        })
        endpoint = f"https://www.facebook.com/plugins/{plugin}?{query}"
        result = await fetcher.fetch(
            endpoint,
            accept="text/html,application/xhtml+xml",
            timeout_seconds=self.fast_timeout,
            max_bytes=2 * 1024 * 1024,
        )
        self._attempt(record, result, "facebook_public_embed", requested_source_url=original)
        if not result.ok:
            return False
        quality = await self._extract_into_async(record, result, "facebook_public_embed")
        if not quality["accepted"]:
            # A deleted/empty individual post is not evidence that the whole
            # Facebook host is down.  Do not poison the shared host circuit.
            fetcher.note_application_failure(endpoint, False, "facebook_embed_item_low_quality")
            return False
        fetcher.note_application_failure(endpoint, False, "successful_facebook_embed")
        record["retrieval_status"] = resolved_status(quality)
        record["retrieved_at"] = result.retrieved_at
        record["final_redirected_url"] = original
        record["failure_reason"] = None
        return True

    async def _telegram_embed(self, fetcher: AsyncHostFetcher, record: dict[str, Any]) -> bool:
        """Fetch Telegram's public, server-rendered message widget."""
        if record.get("source_type") != "public_telegram_post":
            return False
        parsed = urllib.parse.urlsplit(record["original_url"])
        query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        query.update({"embed": "1", "mode": "tme"})
        endpoint = urllib.parse.urlunsplit(
            (parsed.scheme or "https", parsed.netloc, parsed.path, urllib.parse.urlencode(query), "")
        )
        result = await fetcher.fetch(
            endpoint,
            accept="text/html,application/xhtml+xml",
            timeout_seconds=self.fast_timeout,
            max_bytes=2 * 1024 * 1024,
        )
        self._attempt(record, result, "telegram_public_embed")
        if not result.ok:
            return False
        quality = await self._extract_into_async(record, result, "telegram_public_embed")
        if not quality["accepted"]:
            fetcher.note_application_failure(endpoint, False, "telegram_embed_item_low_quality")
            return False
        fetcher.note_application_failure(endpoint, False, "successful_telegram_embed")
        record["retrieval_status"] = resolved_status(quality)
        record["retrieved_at"] = result.retrieved_at
        record["final_redirected_url"] = record["original_url"]
        record["failure_reason"] = None
        return True

    async def _youtube_oembed(self, fetcher: AsyncHostFetcher, record: dict[str, Any]) -> bool:
        """Preserve public YouTube title/author quickly as explicitly partial text."""
        if record.get("source_type") != "youtube_page":
            return False
        endpoint = "https://www.youtube.com/oembed?" + urllib.parse.urlencode(
            {"url": record["original_url"], "format": "json"}
        )
        result = await fetcher.fetch(
            endpoint,
            accept="application/json",
            timeout_seconds=self.fast_timeout,
            max_bytes=256 * 1024,
        )
        self._attempt(record, result, "youtube_oembed")
        if not result.ok:
            return False
        try:
            payload = json.loads(result.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return False
        title = clean_text(payload.get("title") or "")
        if not title:
            return False
        synthetic = FetchResult(
            url=record["original_url"],
            final_url=record["original_url"],
            status=200,
            content_type="text/plain; charset=utf-8",
            body=title.encode("utf-8"),
            retrieved_at=result.retrieved_at,
            elapsed_seconds=result.elapsed_seconds,
        )
        quality = await self._extract_into_async(record, synthetic, "youtube_oembed")
        if not quality["accepted"]:
            return False
        quality["completeness"] = "partial"
        quality["score"] = min(int(quality.get("score") or 0), 70)
        quality["reasons"] = list(quality.get("reasons") or []) + [
            "public_title_and_author_only_no_transcript"
        ]
        record["content_quality"] = quality
        record["author"] = clean_text(payload.get("author_name") or "")
        record["retrieval_status"] = "successful_partial"
        record["retrieved_at"] = result.retrieved_at
        record["final_redirected_url"] = record["original_url"]
        record["failure_reason"] = None
        return True

    def _defer_recovery(self, record: dict[str, Any], reason: str) -> dict[str, Any]:
        source_id = record["source_id"]
        previous = (self.recovery.get("items") or {}).get(source_id, {})
        attempts = int(previous.get("foreground_passes") or 0) + 1
        last_attempt = next(
            (
                row for row in reversed(record.get("attempt_history") or [])
                if isinstance(row, dict) and row.get("run_id") == self.run_id
            ),
            {},
        )
        item = {
            "source_id": source_id,
            "original_url": record.get("original_url"),
            "archived_urls": list(record.get("archived_urls") or []),
            "source_type": record.get("source_type"),
            "reason": reason,
            "foreground_passes": attempts,
            "first_queued_at": previous.get("first_queued_at") or utc_now(),
            "last_queued_at": utc_now(),
            "next_action": "retry_public_adapter_then_listed_archive_then_wayback",
            "last_external_error": record.get("failure_reason") or last_attempt.get("error"),
            "last_http_status": last_attempt.get("status"),
            "last_attempt_role": last_attempt.get("attempt_role"),
        }
        self.recovery["items"][source_id] = item
        record["retrieval_status"] = "recovery_deferred"
        record["preservation_status"] = "source_identity_and_archive_locators_preserved"
        record["recovery"] = item
        record["failure_reason"] = None
        _append_unique(record["review_flags"], "content_recovery_deferred_without_stopping_job")
        return record

    async def _live_source(self, fetcher: AsyncHostFetcher, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        existing_text = clean_text(record.get("text_original") or "")
        if existing_text:
            quality = assess_content_quality(existing_text, record.get("source_type") or "unknown", record.get("page_title") or "")
            quality["provenance"] = "existing_preserved_text"
            record["content_quality"] = quality
            if quality["accepted"]:
                record["retrieval_status"] = "embedded_text_preserved"
                record["preservation_status"] = record.get("preservation_status") or "existing_text_preserved"
                record["failure_reason"] = None
                return record, False
        if await self._facebook_embed(fetcher, record):
            return record, False
        if await self._x_oembed(fetcher, record):
            return record, False
        if await self._telegram_embed(fetcher, record):
            return record, False
        if await self._youtube_oembed(fetcher, record):
            return record, False
        # Full social SPA pages are the measured V2 bottleneck.  If their public
        # lightweight adapter did not yield text, go straight to the already
        # listed archive instead of spending another full-page timeout.
        if record.get("source_type") == "public_x_post":
            return record, True
        social_fallback = record.get("source_type") == "public_facebook_post"
        result = await fetcher.fetch(
            record["original_url"],
            accept="text/html,application/pdf,text/plain;q=0.9,*/*;q=0.1",
            timeout_seconds=self.fast_timeout if social_fallback else self.timeout,
            max_bytes=3 * 1024 * 1024 if social_fallback else 6 * 1024 * 1024,
        )
        self._attempt(record, result, "live_source")
        record["retrieved_at"] = result.retrieved_at
        record["final_redirected_url"] = result.final_url
        preview = result.body[:128 * 1024].decode("utf-8", errors="ignore").casefold() if result.body else ""
        access_wall = record["source_type"].startswith("public_") and any(marker in preview for marker in LOGIN_WALL_MARKERS)
        if access_wall:
            fetcher.note_application_failure(record["original_url"], True, "login_required")
        if result.ok and not access_wall:
            quality = await self._extract_into_async(record, result, "source_live")
            if record.get("retrieval_status") == "media_metadata_preserved":
                return record, False
            if quality["accepted"]:
                fetcher.note_application_failure(record["original_url"], False, "successful_content_response")
                record["retrieval_status"] = resolved_status(quality)
                record["failure_reason"] = None
                return record, False
            record["retrieval_status"] = "low_quality_content"
            record["failure_reason"] = ",".join(quality["reasons"]) or "quality_rejected"
            fetcher.note_application_failure(record["original_url"], False, "item_low_quality_content")
            alternates = [
                row for row in record.get("discovered_alternates") or []
                if isinstance(row, dict)
                and row.get("role") in {"amp", "json"}
                and row.get("url")
            ][:2]
            for alternate_row in alternates:
                alternate_url = str(alternate_row["url"])
                role = str(alternate_row["role"])
                alternate = await fetcher.fetch(
                    alternate_url,
                    accept="application/json,text/html,application/xhtml+xml",
                    timeout_seconds=self.fast_timeout,
                    max_bytes=4 * 1024 * 1024,
                )
                self._attempt(record, alternate, f"{role}_alternate")
                if alternate.ok:
                    alternate_quality = await self._extract_into_async(
                        record, alternate, f"{role}_alternate"
                    )
                    if alternate_quality["accepted"]:
                        fetcher.note_application_failure(
                            alternate_url, False, f"successful_{role}_response"
                        )
                        record["retrieval_status"] = resolved_status(alternate_quality)
                        record["retrieved_at"] = alternate.retrieved_at
                        record["final_redirected_url"] = alternate.final_url
                        record["failure_reason"] = None
                        return record, False
            if should_defer_archive(record):
                record["retrieval_status"] = "embedded_text_preserved"
                record["preservation_status"] = "preserved_in_airwars_incident_page"
                record["archive_retry"] = {
                    "status": "deferred_low_priority",
                    "reason": "complete_original_text_already_preserved",
                    "queued_at": utc_now(),
                }
                return record, False
            return record, True
        taxonomy = "login_required" if access_wall else _classify_fetch_failure(result.status, result.error, preview)
        if result.status not in {None, 401, 403, 408, 425, 429, 500, 502, 503, 504}:
            fetcher.note_application_failure(record["original_url"], False, taxonomy)
        record["retrieval_status"] = taxonomy
        record["failure_reason"] = result.error or taxonomy
        if should_defer_archive(record):
            record["retrieval_status"] = "embedded_text_preserved"
            record["preservation_status"] = "preserved_in_airwars_incident_page"
            record["archive_retry"] = {
                "status": "deferred_low_priority",
                "reason": "complete_original_text_already_preserved",
                "queued_at": utc_now(),
            }
            return record, False
        return record, True

    async def _archive_source(self, fetcher: AsyncHostFetcher, record: dict[str, Any]) -> dict[str, Any]:
        accept = "text/html,application/pdf,text/plain;q=0.9,*/*;q=0.1"
        for archive_url in record.get("archived_urls") or []:
            result = await fetcher.fetch(
                archive_url,
                accept=accept,
                timeout_seconds=self.fast_timeout,
                max_bytes=6 * 1024 * 1024,
            )
            self._attempt(record, result, "listed_archive")
            if result.ok:
                record["retrieved_at"] = result.retrieved_at
                record["final_redirected_url"] = result.final_url
                quality = await self._extract_into_async(record, result, "listed_archive")
                if quality["accepted"]:
                    fetcher.note_application_failure(archive_url, False, "successful_archive_response")
                    record["retrieval_status"] = resolved_status(quality)
                    record["failure_reason"] = None
                    return record
                fetcher.note_application_failure(archive_url, False, "capture_low_quality_content")
        if not self.inline_wayback:
            return self._defer_recovery(record, "listed_archive_unavailable_or_low_quality")
        captures, lookup = await fetcher.wayback_captures(record["original_url"], limit=4)
        lookup["attempt_role"] = "wayback_lookup"
        lookup["engine_version"] = ENGINE_VERSION
        lookup["run_id"] = self.run_id
        record["attempt_history"].append(lookup)
        for capture in captures:
            result = await fetcher.fetch(
                capture["replay_url"],
                accept=accept,
                timeout_seconds=self.timeout,
                max_bytes=6 * 1024 * 1024,
            )
            self._attempt(record, result, "wayback_capture", capture=capture)
            if result.ok:
                record["retrieved_at"] = result.retrieved_at
                record["final_redirected_url"] = result.final_url
                _append_unique(record["archived_urls"], capture["replay_url"])
                quality = await self._extract_into_async(record, result, "wayback_capture")
                if quality["accepted"]:
                    fetcher.note_application_failure(capture["replay_url"], False, "successful_archive_response")
                    record["retrieval_status"] = resolved_status(quality)
                    record["failure_reason"] = None
                    return record
                fetcher.note_application_failure(capture["replay_url"], False, "capture_low_quality_content")
        reason = "no_wayback_capture" if lookup.get("ok") else "wayback_lookup_unavailable"
        return self._defer_recovery(record, reason)

    def _cache_hit(self, source_id: str, previous: dict[str, Any]) -> bool:
        original_url = previous.get("original_url") or ""
        previous_text = clean_text(previous.get("text_original") or "")
        previous_hash = str(previous.get("content_hash") or "")
        return bool(
            previous_text
            and previous_hash
            and sha256_text(previous_text) == previous_hash
            and previous.get("retrieval_status") in RESOLVED_SOURCE_STATUSES
        )

    def _hydrate_from_cache(self, record: dict[str, Any]) -> bool:
        cache_key = canonical_fetch_key(record.get("original_url") or "")
        entry = self.cache.get(cache_key)
        cached_source_id = str(entry.get("source_id") or "")
        if (
            not cached_source_id
            or cached_source_id == record.get("source_id")
            or not entry.get("has_original_text")
            or str(entry.get("retrieval_status") or "") not in RESOLVED_SOURCE_STATUSES
        ):
            return False
        cached = load_json(self.root / "data" / "sources" / f"{cached_source_id}.json", {}) or {}
        cached_text = clean_text(cached.get("text_original") or "")
        cached_hash = str(cached.get("content_hash") or "")
        if (
            not cached_text
            or not cached_hash
            or sha256_text(cached_text) != cached_hash
            or canonical_fetch_key(cached.get("original_url") or "") != cache_key
        ):
            return False
        record["text_original"] = cached_text
        record["content_hash"] = cached_hash
        record["text_original_language"] = cached.get("text_original_language") or "und"
        record["content_variants"] = deepcopy(cached.get("content_variants") or [])
        record["retrieval_status"] = "cached"
        record["extraction_status"] = "persistent_content_cache_hit"
        record["preservation_status"] = cached.get("preservation_status") or "cached_text_preserved"
        record["retrieved_at"] = cached.get("retrieved_at")
        record["attempt_history"].append({
            "attempted_at": utc_now(),
            "url": record["original_url"],
            "attempt_role": "canonical_content_cache",
            "result": "persistent_content_cache_hit",
            "cached_source_id": cached_source_id,
            "engine_version": ENGINE_VERSION,
            "run_id": self.run_id,
            "attempts": 0,
        })
        return True

    def _hydrate_negative_cache(self, record: dict[str, Any]) -> bool:
        entry = self.cache.get(canonical_fetch_key(record.get("original_url") or ""))
        retry_after_epoch = float(entry.get("retry_after_epoch") or 0)
        if entry.get("has_original_text") or retry_after_epoch <= time.time():
            return False
        reason = str(entry.get("deferred_reason") or "recent_public_retrieval_unavailable")
        self._defer_recovery(record, f"negative_cache:{reason}")
        record.setdefault("attempt_history", []).append({
            "attempted_at": utc_now(),
            "attempt_role": "negative_cache",
            "result": "retry_window_not_due",
            "retry_after_epoch": retry_after_epoch,
            "run_id": self.run_id,
            "engine_version": ENGINE_VERSION,
            "attempts": 0,
        })
        return True

    def _update_cache(self, record: dict[str, Any]) -> None:
        normalized = normalize_source_url(record.get("original_url") or "")
        if not normalized:
            return
        cache_key = canonical_fetch_key(normalized)
        existing = self.cache.get(cache_key)
        cache_text = clean_text(record.get("text_original") or "")
        cache_hash = str(record.get("content_hash") or "")
        # Never let a stale or malformed digest become a trusted cross-job
        # cache hit. Source files remain preserved for review, but only
        # cryptographically self-consistent text is promoted into the cache.
        has_text = bool(
            cache_text
            and cache_hash
            and sha256_text(cache_text) == cache_hash
            and record.get("retrieval_status") in RESOLVED_SOURCE_STATUSES
        )
        if record.get("retrieval_status") in RESOLVED_SOURCE_STATUSES and not has_text:
            return
        if existing.get("has_original_text") and not has_text:
            return
        current_quality = int((record.get("content_quality") or {}).get("score") or 0)
        if (
            existing.get("has_original_text")
            and has_text
            and (
                int(existing.get("quality_score") or 0) > current_quality
                or (
                    existing.get("retrieval_status") == "successful"
                    and record.get("retrieval_status") == "successful_partial"
                )
            )
        ):
            return
        recovery_reason = str((record.get("recovery") or {}).get("reason") or record.get("failure_reason") or "")
        if has_text or record.get("retrieval_status") in POLICY_COMPLETE_SOURCE_STATUSES:
            retry_after_epoch = 0.0
        elif any(token in recovery_reason for token in ("not_found", "gone", "no_wayback_capture")):
            retry_after_epoch = time.time() + 7 * 86400
        elif any(token in recovery_reason for token in ("login", "blocked", "forbidden")):
            retry_after_epoch = time.time() + 12 * 3600
        elif record.get("retrieval_status") in OPERATIONAL_FAILURE_STATUSES:
            retry_after_epoch = time.time() + 15 * 60
        else:
            retry_after_epoch = time.time() + 6 * 3600
        entry = {
            "source_id": record["source_id"],
            "retrieval_status": record.get("retrieval_status"),
            "content_hash": record.get("content_hash"),
            "final_url": record.get("final_redirected_url"),
            "etag": next((row.get("etag") for row in reversed(record.get("attempt_history") or []) if row.get("etag")), ""),
            "last_modified": next((row.get("last_modified") for row in reversed(record.get("attempt_history") or []) if row.get("last_modified")), ""),
            "last_attempt_at": record.get("retrieved_at") or utc_now(),
            "has_original_text": has_text,
            "quality_score": current_quality,
            "deferred_reason": recovery_reason,
            "retry_after_epoch": retry_after_epoch,
            "engine_version": ENGINE_VERSION,
        }
        self.cache.set(normalized, entry)
        self.cache.set(cache_key, entry)

    def _finish_source(self, record: dict[str, Any], started: float, outcome: dict[str, Any]) -> None:
        source_id = record["source_id"]
        was_completed = source_id in self._source_completed_set
        duration = round(time.monotonic() - started, 3)
        finished_at = utc_now()
        quality = record.get("content_quality") or {}
        attempts = [
            row for row in record.get("attempt_history") or []
            if isinstance(row, dict) and row.get("run_id") == self.run_id
        ]
        network_seconds = round(sum(float(row.get("elapsed_seconds") or 0) for row in attempts), 3)
        queue_seconds = round(sum(float(row.get("scheduler_waiting_seconds") or row.get("queue_waiting_seconds") or 0) for row in attempts), 3)
        pacing_seconds = round(sum(float(row.get("pacing_waiting_seconds") or 0) for row in attempts), 3)
        retry_seconds = round(sum(float(row.get("retry_waiting_seconds") or 0) for row in attempts), 3)
        provenance = quality.get("provenance") or record.get("extraction_status") or ""
        status = str(record.get("retrieval_status") or "internal_error")
        if status in RESOLVED_SOURCE_STATUSES:
            resolution_class = "content_resolved"
            self.recovery.get("items", {}).pop(source_id, None)
        elif status in POLICY_COMPLETE_SOURCE_STATUSES:
            resolution_class = "policy_complete"
            self.recovery.get("items", {}).pop(source_id, None)
        elif status in DEFERRED_SOURCE_STATUSES:
            resolution_class = "recovery_deferred"
            recovery_item = record.get("recovery")
            if isinstance(recovery_item, dict):
                self.recovery.setdefault("items", {})[source_id] = deepcopy(recovery_item)
        else:
            resolution_class = "operational_error"
        outcome = {
            **outcome,
            "duration_seconds": duration,
            "text_preserved": bool(record.get("text_original")),
            "quality_score": quality.get("score"),
            "source_type": record.get("source_type"),
            "host": urllib.parse.urlsplit(record.get("original_url") or "").hostname or "",
            "network_seconds": network_seconds,
            "queue_seconds": queue_seconds,
            "pacing_seconds": pacing_seconds,
            "retry_seconds": retry_seconds,
            "provenance": provenance,
            "resolution_class": resolution_class,
        }
        record["collection_checkpoint"] = {
            "engine_version": ENGINE_VERSION,
            "scope": self.key,
            "finished_at": finished_at,
            "outcome": outcome,
        }
        persist_started = time.monotonic()
        atomic_write_json(self.root / "data" / "sources" / f"{source_id}.json", record)
        persist_seconds = round(time.monotonic() - persist_started, 3)
        outcome["persist_seconds"] = persist_seconds
        timing_row = {
            "source_id": source_id,
            "duration_seconds": duration,
            "status": record.get("retrieval_status"),
            "attempts": sum(int(row.get("attempts") or 0) for row in attempts),
            "finished_at": finished_at,
        }
        timing_index = self._source_timing_index.get(source_id)
        if timing_index is None:
            self._source_timing_index[source_id] = len(self.progress["source_timings"])
            self.progress["source_timings"].append(timing_row)
        else:
            self.progress["source_timings"][timing_index] = timing_row
        if not was_completed:
            aggregate = self.progress["source_timing_aggregate"]
            aggregate["count"] = int(aggregate.get("count") or 0) + 1
            aggregate["total_seconds"] = round(
                float(aggregate.get("total_seconds") or 0) + duration, 3
            )
            self._source_completed_set.add(source_id)
            self.progress["source_completed_ids"].append(source_id)
        if len(self.progress["source_timings"]) > RECENT_TIMING_LIMIT + 1_000:
            self.progress["source_timings"] = self.progress["source_timings"][-RECENT_TIMING_LIMIT:]
            self._source_timing_index = {
                str(row.get("source_id")): index
                for index, row in enumerate(self.progress["source_timings"])
                if row.get("source_id")
            }
        # Keep the resumable progress index compact for 100k+ sources.  Rich
        # provenance lives in the source JSON and the control-plane item; this
        # index only needs the fields used for resume/report/recovery.
        self.progress["source_outcomes"][source_id] = {
            "finished_at": finished_at,
            "status": status,
            "cache_hit": bool(outcome.get("cache_hit")),
            "archive_deferred": bool(outcome.get("archive_deferred")),
        }
        self._update_cache(record)
        self._dirty_checkpoints += 1
        self._emit_progress({
            "kind": "source",
            "identity": source_id,
            "status": status,
            "duration_seconds": duration,
            "attempts": timing_row["attempts"],
            "error": None if status in SUCCESS_SOURCE_STATUSES else record.get("failure_reason") or status,
            "detail": {
                "cache_hit": bool(outcome.get("cache_hit")),
                "archive_deferred": bool(outcome.get("archive_deferred")),
                "text_preserved": outcome["text_preserved"],
                "quality_score": outcome["quality_score"],
                "source_type": outcome["source_type"],
                "host": outcome["host"],
                "network_seconds": network_seconds,
                "queue_seconds": queue_seconds,
                "pacing_seconds": pacing_seconds,
                "retry_seconds": retry_seconds,
                "persist_seconds": persist_seconds,
                "provenance": provenance,
                "resolution_class": resolution_class,
            },
        })
        self.flush_checkpoints()

    async def _collect_source_batch(self, records: list[dict[str, Any]]) -> None:
        fetcher = AsyncHostFetcher(
            delay_seconds=self.delay,
            timeout_seconds=self.timeout,
            retries=self.retries,
            workers=self.workers,
            per_host_workers=self.per_host_workers,
            circuit_state=self.progress.get("source_fetch_stats", {}).get("circuit_state"),
            circuit_threshold=5,
            circuit_reprobe_every=250,
            host_policies=default_host_policies(
                self.per_host_workers,
                self.social_workers,
                self.archive_workers,
                self.delay,
            ),
        )
        records = fair_source_order(records)
        write_queue: asyncio.Queue[tuple[dict[str, Any], float, dict[str, Any]] | None] = asyncio.Queue(
            maxsize=max(32, self.workers * 4)
        )
        archive_gate = asyncio.Semaphore(self.archive_workers)
        stop_event = asyncio.Event()
        writer_errors: list[Exception] = []

        async def live_job(record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            try:
                return await self._live_source(fetcher, record)
            except Exception as error:
                record["retrieval_status"] = "internal_error"
                record["failure_reason"] = f"{type(error).__name__}:{error}"
                record.setdefault("attempt_history", []).append({
                    "attempted_at": utc_now(),
                    "attempt_role": "live_source",
                    "result": "task_failed",
                    "error": record["failure_reason"],
                    "engine_version": ENGINE_VERSION,
                    "run_id": self.run_id,
                    "attempts": 0,
                })
                return record, False

        async def archive_job(record: dict[str, Any]) -> dict[str, Any]:
            try:
                return await self._archive_source(fetcher, record)
            except Exception as error:
                record["retrieval_status"] = "internal_error"
                record["failure_reason"] = f"{type(error).__name__}:{error}"
                record.setdefault("attempt_history", []).append({
                    "attempted_at": utc_now(),
                    "attempt_role": "archive_retry",
                    "result": "task_failed",
                    "error": record["failure_reason"],
                    "engine_version": ENGINE_VERSION,
                    "run_id": self.run_id,
                    "attempts": 0,
                })
                return record

        async def process_record(record: dict[str, Any]) -> None:
            if stop_event.is_set() or self._stop_requested():
                return
            started = time.monotonic()
            record, needs_archive = await live_job(record)
            if needs_archive and not stop_event.is_set() and not self._stop_requested():
                async with archive_gate:
                    record = await archive_job(record)
                outcome = {
                    "cache_hit": False,
                    "archive_deferred": record.get("retrieval_status") == "recovery_deferred",
                    "status": record.get("retrieval_status"),
                }
            elif needs_archive:
                return
            else:
                deferred = (record.get("archive_retry") or {}).get("status") == "deferred_low_priority"
                outcome = {
                    "cache_hit": False,
                    "archive_deferred": deferred,
                    "status": record.get("retrieval_status"),
                }
            await write_queue.put((record, started, outcome))

        async def writer() -> None:
            while True:
                item = await write_queue.get()
                if item is None:
                    write_queue.task_done()
                    return
                try:
                    record, started, outcome = item
                    try:
                        await asyncio.to_thread(self._finish_source, record, started, outcome)
                    except Exception as error:
                        writer_errors.append(error)
                finally:
                    write_queue.task_done()

        async def control_watcher() -> None:
            while not stop_event.is_set():
                refresh = getattr(self.progress_callback, "refresh_control", None)
                if callable(refresh):
                    await asyncio.to_thread(refresh)
                if self._stop_requested():
                    stop_event.set()
                    return
                await asyncio.sleep(0.25)

        async with fetcher:
            writer_task = asyncio.create_task(writer())
            watcher_task = asyncio.create_task(control_watcher())
            iterator = iter(records)
            inflight: set[asyncio.Task[None]] = set()
            max_inflight = min(len(records), max(self.workers * 4, 64))

            def fill_window() -> None:
                while not stop_event.is_set() and len(inflight) < max_inflight:
                    try:
                        record = next(iterator)
                    except StopIteration:
                        return
                    inflight.add(asyncio.create_task(process_record(record)))

            fill_window()
            while inflight:
                done, inflight = await asyncio.wait(
                    inflight,
                    timeout=0.25,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    await task
                if stop_event.is_set() or self._stop_requested():
                    stop_event.set()
                    for task in inflight:
                        task.cancel()
                    await asyncio.gather(*inflight, return_exceptions=True)
                    inflight.clear()
                    break
                fill_window()

            await write_queue.join()
            write_queue.put_nowait(None)
            await writer_task
            stop_event.set()
            watcher_task.cancel()
            await asyncio.gather(watcher_task, return_exceptions=True)
            if writer_errors:
                raise RuntimeError(
                    f"source_writer_failed:{type(writer_errors[0]).__name__}:{writer_errors[0]}"
                )
        self.progress["source_fetch_stats"] = _merge_stats(self.progress.get("source_fetch_stats", {}), fetcher.stats())
        self.flush_checkpoints(force=True)

    def collect_sources(self, max_items: int | None = None) -> dict[str, Any]:
        seeds, incident_sources, exact_urls = self._source_seeds()
        self._emit_progress({
            "kind": "catalog",
            "source_total": len(seeds),
            "incident_total": len(self.sequences),
            "catalog_path": str(self.source_catalog_path.relative_to(self.root)),
        })
        pending = [source_id for source_id in sorted(seeds) if source_id not in self._source_completed_set]
        if max_items is not None:
            pending = pending[:max_items]
        completed_before = len(self._source_completed_set & set(seeds))
        network_records: list[dict[str, Any]] = []
        for source_id in pending:
            if self._stop_requested():
                break
            started = time.monotonic()
            path = self.root / "data" / "sources" / f"{source_id}.json"
            previous = load_json(path, {}) or {}
            record = _merge_previous_source(seeds[source_id], previous)
            checkpoint = previous.get("collection_checkpoint") or {}
            if checkpoint.get("scope") == self.key and checkpoint.get("engine_version") == ENGINE_VERSION:
                recovered_outcome = dict(checkpoint.get("outcome") or {})
                recovered_outcome["recovered_after_interruption"] = True
                recovered_outcome.setdefault("status", record.get("retrieval_status"))
                self._finish_source(record, started, recovered_outcome)
                continue
            previous_status = str(previous.get("retrieval_status") or "")
            previous_content_is_resolved = (
                previous_status in RESOLVED_SOURCE_STATUSES
                and bool(previous.get("text_original"))
                and bool(previous.get("content_hash"))
            )
            previous_is_policy_complete = previous_status in POLICY_COMPLETE_SOURCE_STATUSES
            if previous_content_is_resolved or previous_is_policy_complete:
                record["cache_status"] = "existing_modern_source_record_reused"
                record["retrieval_status"] = "cached" if previous_content_is_resolved else previous_status
                record.setdefault("attempt_history", []).append({
                    "attempted_at": utc_now(),
                    "url": record.get("original_url"),
                    "attempt_role": "existing_record",
                    "result": "existing_modern_source_record_reused",
                    "engine_version": ENGINE_VERSION,
                    "run_id": self.run_id,
                    "attempts": 0,
                })
                self._finish_source(record, started, {
                    "cache_hit": True,
                    "archive_deferred": False,
                    "status": record["retrieval_status"],
                })
                continue
            if self._cache_hit(source_id, previous):
                record["cache_status"] = "persistent_url_cache_hit"
                record["retrieval_status"] = "cached"
                record["attempt_history"].append({
                    "attempted_at": utc_now(),
                    "url": record["original_url"],
                    "attempt_role": "cache",
                    "result": "persistent_url_cache_hit",
                    "engine_version": ENGINE_VERSION,
                    "run_id": self.run_id,
                    "attempts": 0,
                })
                self._finish_source(record, started, {
                    "cache_hit": True,
                    "archive_deferred": False,
                    "status": record.get("retrieval_status"),
                })
                continue
            if self._hydrate_from_cache(record):
                self._finish_source(record, started, {
                    "cache_hit": True,
                    "archive_deferred": False,
                    "status": "cached",
                })
                continue
            if self._hydrate_negative_cache(record):
                self._finish_source(record, started, {
                    "cache_hit": True,
                    "archive_deferred": True,
                    "status": "recovery_deferred",
                })
                continue
            if record["source_type"] in {"direct_image_url", "direct_video_url", "direct_audio_url"}:
                record["retrieval_status"] = "media_metadata_preserved"
                record["extraction_status"] = "media_metadata_only"
                record["preservation_status"] = "external_only"
                record["failure_reason"] = "media_binary_download_prohibited"
                record["attempt_history"].append({
                    "attempted_at": utc_now(),
                    "url": record["original_url"],
                    "result": "not_downloaded_by_media_policy",
                    "engine_version": ENGINE_VERSION,
                    "run_id": self.run_id,
                    "attempts": 0,
                })
                self._finish_source(record, started, {
                    "cache_hit": False,
                    "archive_deferred": False,
                    "status": record["retrieval_status"],
                })
                continue
            network_records.append(record)
        if network_records:
            asyncio.run(self._collect_source_batch(network_records))
        self.flush_checkpoints(force=True)
        completed_count = len(self._source_completed_set & set(seeds))
        done = completed_count == len(seeds)
        if done:
            relationships = [
                {"incident_id": incident_id, "source_id": source_id}
                for incident_id, source_ids in sorted(incident_sources.items())
                for source_id in sorted(source_ids)
            ]
            atomic_write_json(self.relationship_path, {
                "schema_version": PILOT_SCHEMA_VERSION,
                "scope": self.key,
                "relationships": relationships,
            })
            atomic_write_json(self.root / "data" / "reports" / f"speed-pilot-{self.key}-exact-url-duplicates.json", {
                "exact_duplicate_relationship_count": sum(max(0, len(values) - 1) for values in exact_urls.values()),
                "urls": {key: values for key, values in exact_urls.items() if len(values) > 1},
            })
            duplicate_groups, duplicate_records_updated = _record_exact_content_duplicate_groups(self.root / "data" / "sources")
        else:
            duplicate_groups, duplicate_records_updated = 0, 0
        return {
            "done": done,
            "processed": completed_count - completed_before,
            "completed": completed_count,
            "total": len(seeds),
            "exact_content_duplicate_groups": duplicate_groups,
            "duplicate_records_updated": duplicate_records_updated,
        }

    def write_report(self) -> dict[str, Any]:
        seeds, _, _ = self._source_seeds()
        items = self._manifest_items()
        incidents = [load_json(self.root / "data" / "incidents" / f"{items[sequence]['internal_id']}.json", {}) or {} for sequence in self.sequences]
        sources = [load_json(self.root / "data" / "sources" / f"{source_id}.json", {}) or {} for source_id in sorted(seeds)]
        incident_timings = [row for row in self.progress["incident_timings"] if int(row.get("sequence") or 0) in self.sequences]
        source_timings = [row for row in self.progress["source_timings"] if row.get("source_id") in seeds]
        incident_wall = sum(float(row.get("duration_seconds") or 0) for row in self.progress["stage_runs"] if row.get("stage") == "incident_collection")
        source_wall = sum(float(row.get("duration_seconds") or 0) for row in self.progress["stage_runs"] if row.get("stage") == "source_collection")
        actual_wall = incident_wall + source_wall
        baseline = load_json(self.root / "data" / "reports" / "first-100-timing.json", {}) or {}
        baseline_incident_mean = float((baseline.get("incident") or {}).get("mean_seconds") or 0)
        baseline_source_mean = float((baseline.get("source") or {}).get("mean_seconds") or 0)
        baseline_equivalent = baseline_incident_mean * len(self.sequences) + baseline_source_mean * len(sources)
        outcomes = self.progress.get("source_outcomes") or {}
        report = {
            "generated_at": utc_now(),
            "engine_version": ENGINE_VERSION,
            "scope": self.progress["scope"],
            "translation_policy": "disabled_by_user",
            "media_binaries_downloaded": 0,
            "configuration": {
                "global_workers": self.workers,
                "per_host_workers": self.per_host_workers,
                "social_workers": self.social_workers,
                "archive_workers": self.archive_workers,
                "checkpoint_every": self.checkpoint_every,
                "per_host_delay_seconds": self.delay,
                "timeout_seconds": self.timeout,
                "fast_timeout_seconds": self.fast_timeout,
                "incident_mode": self.incident_mode,
                "inline_wayback": self.inline_wayback,
                "retries": self.retries,
                "passes": [
                    "cache_and_existing_text",
                    "public_social_adapters",
                    "live_non_social",
                    "listed_archive",
                    "durable_recovery_queue",
                ],
                "source_catalog": str(self.source_catalog_path.relative_to(self.root)),
            },
            "incidents": {
                "count": len(incidents),
                "status_counts": dict(Counter(row.get("completeness_status") or "missing" for row in incidents)),
                "mean_seconds": round(statistics.mean(float(row.get("duration_seconds") or 0) for row in incident_timings), 3) if incident_timings else 0,
                "wall_seconds": round(incident_wall, 3),
            },
            "sources": {
                "unique": len(sources),
                "status_counts": dict(Counter(row.get("retrieval_status") or "missing" for row in sources)),
                "texts_preserved": sum(bool(row.get("text_original")) for row in sources),
                "content_resolved": sum(
                    (outcomes.get(source_id) or {}).get("status") in RESOLVED_SOURCE_STATUSES
                    for source_id in seeds
                ),
                "recovery_deferred": sum(
                    (outcomes.get(source_id) or {}).get("status") in DEFERRED_SOURCE_STATUSES
                    for source_id in seeds
                ),
                "operational_errors": sum(
                    (outcomes.get(source_id) or {}).get("status") in OPERATIONAL_FAILURE_STATUSES
                    for source_id in seeds
                ),
                "cache_hits": sum(bool((outcomes.get(source_id) or {}).get("cache_hit")) for source_id in seeds),
                "archive_deferred_existing_text": sum(bool((outcomes.get(source_id) or {}).get("archive_deferred")) for source_id in seeds),
                "mean_task_seconds": round(
                    float((self.progress.get("source_timing_aggregate") or {}).get("total_seconds") or 0)
                    / max(1, int((self.progress.get("source_timing_aggregate") or {}).get("count") or 0)),
                    3,
                ),
                "wall_seconds": round(source_wall, 3),
            },
            "fetching": {
                "incident": self.progress.get("incident_fetch_stats") or {},
                "source": self.progress.get("source_fetch_stats") or {},
            },
            "comparison": {
                "baseline": "first-100 serial collector normalized to this incident and source count",
                "baseline_equivalent_seconds": round(baseline_equivalent, 3),
                "actual_collection_wall_seconds": round(actual_wall, 3),
                "speedup_factor": round(baseline_equivalent / actual_wall, 3) if actual_wall else None,
                "time_saved_seconds": round(max(0.0, baseline_equivalent - actual_wall), 3),
            },
            "validation": {
                "all_scope_incidents_present": len(incidents) == len(self.sequences) and all(bool(row) for row in incidents),
                "all_sources_present": len(sources) == len(seeds) and all(bool(row) for row in sources),
                "outside_scope_sequences_processed": sorted(set(int(value) for value in self.progress["incident_completed_sequences"]) - set(self.sequences)),
                "generated_translations": 0,
                "media_binary_bytes": 0,
            },
        }
        atomic_write_json(self.report_path, report)
        markdown = [
            f"# Speed pilot {self.key}",
            "",
            f"- Incidents: **{len(incidents)}**",
            f"- Unique sources: **{len(sources)}**",
            f"- Preserved source texts: **{report['sources']['texts_preserved']}**",
            f"- Deferred recovery queue: **{report['sources']['recovery_deferred']}**",
            f"- Operational errors: **{report['sources']['operational_errors']}**",
            f"- Persistent cache hits: **{report['sources']['cache_hits']}**",
            f"- Archive lookups deferred because text already existed: **{report['sources']['archive_deferred_existing_text']}**",
            f"- Actual collection wall clock: **{report['comparison']['actual_collection_wall_seconds']} seconds**",
            f"- Normalized serial baseline: **{report['comparison']['baseline_equivalent_seconds']} seconds**",
            f"- Measured speedup: **{report['comparison']['speedup_factor']}x**",
            "- Generated translations: **0**",
            "- Downloaded media binaries: **0**",
            "",
        ]
        self.report_markdown_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_markdown_path.write_text("\n".join(markdown), encoding="utf-8")
        self.progress["finished_at"] = utc_now()
        self.progress["result"] = "complete"
        self.save_progress()
        return report

    def run(self, stage: str, max_items: int | None = None) -> dict[str, Any]:
        stages: dict[str, tuple[str, Callable[[], Any]]] = {
            "manifest": ("manifest", self.build_manifest),
            "incidents": ("incident_collection", lambda: self.collect_incidents(max_items)),
            "sources": ("source_collection", lambda: self.collect_sources(max_items)),
            "report": ("report", lambda: {"done": True, "report": self.write_report()}),
        }
        stage_name, function = stages[stage]
        payload = self._stage(stage_name, function) or {"done": True}
        result = {"stage": stage, **payload, "recorded_at": utc_now()}
        atomic_write_json(self.result_path, result)
        print("SPEED_PILOT_STAGE_RESULT=" + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a resumable, host-aware collection speed pilot")
    parser.add_argument("--legacy-zip", required=True)
    parser.add_argument("--output-root", default=".")
    parser.add_argument("--first-sequence", type=int, default=DEFAULT_FIRST_SEQUENCE)
    parser.add_argument("--last-sequence", type=int, default=DEFAULT_LAST_SEQUENCE)
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--timeout", type=float, default=6.0)
    parser.add_argument("--fast-timeout", type=float, default=3.0)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--per-host-workers", type=int, default=4)
    parser.add_argument("--social-workers", type=int, default=12)
    parser.add_argument("--archive-workers", type=int, default=12)
    parser.add_argument("--incident-mode", choices=("snapshot_first", "network_refresh"), default="snapshot_first")
    parser.add_argument("--inline-wayback", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=5000)
    parser.add_argument("--stage", choices=("manifest", "incidents", "sources", "report"), required=True)
    parser.add_argument("--max-items", type=int)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.max_items is not None and args.max_items < 1:
        raise SystemExit("--max-items must be at least 1")
    runner = SpeedPilotRunner(
        Path(args.output_root),
        Path(args.legacy_zip),
        first_sequence=args.first_sequence,
        last_sequence=args.last_sequence,
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
        workers=args.workers,
        per_host_workers=args.per_host_workers,
        social_workers=args.social_workers,
        archive_workers=args.archive_workers,
        fast_timeout=args.fast_timeout,
        incident_mode=args.incident_mode,
        inline_wayback=args.inline_wayback,
        checkpoint_every=args.checkpoint_every,
    )
    try:
        runner.run(args.stage, args.max_items)
    finally:
        runner.close()


if __name__ == "__main__":
    main()
