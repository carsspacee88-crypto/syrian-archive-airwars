from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .io_utils import as_number, atomic_write_json, atomic_write_text, load_json, stable_internal_id, utc_now
from .legacy import LegacyArchive


EXPECTED_REGION = {
    "latitude_min": 31.0,
    "latitude_max": 38.0,
    "longitude_min": 34.0,
    "longitude_max": 43.0,
    "description": "حدود تحقق واسعة لسوريا والمناطق الحدودية؛ لا تُستخدم لتصحيح القيم.",
}


COLLECTION_STATUSES = [
    "complete",
    "partial",
    "unavailable",
    "blocked",
    "failed",
    "pending_review",
    "conflicting_sources",
]


def generate_collection_summary(legacy_zip: Path, output_root: Path) -> dict[str, Any]:
    with LegacyArchive(legacy_zip) as archive:
        summaries = list(archive.iter_summaries())
    baseline = {
        "complete": sum(1 for row in summaries if row.get("completion") == "complete"),
        "partial": sum(1 for row in summaries if row.get("completion") != "complete"),
    }
    normalized: list[dict[str, Any]] = []
    parse_errors: list[dict[str, str]] = []
    for path in sorted((output_root / "data" / "incidents").glob("*.json")):
        try:
            normalized.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as error:
            parse_errors.append({"path": str(path), "error": f"{type(error).__name__}: {error}"})
    direct_counts = {
        status: sum(1 for item in normalized if item.get("completeness_status") == status)
        for status in COLLECTION_STATUSES
    }
    status_ids = {
        status: [item.get("internal_id") for item in normalized if item.get("completeness_status") == status]
        for status in COLLECTION_STATUSES
    }
    upgrades = [
        item.get("internal_id")
        for item in normalized
        if item.get("legacy_completeness_status") == "partial" and item.get("completeness_status") == "complete"
    ]
    review_ids = [
        item.get("internal_id")
        for item in normalized
        if item.get("completeness_status") in {"pending_review", "conflicting_sources"} or item.get("review_flags")
    ]
    latest_dates = sorted(filter(None, (item.get("retrieved_at") for item in normalized)))
    report = {
        "generated_at": utc_now(),
        "total_incidents": len(summaries),
        "legacy_baseline": baseline,
        "direct_collection": {
            "processed": len(normalized),
            "pending": max(0, len(summaries) - len(normalized)),
            "latest_successful_collection": latest_dates[-1] if latest_dates else None,
            "status_counts": direct_counts,
            "status_incident_ids": status_ids,
            "upgraded_from_partial_to_complete": len(upgrades),
            "upgraded_incident_ids": upgrades,
            "records_requiring_review": len(set(filter(None, review_ids))),
            "review_incident_ids": sorted(set(filter(None, review_ids))),
            "normalized_json_parse_errors": parse_errors,
            "missing_coordinate_incident_ids": [
                item.get("internal_id") for item in normalized
                if any(flag in {"missing_latitude", "missing_longitude", "invalid_latitude", "invalid_longitude"} for flag in item.get("review_flags", []))
            ],
            "missing_source_section_incident_ids": [
                item.get("internal_id") for item in normalized if "sources" in item.get("missing_sections", [])
            ],
            "parser_failure_incident_ids": [
                item.get("internal_id") for item in normalized
                if any("parser_error" in flag for flag in item.get("review_flags", []))
            ],
            "conflicting_source_incident_ids": [
                item.get("internal_id") for item in normalized if item.get("conflicts")
            ],
        },
        "policy": {
            "excel_active_source_of_truth": False,
            "legacy_values_status": "legacy_import_until_verified",
            "media_binaries_downloaded": 0,
        },
    }
    atomic_write_json(output_root / "data" / "reports" / "collection-summary.json", report)
    lines = [
        "# تقرير الجمع المباشر من Airwars",
        "",
        f"- إجمالي الحوادث: **{len(summaries):,}**",
        f"- عولجت مباشرة: **{len(normalized):,}**",
        f"- بانتظار الجمع: **{max(0, len(summaries) - len(normalized)):,}**",
        f"- رُقيت من جزئية إلى مكتملة: **{len(upgrades):,}**",
        f"- تحتاج إلى مراجعة: **{len(set(filter(None, review_ids))):,}**",
        "",
        "## الحالات",
        "",
    ]
    for status in COLLECTION_STATUSES:
        lines.append(f"- `{status}`: **{direct_counts[status]:,}**")
    lines.extend(["", "## ملاحظات", ""])
    if parse_errors:
        lines.append(f"- ملفات JSON تعذر تحليلها: {len(parse_errors)}")
    else:
        lines.append("- لم تتعذر قراءة أي ملف JSON موحّد.")
    lines.append("- لا تتضمن عملية الجمع أي تنزيل لملفات الصور أو الفيديو أو الصوت.")
    lines.append("- القيم المهاجرة تبقى موسومة `legacy_import` إلى أن تُراجع من مصدر حي أو مؤرشف.")
    atomic_write_text(output_root / "data" / "reports" / "collection-summary.md", "\n".join(lines) + "\n")
    return report


def coordinate_reasons(latitude: Any, longitude: Any) -> list[str]:
    reasons: list[str] = []
    lat = as_number(latitude)
    lon = as_number(longitude)
    latitude_missing = latitude is None or latitude == ""
    longitude_missing = longitude is None or longitude == ""
    if latitude_missing:
        reasons.append("missing_latitude")
    elif lat is None:
        reasons.extend(["invalid_latitude", "parsing_error"])
    elif not -90 <= float(lat) <= 90:
        reasons.append("invalid_latitude")
    if longitude_missing:
        reasons.append("missing_longitude")
    elif lon is None:
        reasons.extend(["invalid_longitude", "parsing_error"])
    elif not -180 <= float(lon) <= 180:
        reasons.append("invalid_longitude")
    if not reasons and not (
        EXPECTED_REGION["latitude_min"] <= float(lat) <= EXPECTED_REGION["latitude_max"]
        and EXPECTED_REGION["longitude_min"] <= float(lon) <= EXPECTED_REGION["longitude_max"]
    ):
        reasons.append("outside_expected_region")
    return reasons


def generate_map_coverage(legacy_zip: Path, output_root: Path) -> dict[str, Any]:
    exclusions: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    world_valid_pairs = 0
    with LegacyArchive(legacy_zip) as archive:
        summaries = list(archive.iter_summaries())
        for row in summaries:
            sequence = int(row["sequence"])
            internal_id = stable_internal_id(row.get("airwars_id"), sequence)
            normalized_path = output_root / "data" / "incidents" / f"{internal_id}.json"
            normalized = load_json(normalized_path, {})
            latitude_raw = normalized.get("latitude", row.get("latitude"))
            longitude_raw = normalized.get("longitude", row.get("longitude"))
            if not normalized and (latitude_raw is None or longitude_raw is None):
                legacy_case = archive.case_data(sequence).get("incident", {})
                if latitude_raw is None:
                    latitude_raw = legacy_case.get("خط العرض")
                if longitude_raw is None:
                    longitude_raw = legacy_case.get("خط الطول")
            lat = as_number(latitude_raw)
            lon = as_number(longitude_raw)
            coordinate_source = "normalized_direct_record" if normalized else "legacy_import"
            world_valid = (
                lat is not None
                and lon is not None
                and -90 <= float(lat) <= 90
                and -180 <= float(lon) <= 180
            )
            if world_valid:
                world_valid_pairs += 1
            reasons = coordinate_reasons(latitude_raw, longitude_raw)
            if reasons:
                exclusions.append({
                    "legacy_sequence": sequence,
                    "internal_id": internal_id,
                    "incident_code": row.get("code"),
                    "airwars_id": str(row.get("airwars_id") or ""),
                    "original_latitude": latitude_raw,
                    "original_longitude": longitude_raw,
                    "coordinate_source": coordinate_source,
                    "reasons": reasons,
                    "action": "excluded_without_correction",
                })
                continue
            points.append({
                "sequence": sequence,
                "number": row.get("number") or f"{sequence:04d}",
                "internal_id": internal_id,
                "code": row.get("code") or "",
                "date": row.get("date") or "",
                "location": row.get("location_ar") or row.get("location_original") or "",
                "lat": lat,
                "lon": lon,
                "path": row.get("path") or f"cases/{sequence:04d}/",
                "coordinate_source": coordinate_source,
            })
    reasons_count = Counter(reason for item in exclusions for reason in item["reasons"])
    total = len(points) + len(exclusions)
    report = {
        "generated_at": utc_now(),
        "source": "normalized_direct_records_with_legacy_import_fallback",
        "expected_region": EXPECTED_REGION,
        "counts": {
            "total_incidents": total,
            "incidents_with_world_valid_latitude_and_longitude": world_valid_pairs,
            "incidents_included_on_map": len(points),
            "incidents_missing_coordinates": sum(
                1 for item in exclusions if "missing_latitude" in item["reasons"] or "missing_longitude" in item["reasons"]
            ),
            "incidents_with_invalid_coordinates": sum(
                1 for item in exclusions if "invalid_latitude" in item["reasons"] or "invalid_longitude" in item["reasons"]
            ),
            "incidents_outside_expected_region": reasons_count["outside_expected_region"],
            "incidents_excluded_from_map": len(exclusions),
            "map_coverage_percentage": round((len(points) / total * 100) if total else 0, 4),
        },
        "reason_counts": dict(sorted(reasons_count.items())),
        "policy": {
            "coordinates_invented": 0,
            "coordinates_silently_corrected": 0,
            "suspicious_values_preserved": True,
            "verified_corrections_required_for_reinclusion": True,
        },
        "excluded_incidents": exclusions,
    }
    atomic_write_json(output_root / "data" / "reports" / "map-coverage.json", report)
    atomic_write_json(output_root / "data" / "generated" / "map-points.json", points)
    map_lines = [
        "# تقرير تغطية الخريطة",
        "",
        f"- إجمالي الحوادث: **{total:,}**",
        f"- المعروضة على الخريطة: **{len(points):,}**",
        f"- المستبعدة: **{len(exclusions):,}**",
        f"- نسبة التمثيل: **{report['counts']['map_coverage_percentage']}%**",
        f"- إحداثيات ناقصة: **{report['counts']['incidents_missing_coordinates']:,}**",
        f"- إحداثيات غير صالحة: **{report['counts']['incidents_with_invalid_coordinates']:,}**",
        f"- خارج نطاق التحقق الواسع: **{report['counts']['incidents_outside_expected_region']:,}**",
        "",
        "لم تُخترع إحداثيات ولم تُصحح أي قيمة مشبوهة بصمت. يحتوي ملف JSON المقابل على معرّف كل حالة مستبعدة وقيمها الأصلية وسبب الاستبعاد.",
    ]
    atomic_write_text(output_root / "data" / "reports" / "map-coverage.md", "\n".join(map_lines) + "\n")
    return report
