# Airwars Syria textual-skeleton specification

## Scope

`airwars-syria-v0-structured-text` is the complete textual and relational skeleton for the Airwars Syria material recoverable in this project. It is not a media-completeness release. Its canonical units are:

- one incident entity per stable internal incident identifier;
- one source-reference entity per incident-to-source occurrence, including repeated relationships;
- one external-source entity per stable local source identifier;
- every raw URL as evidence, plus a normalized alternative when normalization succeeds.

The release is structurally complete when no incident, reference relationship, or raw link is missing. An external source may remain blocked, dead, malformed, metadata-only, link-only, or partial without making the structural skeleton incomplete.

## Incident contract

Each incident preserves its internal and Airwars identifiers, original URL, dates, geography and coordinate status, narrative/structured description, classifications and actor, casualty bounds, named people, notes, conflicts, source-reference IDs, important field provenance, current direct-verification status, record origin, and data-quality status. Missing values stay missing and retain a reason; they are never inferred.

## Source-reference contract

Each occurrence preserves a stable ID, incident ID, raw and normalized URL, normalization result and reason, labels and citation metadata, domain/type/date, reachability, content-preservation status, provenance, duplicate flag, malformed flag, review flag, archive URLs, and matched external-source ID when one exists.

## Truthfulness invariants

- HTTP 403 is `BLOCKED_HTTP_403`, never direct success.
- A local reference/entity page is not proof that external content was archived.
- `FULL_TEXT_*` requires non-empty locally preserved main text and a successful independent completeness check.
- Login, deletion, access, player, home-page, and error shells are partial or non-text states, never full text.
- Direct verification and historical/local record origin are separate dimensions.

## Required release layout

The immutable directory contains `release.json`, `checksums/sha256.txt`, `reports/`, `logs/`, `data/`, `exports/`, and `site/`. The site uses only release-local data and application assets for incident/source/search/map behavior; no live Airwars, crawler, admin, or worker dependency exists.

## Navigation and discovery

The site provides incident → source reference → external-source entity → preserved text navigation. Search covers incident ID/code/date/location/narrative/victim, source domain/title/raw URL/status, and actor/allegation. The one-screen local canvas map renders every drawable coordinate and exposes every exclusion category.
