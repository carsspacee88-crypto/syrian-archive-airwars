# Syrian Archive — Airwars data

An independent, Arabic-first static archive of civilian-harm incidents related to Syria. It is not affiliated with Airwars. Attribution remains with Airwars and the original publishers, and some outbound links may lead to distressing content.

Published site: <https://carsspacee88-crypto.github.io/syrian-archive-airwars/>

## Current state

The preserved static snapshot contains 8,114 incident pages and 82 index pages. A resumable Python pipeline now collects directly from a public Airwars endpoint or live incident page, with the Internet Archive as a conservative fallback. The initial representative batch contains 11 normalized records; authoritative progress counts are in `data/reports/collection-summary.json`.

The Excel-derived package is no longer the active source of truth. It remains a migration snapshot, identifier seed, and last-resort fallback. Unverified migrated values are marked `legacy_import`.

No database or backend is required. No image, video, or audio binaries are downloaded or committed.

## Source hierarchy and provenance

1. Reliable public Airwars structured endpoint.
2. Live Airwars incident page.
3. Archived Airwars incident page.
4. Listed archived external sources.
5. Historical legacy import.

Important fields keep field-level provenance. Conflicting values are retained and routed to manual review. Stable IDs use the internal Airwars ID when available; duplicate public incident codes are deliberately preserved as separate records.

Normalized records live in `data/incidents/`. Request metadata and textual/JSON snapshots live in `data/raw/`, reports in `data/reports/`, and resumable checkpoints in `data/state/`. The schema is `data/schema/incident.schema.json`.

## Completeness model

- `complete`: a live, endpoint, or archived source was parsed and all currently required fields/sections are present.
- `partial`: useful data and a valid generated page exist, but a required field or section was not extracted. It does not mean the HTML page is broken.
- `blocked`, `unavailable`, and `failed`: describe the latest collection result.
- `pending_review` and `conflicting_sources`: require human review.

An HTTP 200 response alone never makes a record complete.

## Install and collect

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cat site-package/part-* > /tmp/syrian-archive-site.zip
echo "9f35a90d92f9fb0334cb61ffd8434aac03fd0ee61622908a11923dac40da35f3  /tmp/syrian-archive-site.zip" | sha256sum --check

python scripts/collect_incidents.py \
  --legacy-zip /tmp/syrian-archive-site.zip \
  --limit 25 \
  --delay 1.5
```

The collector writes a checkpoint after every incident. Completed records are skipped unless `--force` is supplied. Temporary failures use retries and exponential backoff. Partial and failed records remain eligible for future runs.

## Build and validate

```bash
python scripts/generate_reports.py --legacy-zip /tmp/syrian-archive-site.zip --output-root .
mkdir -p _site
unzip -q /tmp/syrian-archive-site.zip -d _site
python scripts/build_site.py --site-root _site --project-root .
python scripts/validate_site.py --site-root _site --project-root . --legacy-zip /tmp/syrian-archive-site.zip --report-root _site/data/reports
python scripts/write_checksums.py --site-root _site
python -m http.server 8000 --directory _site
```

Validation covers missing case pages/JSON, broken internal links, invalid pagination, duplicate stable IDs, duplicate public codes, invalid coordinates, required fields, URLs, provenance, empty source sections, impossible completeness labels, missing local media, and absolute paths that break at the GitHub Pages repository subpath.

## GitHub Pages

Set **Settings → Pages → Source** to **GitHub Actions**. The deployment workflow reconstructs and verifies the legacy package, generates current reports, overlays the direct-ingestion UI, validates all 8,114 cases and internal links, writes checksums, adds `.nojekyll`, and deploys the artifact with `index.html` at its root.

The historical 404 was caused by Pages not being enabled with the GitHub Actions source; it was not caused by a missing root index in the uploaded package.

## Complete-content pilot for the first 100 incidents

The pilot branch processes only `cases/0001` through `cases/0100`.
`archive_pipeline.pilot` enforces that boundary and raises an error for
sequence `0101` or later. It preserves `legacy_import` values, enriches them
from live or archived Airwars pages, creates stable source and media-metadata
records, preserves retrieved text in its original language without translation
or summarization, and never downloads or commits image, video, or audio binaries.

```bash
python scripts/run_first_100_pilot.py \
  --legacy-zip /tmp/syrian-archive-site.zip \
  --output-root .
```

Progress resumes from `data/pilot/first-100-progress.json`, with a checkpoint
after every incident and source. Machine translation is disabled at the user's
request, so no OpenAI or DeepL key is required. Detailed measurements are written to `data/reports/first-100-*`;
generated source pages live under `sources/{source_id}/index.html` in the site
artifact.

## Media policy and limitations

Only media URLs and metadata are retained during this phase. See [docs/MEDIA_PRESERVATION_PLAN_EN.md](docs/MEDIA_PRESERVATION_PLAN_EN.md) for the unexecuted future plan.

Airwars can return HTTP 403, Wayback captures can be absent or temporarily unavailable, and archived markup varies over time. The pipeline records these conditions without inventing missing facts or deleting legacy information.
