# تقرير التحقق الآلي / Automated validation report

- النتيجة / Result: **passed**
- أخطاء حرجة / Critical: **0**
- تحذيرات / Warnings: **31**
- معلومات / Info: **0**
- وقت التقرير: `2026-07-31T19:05:59+00:00`

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
    "html_files_scanned": 9639,
    "links_checked": 150882
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
    "count": 113,
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
    "unique_internal_ids": 113
  },
  "pagination": {
    "expected_pages": 82,
    "missing": [],
    "present": 82
  },
  "pilot_first_100": {
    "incident_source_relationships": 1506,
    "manifest_incidents": 100,
    "media_binaries": 0,
    "media_placeholders": 1470,
    "normalized_incidents": 100,
    "schema": {
      "errors": 0,
      "files_checked": 4,
      "instances_checked": 3010
    },
    "scope_exact_0001_0100": true,
    "source_records": 1439
  }
}
```

## المشكلات / Issues

- **warning · unexpected_schema_version** — `data/incidents/airwars-32716.json` — Unexpected normalized schema version.
- **warning · unexpected_parser_version** — `data/incidents/airwars-32716.json` — Unexpected parser version.
- **warning · unexpected_schema_version** — `data/incidents/airwars-51133.json` — Unexpected normalized schema version.
- **warning · unexpected_parser_version** — `data/incidents/airwars-51133.json` — Unexpected parser version.
- **warning · unexpected_schema_version** — `data/incidents/airwars-55996.json` — Unexpected normalized schema version.
- **warning · unexpected_parser_version** — `data/incidents/airwars-55996.json` — Unexpected parser version.
- **warning · unexpected_schema_version** — `data/incidents/airwars-57200.json` — Unexpected normalized schema version.
- **warning · unexpected_parser_version** — `data/incidents/airwars-57200.json` — Unexpected parser version.
- **warning · unexpected_schema_version** — `data/incidents/airwars-57902.json` — Unexpected normalized schema version.
- **warning · unexpected_parser_version** — `data/incidents/airwars-57902.json` — Unexpected parser version.
- **warning · unexpected_schema_version** — `data/incidents/airwars-59776.json` — Unexpected normalized schema version.
- **warning · unexpected_parser_version** — `data/incidents/airwars-59776.json` — Unexpected parser version.
- **warning · unexpected_schema_version** — `data/incidents/airwars-74721.json` — Unexpected normalized schema version.
- **warning · unexpected_parser_version** — `data/incidents/airwars-74721.json` — Unexpected parser version.
- **warning · unexpected_schema_version** — `data/incidents/airwars-83712.json` — Unexpected normalized schema version.
- **warning · unexpected_parser_version** — `data/incidents/airwars-83712.json` — Unexpected parser version.
- **warning · empty_source_section** — `data/incidents/airwars-83712.json` — Source section was found but yielded no source records.
- **warning · unexpected_schema_version** — `data/incidents/airwars-84313.json` — Unexpected normalized schema version.
- **warning · unexpected_parser_version** — `data/incidents/airwars-84313.json` — Unexpected parser version.
- **warning · unexpected_schema_version** — `data/incidents/airwars-93203.json` — Unexpected normalized schema version.
- **warning · unexpected_parser_version** — `data/incidents/airwars-93203.json` — Unexpected parser version.
- **warning · unexpected_schema_version** — `data/incidents/airwars-93205.json` — Unexpected normalized schema version.
- **warning · unexpected_parser_version** — `data/incidents/airwars-93205.json` — Unexpected parser version.
- **warning · unexpected_schema_version** — `data/incidents/airwars-93213.json` — Unexpected normalized schema version.
- **warning · unexpected_parser_version** — `data/incidents/airwars-93213.json` — Unexpected parser version.
- **warning · unexpected_schema_version** — `data/incidents/airwars-93265.json` — Unexpected normalized schema version.
- **warning · unexpected_parser_version** — `data/incidents/airwars-93265.json` — Unexpected parser version.
- **warning · duplicate_public_incident_code** — `data/incidents` — Public incident code is shared by different stable IDs; records were preserved separately.
- **warning · duplicate_public_incident_code** — `data/incidents` — Public incident code is shared by different stable IDs; records were preserved separately.
- **warning · duplicate_legacy_public_incident_code** — `data/cases-summary.json` — Duplicate public code preserved as separate sequences.
- **warning · duplicate_legacy_public_incident_code** — `data/cases-summary.json` — Duplicate public code preserved as separate sequences.
