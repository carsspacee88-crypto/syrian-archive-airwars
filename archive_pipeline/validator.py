from __future__ import annotations

import argparse
import html
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from . import PARSER_VERSION, SCHEMA_VERSION
from .io_utils import atomic_write_json, atomic_write_text, load_json, utc_now
from .legacy import LegacyArchive
from .reports import coordinate_reasons


LINK_RE = re.compile(r'(?<![-\w])(?:href|src)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
REQUIRED_COMPLETE_FIELDS = ["internal_id", "incident_code", "canonical_url", "incident_date", "location"]
PROVENANCE_FIELDS = [
    "incident_code", "incident_date", "location", "latitude", "longitude",
    "civilian_deaths_min", "civilian_deaths_max", "civilian_injuries_min",
    "civilian_injuries_max", "narrative",
]
BINARY_MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".tif", ".tiff",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".mp3", ".wav", ".ogg",
}
PILOT_SOURCE_STATUSES = {
    "successful", "blocked", "timed_out", "not_found", "gone", "login_required",
    "archive_lookup_failed", "no_archive_capture", "parsing_failed",
    "unsupported_content_type", "unavailable", "pending_manual_review",
    # V4 terminal states. Deferred is a truthful foreground outcome whose
    # recovery remains queued; it is not a validation failure.
    "successful_partial", "cached", "embedded_text_preserved",
    "media_metadata_preserved", "recovery_deferred", "failed", "internal_error",
}


class Validation:
    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.checks: dict[str, Any] = {}

    def add(self, severity: str, code: str, path: str, message: str, **details: Any) -> None:
        issue = {"severity": severity, "code": code, "path": path, "message": message}
        if details:
            issue["details"] = details
        self.issues.append(issue)

    def report(self) -> dict[str, Any]:
        counts = Counter(issue["severity"] for issue in self.issues)
        return {
            "generated_at": utc_now(),
            "result": "failed" if counts["critical"] else "passed",
            "issue_counts": {key: counts.get(key, 0) for key in ["critical", "warning", "info"]},
            "checks": self.checks,
            "issues": self.issues,
        }


def _is_external(value: str) -> bool:
    parsed = urlsplit(value)
    return bool(parsed.scheme or value.startswith("//"))


def _valid_external_url(value: str) -> bool:
    parsed = urlsplit(value)
    if parsed.scheme in {"mailto", "tel"}:
        return bool(parsed.path)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _validate_internal_links(validation: Validation, site_root: Path) -> None:
    html_files = sorted(site_root.rglob("*.html"))
    checked = 0
    broken = 0
    root_paths = 0
    for position, path in enumerate(html_files, 1):
        if position == 1 or position % 5000 == 0 or position == len(html_files):
            print(
                f"VALIDATION_PROGRESS phase=internal_links pages={position}/{len(html_files)} links={checked}",
                flush=True,
            )
        relative = path.relative_to(site_root)
        text = path.read_text(encoding="utf-8")
        for raw in LINK_RE.findall(text):
            value = html.unescape(raw.strip())
            if not value or value.startswith("#"):
                continue
            if value.startswith("/") and not value.startswith("//"):
                root_paths += 1
                validation.add(
                    "critical", "absolute_repository_root_path", str(relative),
                    "Absolute root path breaks under the GitHub Pages repository subpath.", value=value,
                )
                continue
            if _is_external(value):
                if not _valid_external_url(value):
                    validation.add("warning", "invalid_external_url", str(relative), "External URL is malformed.", value=value)
                continue
            parsed = urlsplit(value)
            if not parsed.path:
                continue
            checked += 1
            decoded = unquote(parsed.path)
            target = (path.parent / decoded).resolve()
            try:
                target.relative_to(site_root)
            except ValueError:
                broken += 1
                validation.add("critical", "internal_link_escapes_site", str(relative), "Internal link leaves the site artifact.", value=value)
                continue
            if decoded.endswith("/") or target.is_dir():
                target = target / "index.html"
            if not target.is_file():
                broken += 1
                validation.add("critical", "broken_internal_link", str(relative), "Internal link target does not exist.", value=value)
    validation.checks["internal_links"] = {
        "html_files_scanned": len(html_files),
        "links_checked": checked,
        "broken": broken,
        "absolute_root_paths": root_paths,
    }


def _validate_case_files(validation: Validation, site_root: Path, total: int) -> None:
    missing_pages: list[int] = []
    missing_json: list[int] = []
    invalid_json: list[dict[str, Any]] = []
    for sequence in range(1, total + 1):
        case_dir = site_root / "cases" / f"{sequence:04d}"
        if not (case_dir / "index.html").is_file():
            missing_pages.append(sequence)
        data_path = case_dir / "data.json"
        if not data_path.is_file():
            missing_json.append(sequence)
        else:
            try:
                json.loads(data_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                invalid_json.append({"sequence": sequence, "error": f"{type(error).__name__}: {error}"})
    for sequence in missing_pages:
        validation.add("critical", "missing_case_page", f"cases/{sequence:04d}/index.html", "Generated case page is missing.")
    for sequence in missing_json:
        validation.add("critical", "missing_case_json", f"cases/{sequence:04d}/data.json", "Legacy case JSON is missing.")
    for item in invalid_json:
        validation.add("critical", "invalid_case_json", f"cases/{item['sequence']:04d}/data.json", "Case JSON cannot be parsed.", error=item["error"])
    validation.checks["case_files"] = {
        "expected": total,
        "pages_present": total - len(missing_pages),
        "json_present": total - len(missing_json),
        "invalid_json": len(invalid_json),
    }


def _validate_pagination(validation: Validation, site_root: Path, total: int) -> None:
    expected = math.ceil(total / 100)
    missing = []
    for number in range(1, expected + 1):
        path = site_root / "pages" / f"page-{number:03d}.html"
        if not path.is_file():
            missing.append(number)
    for number in missing:
        validation.add("critical", "missing_pagination_page", f"pages/page-{number:03d}.html", "Pagination page is missing.")
    validation.checks["pagination"] = {"expected_pages": expected, "present": expected - len(missing), "missing": missing}


def _read_normalized(project_root: Path, validation: Validation) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((project_root / "data" / "incidents").glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            validation.add("critical", "invalid_normalized_json", str(path.relative_to(project_root)), "Normalized incident JSON cannot be parsed.", error=str(error))
            continue
        record["__path"] = path
        records.append(record)
    return records


def _validate_normalized(validation: Validation, site_root: Path, project_root: Path) -> None:
    records = _read_normalized(project_root, validation)
    id_paths: dict[str, list[str]] = defaultdict(list)
    code_ids: dict[str, list[str]] = defaultdict(list)
    for record in records:
        path: Path = record.pop("__path")
        relative = str(path.relative_to(project_root))
        internal_id = str(record.get("internal_id") or "")
        code = str(record.get("incident_code") or "")
        id_paths[internal_id].append(relative)
        if code:
            code_ids[code].append(internal_id)
        if record.get("schema_version") != SCHEMA_VERSION:
            validation.add("warning", "unexpected_schema_version", relative, "Unexpected normalized schema version.", value=record.get("schema_version"), expected=SCHEMA_VERSION)
        if record.get("parser_version") != PARSER_VERSION:
            validation.add("warning", "unexpected_parser_version", relative, "Unexpected parser version.", value=record.get("parser_version"), expected=PARSER_VERSION)
        if not internal_id:
            validation.add("critical", "missing_internal_id", relative, "Normalized record has no stable internal ID.")
        published = site_root / "data" / "incidents" / f"{internal_id}.json"
        if internal_id and not published.is_file():
            validation.add("critical", "normalized_json_not_published", relative, "Normalized record is not present in the site artifact.")
        canonical = str(record.get("canonical_url") or "")
        if canonical and not _valid_external_url(canonical):
            validation.add("critical", "invalid_primary_source_url", relative, "Primary source URL is malformed.", value=canonical)
        if record.get("completeness_status") == "complete":
            missing = [field for field in REQUIRED_COMPLETE_FIELDS if record.get(field) in (None, "")]
            missing.extend(record.get("missing_sections") or [])
            if missing:
                validation.add("critical", "complete_record_missing_requirements", relative, "Record is marked complete while required data is missing.", missing=missing)
        provenance = record.get("field_provenance") or {}
        for field in PROVENANCE_FIELDS:
            if record.get(field) not in (None, "") and not provenance.get(field):
                validation.add("critical", "missing_field_provenance", relative, "Important value has no provenance.", field=field)
        extraction = record.get("page_extraction") or {}
        if extraction.get("sources_section_present") and not record.get("sources"):
            validation.add("warning", "empty_source_section", relative, "Source section was found but yielded no source records.")
        for source in record.get("sources") or []:
            for key in ("url", "archive_url"):
                value = str(source.get(key) or "")
                if value and not _valid_external_url(value):
                    validation.add("warning", "invalid_source_url", relative, "Source URL is malformed.", field=key, value=value)
        for media in record.get("media_metadata") or []:
            value = str(media.get("url") or "")
            if value.startswith("data:"):
                validation.add("critical", "embedded_media_binary", relative, "Media must not be embedded as Base64/data URI.")
            elif value and not _is_external(value):
                local = (site_root / value).resolve()
                if not local.is_file():
                    validation.add("critical", "missing_local_media_file", relative, "Normalized metadata references a missing local media file.", value=value)
    for internal_id, paths in id_paths.items():
        if not internal_id or len(paths) > 1:
            validation.add("critical", "duplicate_internal_id", paths[0] if paths else "data/incidents", "Stable internal ID is duplicated.", internal_id=internal_id, paths=paths)
    duplicates = {code: ids for code, ids in code_ids.items() if len(set(ids)) > 1}
    for code, ids in duplicates.items():
        validation.add("warning", "duplicate_public_incident_code", "data/incidents", "Public incident code is shared by different stable IDs; records were preserved separately.", incident_code=code, internal_ids=ids)
    validation.checks["normalized_records"] = {
        "count": len(records),
        "unique_internal_ids": len(id_paths),
        "duplicate_public_codes": duplicates,
    }


def _validate_map(validation: Validation, project_root: Path) -> None:
    report = load_json(project_root / "data" / "reports" / "map-coverage.json", {})
    points = load_json(project_root / "data" / "generated" / "map-points.json", [])
    if not report:
        validation.add("critical", "missing_map_coverage_report", "data/reports/map-coverage.json", "Map coverage report is missing.")
        return
    counts = report.get("counts", {})
    included = int(counts.get("incidents_included_on_map") or 0)
    excluded = int(counts.get("incidents_excluded_from_map") or 0)
    total = int(counts.get("total_incidents") or 0)
    if included != len(points):
        validation.add("critical", "map_point_count_mismatch", "data/generated/map-points.json", "Map point list does not match coverage report.", report=included, points=len(points))
    if included + excluded != total:
        validation.add("critical", "map_coverage_total_mismatch", "data/reports/map-coverage.json", "Included and excluded incidents do not sum to total.")
    for point in points:
        reasons = coordinate_reasons(point.get("lat"), point.get("lon"))
        if reasons:
            validation.add("critical", "invalid_coordinate_in_map", "data/generated/map-points.json", "Excluded/suspicious coordinate leaked into map points.", internal_id=point.get("internal_id"), reasons=reasons)
    validation.checks["map"] = {"total": total, "included": included, "excluded": excluded, "point_file_count": len(points)}


def _validate_no_media_binaries(validation: Validation, project_root: Path) -> None:
    found = []
    raw_root = project_root / "data" / "raw"
    if raw_root.exists():
        for path in raw_root.rglob("*"):
            if path.is_file() and path.suffix.lower() in BINARY_MEDIA_SUFFIXES:
                found.append(str(path.relative_to(project_root)))
    for path in found:
        validation.add("critical", "committed_media_binary", path, "Image/video/audio binaries are prohibited in the current phase.")
    validation.checks["media_binary_policy"] = {"binary_files_found": len(found)}


def _schema_errors(instance: Any, schema_path: Path) -> list[str]:
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path))
    ]


def _credential_like_fields(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_lower = str(key).casefold()
            item_path = f"{path}/{key}" if path else str(key)
            if any(token in key_lower for token in ("authorization", "access_token", "private_cookie", "session_cookie", "api_key")) and item not in (None, "", [], {}):
                found.append(item_path)
            found.extend(_credential_like_fields(item, item_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_credential_like_fields(item, f"{path}/{index}"))
    return found


def _validate_pilot(validation: Validation, site_root: Path, project_root: Path) -> None:
    manifest_path = project_root / "data" / "pilot" / "first-100-manifest.json"
    if not manifest_path.is_file():
        validation.add("critical", "missing_first_100_manifest", str(manifest_path.relative_to(project_root)), "The first-100 pilot manifest is missing.")
        return
    manifest = load_json(manifest_path, {})
    schema_root = project_root / "data" / "schema"
    schema_checks = {"files_checked": 0, "instances_checked": 0, "errors": 0}

    def validate_instance(instance: Any, schema_name: str, relative: str) -> None:
        schema_checks["instances_checked"] += 1
        errors = _schema_errors(instance, schema_root / schema_name)
        schema_checks["errors"] += len(errors)
        for message in errors:
            validation.add("critical", "json_schema_validation_failed", relative, "Draft 2020-12 JSON Schema validation failed.", error=message, schema=schema_name)

    for schema_name in ("incident.schema.json", "source.schema.json", "media.schema.json", "pilot-manifest.schema.json"):
        try:
            _schema_errors({}, schema_root / schema_name)
            schema_checks["files_checked"] += 1
        except Exception as error:
            validation.add("critical", "invalid_json_schema", f"data/schema/{schema_name}", "JSON Schema cannot be loaded as Draft 2020-12.", error=f"{type(error).__name__}: {error}")
    validate_instance(manifest, "pilot-manifest.schema.json", "data/pilot/first-100-manifest.json")
    sequences = [int(item.get("sequence", -1)) for item in manifest.get("incidents", [])]
    if sequences != list(range(1, 101)):
        validation.add("critical", "pilot_scope_not_exact", "data/pilot/first-100-manifest.json", "Pilot scope must be the ordered sequence 0001 through 0100 and nothing else.", sequences=sequences)
    incident_ids = {item.get("internal_id") for item in manifest.get("incidents", [])}
    pilot_records: dict[str, dict[str, Any]] = {}
    for item in manifest.get("incidents", []):
        internal_id = item.get("internal_id")
        sequence = int(item.get("sequence", -1))
        path = project_root / "data" / "incidents" / f"{internal_id}.json"
        if not path.is_file():
            validation.add("critical", "missing_pilot_incident_json", str(path.relative_to(project_root)), "Every pilot incident must have a normalized JSON record.")
            continue
        record = load_json(path, {})
        pilot_records[internal_id] = record
        validate_instance(record, "incident.schema.json", str(path.relative_to(project_root)))
        if int(record.get("legacy_sequence", -1)) != sequence or not record.get("pilot", {}).get("in_scope"):
            validation.add("critical", "pilot_incident_identity_mismatch", str(path.relative_to(project_root)), "Pilot incident identity/scope does not match the manifest.")
        for field in ("narrative_original", "narrative_ar", "translation"):
            if field not in record:
                validation.add("critical", "missing_pilot_text_field", str(path.relative_to(project_root)), "Original and Arabic incident text must be stored separately.", field=field)
    for path in (project_root / "data" / "incidents").glob("*.json"):
        record = load_json(path, {})
        if record.get("pilot", {}).get("name") == "first-100-complete-content" and int(record.get("legacy_sequence", 0)) > 100:
            validation.add("critical", "pilot_processed_later_incident", str(path.relative_to(project_root)), "An incident after 0100 was marked as part of the pilot.")

    source_records: dict[str, dict[str, Any]] = {}
    normalized_urls: dict[str, list[str]] = defaultdict(list)
    content_hashes: dict[str, list[str]] = defaultdict(list)
    source_paths = sorted((project_root / "data" / "sources").glob("*.json"))
    for position, path in enumerate(source_paths, 1):
        if position == 1 or position % 5000 == 0 or position == len(source_paths):
            print(
                f"VALIDATION_PROGRESS phase=sources records={position}/{len(source_paths)}",
                flush=True,
            )
        record = load_json(path, {})
        source_id = record.get("source_id")
        if source_id in source_records:
            validation.add("critical", "duplicate_stable_source_id", str(path.relative_to(project_root)), "Stable source ID is duplicated.", source_id=source_id)
        source_records[source_id] = record
        validate_instance(record, "source.schema.json", str(path.relative_to(project_root)))
        normalized_urls[str(record.get("normalized_url") or "")].append(source_id)
        if record.get("content_hash"):
            content_hashes[record["content_hash"]].append(source_id)
        if record.get("retrieval_status") not in PILOT_SOURCE_STATUSES:
            validation.add("critical", "unknown_source_retrieval_status", str(path.relative_to(project_root)), "Source retrieval status is not in the supported taxonomy.", status=record.get("retrieval_status"))
        if not isinstance(record.get("text_original"), str) or not isinstance(record.get("text_ar"), str):
            validation.add("critical", "source_text_not_separated", str(path.relative_to(project_root)), "Original and Arabic source text must be separate strings.")
        source_page = site_root / "sources" / str(source_id) / "index.html"
        if not source_page.is_file():
            validation.add("critical", "missing_source_page", str(source_page.relative_to(site_root)), "Every source record must have a generated local source page.")
        elif "source-provenance" not in source_page.read_text(encoding="utf-8"):
            validation.add("critical", "source_page_missing_provenance", str(source_page.relative_to(site_root)), "Generated source page has no provenance section.")
        for field_path in _credential_like_fields(record):
            validation.add("critical", "saved_private_credential", str(path.relative_to(project_root)), "Credential, cookie, token, or private header was saved.", field=field_path)
    for normalized_url, ids in normalized_urls.items():
        if not normalized_url or len(ids) > 1:
            validation.add("critical", "duplicate_normalized_source_url", "data/sources", "A normalized URL maps to zero or multiple stable source IDs.", normalized_url=normalized_url, source_ids=ids)
    for digest, ids in content_hashes.items():
        if len(ids) > 1 and any(not source_records[source_id].get("exact_content_duplicate_group") for source_id in ids):
            validation.add("critical", "unrecorded_exact_content_duplicate", "data/sources", "Exact duplicate content exists but the duplicate group was not recorded.", sha256=digest, source_ids=ids)

    relationship_root = project_root / "data" / "relationships"
    relationship_paths = sorted(relationship_root.glob("incident-sources*.json"))
    primary_relationship_path = relationship_root / "incident-sources.json"
    primary_relationships = load_json(primary_relationship_path, {}).get("relationships", [])
    relationships: list[dict[str, Any]] = []
    relationship_sources: dict[tuple[str, str], str] = {}
    for relationship_path in relationship_paths:
        payload = load_json(relationship_path, {}) or {}
        for relationship in payload.get("relationships", []):
            relationships.append(relationship)
            relationship_sources[(relationship.get("incident_id"), relationship.get("source_id"))] = str(relationship_path.relative_to(project_root))
    all_incident_ids = {path.stem for path in (project_root / "data" / "incidents").glob("*.json")}
    pairs: set[tuple[str, str]] = set()
    for relationship in relationships:
        pair = (relationship.get("incident_id"), relationship.get("source_id"))
        relationship_path = relationship_sources.get(pair, "data/relationships")
        if pair in pairs:
            validation.add("critical", "duplicate_incident_source_relationship", relationship_path, "Incident-source relationship is duplicated.", pair=pair)
        pairs.add(pair)
        if pair[0] not in all_incident_ids or pair[1] not in source_records:
            validation.add("critical", "orphan_incident_source_relationship", relationship_path, "Relationship references a missing normalized incident or source.", pair=pair)
    for source_id, record in source_records.items():
        for incident_id in record.get("incident_ids", []):
            if (incident_id, source_id) not in pairs:
                validation.add("critical", "missing_incident_source_relationship", f"data/sources/{source_id}.json", "Source incident relationship is missing from the relationship table.", incident_id=incident_id)

    media_count = 0
    for path in sorted((project_root / "data" / "media").glob("*.json")):
        record = load_json(path, {})
        media_count += 1
        validate_instance(record, "media.schema.json", str(path.relative_to(project_root)))
        if record.get("local_path") is not None or record.get("sha256") is not None:
            validation.add("critical", "media_placeholder_has_local_binary", str(path.relative_to(project_root)), "Pilot media placeholders must not reference a local binary or binary hash.")
    media_binaries = []
    for base in (project_root / "data", site_root):
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.casefold() in BINARY_MEDIA_SUFFIXES:
                media_binaries.append(str(path))
    for path in media_binaries:
        validation.add("critical", "pilot_media_binary_found", path, "No media binary may exist in the repository data or generated artifact.")
    validation.checks["pilot_first_100"] = {
        "manifest_incidents": len(sequences),
        "scope_exact_0001_0100": sequences == list(range(1, 101)),
        "normalized_incidents": len(pilot_records),
        "source_records": sum(bool(set(record.get("incident_ids", [])) & incident_ids) for record in source_records.values()),
        "incident_source_relationships": len(primary_relationships),
        "all_source_records": len(source_records),
        "all_incident_source_relationships": len(relationships),
        "relationship_files": [str(path.relative_to(project_root)) for path in relationship_paths],
        "media_placeholders": media_count,
        "media_binaries": len(media_binaries),
        "schema": schema_checks,
    }


def _markdown(report: dict[str, Any]) -> str:
    counts = report["issue_counts"]
    lines = [
        "# تقرير التحقق الآلي / Automated validation report",
        "",
        f"- النتيجة / Result: **{report['result']}**",
        f"- أخطاء حرجة / Critical: **{counts['critical']}**",
        f"- تحذيرات / Warnings: **{counts['warning']}**",
        f"- معلومات / Info: **{counts['info']}**",
        f"- وقت التقرير: `{report['generated_at']}`",
        "",
        "## الفحوص / Checks",
        "",
        "```json",
        json.dumps(report["checks"], ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "## المشكلات / Issues",
        "",
    ]
    if not report["issues"]:
        lines.append("لم تُكتشف مشكلات. / No issues detected.")
    else:
        for issue in report["issues"]:
            lines.append(f"- **{issue['severity']} · {issue['code']}** — `{issue['path']}` — {issue['message']}")
    return "\n".join(lines) + "\n"


def validate(site_root: Path, project_root: Path, legacy_zip: Path | None, report_root: Path) -> dict[str, Any]:
    site_root = site_root.resolve()
    project_root = project_root.resolve()
    validation = Validation()
    if legacy_zip is not None:
        with LegacyArchive(legacy_zip) as archive:
            summaries = list(archive.iter_summaries())
    else:
        summaries = []
        for path in sorted((project_root / "data" / "incidents").glob("*.json")):
            record = load_json(path, {})
            if record:
                summaries.append({
                    "sequence": int(record.get("legacy_sequence") or 0),
                    "code": record.get("incident_code"),
                })
        summaries.sort(key=lambda row: int(row["sequence"]))
    total = len(summaries)
    expected_sequences = list(range(1, total + 1))
    actual_sequences = [int(row.get("sequence") or 0) for row in summaries]
    if actual_sequences != expected_sequences:
        validation.add(
            "critical", "non_contiguous_modern_sequence", "data/incidents",
            "Normalized incident sequence must be contiguous from 1 through the total.",
            expected_total=total,
            missing=sorted(set(expected_sequences) - set(actual_sequences)),
            unexpected=sorted(set(actual_sequences) - set(expected_sequences)),
        )
    for required in ["index.html", "map.html", "methodology.html", ".nojekyll", "assets/css/style.css", "assets/js/site.js", "assets/js/archive-search.js"]:
        if not (site_root / required).exists():
            validation.add("critical", "missing_required_site_file", required, "Required site artifact file is missing.")
    _validate_case_files(validation, site_root, total)
    _validate_pagination(validation, site_root, total)
    _validate_internal_links(validation, site_root)
    _validate_normalized(validation, site_root, project_root)
    _validate_map(validation, project_root)
    _validate_no_media_binaries(validation, project_root)
    if legacy_zip is not None:
        _validate_pilot(validation, site_root, project_root)
    else:
        validation.checks["legacy_package"] = {"required": False, "opened": False}

    legacy_codes: dict[str, list[int]] = defaultdict(list)
    for row in summaries:
        if row.get("code"):
            legacy_codes[str(row["code"])].append(int(row["sequence"]))
    duplicates = {code: sequences for code, sequences in legacy_codes.items() if len(sequences) > 1}
    validation.checks["duplicate_public_codes_by_sequence"] = duplicates
    for code, sequences in duplicates.items():
        validation.add("warning", "duplicate_public_incident_code_by_sequence", "data/cases-summary.json", "Duplicate public code preserved as separate sequences.", incident_code=code, sequences=sequences)

    report = validation.report()
    report_root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(report_root / "validation.json", report)
    atomic_write_text(report_root / "validation.md", _markdown(report))
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate the generated archive and normalized incident records")
    parser.add_argument("--site-root", required=True)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--legacy-zip")
    parser.add_argument("--modern-only", action="store_true")
    parser.add_argument("--report-root", default="data/reports")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.modern_only and not args.legacy_zip:
        parser.error("--legacy-zip is required unless --modern-only is used")
    legacy_zip = None if args.modern_only else Path(args.legacy_zip)
    report = validate(Path(args.site_root), Path(args.project_root), legacy_zip, Path(args.report_root))
    print(json.dumps({"result": report["result"], "issue_counts": report["issue_counts"], "checks": report["checks"]}, ensure_ascii=False, indent=2))
    raise SystemExit(1 if report["issue_counts"]["critical"] else 0)


if __name__ == "__main__":
    main()
