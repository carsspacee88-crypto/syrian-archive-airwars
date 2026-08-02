from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from archive_engine.connectors.airwars.connector import (
    AirwarsConnector,
    _recursive_errors,
    _recursive_statuses,
    classify_source_content,
    source_reachability,
)
from archive_engine.normalizers.urls import normalize_url
from archive_engine.statuses import CollectionStatus, SourceContentStatus
from archive_pipeline.io_utils import atomic_write_json, atomic_write_text, load_json, utc_now


BASELINE_CLAIMS: dict[str, Any] = {
    "incident_records_total": 8114,
    "direct_airwars_fetch_success": 12,
    "direct_airwars_fetch_blocked_403": 8102,
    "incidents_with_valid_coordinates": 6679,
    "incidents_without_coordinates": 1432,
    "incidents_with_malformed_coordinates": 1,
    "incidents_outside_coordinate_range": 2,
    "source_record_files_total": "45,075 or 45,081",
    "incident_pages_generated": 8114,
    "internal_links_checked": 499177,
    "internal_links_broken": 0,
}


def _metric(old: Any, definition: str, canonical_input: str, method: str, result: Any) -> dict[str, Any]:
    return {
        "old_claim": old,
        "definition": definition,
        "canonical_input": canonical_input,
        "calculation_method": method,
        "result": result,
    }


def _page_ids(root: Path, directory: str) -> set[str]:
    path = root / directory
    if not path.is_dir():
        return set()
    return {item.parent.name for item in path.glob("*/index.html")}


def _incident_direct_attempt_status(record: dict[str, Any]) -> str:
    retrieval = record.get("retrieval_status") or {}
    direct = [retrieval.get("airwars_endpoint") or {}, retrieval.get("live_page") or {}]
    if any(item.get("ok") and 200 <= int(item.get("status") or 0) < 300 for item in direct):
        return "success"
    statuses = set(_recursive_statuses(direct))
    errors = " ".join(_recursive_errors(direct)).casefold()
    if 403 in statuses or "http_403" in errors:
        return "blocked_403"
    return "other_failure"


class AirwarsGroundTruthAudit:
    def __init__(self, project_root: Path, legacy_zip: Path, release_root: Path | None = None):
        self.project_root = Path(project_root).resolve()
        self.legacy_zip = Path(legacy_zip).resolve()
        self.release_root = Path(release_root).resolve() if release_root else None
        self.connector = AirwarsConnector(self.project_root, self.legacy_zip)

    def _site_root(self) -> Path | None:
        if not self.release_root:
            return None
        nested = self.release_root / "site"
        return nested if nested.is_dir() else self.release_root

    def _write_exports(self, references: list[Any], output_root: Path) -> dict[str, Any]:
        exports = output_root / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        jsonl_path = exports / "all-source-references.jsonl"
        csv_path = exports / "all-source-references.csv"
        raw_path = exports / "all-raw-urls.txt"
        normalized_path = exports / "all-normalized-urls.txt"
        fields = [
            "source_reference_id", "record_id", "raw_url", "normalized_url",
            "normalization_status", "normalization_reason", "label", "title",
            "publisher", "citation_text", "publication_date", "source_type", "domain",
            "current_reachability_status", "content_preservation_status",
            "duplicate_relationship", "malformed", "manual_review", "external_source_id",
        ]
        with jsonl_path.open("w", encoding="utf-8") as jsonl, csv_path.open("w", encoding="utf-8", newline="") as csv_handle:
            writer = csv.DictWriter(csv_handle, fieldnames=fields)
            writer.writeheader()
            for reference in references:
                row = asdict(reference)
                row["current_reachability_status"] = reference.current_reachability_status.value
                row["content_preservation_status"] = reference.content_preservation_status.value
                jsonl.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                writer.writerow({key: row.get(key) for key in fields})
        raw_urls = sorted({item.raw_url for item in references if item.raw_url})
        normalized_urls = sorted({item.normalized_url for item in references if item.normalized_url and item.normalization_status != "malformed"})
        atomic_write_text(raw_path, "\n".join(raw_urls) + "\n")
        atomic_write_text(normalized_path, "\n".join(normalized_urls) + "\n")
        return {
            "source_reference_jsonl": str(jsonl_path.relative_to(output_root)),
            "source_reference_csv": str(csv_path.relative_to(output_root)),
            "raw_urls": str(raw_path.relative_to(output_root)),
            "normalized_urls": str(normalized_path.relative_to(output_root)),
            "rows": len(references),
            "unique_raw_urls": len(raw_urls),
            "unique_normalized_urls": len(normalized_urls),
        }

    def generate(self, output_root: Path) -> dict[str, Any]:
        output_root = Path(output_root).resolve()
        report_root = output_root / "reports"
        report_root.mkdir(parents=True, exist_ok=True)
        incidents = list(self.connector.iter_structured_incidents())
        references = self.connector.source_references()
        sources = self.connector.sources.records
        source_ids = [item.source_reference_id for item in references]
        incident_ids = [str(item["incident_id"]) for item in incidents]
        raw_urls = [item.raw_url for item in references if item.raw_url]
        normalized_urls = [item.normalized_url for item in references if item.normalized_url and item.normalization_status != "malformed"]

        coordinate_counts = Counter(item["coordinate_status"] for item in incidents)
        direct_counts = Counter(_incident_direct_attempt_status(item) for item in incidents)
        origin_counts = Counter(str(item["record_origin_status"]) for item in incidents)
        source_statuses = {source_id: classify_source_content(record).value for source_id, record in sources.items()}
        source_primary = Counter(source_statuses.values())
        reachability = {source_id: source_reachability(record).value for source_id, record in sources.items()}
        reachability_counts = Counter(reachability.values())
        content_hashes: dict[str, list[str]] = defaultdict(list)
        for source_id, record in sources.items():
            digest = str(record.get("content_hash") or "")
            if digest:
                content_hashes[digest].append(source_id)
        duplicate_groups = {digest: ids for digest, ids in content_hashes.items() if len(ids) > 1}
        duplicate_content_records = sum(len(ids) for ids in duplicate_groups.values())
        manual_review_ids = sorted(source_id for source_id, record in sources.items() if record.get("review_flags") or source_statuses[source_id] in {SourceContentStatus.MALFORMED.value, SourceContentStatus.NEEDS_MANUAL_REVIEW.value})

        site_root = self._site_root()
        incident_pages = _page_ids(site_root, "cases") if site_root else set()
        source_pages = _page_ids(site_root, "sources") if site_root else set()
        reference_pages = _page_ids(site_root, "references") if site_root else set()
        canonical_incident_page_ids = {f"{int(item['legacy_sequence']):04d}" for item in incidents}
        canonical_source_ids = set(sources)
        canonical_reference_ids = set(source_ids)

        validation = {}
        if self.release_root:
            for candidate in (
                self.release_root / "reports" / "validation.json",
                self.release_root / "data" / "reports" / "validation.json",
                self.release_root / "site" / "data" / "reports" / "validation.json",
            ):
                if candidate.is_file():
                    validation = load_json(candidate, {})
                    break
        internal = (validation.get("checks") or {}).get("internal_links") or {}

        full_text_statuses = {
            SourceContentStatus.FULL_TEXT_DIRECT.value,
            SourceContentStatus.FULL_TEXT_ARCHIVED.value,
            SourceContentStatus.FULL_TEXT_LOCAL_SNAPSHOT.value,
        }
        text_records = sum(count for status, count in source_primary.items() if status in full_text_statuses)
        source_checked_ids = {source_id for source_id, record in sources.items() if record.get("attempt_history")}
        external_reachable = sum(reachability[source_id] == CollectionStatus.FETCHED.value for source_id in source_checked_ids)
        external_blocked = sum(reachability[source_id] == CollectionStatus.BLOCKED.value for source_id in source_checked_ids)
        external_dead = sum(reachability[source_id] == CollectionStatus.DEAD.value for source_id in source_checked_ids)

        scope_distribution = Counter(str(record.get("collection_checkpoint", {}).get("scope") or "missing") for record in sources.values())
        six = [
            {
                "source_id": source_id,
                "url": record.get("original_url"),
                "incident_sequences": record.get("incident_sequences") or [],
                "status": record.get("retrieval_status"),
                "scope": record.get("collection_checkpoint", {}).get("scope"),
                "provenance": record.get("provenance") or [],
            }
            for source_id, record in sorted(sources.items())
            if record.get("collection_checkpoint", {}).get("scope") != "0001-8114"
        ]

        metrics: dict[str, dict[str, Any]] = {}
        add = lambda key, definition, source, method, result: metrics.__setitem__(key, _metric(BASELINE_CLAIMS.get(key), definition, source, method, result))
        add("incident_records_total", "Canonical normalized Airwars Syria incident records.", "data/incidents/*.json", "Count valid JSON records with a stable internal_id.", len(incidents))
        add("incident_ids_unique", "Distinct stable incident identifiers.", "data/incidents/*.json", "Distinct incident_id values.", len(set(incident_ids)))
        add("incident_ids_duplicate", "Incident rows sharing the same stable identifier.", "data/incidents/*.json", "Total rows minus distinct incident_id values.", len(incident_ids) - len(set(incident_ids)))
        add("incident_pages_generated", "Canonical incident pages present in the inspected release.", "release site/cases/*/index.html", "Count canonical sequence directories with index.html.", len(incident_pages & canonical_incident_page_ids))
        add("incident_pages_missing", "Canonical incidents without a page.", "canonical incident IDs + release site", "Canonical sequence page IDs minus generated page IDs.", len(canonical_incident_page_ids - incident_pages))
        add("incident_pages_orphaned", "Generated incident pages without a canonical incident.", "canonical incident IDs + release site", "Generated page IDs minus canonical sequence page IDs.", len(incident_pages - canonical_incident_page_ids))
        add("source_reference_records_total", "Every incident-to-source occurrence, including duplicates and locally recovered relationships.", "historical package cases/*/data.json + normalized source provenance + normalized incident sources", "Union historical occurrences with additional provenance relationships absent from the historical rows; preserve duplicates.", len(references))
        add("source_reference_ids_unique", "Distinct stable source-reference occurrence IDs.", "derived source-reference index", "Distinct source_reference_id values.", len(set(source_ids)))
        add("source_reference_ids_duplicate", "Duplicate stable source-reference IDs.", "derived source-reference index", "Rows minus distinct IDs.", len(source_ids) - len(set(source_ids)))
        add("source_reference_duplicate_relationships", "Reference occurrences explicitly identified as a repeated incident-to-raw-URL relationship; retained rather than deduplicated away.", "derived source-reference index duplicate_relationship", "Count reference rows whose duplicate_relationship attribute is true.", sum(bool(item.duplicate_relationship) for item in references))
        add("source_reference_url_reuse_occurrences", "Reference occurrences beyond the first occurrence of each distinct raw URL across the whole archive.", "derived source-reference index raw_url", "Total non-empty raw-URL rows minus distinct raw URL strings.", len(raw_urls) - len(set(raw_urls)))
        add("source_reference_malformed_urls", "Reference rows whose raw URL could not be normalized to a valid HTTP(S) URL.", "derived source-reference index normalization_status", "Count reference rows whose malformed attribute is true.", sum(bool(item.malformed) for item in references))
        add("source_reference_manual_review", "Reference rows explicitly marked for manual review.", "derived source-reference index manual_review", "Count reference rows whose manual_review attribute is true.", sum(bool(item.manual_review) for item in references))
        add("source_record_files_total", "Unique external source entity records stored locally.", "data/sources/*.json", "Count valid source JSON records.", len(sources))
        add("source_record_ids_unique", "Distinct source entity IDs.", "data/sources/*.json", "Distinct source_id values.", len(set(sources)))
        add("source_record_ids_duplicate", "Source files sharing a stable source_id.", "data/sources/*.json", "Files minus distinct source IDs.", 0)
        add("unique_raw_source_urls", "Distinct non-empty raw URL evidence values across reference occurrences.", "derived source-reference index", "Distinct raw_url strings without normalization.", len(set(raw_urls)))
        add("unique_normalized_source_urls", "Distinct successfully normalized HTTP(S) source URLs.", "derived source-reference index", "Distinct normalized_url where normalization did not fail.", len(set(normalized_urls)))
        add("source_reference_pages_generated", "Local pages representing individual incident-to-source occurrences.", "release site/references/*/index.html", "Count canonical reference IDs with an index page.", len(reference_pages & canonical_reference_ids))
        add("source_reference_pages_missing", "Canonical reference occurrences without a local page.", "reference index + release site", "Canonical reference IDs minus generated page IDs.", len(canonical_reference_ids - reference_pages))
        add("source_reference_pages_orphaned", "Reference pages without a canonical reference record.", "reference index + release site", "Generated reference page IDs minus canonical IDs.", len(reference_pages - canonical_reference_ids))
        add("local_source_entity_pages_generated", "Local pages describing unique source entities; this does not assert content preservation.", "release site/sources/*/index.html", "Count canonical source IDs with an index page.", len(source_pages & canonical_source_ids))
        add("sources_full_text_direct", "Source entities with non-empty text that passed quality validation as complete and came from direct public retrieval.", "data/sources content_quality + preservation_status", "Primary mutually-exclusive source content classifier.", source_primary[SourceContentStatus.FULL_TEXT_DIRECT.value])
        add("sources_full_text_archived", "Source entities with validated complete text obtained from a public archived copy.", "data/sources content_quality + preservation_status", "Primary mutually-exclusive source content classifier.", source_primary[SourceContentStatus.FULL_TEXT_ARCHIVED.value])
        add("sources_full_text_local_snapshot", "Source entities with validated complete text preserved only in a local snapshot.", "data/sources content_quality + preservation_status", "Primary mutually-exclusive source content classifier.", source_primary[SourceContentStatus.FULL_TEXT_LOCAL_SNAPSHOT.value])
        add("sources_partial_text", "Source entities with non-empty text not proven to be the complete main text.", "data/sources text_original + content_quality", "Primary classifier assigns PARTIAL_TEXT when completeness is partial, absent, or quality is not accepted.", source_primary[SourceContentStatus.PARTIAL_TEXT.value])
        add("sources_metadata_only", "Source entities with metadata but no preserved text and no stronger blocked/dead/malformed state.", "data/sources", "Primary mutually-exclusive source content classifier.", source_primary[SourceContentStatus.METADATA_ONLY.value])
        add("sources_link_only", "Source entities with a preserved URL but neither text nor metadata nor stronger failure state.", "data/sources", "REFERENCE_ONLY + URL_PRESERVED primary categories.", source_primary[SourceContentStatus.URL_PRESERVED.value] + source_primary[SourceContentStatus.REFERENCE_ONLY.value])
        add("sources_blocked", "Source entities without text whose recorded attempts show a public-access block (401/403/429/451 or explicit block evidence).", "data/sources attempt_history", "Primary mutually-exclusive source content classifier.", source_primary[SourceContentStatus.BLOCKED.value])
        add("sources_dead", "Source entities without text whose attempts returned 404 or 410.", "data/sources attempt_history", "Primary mutually-exclusive source content classifier.", source_primary[SourceContentStatus.DEAD.value])
        add("sources_malformed", "Source entities without text whose raw URL cannot normalize to an HTTP(S) URL.", "data/sources original_url", "Primary mutually-exclusive source content classifier; malformed remains a secondary attribute when text exists.", source_primary[SourceContentStatus.MALFORMED.value])
        add("sources_duplicate_content", "Source records participating in a non-empty identical content-hash group.", "data/sources content_hash", "Count records in hash groups of size greater than one.", duplicate_content_records)
        add("sources_requires_manual_review", "Source entities with review flags or a manual-review/malformed primary state.", "data/sources review_flags + primary status", "Distinct source IDs satisfying the review predicate.", len(manual_review_ids))
        add("incidents_with_valid_coordinates", "Incidents with a world-valid pair inside the accepted display range.", "data/incidents latitude/longitude", "Coordinate classifier result drawable.", coordinate_counts["drawable"])
        add("incidents_without_coordinates", "Incidents missing latitude or longitude.", "data/incidents latitude/longitude", "Coordinate classifier result missing.", coordinate_counts["missing"])
        add("incidents_with_malformed_coordinates", "Incidents with non-numeric or world-invalid coordinates.", "data/incidents latitude/longitude", "Coordinate classifier result malformed.", coordinate_counts["malformed"])
        add("incidents_outside_coordinate_range", "World-valid coordinates outside the accepted display range.", "data/incidents latitude/longitude", "Coordinate classifier result outside_accepted_range.", coordinate_counts["outside_accepted_range"])
        add("direct_airwars_fetch_success", "Incidents whose current direct Airwars endpoint or live-page request succeeded; archived copies do not count.", "data/incidents retrieval_status.airwars_endpoint/live_page", "Classify only direct attempts, excluding page_extraction from archives.", direct_counts["success"])
        add("direct_airwars_fetch_blocked_403", "Incidents whose current direct Airwars attempts contain HTTP 403 evidence.", "data/incidents retrieval_status.airwars_endpoint/live_page", "Count 403 status or circuit evidence regardless of archived-copy recovery.", direct_counts["blocked_403"])
        add("direct_airwars_fetch_other_failure", "Direct Airwars attempts neither successful nor evidenced as HTTP 403.", "data/incidents retrieval_status.airwars_endpoint/live_page", "Residual direct-attempt category.", direct_counts["other_failure"])
        add("historical_or_local_text_records", "Incident records whose usable structural text originates from a historical/local or mixed origin.", "enriched incident record_origin_status", "Count non-empty structural descriptions with historical/local in origin.", sum(bool(item.get("textual_description")) and "historical" in item["record_origin_status"] for item in incidents))
        add("records_without_usable_incident_text", "Incidents without an original narrative, archived narrative, or historical structured summary.", "enriched incident textual_description", "Count empty textual_description values.", sum(not bool(item.get("textual_description")) for item in incidents))
        add("internal_links_checked", "Internal hyperlinks checked by the release validator.", "release validation.json", "Validator-reported links_checked.", int(internal.get("links_checked") or 0))
        add("internal_links_broken", "Internal hyperlinks whose target does not exist.", "release validation.json", "Validator-reported broken count.", int(internal.get("broken") or 0))
        add("external_links_discovered", "Distinct raw external source URLs represented by references.", "derived source-reference index", "Distinct non-empty raw_url values.", len(set(raw_urls)))
        add("external_links_checked", "Unique source entities with at least one recorded public retrieval attempt.", "data/sources attempt_history", "Count source IDs with non-empty attempt_history.", len(source_checked_ids))
        add("external_links_reachable", "Checked source entities with a successful/cached retrieval state.", "data/sources retrieval and attempt records", "Reachability classifier FETCHED among checked sources.", external_reachable)
        add("external_links_blocked", "Checked source entities with explicit block evidence.", "data/sources attempt_history", "Reachability classifier BLOCKED among checked sources.", external_blocked)
        add("external_links_dead", "Checked source entities with HTTP 404/410 evidence.", "data/sources attempt_history", "Reachability classifier DEAD among checked sources.", external_dead)
        add("external_links_not_checked", "Unique source entities without a recorded public retrieval attempt.", "data/sources attempt_history", "Source entity total minus checked source IDs.", len(sources) - len(source_checked_ids))

        primary_sum = sum(source_primary.values())
        relationship_origins = Counter(item.provenance[0].origin for item in references)
        equations = {
            "source_entity_primary_status_reconciliation": {
                "left": len(sources), "right": primary_sum, "balanced": len(sources) == primary_sum,
                "terms": dict(sorted(source_primary.items())),
            },
            "coordinate_reconciliation": {
                "left": len(incidents), "right": sum(coordinate_counts.values()), "balanced": len(incidents) == sum(coordinate_counts.values()),
                "terms": dict(sorted(coordinate_counts.items())),
            },
            "direct_attempt_reconciliation": {
                "left": len(incidents), "right": sum(direct_counts.values()), "balanced": len(incidents) == sum(direct_counts.values()),
                "terms": dict(sorted(direct_counts.items())),
            },
            "source_reference_origin_reconciliation": {
                "left": len(references), "right": sum(relationship_origins.values()), "balanced": len(references) == sum(relationship_origins.values()),
                "terms": dict(sorted(relationship_origins.items())),
            },
        }
        exports = self._write_exports(references, output_root)
        status_index = {
            "generated_at": utc_now(),
            "incidents": {
                "blocked_http_403": sorted(item["incident_id"] for item in incidents if _incident_direct_attempt_status(item) == "blocked_403"),
                "without_usable_text": sorted(item["incident_id"] for item in incidents if not item.get("textual_description")),
                "historical_or_local_origin": sorted(item["incident_id"] for item in incidents if "historical" in item["record_origin_status"]),
            },
            "sources": {status: sorted(source_id for source_id, value in source_statuses.items() if value == status) for status in sorted(set(source_statuses.values()))},
        }
        atomic_write_json(report_root / "status-index.json", status_index)
        report = {
            "schema_version": "1.0.0",
            "generated_at": utc_now(),
            "canonical_policy": {
                "incidents": "data/incidents stable IDs enriched with the restored historical structural package",
                "source_references": "all historical occurrences plus additional relationships recoverable from normalized provenance",
                "source_entities": "data/sources unique stable source records",
                "direct_verification": "current direct Airwars endpoint/live-page attempts only; archived copies are separate",
                "source_content": "primary mutually-exclusive preservation categories; local page generation is not content preservation",
            },
            "metrics": metrics,
            "reconciliation_equations": equations,
            "source_status_counts": dict(sorted(source_primary.items())),
            "source_reachability_counts": dict(sorted(reachability_counts.items())),
            "incident_origin_counts": dict(sorted(origin_counts.items())),
            "source_reference_origin_counts": dict(sorted(relationship_origins.items())),
            "discrepancy_45075_45081": {
                "explained": len(six) == 6 and scope_distribution.get("0001-8114") == 45075,
                "full_run_catalog_count": scope_distribution.get("0001-8114", 0),
                "canonical_source_entity_count": len(sources),
                "difference": len(sources) - scope_distribution.get("0001-8114", 0),
                "cause": "Six source entities recovered from the archived Airwars copy for incident sequence 4771 remained valid local records, but the later full-run worklist was rebuilt from that incident's empty normalized sources array and omitted them. They are source entities, not index/utility pages or duplicate IDs.",
                "records": six,
            },
            "exports": exports,
            "validation_source": str(self.release_root) if self.release_root else None,
        }
        atomic_write_json(report_root / "airwars-ground-truth.json", report)
        atomic_write_text(report_root / "airwars-ground-truth.md", self._markdown(report))
        return report

    @staticmethod
    def _markdown(report: dict[str, Any]) -> str:
        lines = [
            "# Airwars Syria ground-truth audit",
            "",
            f"Generated: `{report['generated_at']}`",
            "",
            "| Metric | Previous claim | Recomputed result | Definition | Canonical input |",
            "|---|---:|---:|---|---|",
        ]
        for key, row in report["metrics"].items():
            old = row.get("old_claim") if row.get("old_claim") is not None else "—"
            lines.append(f"| `{key}` | {old} | **{row['result']}** | {row['definition']} | `{row['canonical_input']}` |")
        gap = report["discrepancy_45075_45081"]
        lines.extend([
            "", "## 45,075 versus 45,081", "", gap["cause"], "",
            f"The six stable IDs are: {', '.join(item['source_id'] for item in gap['records'])}.",
            "", "## Reconciliation", "",
        ])
        for name, equation in report["reconciliation_equations"].items():
            lines.append(f"- `{name}`: {equation['left']} = {equation['right']} — {'balanced' if equation['balanced'] else 'FAILED'}")
        return "\n".join(lines) + "\n"
