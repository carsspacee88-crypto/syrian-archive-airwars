from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from . import PILOT_PARSER_VERSION, PILOT_SCHEMA_VERSION, TRANSLATION_VERSION
from .collector import collect_one
from .extractors import extract_payload
from .fetcher import AsyncHostFetcher, CircuitBreakingFetcher, FetchResult
from .io_utils import atomic_write_json, clean_text, load_json, sha256_bytes, utc_now
from .legacy import LegacyArchive
from .normalize import finalize_status
from .pilot import (
    DIRECT_AUDIO_SUFFIXES,
    DIRECT_IMAGE_SUFFIXES,
    DIRECT_VIDEO_SUFFIXES,
    LOGIN_WALL_MARKERS,
    _append_unique,
    _classify_fetch_failure,
    _legacy_source_seed,
    _normalized_source_seed,
    _record_exact_url_observations,
    _source_variant,
    classify_source_type,
    detect_language,
    normalize_source_url,
    stable_source_id,
)
from .v4 import (
    ENGINE_VERSION,
    RecoveryQueue,
    fair_host_order,
    source_has_text,
    source_host,
    source_is_policy_complete,
    timing_summary,
)


DEFAULT_FIRST_SEQUENCE = 101
DEFAULT_LAST_SEQUENCE = 150


def _scope_key(first_sequence: int, last_sequence: int) -> str:
    return f"{first_sequence:04d}-{last_sequence:04d}"


def _scope_sequences(first_sequence: int, last_sequence: int) -> tuple[int, ...]:
    if first_sequence < 1 or last_sequence < first_sequence or last_sequence > 8114:
        raise ValueError(f"invalid_speed_pilot_scope:{first_sequence}-{last_sequence}")
    return tuple(range(first_sequence, last_sequence + 1))


def should_defer_archive(record: dict[str, Any]) -> bool:
    """Archived lookup is low priority when a complete original text is already preserved."""
    return bool(clean_text(record.get("text_original") or ""))


def _merge_stats(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = dict(previous or {})
    for key in ("requests", "retries", "circuit_skips"):
        merged[key] = int(merged.get(key) or 0) + int(current.get(key) or 0)
    merged["waiting_seconds"] = round(float(merged.get("waiting_seconds") or 0) + float(current.get("waiting_seconds") or 0), 3)
    merged["circuit_state"] = deepcopy(current.get("circuit_state") or merged.get("circuit_state") or {})
    return merged


def _inherit_airwars_circuit_classification(records: list[dict[str, Any]]) -> int:
    """Annotate skipped Airwars requests from explicit same-batch 403 evidence."""
    retrieval_keys = ("airwars_endpoint", "live_page")
    observed_403 = any(
        (record.get("retrieval_status") or {}).get(key, {}).get("status") == 403
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
        "failure_reason", "pdf", "timing",
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
        legacy_zip: Path,
        first_sequence: int = DEFAULT_FIRST_SEQUENCE,
        last_sequence: int = DEFAULT_LAST_SEQUENCE,
        delay: float = 0.75,
        timeout: float = 10.0,
        retries: int = 1,
        workers: int = 64,
        per_host_workers: int = 1,
    ):
        self.root = root.resolve()
        self.legacy_zip = legacy_zip.resolve()
        self.first_sequence = first_sequence
        self.last_sequence = last_sequence
        self.sequences = _scope_sequences(first_sequence, last_sequence)
        self.key = _scope_key(first_sequence, last_sequence)
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.workers = workers
        self.per_host_workers = per_host_workers
        self.progress_path = self.root / "data" / "pilot" / f"speed-pilot-{self.key}-progress.json"
        self.manifest_path = self.root / "data" / "pilot" / f"speed-pilot-{self.key}-manifest.json"
        self.result_path = self.root / "data" / "pilot" / f"speed-pilot-{self.key}-stage-result.json"
        self.cache_path = self.root / "data" / "cache" / "source-url-index.json"
        self.report_path = self.root / "data" / "reports" / f"speed-pilot-{self.key}.json"
        self.report_markdown_path = self.report_path.with_suffix(".md")
        self.relationship_path = self.root / "data" / "relationships" / f"incident-sources-{self.key}.json"
        self.recovery_path = self.root / "data" / "recovery" / f"v4-{self.key}.json"
        self.progress = load_json(self.progress_path, {}) or {}
        self.progress.setdefault("engine_version", ENGINE_VERSION)
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
        self.progress.setdefault("source_outcomes", {})
        self.progress.setdefault("stage_runs", [])
        self.progress.setdefault("incident_fetch_stats", {})
        self.progress.setdefault("source_fetch_stats", {})
        self.progress.setdefault("recovery_runs", [])
        self.cache = load_json(self.cache_path, {}) or {}
        self.cache.setdefault("schema_version", "1.0.0")
        self.cache.setdefault("urls", {})
        self.recovery = RecoveryQueue(self.recovery_path, self.progress["scope"])

    def save_progress(self) -> None:
        self.progress["engine_version"] = ENGINE_VERSION
        self.progress["updated_at"] = utc_now()
        atomic_write_json(self.progress_path, self.progress)

    def save_cache(self) -> None:
        self.cache["updated_at"] = utc_now()
        atomic_write_json(self.cache_path, self.cache)

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
        with LegacyArchive(self.legacy_zip) as archive:
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

    def collect_incidents(self, max_items: int | None = None) -> dict[str, Any]:
        items = self._manifest_items()
        if set(items) != set(self.sequences):
            raise ValueError("speed_pilot_manifest_missing_or_invalid")
        completed = {int(value) for value in self.progress["incident_completed_sequences"]}
        fetcher = CircuitBreakingFetcher(
            delay_seconds=self.delay,
            timeout_seconds=self.timeout,
            retries=self.retries,
            circuit_state=self.progress.get("incident_fetch_stats", {}).get("circuit_state"),
            circuit_threshold=3,
            circuit_reprobe_every=max(10, len(self.sequences) * 2),
        )
        processed = 0
        with LegacyArchive(self.legacy_zip) as archive:
            for sequence in self.sequences:
                item = items[sequence]
                path = self.root / "data" / "incidents" / f"{item['internal_id']}.json"
                if sequence in completed and path.is_file():
                    continue
                started = time.monotonic()
                status = "failed"
                error_text = ""
                try:
                    record = collect_one(archive, sequence, self.root, fetcher)
                    legacy = archive.case_data(sequence)
                    record["pilot"] = {
                        "name": f"speed-pilot-{self.key}",
                        "in_scope": True,
                        "sequence": sequence,
                        "parser_version": PILOT_PARSER_VERSION,
                        "translation_policy": "disabled_by_user",
                    }
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
                    atomic_write_json(path, record)
                    status = record.get("completeness_status") or "partial"
                except Exception as error:
                    error_text = f"{type(error).__name__}:{error}"
                duration = round(time.monotonic() - started, 3)
                self.progress["incident_timings"] = [row for row in self.progress["incident_timings"] if row.get("sequence") != sequence]
                self.progress["incident_timings"].append({
                    "sequence": sequence,
                    "internal_id": item["internal_id"],
                    "duration_seconds": duration,
                    "status": status,
                    "error": error_text or None,
                    "finished_at": utc_now(),
                })
                if sequence not in self.progress["incident_completed_sequences"]:
                    self.progress["incident_completed_sequences"].append(sequence)
                    self.progress["incident_completed_sequences"].sort()
                self.progress["incident_fetch_stats"] = _merge_stats(self.progress.get("incident_fetch_stats", {}), fetcher.stats())
                # Numeric counters are already merged; reset the current fetcher counters before the next checkpoint.
                fetcher.inner.total_requests = 0
                fetcher.inner.total_retries = 0
                fetcher.inner.total_waiting_seconds = 0.0
                fetcher.circuit_skips = 0
                self.save_progress()
                print(f"speed incident [{processed + 1}/{len(self.sequences)}] {sequence:04d} -> {status}", flush=True)
                processed += 1
                if max_items is not None and processed >= max_items:
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
        complete = len(set(self.progress["incident_completed_sequences"]) & set(self.sequences))
        return {
            "done": complete == len(self.sequences),
            "processed": processed,
            "completed": complete,
            "total": len(self.sequences),
            "reclassified_circuit_records": repaired,
        }

    def _source_seeds(self) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], dict[str, list[str]]]:
        items = self._manifest_items()
        records: dict[str, dict[str, Any]] = {}
        incident_sources: dict[str, list[str]] = defaultdict(list)
        exact_urls: dict[str, list[str]] = defaultdict(list)
        with LegacyArchive(self.legacy_zip) as archive:
            for sequence in self.sequences:
                manifest_item = items[sequence]
                incident_id = manifest_item["internal_id"]
                normalized = load_json(self.root / "data" / "incidents" / f"{incident_id}.json", {}) or {}
                legacy_seeds = [_legacy_source_seed(raw) for raw in archive.case_data(sequence).get("sources", [])]
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

    @staticmethod
    def _attempt(record: dict[str, Any], result: FetchResult, role: str, **extra: Any) -> None:
        metadata = result.metadata()
        metadata["ok"] = result.ok
        metadata["attempt_role"] = role
        metadata.update(extra)
        record.setdefault("attempt_history", []).append(metadata)

    @staticmethod
    def _extract_into(record: dict[str, Any], result: FetchResult, provenance: str) -> None:
        extraction_started = time.monotonic()
        try:
            extracted = extract_payload(
                result.body,
                result.content_type,
                result.final_url,
                str(record.get("source_type") or ""),
            )
            detected_format = str(extracted.get("format") or "unsupported")
            record["detected_format"] = detected_format
            if detected_format == "pdf":
                record["pdf"] = {
                    "byte_size": len(result.body),
                    "sha256": sha256_bytes(result.body),
                    "page_count": extracted.get("page_count"),
                    "ocr_pending": extracted.get("ocr_pending", False),
                    "binary_committed": False,
                }
                record["extraction_status"] = "ocr_pending" if extracted.get("ocr_pending") else "text_extracted"
            elif detected_format != "unsupported":
                record["extraction_status"] = "text_extracted" if extracted.get("text") else "parsing_failed"
            else:
                record["retrieval_status"] = "unsupported_content_type"
                record["extraction_status"] = "media_metadata_only"
                record["preservation_status"] = "external_only"
                record["failure_reason"] = f"unsupported_content_type:{result.content_type}"
                return
        except Exception as error:
            record["extraction_status"] = "parsing_failed"
            record["failure_reason"] = f"{type(error).__name__}:{error}"
            _append_unique(record["review_flags"], "source_parser_failed")
            record.setdefault("timing", {})["text_extraction_seconds"] = round(time.monotonic() - extraction_started, 6)
            return
        record.setdefault("timing", {})["text_extraction_seconds"] = round(time.monotonic() - extraction_started, 6)
        record["page_title"] = extracted.get("title") or record.get("page_title") or ""
        record["author"] = extracted.get("author") or record.get("author") or ""
        record["publication_date"] = extracted.get("publication_date") or record.get("publication_date") or ""
        text = clean_text(extracted.get("text") or "")
        if text:
            variant = _source_variant(text, provenance, result.final_url, result.retrieved_at)
            _append_unique(record["content_variants"], variant, key=lambda row: row["sha256"])
            if not record.get("text_original"):
                record["text_original"] = text
                record["content_hash"] = variant["sha256"]
                record["preservation_status"] = "live_text_preserved" if provenance == "source_live" else "archived_text_preserved"
            elif record.get("content_hash") != variant["sha256"]:
                _append_unique(record["review_flags"], "content_variants_require_review")
        if record.get("text_original"):
            record["text_original_language"] = detect_language(record["text_original"], record.get("text_original_language") or "")

    async def _live_source(self, fetcher: AsyncHostFetcher, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        result = await fetcher.fetch(record["original_url"], accept="text/html,application/pdf,text/plain;q=0.9,*/*;q=0.1")
        self._attempt(record, result, "live_source")
        record["retrieved_at"] = result.retrieved_at
        record["final_redirected_url"] = result.final_url
        preview = result.body[:128 * 1024].decode("utf-8", errors="ignore").casefold() if result.body else ""
        access_wall = record["source_type"].startswith("public_") and any(marker in preview for marker in LOGIN_WALL_MARKERS)
        if access_wall:
            fetcher.note_application_failure(record["original_url"], True, "login_required")
        if result.ok and not access_wall:
            fetcher.note_application_failure(record["original_url"], False, "successful_content_response")
            record["retrieval_status"] = "successful"
            record["failure_reason"] = None
            self._extract_into(record, result, "source_live")
            needs_recovery = not source_has_text(record) and record.get("extraction_status") != "media_metadata_only"
            return record, needs_recovery
        taxonomy = "login_required" if access_wall else _classify_fetch_failure(result.status, result.error, preview)
        if result.status not in {None, 401, 403, 408, 425, 429, 500, 502, 503, 504}:
            fetcher.note_application_failure(record["original_url"], False, taxonomy)
        record["retrieval_status"] = taxonomy
        record["failure_reason"] = result.error or taxonomy
        if should_defer_archive(record):
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
            result = await fetcher.fetch(archive_url, accept=accept)
            self._attempt(record, result, "listed_archive")
            if result.ok:
                fetcher.note_application_failure(archive_url, False, "successful_archive_response")
                record["retrieval_status"] = "successful"
                record["retrieved_at"] = result.retrieved_at
                record["final_redirected_url"] = result.final_url
                record["failure_reason"] = None
                self._extract_into(record, result, "listed_archive")
                return record
        capture, lookup = await fetcher.latest_wayback_capture(record["original_url"])
        lookup["attempt_role"] = "wayback_lookup"
        record["attempt_history"].append(lookup)
        if capture:
            result = await fetcher.fetch(capture["replay_url"], accept=accept)
            self._attempt(record, result, "wayback_capture", capture=capture)
            if result.ok:
                fetcher.note_application_failure(capture["replay_url"], False, "successful_archive_response")
                record["retrieval_status"] = "successful"
                record["retrieved_at"] = result.retrieved_at
                record["final_redirected_url"] = result.final_url
                record["failure_reason"] = None
                _append_unique(record["archived_urls"], capture["replay_url"])
                self._extract_into(record, result, "wayback_capture")
                return record
        record["retrieval_status"] = "no_archive_capture" if lookup.get("ok") else "archive_lookup_failed"
        record["failure_reason"] = record["retrieval_status"]
        return record

    def _cache_hit(self, source_id: str, previous: dict[str, Any]) -> bool:
        entry = (self.cache.get("urls") or {}).get(normalize_source_url(previous.get("original_url") or ""), {})
        return bool(
            previous.get("text_original")
            and previous.get("content_hash")
            and (previous.get("retrieval_status") == "successful" or entry.get("source_id") == source_id)
        )

    def _update_cache(self, record: dict[str, Any]) -> None:
        normalized = normalize_source_url(record.get("original_url") or "")
        if not normalized:
            return
        self.cache["urls"][normalized] = {
            "source_id": record["source_id"],
            "retrieval_status": record.get("retrieval_status"),
            "content_hash": record.get("content_hash"),
            "final_url": record.get("final_redirected_url"),
            "etag": next((row.get("etag") for row in reversed(record.get("attempt_history") or []) if row.get("etag")), ""),
            "last_modified": next((row.get("last_modified") for row in reversed(record.get("attempt_history") or []) if row.get("last_modified")), ""),
            "last_attempt_at": record.get("retrieved_at") or utc_now(),
            "has_original_text": bool(record.get("text_original")),
        }

    def _finish_source(self, record: dict[str, Any], started: float, outcome: dict[str, Any]) -> None:
        source_id = record["source_id"]
        attempt_offset = int(record.pop("_v4_attempt_offset", 0) or 0)
        save_started = time.monotonic()
        atomic_write_json(self.root / "data" / "sources" / f"{source_id}.json", record)
        save_seconds = round(time.monotonic() - save_started, 6)
        duration = round(time.monotonic() - started, 3)
        attempt_rows = [
            row
            for row in (record.get("attempt_history") or [])[attempt_offset:]
            if isinstance(row, dict)
        ]
        network_seconds = sum(float(row.get("elapsed_seconds") or 0) for row in attempt_rows)
        throttle_seconds = sum(float(row.get("waiting_seconds") or 0) for row in attempt_rows)
        extraction_seconds = float((record.get("timing") or {}).get("text_extraction_seconds") or 0)
        queue_seconds = max(
            0.0,
            duration - network_seconds - throttle_seconds - extraction_seconds - save_seconds,
        )
        self.progress["source_timings"] = [row for row in self.progress["source_timings"] if row.get("source_id") != source_id]
        self.progress["source_timings"].append({
            "source_id": source_id,
            "host": source_host(str(record.get("original_url") or "")),
            "duration_seconds": duration,
            "network_seconds": round(network_seconds, 6),
            "throttle_seconds": round(throttle_seconds, 6),
            "queue_seconds": round(queue_seconds, 6),
            "save_seconds": save_seconds,
            "extraction_seconds": round(extraction_seconds, 6),
            "status": record.get("retrieval_status"),
            "attempts": sum(int(row.get("attempts") or 0) for row in record.get("attempt_history") or [] if isinstance(row, dict)),
            "finished_at": utc_now(),
        })
        if source_id not in self.progress["source_completed_ids"]:
            self.progress["source_completed_ids"].append(source_id)
        self.progress["source_outcomes"][source_id] = {"finished_at": utc_now(), **outcome}
        self._update_cache(record)
        self.save_progress()
        self.save_cache()

    async def _collect_source_batch(self, records: list[dict[str, Any]], starts: dict[str, float]) -> None:
        fetcher = AsyncHostFetcher(
            delay_seconds=self.delay,
            timeout_seconds=self.timeout,
            retries=self.retries,
            workers=self.workers,
            per_host_workers=self.per_host_workers,
            circuit_state=self.progress.get("source_fetch_stats", {}).get("circuit_state"),
            circuit_threshold=3,
            circuit_reprobe_every=100,
        )
        async def live_job(record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            try:
                return await self._live_source(fetcher, record)
            except Exception as error:
                record["retrieval_status"] = "failed"
                record["failure_reason"] = f"{type(error).__name__}:{error}"
                record.setdefault("attempt_history", []).append({
                    "attempted_at": utc_now(),
                    "attempt_role": "live_source",
                    "result": "task_failed",
                    "error": record["failure_reason"],
                    "attempts": 0,
                })
                return record, True

        async with fetcher:
            live_tasks = [asyncio.create_task(live_job(record)) for record in fair_host_order(records)]
            for future in asyncio.as_completed(live_tasks):
                record, needs_archive = await future
                if needs_archive:
                    record["recovery"] = {
                        "status": "deferred",
                        "queue": self.recovery_path.relative_to(self.root).as_posix(),
                        "queued_at": utc_now(),
                        "reason": record.get("failure_reason") or record.get("retrieval_status"),
                    }
                    self.recovery.defer(record)
                    self._finish_source(record, starts[record["source_id"]], {
                        "cache_hit": False,
                        "archive_deferred": True,
                        "status": "deferred_recovery",
                    })
                else:
                    deferred = (record.get("archive_retry") or {}).get("status") == "deferred_low_priority"
                    if source_has_text(record) or source_is_policy_complete(record):
                        self.recovery.resolve(
                            record,
                            "successful" if source_has_text(record) else "policy_complete",
                        )
                    self._finish_source(record, starts[record["source_id"]], {
                        "cache_hit": False,
                        "archive_deferred": deferred,
                        "status": "successful" if source_has_text(record) else "policy_complete",
                    })
                    print(f"speed source live {record['source_id']} -> {record.get('retrieval_status')}", flush=True)
        self.progress["source_fetch_stats"] = _merge_stats(self.progress.get("source_fetch_stats", {}), fetcher.stats())
        self.save_progress()

    def collect_sources(self, max_items: int | None = None) -> dict[str, Any]:
        seeds, incident_sources, exact_urls = self._source_seeds()
        completed: set[str] = set()
        outcomes = self.progress.get("source_outcomes") or {}
        for source_id in self.progress["source_completed_ids"]:
            previous = load_json(self.root / "data" / "sources" / f"{source_id}.json", {}) or {}
            outcome = str((outcomes.get(source_id) or {}).get("status") or "")
            if (
                source_has_text(previous)
                or source_is_policy_complete(previous)
                or outcome in {"policy_complete", "deferred_recovery"}
            ):
                completed.add(source_id)
        pending = [source_id for source_id in sorted(seeds) if source_id not in completed]
        if max_items is not None:
            pending = pending[:max_items]
        starts: dict[str, float] = {source_id: time.monotonic() for source_id in pending}
        network_records: list[dict[str, Any]] = []
        for source_id in pending:
            path = self.root / "data" / "sources" / f"{source_id}.json"
            previous = load_json(path, {}) or {}
            record = _merge_previous_source(seeds[source_id], previous)
            record["_v4_attempt_offset"] = len(record.get("attempt_history") or [])
            if self._cache_hit(source_id, previous):
                record["cache_status"] = "persistent_url_cache_hit"
                record["attempt_history"].append({
                    "attempted_at": utc_now(),
                    "url": record["original_url"],
                    "attempt_role": "cache",
                    "result": "persistent_url_cache_hit",
                    "attempts": 0,
                })
                self.recovery.resolve(record, "cached")
                self._finish_source(record, starts[source_id], {
                    "cache_hit": True,
                    "archive_deferred": False,
                    "status": "cached",
                })
                continue
            if record["source_type"] in {"direct_image_url", "direct_video_url", "direct_audio_url"}:
                record["retrieval_status"] = "unsupported_content_type"
                record["extraction_status"] = "media_metadata_only"
                record["preservation_status"] = "external_only"
                record["failure_reason"] = "media_binary_download_prohibited"
                record["attempt_history"].append({
                    "attempted_at": utc_now(),
                    "url": record["original_url"],
                    "result": "not_downloaded_by_media_policy",
                    "attempts": 0,
                })
                self.recovery.resolve(record, "policy_complete")
                self._finish_source(record, starts[source_id], {
                    "cache_hit": False,
                    "archive_deferred": False,
                    "status": "policy_complete",
                })
                continue
            network_records.append(record)
        if network_records:
            asyncio.run(self._collect_source_batch(network_records, starts))
        completed_ids: set[str] = set()
        for source_id in seeds:
            record = load_json(self.root / "data" / "sources" / f"{source_id}.json", {}) or {}
            outcome = str((outcomes.get(source_id) or {}).get("status") or "")
            if (
                source_has_text(record)
                or source_is_policy_complete(record)
                or outcome in {"policy_complete", "deferred_recovery"}
            ):
                completed_ids.add(source_id)
        completed_count = len(completed_ids)
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
            "processed": len(pending),
            "completed": completed_count,
            "total": len(seeds),
            "exact_content_duplicate_groups": duplicate_groups,
            "duplicate_records_updated": duplicate_records_updated,
            "recovery": self.recovery.summary(),
        }

    async def _recover_source_batch(
        self,
        records: list[dict[str, Any]],
        starts: dict[str, float],
    ) -> None:
        fetcher = AsyncHostFetcher(
            delay_seconds=self.delay,
            timeout_seconds=self.timeout,
            retries=self.retries,
            workers=self.workers,
            per_host_workers=self.per_host_workers,
            circuit_state=self.progress.get("source_fetch_stats", {}).get("circuit_state"),
            circuit_threshold=3,
            circuit_reprobe_every=max(10, len(records)),
        )

        async def recovery_job(record: dict[str, Any]) -> dict[str, Any]:
            try:
                record, needs_archive = await self._live_source(fetcher, record)
                if needs_archive:
                    record = await self._archive_source(fetcher, record)
                return record
            except Exception as error:
                record["retrieval_status"] = "failed"
                record["failure_reason"] = f"{type(error).__name__}:{error}"
                record.setdefault("attempt_history", []).append({
                    "attempted_at": utc_now(),
                    "attempt_role": "recovery",
                    "result": "task_failed",
                    "error": record["failure_reason"],
                    "attempts": 0,
                })
                return record

        async with fetcher:
            tasks = [asyncio.create_task(recovery_job(record)) for record in fair_host_order(records)]
            for future in asyncio.as_completed(tasks):
                record = await future
                source_id = record["source_id"]
                self.recovery.note_attempt(source_id, str(record.get("retrieval_status") or "failed"))
                if source_has_text(record):
                    record["recovery"] = {"status": "resolved", "resolved_at": utc_now()}
                    self.recovery.resolve(record, "successful")
                    outcome = "successful"
                elif source_is_policy_complete(record) or record.get("extraction_status") == "media_metadata_only":
                    record["recovery"] = {"status": "policy_complete", "resolved_at": utc_now()}
                    self.recovery.resolve(record, "policy_complete")
                    outcome = "policy_complete"
                else:
                    record["recovery"] = {
                        "status": "deferred",
                        "queue": self.recovery_path.relative_to(self.root).as_posix(),
                        "queued_at": utc_now(),
                        "reason": record.get("failure_reason") or record.get("retrieval_status"),
                    }
                    self.recovery.defer(record)
                    outcome = "deferred_recovery"
                self._finish_source(record, starts[source_id], {
                    "cache_hit": False,
                    "archive_deferred": outcome == "deferred_recovery",
                    "status": outcome,
                })
                print(f"v4 recovery {source_id} -> {outcome}", flush=True)
        self.progress["source_fetch_stats"] = _merge_stats(
            self.progress.get("source_fetch_stats", {}), fetcher.stats()
        )
        self.save_progress()

    def recover_sources(self, max_items: int | None = None) -> dict[str, Any]:
        seeds, _, _ = self._source_seeds()
        selected_ids = [source_id for source_id in self.recovery.select(max_items) if source_id in seeds]
        starts = {source_id: time.monotonic() for source_id in selected_ids}
        records: list[dict[str, Any]] = []
        for source_id in selected_ids:
            previous = load_json(self.root / "data" / "sources" / f"{source_id}.json", {}) or {}
            record = _merge_previous_source(seeds[source_id], previous)
            record["_v4_attempt_offset"] = len(record.get("attempt_history") or [])
            records.append(record)
        if records:
            asyncio.run(self._recover_source_batch(records, starts))
        summary = self.recovery.summary()
        result = {
            "done": summary["pending"] == 0,
            "processed": len(selected_ids),
            "completed": summary["resolved"],
            "total": summary["pending"] + summary["resolved"],
            "recovery": summary,
        }
        self.progress["recovery_runs"].append({"recorded_at": utc_now(), **result})
        self.save_progress()
        return result

    def write_report(self) -> dict[str, Any]:
        seeds, _, _ = self._source_seeds()
        items = self._manifest_items()
        incidents = [load_json(self.root / "data" / "incidents" / f"{items[sequence]['internal_id']}.json", {}) or {} for sequence in self.sequences]
        sources = [load_json(self.root / "data" / "sources" / f"{source_id}.json", {}) or {} for source_id in sorted(seeds)]
        incident_timings = [row for row in self.progress["incident_timings"] if int(row.get("sequence") or 0) in self.sequences]
        source_timings = [row for row in self.progress["source_timings"] if row.get("source_id") in seeds]
        incident_wall = sum(float(row.get("duration_seconds") or 0) for row in self.progress["stage_runs"] if row.get("stage") == "incident_collection")
        source_wall = sum(float(row.get("duration_seconds") or 0) for row in self.progress["stage_runs"] if row.get("stage") == "source_collection")
        recovery_wall = sum(float(row.get("duration_seconds") or 0) for row in self.progress["stage_runs"] if row.get("stage") == "recovery")
        actual_wall = incident_wall + source_wall + recovery_wall
        baseline = load_json(self.root / "data" / "reports" / "first-100-timing.json", {}) or {}
        baseline_incident_mean = float((baseline.get("incident") or {}).get("mean_seconds") or 0)
        baseline_source_mean = float((baseline.get("source") or {}).get("mean_seconds") or 0)
        baseline_equivalent = baseline_incident_mean * len(self.sequences) + baseline_source_mean * len(sources)
        outcomes = self.progress.get("source_outcomes") or {}
        recovery_summary = self.recovery.summary()
        texts_preserved = sum(source_has_text(row) for row in sources)
        policy_completed = sum(
            source_is_policy_complete(row)
            or str((outcomes.get(source_id) or {}).get("status") or "") == "policy_complete"
            for source_id, row in zip(sorted(seeds), sources)
        )
        deferred_recovery = sum(
            str((outcomes.get(source_id) or {}).get("status") or "") == "deferred_recovery"
            for source_id in seeds
        )
        host_rows: dict[str, dict[str, Any]] = {}
        for row in source_timings:
            host = str(row.get("host") or "unknown")
            entry = host_rows.setdefault(host, {"processed": 0, "total_seconds": 0.0, "throttle_seconds": 0.0})
            entry["processed"] += 1
            entry["total_seconds"] += float(row.get("duration_seconds") or 0)
            entry["throttle_seconds"] += float(row.get("throttle_seconds") or 0)
        for entry in host_rows.values():
            entry["total_seconds"] = round(entry["total_seconds"], 3)
            entry["throttle_seconds"] = round(entry["throttle_seconds"], 3)
        report = {
            "generated_at": utc_now(),
            "engine_version": ENGINE_VERSION,
            "status": "completed_with_deferred_recovery" if recovery_summary["pending"] else "completed",
            "scope": self.progress["scope"],
            "translation_policy": "disabled_by_user",
            "media_binaries_downloaded": 0,
            "configuration": {
                "global_workers": self.workers,
                "per_host_workers": self.per_host_workers,
                "per_host_delay_seconds": self.delay,
                "timeout_seconds": self.timeout,
                "retries": self.retries,
                "scheduler": "fair_host_round_robin",
                "passes": ["live_primary", "durable_recovery_on_later_action_run"],
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
                "texts_preserved": texts_preserved,
                "text_coverage_percent": round((texts_preserved / len(sources)) * 100, 1) if sources else 0,
                "cache_hits": sum(bool((outcomes.get(source_id) or {}).get("cache_hit")) for source_id in seeds),
                "policy_completed": policy_completed,
                "deferred_recovery": deferred_recovery,
                "mean_task_seconds": round(statistics.mean(float(row.get("duration_seconds") or 0) for row in source_timings), 3) if source_timings else 0,
                "wall_seconds": round(source_wall, 3),
            },
            "recovery": recovery_summary,
            "performance": {
                "timings": timing_summary(source_timings),
                "hosts": dict(sorted(host_rows.items(), key=lambda item: (-item[1]["processed"], item[0]))),
                "recovery_wall_seconds": round(recovery_wall, 3),
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
            f"# V4 collection {self.key}",
            "",
            f"- Engine: **{ENGINE_VERSION}**",
            f"- Incidents: **{len(incidents)}**",
            f"- Unique sources: **{len(sources)}**",
            f"- Preserved source texts: **{report['sources']['texts_preserved']}**",
            f"- Text coverage: **{report['sources']['text_coverage_percent']}%**",
            f"- Deferred to durable recovery: **{report['recovery']['pending']}**",
            f"- Persistent cache hits: **{report['sources']['cache_hits']}**",
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
        self.progress["result"] = report["status"]
        self.save_progress()
        return report

    def run(self, stage: str, max_items: int | None = None) -> dict[str, Any]:
        stages: dict[str, tuple[str, Callable[[], Any]]] = {
            "manifest": ("manifest", self.build_manifest),
            "incidents": ("incident_collection", lambda: self.collect_incidents(max_items)),
            "sources": ("source_collection", lambda: self.collect_sources(max_items)),
            "recover": ("recovery", lambda: self.recover_sources(max_items)),
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
    parser.add_argument("--delay", type=float, default=0.75)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--per-host-workers", type=int, default=1)
    parser.add_argument("--stage", choices=("manifest", "incidents", "sources", "recover", "report"), required=True)
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
    )
    runner.run(args.stage, args.max_items)


if __name__ == "__main__":
    main()
