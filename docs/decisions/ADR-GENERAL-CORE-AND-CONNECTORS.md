# ADR: general core and site connectors

- Status: accepted
- Date: 2026-08-02

## Context

The proven Airwars workflow mixed general concerns—fetching, checkpointing, provenance, validation, and publishing—with Airwars identifiers and selectors. Reuse by another textual archive would either duplicate durability logic or contaminate the core with more site-specific branches.

## Decision

Keep generic entities and orchestration in `archive_engine` modules and require connectors to implement analysis, discovery, record types/rules, and parsing. Airwars reconciliation/release policy stays in `connectors/airwars`; an unrelated library connector proves the boundary. The existing V4 collector remains operational and is integrated beside, not rewritten into, the new core.

The admin panel persists generic projects/runs/releases, queues execution through the existing Celery worker, and uses the same atomic publisher. Generated releases remain filesystem artifacts with manifests/checksums, not bulk Git content.

## Consequences

New connectors must provide fixtures and pass common recovery/release gates. Generic models allow site-specific fields through named `FieldValue` records with provenance instead of schema columns. Site-specific optimized release builders may extend the generic builder but cannot weaken core truthfulness or immutability gates.
