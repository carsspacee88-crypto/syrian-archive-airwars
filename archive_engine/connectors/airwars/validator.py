from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from archive_engine.models import ValidationResult
from archive_engine.statuses import SourceContentStatus
from archive_engine.validators.release import ReleaseValidator


FULL_TEXT_STATUSES = {
    SourceContentStatus.FULL_TEXT_DIRECT.value,
    SourceContentStatus.FULL_TEXT_ARCHIVED.value,
    SourceContentStatus.FULL_TEXT_LOCAL_SNAPSHOT.value,
}
HREF_PATTERN = re.compile(r"\b(?:href|src)=[\"']([^\"']+)[\"']", re.I)


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _page_ids(root: Path, relative: str) -> set[str]:
    return {path.parent.name for path in (root / relative).glob("*/index.html")}


class AirwarsReleaseValidator:
    """Blocking release gates for the complete Airwars textual skeleton."""

    def __init__(self, expected_incidents: int = 8114):
        self.expected_incidents = expected_incidents

    @staticmethod
    def _add(blocking: list[dict[str, Any]], condition: bool, code: str, **detail: Any) -> None:
        if not condition:
            blocking.append({"code": code, **detail})

    @staticmethod
    def _resolve_internal(root: Path, page: Path, target: str) -> Path | None:
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or target.startswith(("mailto:", "tel:", "javascript:")):
            return None
        path = unquote(parsed.path)
        if not path or path == "/admin" or path.startswith("/admin/"):
            return None
        if path.startswith("/release-data/"):
            candidate = root / "data" / path.removeprefix("/release-data/")
        elif path.startswith("/reports/") and path != "/reports/":
            site_candidate = root / "site" / path.lstrip("/")
            candidate = site_candidate if site_candidate.exists() else root / "reports" / path.removeprefix("/reports/")
        elif path.startswith("/exports/") and path != "/exports/":
            site_candidate = root / "site" / path.lstrip("/")
            candidate = site_candidate if site_candidate.exists() else root / "exports" / path.removeprefix("/exports/")
        elif path.startswith("/"):
            candidate = root / "site" / path.lstrip("/")
        else:
            candidate = page.parent / path
        if path.endswith("/") or candidate.is_dir():
            candidate = candidate / "index.html"
        return candidate.resolve(strict=False)

    def _validate_internal_links(self, root: Path) -> dict[str, Any]:
        site = root / "site"
        allowed_roots = [site.resolve(), (root / "data").resolve(), (root / "reports").resolve(), (root / "exports").resolve()]
        checked = 0
        broken: list[dict[str, str]] = []
        escaped: list[dict[str, str]] = []
        for page in sorted(site.rglob("*.html")):
            text = page.read_text(encoding="utf-8")
            for target in HREF_PATTERN.findall(text):
                candidate = self._resolve_internal(root, page, target)
                if candidate is None:
                    continue
                checked += 1
                if not any(candidate == allowed or allowed in candidate.parents for allowed in allowed_roots):
                    escaped.append({"page": page.relative_to(site).as_posix(), "target": target})
                elif not candidate.is_file():
                    broken.append({"page": page.relative_to(site).as_posix(), "target": target})
        return {"links_checked": checked, "broken": len(broken), "escaped": len(escaped), "broken_samples": broken[:100], "escaped_samples": escaped[:100]}

    @staticmethod
    def _read_reference_rows(path: Path) -> tuple[list[dict[str, Any]], set[str]]:
        rows: list[dict[str, Any]] = []
        ids: set[str] = set()
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                row = json.loads(line)
                row["_line"] = line_number
                rows.append(row)
                ids.add(str(row.get("source_reference_id") or ""))
        return rows, ids

    def validate(
        self,
        root: Path,
        *,
        include_checksums: bool = False,
        internal_link_result: dict[str, Any] | None = None,
    ) -> ValidationResult:
        root = Path(root).resolve()
        blocking: list[dict[str, Any]] = []
        notes: list[dict[str, Any]] = []
        checks: dict[str, Any] = {}

        required = ["data", "site", "reports", "logs", "exports"]
        missing = [item for item in required if not (root / item).exists()]
        self._add(blocking, not missing, "missing_release_artifacts", paths=missing)
        manifest_path = root / "data" / "build-manifest.json"
        self._add(blocking, manifest_path.is_file(), "missing_build_manifest")
        if blocking:
            return ValidationResult(False, blocking, notes, checks)
        manifest = _json(manifest_path)

        incident_files = sorted((root / "data" / "incidents").glob("*.json"))
        source_files = sorted((root / "data" / "sources").glob("*.json"))
        reference_path = root / "data" / "source-references.jsonl"
        self._add(blocking, reference_path.is_file(), "missing_source_reference_index")
        if not reference_path.is_file():
            return ValidationResult(False, blocking, notes, checks)
        references, reference_ids = self._read_reference_rows(reference_path)
        incident_ids: set[str] = set()
        incident_sequences: set[str] = set()
        direct = Counter()
        coordinates = Counter()
        usable_text = 0
        utf8_errors: list[str] = []
        for path in incident_files:
            try:
                record = _json(path)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                utf8_errors.append(f"{path.name}:{error}")
                continue
            incident_ids.add(str(record.get("incident_id") or ""))
            incident_sequences.add(f"{int(record.get('legacy_sequence') or 0):04d}")
            direct[str(record.get("direct_verification_status") or "missing")] += 1
            coordinates[str(record.get("coordinate_status") or "missing")] += 1
            usable_text += bool(record.get("textual_description"))
            self._add(blocking, "direct_verification_status" in record and "record_origin_status" in record, "incident_missing_separate_origin_fields", incident_id=record.get("incident_id"))
        source_ids: set[str] = set()
        source_status = Counter()
        source_full_without_text: list[str] = []
        source_false_full: list[str] = []
        metadata_counted_full: list[str] = []
        for path in source_files:
            try:
                record = _json(path)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                utf8_errors.append(f"{path.name}:{error}")
                continue
            source_id = str(record.get("source_id") or "")
            source_ids.add(source_id)
            status = str(record.get("content_preservation_status") or "")
            source_status[status] += 1
            if status in FULL_TEXT_STATUSES and not str(record.get("text_original") or "").strip():
                source_full_without_text.append(source_id)
            if status in FULL_TEXT_STATUSES and not bool((record.get("full_text_validation") or {}).get("passed")):
                source_false_full.append(source_id)
            if status == SourceContentStatus.METADATA_ONLY.value and status in FULL_TEXT_STATUSES:
                metadata_counted_full.append(source_id)

        incident_pages = _page_ids(root / "site", "cases")
        source_pages = _page_ids(root / "site", "sources")
        reference_pages = _page_ids(root / "site", "references")
        counts = manifest["counts"]
        self._add(blocking, len(incident_files) == self.expected_incidents, "incident_file_count_mismatch", expected=self.expected_incidents, actual=len(incident_files))
        self._add(blocking, len(incident_ids) == len(incident_files) and "" not in incident_ids, "incident_ids_not_unique", files=len(incident_files), unique=len(incident_ids))
        self._add(blocking, incident_pages == incident_sequences, "incident_page_set_mismatch", missing=len(incident_sequences - incident_pages), orphaned=len(incident_pages - incident_sequences))
        self._add(blocking, len(references) == int(counts["source_reference_records"]), "source_reference_count_mismatch", manifest=counts["source_reference_records"], actual=len(references))
        self._add(blocking, len(reference_ids) == len(references) and "" not in reference_ids, "source_reference_ids_not_unique", records=len(references), unique=len(reference_ids))
        self._add(blocking, reference_pages == reference_ids, "source_reference_page_set_mismatch", missing=len(reference_ids - reference_pages), orphaned=len(reference_pages - reference_ids))
        self._add(blocking, len(source_files) == int(counts["source_entities"]), "source_entity_count_mismatch", manifest=counts["source_entities"], actual=len(source_files))
        self._add(blocking, len(source_ids) == len(source_files) and "" not in source_ids, "source_ids_not_unique", files=len(source_files), unique=len(source_ids))
        self._add(blocking, source_pages == source_ids, "source_page_set_mismatch", missing=len(source_ids - source_pages), orphaned=len(source_pages - source_ids))

        bad_incident_relations = [row["source_reference_id"] for row in references if row.get("record_id") not in incident_ids]
        bad_source_relations = [row["source_reference_id"] for row in references if row.get("external_source_id") and row.get("external_source_id") not in source_ids]
        self._add(blocking, not bad_incident_relations, "reference_to_missing_incident", count=len(bad_incident_relations), samples=bad_incident_relations[:20])
        self._add(blocking, not bad_source_relations, "reference_to_missing_source_entity", count=len(bad_source_relations), samples=bad_source_relations[:20])

        raw_export = set((root / "exports" / "all-raw-urls.txt").read_text(encoding="utf-8").splitlines()) if (root / "exports" / "all-raw-urls.txt").is_file() else set()
        normalized_export = set((root / "exports" / "all-normalized-urls.txt").read_text(encoding="utf-8").splitlines()) if (root / "exports" / "all-normalized-urls.txt").is_file() else set()
        expected_raw = {str(row["raw_url"]) for row in references if row.get("raw_url")}
        expected_normalized = {str(row["normalized_url"]) for row in references if row.get("normalized_url") and row.get("normalization_status") != "malformed"}
        self._add(blocking, expected_raw == raw_export, "raw_url_export_mismatch", missing=len(expected_raw - raw_export), extra=len(raw_export - expected_raw))
        self._add(blocking, expected_normalized == normalized_export, "normalized_url_export_mismatch", missing=len(expected_normalized - normalized_export), extra=len(normalized_export - expected_normalized))

        self._add(blocking, direct["DIRECT_FETCH_SUCCESS"] == 0, "direct_fetch_success_not_zero", actual=direct["DIRECT_FETCH_SUCCESS"])
        self._add(blocking, direct["BLOCKED_HTTP_403"] == len(incident_files), "http_403_reconciliation_failed", blocked=direct["BLOCKED_HTTP_403"], incidents=len(incident_files))
        self._add(blocking, not source_full_without_text, "empty_source_counted_full", count=len(source_full_without_text), samples=source_full_without_text[:20])
        self._add(blocking, not source_false_full, "unvalidated_source_counted_full", count=len(source_false_full), samples=source_false_full[:20])
        self._add(blocking, not metadata_counted_full, "metadata_source_counted_full", count=len(metadata_counted_full))
        self._add(blocking, sum(source_status.values()) == len(source_files), "source_status_reconciliation_failed", statuses=dict(source_status), files=len(source_files))
        self._add(blocking, sum(coordinates.values()) == len(incident_files), "coordinate_reconciliation_failed", coordinates=dict(coordinates), files=len(incident_files))
        self._add(blocking, not utf8_errors, "utf8_or_json_errors", count=len(utf8_errors), samples=utf8_errors[:20])

        search_path = root / "site" / "data" / "search-index.json"
        search = _json(search_path) if search_path.is_file() else {}
        search_docs = search.get("documents") or []
        search_fields = set(search.get("fields") or [])
        required_search = {"incident identifier", "date", "location", "narrative", "victim", "source domain", "source title", "original URL", "Airwars code", "actor", "allegation", "source preservation status"}
        self._add(blocking, len(search_docs) == len(incident_files) + len(source_files), "search_document_count_mismatch", actual=len(search_docs), expected=len(incident_files) + len(source_files))
        self._add(blocking, required_search <= search_fields, "search_fields_missing", missing=sorted(required_search - search_fields))
        search_js = (root / "site" / "assets" / "textual-search.js").read_text(encoding="utf-8") if (root / "site" / "assets" / "textual-search.js").is_file() else ""
        self._add(blocking, all(token in search_js for token in ("source_status", "coordinates", "date", "search_text")), "search_filters_not_implemented")

        map_path = root / "site" / "assets" / "map-points.js"
        map_text = map_path.read_text(encoding="utf-8") if map_path.is_file() else ""
        map_count = map_text.count('"incident_id"')
        self._add(blocking, map_count == coordinates["drawable"], "map_point_count_mismatch", actual=map_count, expected=coordinates["drawable"])
        self._add(blocking, (root / "site" / "assets" / "textual-map.js").is_file() and (root / "site" / "map.html").is_file(), "map_assets_missing")
        css = (root / "site" / "assets" / "textual-site.css").read_text(encoding="utf-8") if (root / "site" / "assets" / "textual-site.css").is_file() else ""
        self._add(blocking, "overflow-wrap: anywhere" in css and ".raw-url" in css, "long_url_layout_protection_missing")

        # A caller may reuse a link audit only when no file under site/ has
        # changed since that audit.  Release finalization uses this after its
        # definitive pre-check; only release.json, .immutable and checksums are
        # written afterwards.
        internal = internal_link_result or self._validate_internal_links(root)
        self._add(blocking, internal["broken"] == 0 and internal["escaped"] == 0, "internal_links_broken", **internal)

        ground_truth_path = root / "reports" / "airwars-ground-truth.json"
        self._add(blocking, ground_truth_path.is_file(), "ground_truth_report_missing")
        if ground_truth_path.is_file():
            ground = _json(ground_truth_path)
            metrics = ground.get("metrics") or {}
            comparisons = {
                "incident_records_total": len(incident_files),
                "source_reference_records_total": len(references),
                "source_record_files_total": len(source_files),
                "incident_pages_generated": len(incident_pages),
                "source_reference_pages_generated": len(reference_pages),
                "local_source_entity_pages_generated": len(source_pages),
                "internal_links_broken": internal["broken"],
            }
            mismatches = {key: {"report": (metrics.get(key) or {}).get("result"), "release": value} for key, value in comparisons.items() if (metrics.get(key) or {}).get("result") != value}
            self._add(blocking, not mismatches, "ground_truth_release_mismatch", mismatches=mismatches)

        if include_checksums:
            generic = ReleaseValidator().validate(root)
            blocking.extend(generic.blocking_failures)
            checks.update(generic.checks)

        checks.update({
            "canonical_counts": {
                "incidents": len(incident_files),
                "source_references": len(references),
                "source_entities": len(source_files),
                "usable_incident_text": usable_text,
                "source_statuses": dict(sorted(source_status.items())),
                "direct_verification": dict(sorted(direct.items())),
                "coordinates": dict(sorted(coordinates.items())),
            },
            "relationships": {"missing_incidents": len(bad_incident_relations), "missing_sources": len(bad_source_relations)},
            "raw_urls": {"expected": len(expected_raw), "exported": len(raw_export)},
            "normalized_urls": {"expected": len(expected_normalized), "exported": len(normalized_export)},
            "pages": {"incidents": len(incident_pages), "source_references": len(reference_pages), "sources": len(source_pages)},
            "search": {"documents": len(search_docs), "required_fields": sorted(required_search)},
            "map": {"points": map_count, "excluded": len(incident_files) - map_count},
            "internal_links": internal,
            "truthfulness": {
                "http_403_counted_success": direct["DIRECT_FETCH_SUCCESS"],
                "empty_sources_counted_full": len(source_full_without_text),
                "unvalidated_sources_counted_full": len(source_false_full),
                "metadata_sources_counted_full": len(metadata_counted_full),
            },
        })
        return ValidationResult(not blocking, blocking, notes, checks)
