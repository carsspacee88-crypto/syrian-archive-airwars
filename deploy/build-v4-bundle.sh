#!/usr/bin/env bash
set -Eeuo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
output_dir=${1:-$project_dir/dist}
version=4.0.0
installer_revision=r1
bundle_name="syrian-archive-airwars-v${version}-${installer_revision}"
temporary_root=$(mktemp -d)
bundle_dir="$temporary_root/$bundle_name"

cleanup() {
  rm -rf -- "$temporary_root"
}
trap cleanup EXIT

command -v python3 >/dev/null || {
  printf 'ERROR: python3 is required to create the ZIP bundle.\n' >&2
  exit 1
}
command -v sha256sum >/dev/null || {
  printf 'ERROR: sha256sum is required.\n' >&2
  exit 1
}

install -d "$output_dir"
output_dir=$(cd "$output_dir" && pwd)
install -d "$bundle_dir/payload" "$bundle_dir/deploy"

# Copy only deployable source trees. Runtime state, secrets, Git metadata and
# previous release artifacts are excluded even when they exist in the checkout.
copy_tree() {
  source_name=$1
  test -d "$project_dir/$source_name"
  install -d "$bundle_dir/payload/$source_name"
  tar -C "$project_dir/$source_name" \
    --exclude='.git' \
    --exclude='.git/*' \
    --exclude='dist' \
    --exclude='dist/*' \
    --exclude='data' \
    --exclude='data/*' \
    --exclude='secrets' \
    --exclude='secrets/*' \
    --exclude='__pycache__' \
    --exclude='__pycache__/*' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='build-v2-bundle.sh' \
    --exclude='build-v3-bundle.sh' \
    --exclude='install-v2.sh' \
    --exclude='install-v3.sh' \
    --exclude='benchmark_v2_offline.py' \
    --exclude='benchmark_v3_offline.py' \
    --exclude='V2_*' \
    --exclude='V3_*' \
    -cf - . | tar -C "$bundle_dir/payload/$source_name" -xf -
}

for path in archive_pipeline control_plane scripts tests deploy docs; do
  copy_tree "$path"
done

for path in Dockerfile.vps compose.vps.yaml requirements.txt requirements-vps.txt .env.vps.example; do
  test -f "$project_dir/$path"
  cp -a "$project_dir/$path" "$bundle_dir/payload/$path"
done

cp -a "$project_dir/deploy/install-v4.sh" "$bundle_dir/deploy/install-v4.sh"
cp -a "$project_dir/docs/V4_UPGRADE_AR.md" "$bundle_dir/README_AR.md"
printf '%s\n' "$version" > "$bundle_dir/VERSION"

find "$bundle_dir" -type d -name __pycache__ -prune -exec rm -rf -- {} +
find "$bundle_dir" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

# Refuse accidental inclusion of private/runtime and older release material.
if find "$bundle_dir" \
  \( -name .git -o -name secrets -o -name dist -o -name '.env.vps' \) \
  -print -quit | grep -q .; then
  printf 'ERROR: forbidden runtime/private path entered the V4 bundle.\n' >&2
  exit 1
fi
if find "$bundle_dir/payload" -type f \
  \( -iname '*v2*' -o -iname '*v3*' \) -print -quit | grep -q .; then
  printf 'ERROR: a V2/V3 artifact entered the V4 payload.\n' >&2
  exit 1
fi

cd "$bundle_dir"
find payload deploy README_AR.md VERSION -type f -print0 \
  | LC_ALL=C sort -z \
  | xargs -0 sha256sum > SHA256SUMS
sha256sum -c SHA256SUMS >/dev/null

cd "$temporary_root"
rm -f -- "$output_dir/$bundle_name.zip" "$output_dir/$bundle_name.zip.sha256"
python3 -m zipfile -c "$output_dir/$bundle_name.zip" "$bundle_name"
python3 -m zipfile -t "$output_dir/$bundle_name.zip" >/dev/null
cd "$output_dir"
sha256sum "$bundle_name.zip" > "$bundle_name.zip.sha256"
printf '%s\n' "$output_dir/$bundle_name.zip"
