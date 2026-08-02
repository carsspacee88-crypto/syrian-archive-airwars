#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$project_dir/compose.vps.yaml"
env_file="$project_dir/.env.vps"
releases_root="$project_dir/site-releases"
release_id="$(date -u +%Y%m%dT%H%M%SZ)-$(git -C "$project_dir" rev-parse --short HEAD 2>/dev/null || printf local)"
release_dir="$releases_root/releases/$release_id"

command -v docker >/dev/null || { printf 'docker غير متوفر\n' >&2; exit 1; }
[ -f "$env_file" ] || { printf 'ملف %s غير موجود\n' "$env_file" >&2; exit 1; }
[ ! -e "$release_dir" ] || { printf 'الإصدار موجود مسبقًا: %s\n' "$release_dir" >&2; exit 1; }

install -d -m 755 "$releases_root/releases" "$release_dir"

printf 'SITE_BUILD_START release=%s\n' "$release_id"
docker compose --env-file "$env_file" -f "$compose_file" build site-builder
docker compose --env-file "$env_file" -f "$compose_file" run --rm --no-deps \
  -e "SITE_RELEASE_DIR=/srv/site-releases/releases/$release_id" \
  site-builder sh -ceu '
    python scripts/generate_reports.py \
      --modern-only \
      --output-root /srv/archive
    python scripts/build_modern_site.py \
      --site-root "$SITE_RELEASE_DIR" \
      --project-root /srv/archive
    python scripts/validate_site.py \
      --site-root "$SITE_RELEASE_DIR" \
      --project-root /srv/archive \
      --modern-only \
      --report-root "$SITE_RELEASE_DIR/data/reports"
    python scripts/write_checksums.py --site-root "$SITE_RELEASE_DIR"
  '

test -f "$release_dir/index.html"
test -f "$release_dir/checksums.sha256"
ln -s "releases/$release_id" "$releases_root/.current-$release_id"
mv -Tf "$releases_root/.current-$release_id" "$releases_root/current"

docker compose --env-file "$env_file" -f "$compose_file" config -q
docker compose --env-file "$env_file" -f "$compose_file" up -d --no-deps --force-recreate caddy

archive_domain=$(sed -n 's/^ARCHIVE_DOMAIN=//p' "$env_file" | tail -n 1)
archive_host=${archive_domain#http://}
archive_host=${archive_host#https://}
archive_host=${archive_host%%/*}
curl -fsS --max-time 10 -H "Host: $archive_host" http://127.0.0.1/health >/dev/null
curl -fsS --max-time 10 -H "Host: $archive_host" http://127.0.0.1/ >/dev/null

printf 'SITE_PUBLISH_OK release=%s url=%s/ admin=%s/admin\n' \
  "$release_id" "${archive_domain%/}" "${archive_domain%/}"
