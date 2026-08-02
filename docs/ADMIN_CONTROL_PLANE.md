# Admin control plane

The authenticated `/admin` interface exposes both the preserved V4 collection jobs and the general engine.

## Ground-truth dashboard

Three sections read the active immutable release report: incident coverage, source-reference coverage, and source-content preservation. Every counter links to its stable IDs or URL evidence. HTTP 403, historical origin, coordinate exclusions, metadata-only, partial, blocked, dead, direct-full, archived-full, local-full, and manual-review states are separate.

## General workflow

1. Create a project with target URL, connector, scope, allowed domains, limits, rate policy, text-only mode, and release name.
2. Analyze the target to view candidate record types, sample pages, detected fields, and source-link patterns.
3. Run a configurable pilot and inspect records, failures, provenance, counts, and checkpoint.
4. Start a full immutable worklist; pause, resume, cancel safely, or retry from the same checkpoint.
5. Build and validate a release. Blocking failures prevent publication; non-blocking notes remain visible.
6. Publish with an atomic `current` symlink switch, view the parent, or roll back to a preserved validated release.

Celery provides the queue and worker boundary. PostgreSQL stores control-plane state; filesystem artifacts and audit logs remain under `data/archive-engine/`. The control plane is not required to serve an already published static release.
