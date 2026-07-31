# تقرير التحقق الآلي / Automated validation report

- النتيجة / Result: **passed**
- أخطاء حرجة / Critical: **0**
- تحذيرات / Warnings: **5**
- معلومات / Info: **0**
- وقت التقرير: `2026-07-31T11:49:14+00:00`

## الفحوص / Checks

```json
{
  "case_files": {
    "expected": 8114,
    "invalid_json": 0,
    "json_present": 8114,
    "pages_present": 8114
  },
  "internal_links": {
    "absolute_root_paths": 0,
    "broken": 0,
    "html_files_scanned": 8200,
    "links_checked": 139236
  },
  "legacy_duplicate_public_codes": {
    "TS631": [
      494,
      496
    ],
    "TS633": [
      480,
      481
    ]
  },
  "map": {
    "excluded": 1435,
    "included": 6679,
    "point_file_count": 6679,
    "total": 8114
  },
  "media_binary_policy": {
    "binary_files_found": 0
  },
  "normalized_records": {
    "count": 16,
    "duplicate_public_codes": {
      "TS631": [
        "airwars-93203",
        "airwars-93265"
      ],
      "TS633": [
        "airwars-93205",
        "airwars-93213"
      ]
    },
    "unique_internal_ids": 16
  },
  "pagination": {
    "expected_pages": 82,
    "missing": [],
    "present": 82
  }
}
```

## المشكلات / Issues

- **warning · empty_source_section** — `data/incidents/airwars-83712.json` — Source section was found but yielded no source records.
- **warning · duplicate_public_incident_code** — `data/incidents` — Public incident code is shared by different stable IDs; records were preserved separately.
- **warning · duplicate_public_incident_code** — `data/incidents` — Public incident code is shared by different stable IDs; records were preserved separately.
- **warning · duplicate_legacy_public_incident_code** — `data/cases-summary.json` — Duplicate public code preserved as separate sequences.
- **warning · duplicate_legacy_public_incident_code** — `data/cases-summary.json` — Duplicate public code preserved as separate sequences.
