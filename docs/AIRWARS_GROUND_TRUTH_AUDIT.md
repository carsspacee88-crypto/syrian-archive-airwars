# Airwars ground-truth audit

Run:

```bash
python scripts/audit_airwars_ground_truth.py \
  --project-root /srv/archive \
  --legacy-zip /srv/archive/data/cache/syrian-archive-historical.zip \
  --release-root /srv/site-releases/current \
  --output-root /srv/archive
```

Canonical outputs are `reports/airwars-ground-truth.json` and `.md`; full relationship exports are under `exports/`. Every metric contains its definition, canonical input, calculation, old claim, and result. Primary source-content states are mutually exclusive and reconciled against the source-entity count.

## Canonical sources

- Incidents: `data/incidents/*.json`, enriched only with fields from the restored, checksummed historical package.
- Relationships: every historical source occurrence plus relationships uniquely recoverable from normalized provenance.
- External sources: `data/sources/*.json` stable entities.
- Pages: inspected only to verify build coverage; HTML file counts never define canonical entities.
- Current direct verification: the current Airwars endpoint/live-page attempt fields only. Archived copies are excluded from direct success.

## 45,075 versus 45,081

All 45,075 entities in the last full worklist carry scope `0001-8114`. Six valid entities retained scope `3000-5999`; all relate to `airwars-32716` (`CS1033`, sequence 4771) and were recovered from a public archived incident copy. A later worklist was reconstructed from that incident's empty normalized `sources` array and omitted the six. They are real source entities—not index pages, utility pages, malformed IDs, duplicate IDs, orphan HTML, or a counting error—so the canonical source-entity total is 45,081.

The six stable IDs and complete evidence are emitted under `discrepancy_45075_45081.records` in the JSON report.

## Quality correction

The audit does not trust a legacy `completeness=full` flag alone. `validate_full_source_text` rejects known login/deletion/restriction/error shells and requires either a structured extractor or a content-shaped main-text extraction. Rejected non-empty bodies become `PARTIAL_TEXT`; they remain preserved and reviewable.
