# Operations runbook

## Inputs and backup

Verify the restored historical ZIP SHA-256 is `9f35a90d92f9fb0334cb61ffd8434aac03fd0ee61622908a11923dac40da35f3`. Keep `site-package/` split parts, `data/`, PostgreSQL/Redis/Caddy volumes, existing releases, Git history, and timestamped code backup. Do not clean generated data during release work.

## Audit

Run `scripts/audit_airwars_ground_truth.py`, inspect every reconciliation equation, and stop publication on an unbalanced equation or unexplained count. Review known-shell reclassifications before calling any source full text.

## Build and validate

Use `scripts/build_airwars_textual_release.py` with project root, restored legacy ZIP, and `site-releases`. The script supports explicit resume and intentional interruption tests. Its final validation record is written under `site-releases/operations/`; generated release data stays out of Git and is fully described by the release manifest/checksums.

## Deploy

Rebuild web/worker, initialize additive database tables, validate Caddy, atomically switch the release pointer, reload Caddy, then check `/`, `/map.html`, `/search.html`, `/reports/`, incident/reference/source category samples, `/health`, and `/admin`.

## Rollback

Use the recorded previous release path with `AtomicPublisher.rollback`, verify the same endpoints, and retain both releases. Never remove a release, cache, source record, Docker volume, or backup as part of rollback.

## Git

Commit code/tests/config/docs first. Commit small canonical manifest/audit reports separately. Do not stage `data/`, `exports/`, `site-releases/`, `data/archive-engine/`, secrets, or the historical cache artifact. Record commit hashes and push status in the operation report.
