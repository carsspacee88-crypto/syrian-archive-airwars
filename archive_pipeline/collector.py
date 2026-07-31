from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .fetcher import RespectfulFetcher, airwars_endpoint_urls
from .io_utils import atomic_write_json, atomic_write_text, load_json, sha256_text, utc_now
from .legacy import LegacyArchive
from .normalize import (
    apply_api_record,
    apply_page_extraction,
    build_legacy_record,
    finalize_status,
)
from .parser import parse_incident_html
from .reports import generate_collection_summary


def _result_status(result: Any) -> dict[str, Any]:
    metadata = result.metadata()
    metadata["ok"] = result.ok
    return metadata


def _slug(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1]


def _write_raw_metadata(root: Path, source: str, internal_id: str, name: str, value: dict[str, Any]) -> None:
    path = root / "data" / "raw" / source / internal_id / name
    previous = load_json(path, {})
    if previous.get("ok") and not value.get("ok"):
        atomic_write_json(path.with_name(f"last-attempt-{name}"), value)
    else:
        atomic_write_json(path, value)


def _load_api_payload(body: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, list):
        return payload[0] if payload and isinstance(payload[0], dict) else None
    return payload if isinstance(payload, dict) and payload.get("id") else None


def _repair_utf8_mojibake(text: str) -> str:
    def repair_segment(match: re.Match[str]) -> str:
        value = match.group(0)
        try:
            return value.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return value

    return "\n".join(
        re.sub(r"[\x00-\xff]+", repair_segment, line)
        for line in text.splitlines()
    )


def _apply_saved_archive_snapshot(record: dict[str, Any], output_root: Path, metadata: dict[str, Any]) -> bool:
    snapshot_path = output_root / "data" / "raw" / "archive" / record["internal_id"] / "snapshot.txt"
    if not snapshot_path.is_file():
        return False
    snapshot = _repair_utf8_mojibake(snapshot_path.read_text(encoding="utf-8"))
    if len(snapshot.strip()) < 100:
        return False
    capture = metadata.get("capture") or {}
    source_url = capture.get("replay_url") or metadata.get("final_url") or ""
    snapshot_retrieved_at = metadata.get("retrieved_at") if metadata.get("ok") else None
    if not snapshot_retrieved_at:
        snapshot_retrieved_at = datetime.fromtimestamp(snapshot_path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat()
    snapshot_hash = metadata.get("content_hash") or sha256_text(snapshot)
    declared_matches = [int(value) for value in re.findall(r"\bSources?\s*\((\d+)\)", snapshot, re.IGNORECASE)]
    sources_declared = max(declared_matches) if declared_matches else None
    parsed = {
        "title": "Saved archived textual snapshot",
        "canonical_url": record.get("canonical_url"),
        "fields": [],
        "sections": [{
            "id": "archived-text-snapshot",
            "heading": "Archived Airwars textual snapshot",
            "text": snapshot,
        }],
        "sources_section_present": bool(re.search(r"\bSources?\b", snapshot, re.IGNORECASE)),
        "sources_declared": sources_declared,
        "sources": [],
        "media_section_present": bool(re.search(r"\bMedia\b", snapshot, re.IGNORECASE)),
        "media_declared": None,
        "media_metadata": [],
        "links": [],
        "snapshot_text": snapshot,
    }
    apply_page_extraction(
        record,
        parsed,
        "airwars_archive",
        source_url,
        snapshot_retrieved_at,
        snapshot_hash,
    )
    record["page_extraction"]["snapshot_only"] = True
    record["page_extraction"]["snapshot_text_hash"] = sha256_text(snapshot)
    record["review_flags"].append("reused_saved_archived_text_snapshot")
    if re.search(r"(?:Ã|Â|Ø|Ù|â[\x80-\xbf])", snapshot):
        record["review_flags"].append("text_encoding_requires_review")
    if source_url:
        record["archived_urls"].insert(0, source_url)
        record["archived_urls"] = list(dict.fromkeys(record["archived_urls"]))
    return True


def collect_one(
    archive: LegacyArchive,
    sequence: int,
    output_root: Path,
    fetcher: RespectfulFetcher,
) -> dict[str, Any]:
    summary = archive.summary_by_sequence(sequence)
    legacy = archive.case_data(sequence)
    record = build_legacy_record(summary, legacy)
    incident_url = record["canonical_url"]
    internal_id = record["internal_id"]
    normalized_path = output_root / "data" / "incidents" / f"{internal_id}.json"
    existing = load_json(normalized_path, {})
    slug = _slug(incident_url)
    airwars_id = record["airwars_id"]

    endpoint_result = None
    for endpoint_url in airwars_endpoint_urls(airwars_id, slug):
        endpoint_result = fetcher.fetch(endpoint_url, accept="application/json")
        record["retrieval_status"]["airwars_endpoint"] = _result_status(endpoint_result)
        _write_raw_metadata(
            output_root,
            "airwars",
            internal_id,
            "endpoint-metadata.json",
            record["retrieval_status"]["airwars_endpoint"],
        )
        if endpoint_result.ok:
            api_record = _load_api_payload(endpoint_result.body)
            if api_record:
                atomic_write_json(
                    output_root / "data" / "raw" / "airwars" / internal_id / "endpoint-snapshot.json",
                    api_record,
                )
                apply_api_record(
                    record,
                    api_record,
                    endpoint_result.final_url,
                    endpoint_result.retrieved_at,
                    endpoint_result.content_hash or "",
                )
                break
        if endpoint_result.status not in {404}:
            break

    live_result = fetcher.fetch(incident_url)
    record["retrieval_status"]["live_page"] = _result_status(live_result)
    _write_raw_metadata(
        output_root,
        "airwars",
        internal_id,
        "live-page-metadata.json",
        record["retrieval_status"]["live_page"],
    )
    live_parsed = False
    if live_result.ok and "html" in live_result.content_type.lower():
        try:
            parsed = parse_incident_html(live_result.body, incident_url)
            apply_page_extraction(
                record,
                parsed,
                "airwars_live",
                live_result.final_url,
                live_result.retrieved_at,
                live_result.content_hash or "",
            )
            atomic_write_text(
                output_root / "data" / "raw" / "airwars" / internal_id / "snapshot.txt",
                parsed["snapshot_text"] + "\n",
            )
            live_parsed = True
        except Exception as error:
            record["review_flags"].append(f"live_parser_error:{type(error).__name__}")

    needs_archive = not live_parsed or not record.get("narrative") or not (record.get("page_extraction") or {}).get("sources_section_present")
    if needs_archive:
        previous_archive_metadata = load_json(
            output_root / "data" / "raw" / "archive" / internal_id / "page-metadata.json",
            {},
        )
        capture = previous_archive_metadata.get("capture") or fetcher.latest_wayback_capture(incident_url)
        if capture:
            archive_result = fetcher.fetch(capture["replay_url"])
            archive_meta = _result_status(archive_result)
            archive_meta["capture"] = capture
            record["retrieval_status"]["archive_page"] = archive_meta
            _write_raw_metadata(
                output_root,
                "archive",
                internal_id,
                "page-metadata.json",
                archive_meta,
            )
            if archive_result.ok and "html" in archive_result.content_type.lower():
                try:
                    parsed = parse_incident_html(archive_result.body, incident_url)
                    apply_page_extraction(
                        record,
                        parsed,
                        "airwars_archive",
                        capture["replay_url"],
                        archive_result.retrieved_at,
                        archive_result.content_hash or "",
                    )
                    record["archived_urls"].insert(0, capture["replay_url"])
                    record["archived_urls"] = list(dict.fromkeys(record["archived_urls"]))
                    atomic_write_text(
                        output_root / "data" / "raw" / "archive" / internal_id / "snapshot.txt",
                        parsed["snapshot_text"] + "\n",
                    )
                except Exception as error:
                    record["review_flags"].append(f"archive_parser_error:{type(error).__name__}")
        else:
            record["retrieval_status"]["archive_page"] = {
                "ok": False,
                "status": None,
                "error": "no_wayback_capture_found",
                "retrieved_at": utc_now(),
            }
        if not record.get("page_extraction"):
            _apply_saved_archive_snapshot(record, output_root, previous_archive_metadata)

    finalize_status(record)
    existing_has_direct_data = bool(existing.get("page_extraction") or existing.get("api_extraction"))
    candidate_has_direct_data = bool(record.get("page_extraction") or record.get("api_extraction"))
    if existing_has_direct_data and not candidate_has_direct_data:
        attempt = {
            "attempted_at": utc_now(),
            "candidate_status": record.get("completeness_status"),
            "retrieval_status": record.get("retrieval_status"),
            "result": "previous_verified_direct_record_preserved",
        }
        history = list(existing.get("collection_attempts") or [])[-19:]
        history.append(attempt)
        existing["collection_attempts"] = history
        existing["last_collection_attempt"] = attempt
        record = existing
    atomic_write_json(normalized_path, record)
    return record


def _parse_sequence_list(value: str) -> list[int]:
    sequences: list[int] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start, end = token.split("-", 1)
            sequences.extend(range(int(start), int(end) + 1))
        else:
            sequences.append(int(token))
    return list(dict.fromkeys(sequences))


def _selection(args: argparse.Namespace, archive: LegacyArchive, state: dict[str, Any]) -> list[int]:
    if args.batch_file:
        payload = load_json(Path(args.batch_file), {})
        requested = payload.get("sequences", payload if isinstance(payload, list) else [])
        sequences = [int(value) for value in requested]
    elif args.sequences:
        sequences = _parse_sequence_list(args.sequences)
    else:
        summaries = list(archive.iter_summaries())
        summaries.sort(key=lambda row: (row.get("completion") != "partial", int(row["sequence"])))
        sequences = [int(row["sequence"]) for row in summaries]
    completed = {int(value) for value in state.get("completed_sequences", [])}
    if not args.force:
        sequences = [value for value in sequences if value not in completed]
    if args.start_after:
        sequences = [value for value in sequences if value > args.start_after]
    return sequences[: args.limit] if args.limit else sequences


def build_collection_summary(output_root: Path, archive: LegacyArchive) -> dict[str, Any]:
    archive_path = archive.archive_path
    return generate_collection_summary(archive_path, output_root)


def run(args: argparse.Namespace) -> int:
    output_root = Path(args.output_root).resolve()
    state_path = output_root / "data" / "state" / "collector-state.json"
    state = load_json(state_path, {"completed_sequences": [], "records": {}})
    state.setdefault("completed_sequences", [])
    state.setdefault("records", {})
    fetcher = RespectfulFetcher(
        delay_seconds=args.delay,
        timeout_seconds=args.timeout,
        retries=args.retries,
    )
    errors = 0
    with LegacyArchive(Path(args.legacy_zip)) as archive:
        sequences = _selection(args, archive, state)
        state["last_run_started_at"] = utc_now()
        state["requested_sequences"] = sequences
        atomic_write_json(state_path, state)
        for index, sequence in enumerate(sequences, 1):
            try:
                record = collect_one(archive, sequence, output_root, fetcher)
                if record["completeness_status"] == "complete" and sequence not in state["completed_sequences"]:
                    state["completed_sequences"].append(sequence)
                elif record["completeness_status"] != "complete" and sequence in state["completed_sequences"]:
                    state["completed_sequences"].remove(sequence)
                state["records"][str(sequence)] = {
                    "internal_id": record["internal_id"],
                    "status": record["completeness_status"],
                    "retrieved_at": record.get("retrieved_at"),
                    "content_hash": record.get("content_hash"),
                }
                print(f"[{index}/{len(sequences)}] {sequence:04d} {record['internal_id']} -> {record['completeness_status']}", flush=True)
            except Exception as error:
                errors += 1
                state["records"][str(sequence)] = {
                    "status": "failed",
                    "error": f"{type(error).__name__}: {error}",
                    "failed_at": utc_now(),
                }
                print(f"[{index}/{len(sequences)}] {sequence:04d} FAILED: {error}", file=sys.stderr, flush=True)
            state["last_sequence"] = sequence
            state["last_progress_at"] = utc_now()
            atomic_write_json(state_path, state)
        state["last_run_finished_at"] = utc_now()
        state["last_run_errors"] = errors
        atomic_write_json(state_path, state)
        build_collection_summary(output_root, archive)
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resumable direct Airwars incident collector")
    parser.add_argument("--legacy-zip", required=True, help="Historical site ZIP used only for migration identifiers and fallback")
    parser.add_argument("--output-root", default=".")
    parser.add_argument("--batch-file")
    parser.add_argument("--sequences", help="Comma-separated sequences and ranges, e.g. 1,4,480-481")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--start-after", type=int, default=0)
    parser.add_argument("--delay", type=float, default=1.25)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> None:
    raise SystemExit(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
