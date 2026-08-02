# Airwars Syria ground-truth audit

Generated: `2026-08-02T15:16:47+00:00`

| Metric | Previous claim | Recomputed result | Definition | Canonical input |
|---|---:|---:|---|---|
| `incident_records_total` | 8114 | **8114** | Canonical normalized Airwars Syria incident records. | `data/incidents/*.json` |
| `incident_ids_unique` | — | **8114** | Distinct stable incident identifiers. | `data/incidents/*.json` |
| `incident_ids_duplicate` | — | **0** | Incident rows sharing the same stable identifier. | `data/incidents/*.json` |
| `incident_pages_generated` | 8114 | **8114** | Canonical incident pages present in the inspected release. | `release site/cases/*/index.html` |
| `incident_pages_missing` | — | **0** | Canonical incidents without a page. | `canonical incident IDs + release site` |
| `incident_pages_orphaned` | — | **0** | Generated incident pages without a canonical incident. | `canonical incident IDs + release site` |
| `source_reference_records_total` | — | **50332** | Every incident-to-source occurrence, including duplicates and locally recovered relationships. | `historical package cases/*/data.json + normalized source provenance + normalized incident sources` |
| `source_reference_ids_unique` | — | **50332** | Distinct stable source-reference occurrence IDs. | `derived source-reference index` |
| `source_reference_ids_duplicate` | — | **0** | Duplicate stable source-reference IDs. | `derived source-reference index` |
| `source_reference_duplicate_relationships` | — | **526** | Reference occurrences explicitly identified as a repeated incident-to-raw-URL relationship; retained rather than deduplicated away. | `derived source-reference index duplicate_relationship` |
| `source_reference_url_reuse_occurrences` | — | **5234** | Reference occurrences beyond the first occurrence of each distinct raw URL across the whole archive. | `derived source-reference index raw_url` |
| `source_reference_malformed_urls` | — | **12** | Reference rows whose raw URL could not be normalized to a valid HTTP(S) URL. | `derived source-reference index normalization_status` |
| `source_reference_manual_review` | — | **19449** | Reference rows explicitly marked for manual review. | `derived source-reference index manual_review` |
| `source_record_files_total` | 45,075 or 45,081 | **45081** | Unique external source entity records stored locally. | `data/sources/*.json` |
| `source_record_ids_unique` | — | **45081** | Distinct source entity IDs. | `data/sources/*.json` |
| `source_record_ids_duplicate` | — | **0** | Source files sharing a stable source_id. | `data/sources/*.json` |
| `unique_raw_source_urls` | — | **45098** | Distinct non-empty raw URL evidence values across reference occurrences. | `derived source-reference index` |
| `unique_normalized_source_urls` | — | **45069** | Distinct successfully normalized HTTP(S) source URLs. | `derived source-reference index` |
| `source_reference_pages_generated` | — | **50332** | Local pages representing individual incident-to-source occurrences. | `release site/references/*/index.html` |
| `source_reference_pages_missing` | — | **0** | Canonical reference occurrences without a local page. | `reference index + release site` |
| `source_reference_pages_orphaned` | — | **0** | Reference pages without a canonical reference record. | `reference index + release site` |
| `local_source_entity_pages_generated` | — | **45081** | Local pages describing unique source entities; this does not assert content preservation. | `release site/sources/*/index.html` |
| `sources_full_text_direct` | — | **17521** | Source entities with non-empty text that passed quality validation as complete and came from direct public retrieval. | `data/sources content_quality + preservation_status` |
| `sources_full_text_archived` | — | **1** | Source entities with validated complete text obtained from a public archived copy. | `data/sources content_quality + preservation_status` |
| `sources_full_text_local_snapshot` | — | **0** | Source entities with validated complete text preserved only in a local snapshot. | `data/sources content_quality + preservation_status` |
| `sources_partial_text` | — | **13762** | Source entities with non-empty text not proven to be the complete main text. | `data/sources text_original + content_quality` |
| `sources_metadata_only` | — | **706** | Source entities with metadata but no preserved text and no stronger blocked/dead/malformed state. | `data/sources` |
| `sources_link_only` | — | **0** | Source entities with a preserved URL but neither text nor metadata nor stronger failure state. | `data/sources` |
| `sources_blocked` | — | **12602** | Source entities without text whose recorded attempts show a public-access block (401/403/429/451 or explicit block evidence). | `data/sources attempt_history` |
| `sources_dead` | — | **465** | Source entities without text whose attempts returned 404 or 410. | `data/sources attempt_history` |
| `sources_malformed` | — | **12** | Source entities without text whose raw URL cannot normalize to an HTTP(S) URL. | `data/sources original_url` |
| `sources_duplicate_content` | — | **8481** | Source records participating in a non-empty identical content-hash group. | `data/sources content_hash` |
| `sources_requires_manual_review` | — | **16817** | Source entities with review flags or a manual-review/malformed primary state. | `data/sources review_flags + primary status` |
| `incidents_with_valid_coordinates` | 6679 | **6679** | Incidents with a world-valid pair inside the accepted display range. | `data/incidents latitude/longitude` |
| `incidents_without_coordinates` | 1432 | **1432** | Incidents missing latitude or longitude. | `data/incidents latitude/longitude` |
| `incidents_with_malformed_coordinates` | 1 | **1** | Incidents with non-numeric or world-invalid coordinates. | `data/incidents latitude/longitude` |
| `incidents_outside_coordinate_range` | 2 | **2** | World-valid coordinates outside the accepted display range. | `data/incidents latitude/longitude` |
| `direct_airwars_fetch_success` | 12 | **0** | Incidents whose current direct Airwars endpoint or live-page request succeeded; archived copies do not count. | `data/incidents retrieval_status.airwars_endpoint/live_page` |
| `direct_airwars_fetch_blocked_403` | 8102 | **8114** | Incidents whose current direct Airwars attempts contain HTTP 403 evidence. | `data/incidents retrieval_status.airwars_endpoint/live_page` |
| `direct_airwars_fetch_other_failure` | — | **0** | Direct Airwars attempts neither successful nor evidenced as HTTP 403. | `data/incidents retrieval_status.airwars_endpoint/live_page` |
| `historical_or_local_text_records` | — | **8114** | Incident records whose usable structural text originates from a historical/local or mixed origin. | `enriched incident record_origin_status` |
| `records_without_usable_incident_text` | — | **0** | Incidents without an original narrative, archived narrative, or historical structured summary. | `enriched incident textual_description` |
| `internal_links_checked` | 499177 | **1294531** | Internal hyperlinks checked by the release validator. | `release validation.json` |
| `internal_links_broken` | 0 | **0** | Internal hyperlinks whose target does not exist. | `release validation.json` |
| `external_links_discovered` | — | **45098** | Distinct raw external source URLs represented by references. | `derived source-reference index` |
| `external_links_checked` | — | **45081** | Unique source entities with at least one recorded public retrieval attempt. | `data/sources attempt_history` |
| `external_links_reachable` | — | **31285** | Checked source entities with a successful/cached retrieval state. | `data/sources retrieval and attempt records` |
| `external_links_blocked` | — | **12611** | Checked source entities with explicit block evidence. | `data/sources attempt_history` |
| `external_links_dead` | — | **465** | Checked source entities with HTTP 404/410 evidence. | `data/sources attempt_history` |
| `external_links_not_checked` | — | **0** | Unique source entities without a recorded public retrieval attempt. | `data/sources attempt_history` |

## 45,075 versus 45,081

Six source entities recovered from the archived Airwars copy for incident sequence 4771 remained valid local records, but the later full-run worklist was rebuilt from that incident's empty normalized sources array and omitted them. They are source entities, not index/utility pages or duplicate IDs.

The six stable IDs are: source-0b65993be9d09db6cb3933ee, source-3992eac147bec71a8a866096, source-4db2cb82be45b81e732c32de, source-5807d6a207f89eeff690bd1d, source-5c378944d2b9d0b4b7989dbf, source-dda3f9afcab65dc627177fe6.

## Reconciliation

- `source_entity_primary_status_reconciliation`: 45081 = 45081 — balanced
- `coordinate_reconciliation`: 8114 = 8114 — balanced
- `direct_attempt_reconciliation`: 8114 = 8114 — balanced
- `source_reference_origin_reconciliation`: 50332 = 50332 — balanced
