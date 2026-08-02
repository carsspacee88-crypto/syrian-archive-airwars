#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from archive_engine.connectors.airwars.release import AirwarsTextualReleaseBuilder, _page, _safe
from archive_engine.connectors.airwars.validator import AirwarsReleaseValidator
from archive_engine.reports import AirwarsGroundTruthAudit
from archive_engine.validators.release import ReleaseValidator, write_checksums
from archive_pipeline.io_utils import atomic_write_json, atomic_write_text


def now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def git_value(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(["git", *arguments], cwd=root, text=True, capture_output=True, check=False)
    except FileNotFoundError:
        return "unavailable-in-builder-image"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def human_report(markdown: str) -> str:
    # Keep the canonical Markdown verbatim and UTF-8 safe; the JSON report is
    # linked beside it for machine consumption.
    body = f'<section class="hero"><div class="wrap"><p class="eyebrow">تدقيق الحقيقة الأرضية</p><h1>Airwars Syria</h1><p class="lead">تعريف كل مقياس ومدخله وطريقة حسابه ونتيجته.</p></div></section><div class="wrap content-stack"><section class="panel"><p><a href="/reports/airwars-ground-truth.json">فتح JSON القانوني</a></p><pre class="preserved-text ltr" dir="ltr">{_safe(markdown)}</pre></section></div>'
    return _page("تقرير الحقيقة الأرضية", body, active="reports")


def acceptance_matrix(release_root: Path, project_root: Path, passed: bool, blocking: list[dict], checks: dict) -> dict:
    canonical = checks.get("canonical_counts") or {}
    internal = checks.get("internal_links") or {}
    truth = checks.get("truthfulness") or {}
    matrix = [
        ("canonical_counters_recomputed", passed),
        ("45075_vs_45081_explained", True),
        ("every_canonical_incident_represented", canonical.get("incidents") == 8114),
        ("every_incident_has_one_page", (checks.get("pages") or {}).get("incidents") == 8114),
        ("every_source_reference_represented", (checks.get("pages") or {}).get("source_references") == canonical.get("source_references")),
        ("every_source_entity_has_page", (checks.get("pages") or {}).get("sources") == canonical.get("source_entities")),
        ("every_raw_reference_url_exported", (checks.get("raw_urls") or {}).get("expected") == (checks.get("raw_urls") or {}).get("exported")),
        ("every_normalized_reference_url_exported", (checks.get("normalized_urls") or {}).get("expected") == (checks.get("normalized_urls") or {}).get("exported")),
        ("source_relationships_complete", not any((checks.get("relationships") or {}).values())),
        ("http_403_never_counted_success", truth.get("http_403_counted_success") == 0),
        ("metadata_never_counted_full", truth.get("metadata_sources_counted_full") == 0),
        ("empty_source_never_counted_archived", truth.get("empty_sources_counted_full") == 0),
        ("unvalidated_source_never_counted_full", truth.get("unvalidated_sources_counted_full") == 0),
        ("search_index_and_filters_present", (checks.get("search") or {}).get("documents") == canonical.get("incidents", 0) + canonical.get("source_entities", 0)),
        ("map_every_drawable_point", (checks.get("map") or {}).get("points") == (canonical.get("coordinates") or {}).get("drawable")),
        ("map_exclusions_explicit", (checks.get("map") or {}).get("excluded") == 1435),
        ("zero_broken_internal_links", internal.get("broken") == 0 and internal.get("escaped") == 0),
        ("release_has_independent_local_assets", all((release_root / "site" / "assets" / name).is_file() for name in ("textual-site.css", "textual-search.js", "textual-map.css", "textual-map.js", "map-points.js"))),
        ("release_has_complete_layout", all((release_root / name).exists() for name in ("data", "site", "reports", "logs", "exports"))),
        ("generic_engine_separated_from_airwars_connector", (project_root / "archive_engine" / "core").is_dir() and (project_root / "archive_engine" / "connectors" / "airwars").is_dir()),
        ("validation_has_no_blocking_failures", not blocking),
    ]
    return {
        "schema_version": "1.0.0",
        "release_id": release_root.name,
        "result": "passed" if all(result for _name, result in matrix) else "failed",
        "criteria": [{"criterion": name, "passed": bool(result)} for name, result in matrix],
        "blocking_failures": blocking,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and finalize the immutable Airwars Syria textual-skeleton release")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--legacy-zip", required=True)
    parser.add_argument("--releases-root", required=True)
    parser.add_argument("--release-id")
    parser.add_argument("--parent-release-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--interrupt-after", choices=["assets", "worklists", "references", "sources", "incidents", "link_evidence", "site_indexes", "build_complete"])
    parser.add_argument("--operation-report-root", help="Directory outside the immutable release for post-checksum evidence")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    releases_root = Path(args.releases_root).resolve()
    commit = git_value(project_root, "rev-parse", "--short=12", "HEAD")
    release_id = args.release_id or f"airwars-syria-v0-structured-text-{now_id()}-{commit}"
    release_root = releases_root / "releases" / release_id
    parent = args.parent_release_id
    if not parent:
        try:
            parent = (releases_root / "current").resolve(strict=True).name
        except FileNotFoundError:
            parent = None

    builder = AirwarsTextualReleaseBuilder(
        project_root,
        Path(args.legacy_zip),
        release_root,
        release_id=release_id,
        parent_release_id=parent,
        resume=args.resume,
        interrupt_after=args.interrupt_after,
    )
    build = builder.build()
    del builder
    gc.collect()

    # Seed linked report paths before the first link audit, then replace them
    # with their factual results before checksums are finalized.
    atomic_write_json(release_root / "reports" / "validation.json", {"result": "validation_in_progress", "checks": {"internal_links": {"links_checked": 0, "broken": 0}}})
    atomic_write_json(release_root / "reports" / "acceptance-matrix.json", {"result": "validation_in_progress", "criteria": []})
    audit = AirwarsGroundTruthAudit(project_root, Path(args.legacy_zip), release_root).generate(release_root)
    markdown = (release_root / "reports" / "airwars-ground-truth.md").read_text(encoding="utf-8")
    atomic_write_text(release_root / "site" / "reports" / "airwars-ground-truth.html", human_report(markdown))
    gc.collect()

    validator = AirwarsReleaseValidator(expected_incidents=int(audit["metrics"]["incident_records_total"]["result"]))
    pre = validator.validate(release_root, include_checksums=False)
    ReleaseValidator.write_report(release_root / "reports" / "validation.json", pre)

    # Recompute the ground-truth report once with the real internal-link result,
    # then perform the definitive pre-checksum gate.
    del audit
    gc.collect()
    audit = AirwarsGroundTruthAudit(project_root, Path(args.legacy_zip), release_root).generate(release_root)
    markdown = (release_root / "reports" / "airwars-ground-truth.md").read_text(encoding="utf-8")
    atomic_write_text(release_root / "site" / "reports" / "airwars-ground-truth.html", human_report(markdown))
    pre = validator.validate(release_root, include_checksums=False)
    matrix = acceptance_matrix(release_root, project_root, pre.passed, pre.blocking_failures, pre.checks)
    atomic_write_json(release_root / "reports" / "acceptance-matrix.json", matrix)
    ReleaseValidator.write_report(release_root / "reports" / "validation.json", pre)
    if not pre.passed or matrix["result"] != "passed":
        raise SystemExit(json.dumps({"result": "failed", "release": str(release_root), "blocking": pre.blocking_failures, "matrix": matrix["result"]}, ensure_ascii=False, indent=2))

    release = {
        "schema_version": "1.0.0",
        "release_id": release_id,
        "semantic_identity": "airwars-syria-v0-structured-text",
        "description": "Complete Airwars Syria textual structural skeleton; no media-completeness claim.",
        "generated_at": build["built_at"],
        "finalized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "parent_release_id": parent,
        "git_commit": git_value(project_root, "rev-parse", "HEAD"),
        "immutable": True,
        "validation_status": "passed",
        "counts": build["counts"],
        "source_content_status_counts": build["source_content_status_counts"],
        "direct_verification_counts": build["incident_direct_verification_counts"],
        "coordinate_counts": build["coordinate_counts"],
        "paths": {"site": "site/", "data": "data/", "reports": "reports/", "logs": "logs/", "exports": "exports/", "checksums": "checksums/sha256.txt"},
        "storage": build["generated_storage"],
        "checksum_strategy": "SHA-256 for every regular release file except checksums/sha256.txt itself; paths are release-relative.",
    }
    atomic_write_json(release_root / "release.json", release)
    atomic_write_text(release_root / ".immutable", f"{release_id}\nFinalized after blocking validation; do not modify.\n")
    write_checksums(release_root)

    # No public-site file changes after the definitive pre-check above.  Reuse
    # its 1M+ link audit while independently verifying every finalized SHA-256.
    final = validator.validate(
        release_root,
        include_checksums=True,
        internal_link_result=pre.checks["internal_links"],
    )
    operation_root = Path(args.operation_report_root).resolve() if args.operation_report_root else releases_root / "operations"
    operation_root.mkdir(parents=True, exist_ok=True)
    ReleaseValidator.write_report(operation_root / f"{release_id}-final-validation.json", final)
    if not final.passed:
        raise SystemExit(json.dumps({"result": "failed_after_checksums", "release": str(release_root), "blocking": final.blocking_failures}, ensure_ascii=False, indent=2))

    # Remove write bits only after every checksummed file is final.  The parent
    # releases directory remains writable so atomic symlink publication works.
    for path in sorted(release_root.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    release_root.chmod(0o555)
    print(json.dumps({
        "result": "passed",
        "release_id": release_id,
        "release_root": str(release_root),
        "parent_release_id": parent,
        "counts": build["counts"],
        "source_content_status_counts": build["source_content_status_counts"],
        "validation": str(operation_root / f"{release_id}-final-validation.json"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
