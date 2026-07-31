from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime
from html import unescape
from typing import Any
from urllib.parse import urlparse

from lxml import html

from . import PARSER_VERSION, SCHEMA_VERSION
from .io_utils import as_number, clean_text, sha256_bytes, stable_internal_id, utc_now


SOURCE_PRIORITY = {
    "manual_correction": 100,
    "airwars_endpoint": 90,
    "airwars_live": 80,
    "airwars_archive": 70,
    "external_archive": 60,
    "legacy_import": 10,
}


def _legacy_value(incident: dict[str, Any], key: str) -> Any:
    value = incident.get(key)
    return None if value in ("", None) else value


def _legacy_sources(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for position, row in enumerate(rows, 1):
        output.append({
            "order": row.get("ترتيب المصدر") or position,
            "source_id": row.get("معرّف المصدر") or "",
            "format": row.get("صيغة السجل") or "legacy_import",
            "name_ar": row.get("اسم المصدر — عربي") or "",
            "name_original": row.get("اسم المصدر — الأصل") or "",
            "author": row.get("كاتب/صاحب المصدر") or "",
            "platform_ar": row.get("اللغة/المنصة — عربي") or "",
            "platform_original": row.get("اللغة/المنصة — الأصل") or "",
            "published_date": row.get("تاريخ المصدر") or "",
            "description_ar": row.get("وصف المصدر — عربي") or "",
            "description_original": row.get("وصف المصدر — الأصل") or "",
            "url": row.get("رابط المصدر") or "",
            "archive_url": row.get("رابط الأرشيف") or "",
            "media_count": row.get("عدد الوسائط") or 0,
            "provenance": "legacy_import",
        })
    return output


def _legacy_victims(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for position, row in enumerate(rows, 1):
        output.append({
            "order": row.get("ترتيب الضحية") or position,
            "name_local": row.get("الاسم المحلي") or "",
            "name_original": row.get("الاسم الأصلي/المنقول") or "",
            "gender_ar": row.get("الجنس — عربي") or "",
            "gender_original": row.get("الجنس — الأصل") or "",
            "age": row.get("العمر") or "",
            "age_group_ar": row.get("الفئة العمرية — عربي") or "",
            "age_group_original": row.get("الفئة العمرية — الأصل") or "",
            "status_ar": row.get("الحالة — عربي") or "",
            "status_original": row.get("الحالة — الأصل") or "",
            "additional_information": row.get("معلومات إضافية") or "",
            "person_url": row.get("رابط الشخص") or row.get("رابط الصورة") or "",
            "provenance": "legacy_import",
        })
    return output


def _legacy_media(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for position, wrapper in enumerate(rows, 1):
        row = wrapper.get("data", wrapper)
        all_sizes = wrapper.get("all_sizes", [])
        output.append({
            "order": row.get("ترتيب الوسيط") or position,
            "type_ar": row.get("نوع الوسيط — عربي") or "",
            "type_original": row.get("نوع الوسيط — الأصل") or "",
            "caption_ar": row.get("الوصف/العنوان — عربي") or "",
            "caption_original": row.get("الوصف/العنوان — الأصل") or "",
            "url": row.get("رابط الوسيط/الصورة") or wrapper.get("display_url") or "",
            "thumbnail_url": row.get("رابط الصورة المصغرة") or "",
            "source_name": wrapper.get("source_name") or "",
            "sensitive": bool(wrapper.get("is_sensitive")) or row.get("محتوى صادم") == "نعم",
            "sizes": all_sizes,
            "storage_status": "external_only",
            "provenance": "legacy_import",
        })
    return output


def _archive_urls(legacy: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for source in legacy.get("sources", []):
        if source.get("رابط الأرشيف"):
            values.append(source["رابط الأرشيف"])
    for wrapper in legacy.get("additional_links", []) + legacy.get("api_links", []):
        row = wrapper.get("data", wrapper)
        url = wrapper.get("url") or row.get("الرابط") or ""
        category = wrapper.get("category") or row.get("فئة الرابط — عربي") or ""
        if url and ("أرشيف" in category or "archive" in url.lower()):
            values.append(url)
    return list(dict.fromkeys(values))


def build_legacy_record(summary: dict[str, Any], legacy: dict[str, Any]) -> dict[str, Any]:
    sequence = int(summary["sequence"])
    incident = legacy.get("incident", {})
    internal_id = stable_internal_id(summary.get("airwars_id"), sequence)
    now = utc_now()
    base_provenance = {
        "source_type": "legacy_import",
        "source_url": summary.get("airwars_url") or "",
        "retrieved_at": None,
        "note": "Imported from the historical Excel-derived snapshot; not independently re-verified.",
    }
    fields = {
        "incident_code": summary.get("code") or _legacy_value(incident, "رمز الحادثة"),
        "incident_date": summary.get("date") or _legacy_value(incident, "تاريخ الحادثة"),
        "location": summary.get("location_original") or _legacy_value(incident, "الموقع بالإنجليزية"),
        "location_ar": summary.get("location_ar") or _legacy_value(incident, "الموقع بالعربية"),
        "country": "Syria",
        "country_ar": _legacy_value(incident, "الدولة") or "سوريا",
        "latitude": summary.get("latitude") if summary.get("latitude") is not None else _legacy_value(incident, "خط العرض"),
        "longitude": summary.get("longitude") if summary.get("longitude") is not None else _legacy_value(incident, "خط الطول"),
        "alleged_belligerent": summary.get("military_original") or _legacy_value(incident, "الجهة العسكرية — الأصل"),
        "alleged_belligerent_ar": summary.get("military_ar") or _legacy_value(incident, "الجهة العسكرية — عربي"),
        "strike_type": summary.get("strike_type_original") or _legacy_value(incident, "نوع الضربة — الأصل"),
        "strike_type_ar": summary.get("strike_type_ar") or _legacy_value(incident, "نوع الضربة — عربي"),
        "assessment": _legacy_value(incident, "قوة الأدلة — الأصل"),
        "assessment_ar": summary.get("evidence_ar") or _legacy_value(incident, "قوة الأدلة — عربي"),
        "civilian_deaths_min": summary.get("killed_min"),
        "civilian_deaths_max": summary.get("killed_max"),
        "civilian_injuries_min": summary.get("injured_min"),
        "civilian_injuries_max": summary.get("injured_max"),
        "narrative": "",
        "narrative_ar": _legacy_value(incident, "ملخص عربي منظم") or "",
    }
    provenance = {
        key: [deepcopy(base_provenance)]
        for key, value in fields.items()
        if value not in (None, "")
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "internal_id": internal_id,
        "legacy_sequence": sequence,
        "incident_code": fields["incident_code"],
        "airwars_id": str(summary.get("airwars_id") or ""),
        "canonical_url": summary.get("airwars_url") or "",
        "original_source_url": summary.get("airwars_url") or "",
        "archived_urls": _archive_urls(legacy),
        "incident_date": fields["incident_date"],
        "location": fields["location"],
        "location_ar": fields["location_ar"],
        "country": fields["country"],
        "country_ar": fields["country_ar"],
        "latitude": fields["latitude"],
        "longitude": fields["longitude"],
        "alleged_belligerent": fields["alleged_belligerent"],
        "alleged_belligerent_ar": fields["alleged_belligerent_ar"],
        "strike_type": fields["strike_type"],
        "strike_type_ar": fields["strike_type_ar"],
        "civilian_deaths_min": fields["civilian_deaths_min"],
        "civilian_deaths_max": fields["civilian_deaths_max"],
        "civilian_injuries_min": fields["civilian_injuries_min"],
        "civilian_injuries_max": fields["civilian_injuries_max"],
        "assessment": fields["assessment"],
        "assessment_ar": fields["assessment_ar"],
        "narrative": fields["narrative"],
        "narrative_ar": fields["narrative_ar"],
        "victims": _legacy_victims(legacy.get("victims", [])),
        "sources": _legacy_sources(legacy.get("sources", [])),
        "media_metadata": _legacy_media(legacy.get("media", [])),
        "source_sections": [],
        "additional_links": deepcopy(legacy.get("additional_links", [])),
        "retrieval_status": {
            "overall": "pending",
            "live_page": None,
            "airwars_endpoint": None,
            "archive_page": None,
        },
        "extraction_status": "legacy_import_only",
        "legacy_completeness_status": summary.get("completion") or "partial",
        "completeness_status": "pending",
        "missing_fields": [],
        "missing_sections": [],
        "review_flags": [],
        "conflicts": [],
        "field_provenance": provenance,
        "retrieved_at": None,
        "source_last_modified": _legacy_value(incident, "آخر تعديل"),
        "content_hash": None,
        "legacy_snapshot": {
            "source_type": "legacy_import",
            "sequence": sequence,
            "completion": summary.get("completion"),
            "workbook_sha256": legacy.get("case", {}).get("source_workbook_sha256"),
            "migrated_at": now,
        },
    }


def _same_value(left: Any, right: Any) -> bool:
    if left in (None, "") or right in (None, ""):
        return left in (None, "") and right in (None, "")
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        try:
            return abs(float(left) - float(right)) < 1e-7
        except (TypeError, ValueError):
            pass
    return clean_text(left).casefold() == clean_text(right).casefold()


def _set_verified(
    record: dict[str, Any],
    field: str,
    value: Any,
    source_type: str,
    source_url: str,
    retrieved_at: str,
    raw_label: str = "",
) -> None:
    if value in (None, ""):
        return
    old = record.get(field)
    if old not in (None, "") and not _same_value(old, value):
        record["conflicts"].append({
            "field": field,
            "preferred_value": value,
            "preferred_source": source_type,
            "other_value": old,
            "other_source": record.get("field_provenance", {}).get(field, [{}])[0].get("source_type", "legacy_import"),
            "resolution": "stronger_airwars_source_preferred_legacy_preserved",
        })
    record[field] = value
    record.setdefault("field_provenance", {})[field] = [{
        "source_type": source_type,
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "raw_label": raw_label,
    }]


def _parse_date(value: str) -> str:
    text = clean_text(value)
    if not text:
        return ""
    match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if match:
        return match.group(0)
    for pattern in ("%B %d, %Y", "%d %B %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, pattern).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


def _parse_coordinates(value: str) -> tuple[float | None, float | None]:
    numbers = re.findall(r"-?(?:\d+(?:\.\d+)?|\.\d+)", clean_text(value))
    if len(numbers) < 2:
        return None, None
    lat = as_number(numbers[0])
    lon = as_number(numbers[1])
    return (float(lat) if lat is not None else None, float(lon) if lon is not None else None)


def _strip_html(value: str) -> str:
    if not value:
        return ""
    try:
        fragment = html.fromstring(f"<div>{value}</div>")
        return clean_text(fragment.text_content())
    except Exception:
        return clean_text(unescape(re.sub(r"<[^>]+>", " ", value)))


def apply_page_extraction(
    record: dict[str, Any],
    parsed: dict[str, Any],
    source_type: str,
    source_url: str,
    retrieved_at: str,
    content_hash: str,
) -> None:
    canonical_found = clean_text(parsed.get("canonical_url", ""))
    if canonical_found and urlparse(canonical_found).netloc.endswith("airwars.org"):
        _set_verified(record, "canonical_url", canonical_found, source_type, source_url, retrieved_at, "link[rel=canonical]")
    field_map = {clean_text(row.get("label")).casefold(): row for row in parsed.get("fields", [])}
    mappings = {
        "incident code": ("incident_code", lambda value: clean_text(value).splitlines()[0]),
        "incident date": ("incident_date", _parse_date),
    }
    for label, (field, transform) in mappings.items():
        row = field_map.get(label)
        if row:
            _set_verified(record, field, transform(row.get("value", "")), source_type, source_url, retrieved_at, row.get("label", ""))
    location_row = field_map.get("location")
    if location_row:
        source_location = clean_text(location_row.get("value", ""))
        existing_location = clean_text(record.get("location", ""))
        if existing_location and existing_location.casefold() in source_location.casefold():
            _set_verified(record, "location", existing_location, source_type, source_url, retrieved_at, location_row.get("label", ""))
            record["location_source_value"] = source_location
            record.setdefault("field_provenance", {})["location_source_value"] = [{
                "source_type": source_type,
                "source_url": source_url,
                "retrieved_at": retrieved_at,
                "raw_label": location_row.get("label", ""),
            }]
        else:
            _set_verified(record, "location", source_location, source_type, source_url, retrieved_at, location_row.get("label", ""))
    geo = field_map.get("geolocation")
    if geo:
        lat, lon = _parse_coordinates(geo.get("value", ""))
        if lat is not None and -90 <= lat <= 90:
            _set_verified(record, "latitude", lat, source_type, source_url, retrieved_at, "Geolocation")
        if lon is not None and -180 <= lon <= 180:
            _set_verified(record, "longitude", lon, source_type, source_url, retrieved_at, "Geolocation")

    sections = parsed.get("sections", [])
    narrative_parts = [
        section.get("text", "")
        for section in sections
        if section.get("id") not in {"incident-header", "key-information", "sources", "media"}
    ]
    narrative = clean_text("\n\n".join(filter(None, narrative_parts)))
    if narrative:
        _set_verified(record, "narrative", narrative, source_type, source_url, retrieved_at, "page_sections")
    record["source_sections"] = [
        {**section, "provenance": source_type, "source_url": source_url}
        for section in sections
    ]
    if parsed.get("sources"):
        record["sources"] = [
            {**source, "provenance": source_type}
            for source in parsed.get("sources", [])
        ]
    if parsed.get("media_metadata"):
        record["media_metadata"] = [
            {**media, "provenance": source_type, "storage_status": "external_only"}
            for media in parsed["media_metadata"]
        ]
    for link in parsed.get("links", []):
        if link.get("type") == "archive" and link.get("url"):
            record["archived_urls"].append(link["url"])
    record["archived_urls"] = list(dict.fromkeys(record["archived_urls"]))
    record["page_extraction"] = {
        "source_type": source_type,
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "content_hash": content_hash,
        "title": parsed.get("title"),
        "canonical_url_found": parsed.get("canonical_url"),
        "sources_section_present": parsed.get("sources_section_present"),
        "sources_declared": parsed.get("sources_declared"),
        "sources_extracted": len(parsed.get("sources", [])),
        "media_section_present": parsed.get("media_section_present"),
        "media_declared": parsed.get("media_declared"),
        "media_metadata_extracted": len(parsed.get("media_metadata", [])),
    }
    record["content_hash"] = content_hash
    record["retrieved_at"] = retrieved_at


def apply_api_record(
    record: dict[str, Any],
    api_record: dict[str, Any],
    source_url: str,
    retrieved_at: str,
    content_hash: str,
) -> None:
    acf = api_record.get("acf") or {}
    source_type = "airwars_endpoint"
    _set_verified(record, "incident_code", acf.get("unique_reference_code"), source_type, source_url, retrieved_at, "acf.unique_reference_code")
    _set_verified(record, "incident_date", _parse_date(str(acf.get("incident_date") or "")), source_type, source_url, retrieved_at, "acf.incident_date")
    _set_verified(record, "location", acf.get("location_name"), source_type, source_url, retrieved_at, "acf.location_name")
    _set_verified(record, "location_ar", acf.get("location_name_local"), source_type, source_url, retrieved_at, "acf.location_name_local")
    geolocations = acf.get("geolocations") if isinstance(acf.get("geolocations"), list) else []
    geo = next((item for item in geolocations if item.get("primary_coordinate")), geolocations[0] if geolocations else {})
    lat = as_number(geo.get("latitude", acf.get("latitude")))
    lon = as_number(geo.get("longitude", acf.get("longitude")))
    if lat is not None and -90 <= float(lat) <= 90:
        _set_verified(record, "latitude", lat, source_type, source_url, retrieved_at, "acf.geolocations.latitude")
    if lon is not None and -180 <= float(lon) <= 180:
        _set_verified(record, "longitude", lon, source_type, source_url, retrieved_at, "acf.geolocations.longitude")
    narrative = _strip_html((api_record.get("content") or {}).get("rendered", ""))
    if narrative:
        _set_verified(record, "narrative", narrative, source_type, source_url, retrieved_at, "content.rendered")
    if api_record.get("link"):
        _set_verified(record, "canonical_url", api_record["link"], source_type, source_url, retrieved_at, "link")
    casualties = {
        "civilian_deaths_min": ("killed_injured_civilian_non_combatants", "killed_min"),
        "civilian_deaths_max": ("killed_injured_civilian_non_combatants", "killed_max"),
        "civilian_injuries_min": ("killed_injured_civilian_non_combatants", "injured_min"),
        "civilian_injuries_max": ("killed_injured_civilian_non_combatants", "injured_max"),
    }
    for field, (group, key) in casualties.items():
        payload = acf.get(group) if isinstance(acf.get(group), dict) else {}
        value = as_number(payload.get(key))
        _set_verified(record, field, value, source_type, source_url, retrieved_at, f"acf.{group}.{key}")

    api_victims = acf.get("victims")
    if isinstance(api_victims, dict):
        api_victims = list(api_victims.values())
    if isinstance(api_victims, list) and api_victims:
        normalized_victims = []
        for position, item in enumerate(api_victims, 1):
            if not isinstance(item, dict):
                continue
            normalized_victims.append({
                "order": position,
                "name_local": item.get("name_local") or item.get("name") or item.get("victim_name") or "",
                "name_original": item.get("name_original") or item.get("name_latin") or "",
                "gender_original": item.get("gender") or "",
                "age": item.get("age") or "",
                "status_original": item.get("status") or item.get("harm_status") or "",
                "additional_information": item.get("notes") or item.get("description") or "",
                "person_url": item.get("url") or item.get("link") or "",
                "raw": item,
                "provenance": source_type,
            })
        if normalized_victims:
            record["victims"] = normalized_victims

    api_sources = acf.get("sources")
    if isinstance(api_sources, dict):
        api_sources = list(api_sources.values())
    if isinstance(api_sources, list) and api_sources:
        normalized_sources = []
        for position, item in enumerate(api_sources, 1):
            if not isinstance(item, dict):
                continue
            normalized_sources.append({
                "order": position,
                "source_id": item.get("source_id") or item.get("id") or "",
                "format": "airwars_endpoint",
                "name": item.get("name") or item.get("source_name") or "",
                "date": item.get("date") or item.get("published_date") or "",
                "language": item.get("language") or item.get("languages") or "",
                "author": item.get("author") or item.get("source_author") or "",
                "url": item.get("url") or item.get("source_url") or "",
                "archive_url": item.get("archive_url") or "",
                "content": item.get("content") or "",
                "content_translated": item.get("translated_content") or "",
                "raw": item,
                "provenance": source_type,
            })
        if normalized_sources:
            record["sources"] = normalized_sources
            for item in normalized_sources:
                if item.get("archive_url"):
                    record["archived_urls"].append(item["archive_url"])
            record["archived_urls"] = list(dict.fromkeys(record["archived_urls"]))

    record["api_structured_sections"] = {
        key: acf.get(key)
        for key in ("victims", "victim_groups", "casualties", "belligerents", "infrastructure")
        if acf.get(key) not in (None, "", [], {})
    }
    record["source_last_modified"] = str(api_record.get("modified") or "")[:19]
    record["api_extraction"] = {
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "content_hash": content_hash,
        "wordpress_id": api_record.get("id"),
        "slug": api_record.get("slug"),
        "sources_extracted": sum(1 for item in record.get("sources", []) if item.get("provenance") == source_type),
        "victims_extracted": sum(1 for item in record.get("victims", []) if item.get("provenance") == source_type),
    }
    record["content_hash"] = content_hash
    record["retrieved_at"] = retrieved_at


def calculate_coordinate_flags(record: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    lat = as_number(record.get("latitude"))
    lon = as_number(record.get("longitude"))
    if lat is None:
        flags.append("missing_latitude")
    elif not -90 <= float(lat) <= 90:
        flags.append("invalid_latitude")
    if lon is None:
        flags.append("missing_longitude")
    elif not -180 <= float(lon) <= 180:
        flags.append("invalid_longitude")
    if lat is not None and lon is not None and -90 <= float(lat) <= 90 and -180 <= float(lon) <= 180:
        if not (31.0 <= float(lat) <= 38.0 and 34.0 <= float(lon) <= 43.0):
            flags.append("outside_expected_region")
    return flags


def finalize_status(record: dict[str, Any]) -> None:
    required_fields = ["incident_code", "canonical_url", "location"]
    missing_fields = [field for field in required_fields if record.get(field) in (None, "")]
    if not record.get("incident_date"):
        missing_fields.append("incident_date")
    extraction = record.get("page_extraction") or {}
    api_extraction = record.get("api_extraction") or {}
    missing_sections = []
    if not record.get("narrative"):
        missing_sections.append("narrative")
    sources_declared = extraction.get("sources_declared")
    declared_sources_incomplete = (
        isinstance(sources_declared, int)
        and sources_declared > len(record.get("sources", []))
    )
    if (not extraction.get("sources_section_present") and not api_extraction.get("sources_extracted")) or declared_sources_incomplete:
        missing_sections.append("sources")
    record["missing_fields"] = missing_fields
    record["missing_sections"] = missing_sections
    record["review_flags"] = list(dict.fromkeys(record.get("review_flags", []) + calculate_coordinate_flags(record)))

    retrieval = record.get("retrieval_status", {})
    source_succeeded = any(
        isinstance(retrieval.get(key), dict) and retrieval[key].get("ok")
        for key in ("airwars_endpoint", "live_page", "archive_page")
    )
    page_succeeded = bool(record.get("page_extraction") or record.get("api_extraction"))
    if record.get("conflicts"):
        status = "conflicting_sources"
    elif "text_encoding_requires_review" in record.get("review_flags", []):
        status = "pending_review"
    elif page_succeeded and not missing_fields and not missing_sections:
        status = "complete"
    elif page_succeeded:
        status = "partial"
    elif not source_succeeded:
        statuses = [
            item.get("status")
            for item in retrieval.values()
            if isinstance(item, dict)
        ]
        concrete_statuses = [status for status in statuses if status is not None]
        if concrete_statuses and all(status == 404 for status in concrete_statuses):
            status = "unavailable"
        elif 403 in statuses:
            status = "blocked"
        else:
            status = "failed"
    else:
        status = "partial"
    record["completeness_status"] = status
    record["extraction_status"] = "parsed" if page_succeeded else "not_parsed"
    retrieval["overall"] = "success" if source_succeeded else status
