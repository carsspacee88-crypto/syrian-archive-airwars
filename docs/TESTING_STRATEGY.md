# Testing strategy

The suite combines unit, integration, end-to-end, release, and live-deployment checks.

Required failure fixtures cover HTTP 403 (and prove it is not success), 404, timeout, malformed HTML, missing required field, duplicate identifier, duplicate URL, one source across records, malformed URL, interrupted run/build, checkpoint resume, checksum mismatch, failed atomic deployment, rollback, interrupted JSON replacement, metadata-only, partial text, and validated full text.

The synthetic library connector uses catalogue entries, accession numbers, abstracts, and bibliography links. It shares no Airwars URL, selector, incident label, or field name. Its end-to-end test uses the same core to analyze/discover/fetch/parse, preserve raw responses and relationships, resume, build/search/validate, publish, and roll back.

Release tests compare canonical ID sets to generated page sets, stream every relationship export, inspect source truthfulness, parse every internal link, check search fields/filters, count every map point and exclusion, decode UTF-8, verify long-URL CSS, and validate every SHA-256 entry.

Run the repository suite inside the VPS image:

```bash
docker compose -f compose.vps.yaml run --rm --no-deps site-builder \
  python -m unittest discover -s tests -v
```
