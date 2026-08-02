# General textual-structure engine

The connector-neutral implementation lives in `archive_engine/`:

- `models.py` defines project, site, record type, field/provenance, reference/source/content/location/person, run, release, and validation entities.
- `core/` owns immutable worklists, orchestration, checkpoints, pause/resume/cancel, raw preservation, and audit events.
- `fetchers/` owns lawful HTTP access, allowlists, pacing, retries, and exact outcome classification.
- `normalizers/` owns evidence-preserving URL normalization.
- `release_builder/` builds searchable immutable textual releases without site terminology.
- `validators/` verifies layout and checksums.
- `publisher/` performs atomic pointer switches and rollback.
- `connectors/airwars/` owns every Airwars URL pattern, label, mapping, extraction rule, historical reconciliation, page rule, and Airwars release gate.
- `connectors/synthetic/` is a deliberately unrelated library-catalogue implementation.

## Connector contract

A connector declares record types, required fields, field/relationship rules, analysis output, discovery, and parsing. The core sees only `DiscoveredTarget`, `ParsedRecord`, and generic models. It contains no Airwars selector, incident URL pattern, or Airwars field name.

## Durable execution

`ProjectStore` writes JSON atomically, content-addresses raw bodies, and appends fsynced audit events. A run creates a checksummed immutable worklist. Checkpoints record each stable target outcome. Resume verifies the worklist hash and skips already completed IDs; cancellation keeps every committed artifact.

## Output

The generic builder writes normalized records, raw values and provenance, reference records, de-duplicated external-source entities without relationship loss, raw-link inventory, coverage report, search site, manifest, SHA-256 checksums, and publishable layout.

## Creating a connector

Implement `Connector`, use site-specific terms only in the connector, return generic `SiteRecord`/`SourceReference` values, register the connector in the control-plane factory, add sanitized analysis/parser fixtures, then pass the synthetic-style end-to-end release and recovery suite.
