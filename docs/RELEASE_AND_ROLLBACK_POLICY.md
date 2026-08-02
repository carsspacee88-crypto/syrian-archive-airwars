# Release and rollback policy

## Build

Build into a new directory under `site-releases/releases/`; never reuse an active release directory. Preserve the prior release and `current` target. The builder checkpoints its phases, refuses a non-empty destination unless explicit resume is used, and never deletes prior artifacts.

## Gates and finalization

Before final checksums, validate canonical entity/page sets, every relationship and raw URL, truthfulness invariants, search/filter/map assets, UTF-8, layout safety, report equality, and zero broken internal links. Write `release.json`, validation/acceptance reports, logs, data, site, and `.immutable`; then write SHA-256 for every regular release file except the checksum manifest itself. A post-manifest validation record lives outside the immutable directory.

After the checksum manifest, no release file changes. Read-only mode is applied to files/directories.

## Publish

`AtomicPublisher` creates a temporary relative symlink and uses `os.replace` to switch `site-releases/current`. A failed health check restores the previous target. Caddy resolves both the new `site/` layout and the prior root layout.

## Rollback

Select a preserved release, validate its checksum/layout and `site/index.html`, atomically repoint `current`, then verify homepage, map/search, representative pages, reports, and admin. Rollback never deletes the failed or newer release; it remains available for forensic comparison.
