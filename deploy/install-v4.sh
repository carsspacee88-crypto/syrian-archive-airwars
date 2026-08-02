#!/usr/bin/env bash
set -Eeuo pipefail

readonly target_version=4.0.0
project_dir=${ARCHIVE_PROJECT_DIR:-/root/projects/syrian-archive-airwars}
bundle_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$project_dir/compose.vps.yaml"
env_file="$project_dir/.env.vps"
backup_root=${ARCHIVE_BACKUP_ROOT:-/root/projects/_backups}
stage_root=${ARCHIVE_STAGE_ROOT:-/root/projects/_staged}
idle_timeout=${V4_IDLE_TIMEOUT_SECONDS:-86400}
idle_poll=${V4_IDLE_POLL_SECONDS:-5}
web_ready_timeout=${V4_WEB_READY_TIMEOUT_SECONDS:-240}
worker_ready_timeout=${V4_WORKER_READY_TIMEOUT_SECONDS:-180}
caddy_ready_timeout=${V4_CADDY_READY_TIMEOUT_SECONDS:-120}
readiness_poll=${V4_READINESS_POLL_SECONDS:-2}
mode=activate-now

usage() {
  cat <<'EOF'
Usage: install-v4.sh [--stage-only | --activate-when-idle]

  --stage-only          Verify and build V4 in an isolated staging directory.
                        Running/paused jobs and production containers are untouched.
  --activate-when-idle  Stage now, then wait without stopping any job. Activate only
                        after PostgreSQL confirms that no active/paused job remains.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --stage-only)
      [ "$mode" = activate-now ] || fail "استخدم خيار تشغيل واحدًا فقط"
      mode=stage-only
      ;;
    --activate-when-idle)
      [ "$mode" = activate-now ] || fail "استخدم خيار تشغيل واحدًا فقط"
      mode=activate-when-idle
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) fail "خيار غير معروف: $1" ;;
  esac
  shift
done

test "$(id -u)" -eq 0 || fail "شغّل المثبّت بحساب root"
case "$idle_timeout" in ''|*[!0-9]*) fail "V4_IDLE_TIMEOUT_SECONDS يجب أن يكون عددًا صحيحًا" ;; esac
case "$idle_poll" in ''|*[!0-9]*) fail "V4_IDLE_POLL_SECONDS يجب أن يكون عددًا صحيحًا" ;; esac
case "$web_ready_timeout" in ''|*[!0-9]*) fail "V4_WEB_READY_TIMEOUT_SECONDS يجب أن يكون عددًا صحيحًا" ;; esac
case "$worker_ready_timeout" in ''|*[!0-9]*) fail "V4_WORKER_READY_TIMEOUT_SECONDS يجب أن يكون عددًا صحيحًا" ;; esac
case "$caddy_ready_timeout" in ''|*[!0-9]*) fail "V4_CADDY_READY_TIMEOUT_SECONDS يجب أن يكون عددًا صحيحًا" ;; esac
case "$readiness_poll" in ''|*[!0-9]*) fail "V4_READINESS_POLL_SECONDS يجب أن يكون عددًا صحيحًا" ;; esac
[ "$idle_poll" -ge 1 ] || fail "V4_IDLE_POLL_SECONDS يجب ألا يقل عن 1"
[ "$web_ready_timeout" -ge 10 ] || fail "V4_WEB_READY_TIMEOUT_SECONDS يجب ألا يقل عن 10"
[ "$worker_ready_timeout" -ge 10 ] || fail "V4_WORKER_READY_TIMEOUT_SECONDS يجب ألا يقل عن 10"
[ "$caddy_ready_timeout" -ge 10 ] || fail "V4_CADDY_READY_TIMEOUT_SECONDS يجب ألا يقل عن 10"
[ "$readiness_poll" -ge 1 ] || fail "V4_READINESS_POLL_SECONDS يجب ألا يقل عن 1"
for command_name in docker sha256sum sort tar curl awk sed stat df du; do
  command -v "$command_name" >/dev/null || fail "$command_name غير متوفر"
done
test -d "$project_dir" || fail "لم يوجد المشروع في $project_dir"
test -f "$compose_file" || fail "compose.vps.yaml غير موجود"
test -f "$env_file" || fail ".env.vps غير موجود"
test -f "$bundle_dir/VERSION" || fail "VERSION غير موجود في الحزمة"
test -f "$bundle_dir/SHA256SUMS" || fail "SHA256SUMS غير موجود في الحزمة"
test -d "$bundle_dir/payload/archive_pipeline" || fail "حزمة payload غير مكتملة"

bundle_version=$(tr -d '[:space:]' < "$bundle_dir/VERSION")
[ "$bundle_version" = "$target_version" ] || fail "هذه ليست حزمة V4.0.0"

version_gt() {
  first=$1
  second=$2
  [ "$first" != "$second" ] && [ "$(printf '%s\n%s\n' "$first" "$second" | sort -V | tail -n 1)" = "$first" ]
}

installed_versions=()
for marker in .archive-version .v4-version .v3-version .v2-version; do
  if [ -f "$project_dir/$marker" ]; then
    value=$(tr -d '[:space:]' < "$project_dir/$marker")
    case "$value" in
      [0-9]*.[0-9]*.[0-9]*) installed_versions+=("$value") ;;
    esac
  fi
done
if [ -f "$project_dir/archive_pipeline/speed_pilot.py" ]; then
  source_version=$(sed -n 's/^ENGINE_VERSION = "\([0-9][0-9.]*\)"/\1/p' \
    "$project_dir/archive_pipeline/speed_pilot.py" | head -n 1)
  [ -z "$source_version" ] || installed_versions+=("$source_version")
fi
if [ "${#installed_versions[@]}" -gt 0 ]; then
  current_version=$(printf '%s\n' "${installed_versions[@]}" | sort -V | tail -n 1)
  if version_gt "$current_version" "$bundle_version"; then
    fail "رفض downgrade: النسخة المثبتة $current_version أحدث من الحزمة $bundle_version"
  fi
else
  current_version=unknown
fi

cd "$bundle_dir"
if awk '
  {
    path=$2
    sub(/^\*/, "", path)
    if (path ~ /^\// || path ~ /(^|\/)\.\.($|\/)/ || path == "SHA256SUMS") exit 1
  }
' SHA256SUMS; then
  :
else
  fail "SHA256SUMS يحتوي مسارًا غير آمن"
fi
sha256sum -c SHA256SUMS >/dev/null || fail "فشل التحقق من سلامة ملفات الحزمة"

manifest_list=$(mktemp)
actual_list=$(mktemp)
stage_temp=
cleanup() {
  rm -f -- "$manifest_list" "$actual_list"
  if [ -n "$stage_temp" ] && [ -d "$stage_temp" ]; then
    rm -rf -- "$stage_temp"
  fi
}
trap cleanup EXIT
awk '{path=$2; sub(/^\*/, "", path); print path}' SHA256SUMS | LC_ALL=C sort > "$manifest_list"
find payload deploy README_AR.md VERSION -type f -print | LC_ALL=C sort > "$actual_list"
cmp -s "$manifest_list" "$actual_list" || fail "قائمة ملفات الحزمة لا تطابق SHA256SUMS"

verify_secret() {
  secret_name=$1
  secret_path="$project_dir/secrets/$secret_name"
  test -s "$secret_path" || fail "السر $secret_name مفقود أو فارغ"
  mode_value=$(stat -c '%a' "$secret_path")
  if (( (8#$mode_value & 077) != 0 )); then
    fail "صلاحيات $secret_name تسمح للمجموعة/الآخرين بالقراءة"
  fi
}

verify_secret postgres_password
verify_secret session_secret
verify_secret admin_password_hash
[ "$(wc -c < "$project_dir/secrets/session_secret")" -ge 33 ] || fail "session_secret أقصر من 32 محرفًا"
grep -q '^pending$' "$project_dir/secrets/admin_password_hash" && fail "admin_password_hash ما زال pending"

payload_bytes=$(du -sb "$bundle_dir/payload" | awk '{print $1}')
free_bytes=$(df -PB1 "$project_dir" | awk 'NR==2 {print $4}')
minimum_bytes=$((1024 * 1024 * 1024))
scaled_bytes=$((payload_bytes * 6 + 256 * 1024 * 1024))
required_bytes=$minimum_bytes
[ "$scaled_bytes" -le "$required_bytes" ] || required_bytes=$scaled_bytes
[ "$free_bytes" -ge "$required_bytes" ] || fail "المساحة الحرة غير كافية للتجهيز والنسخة الاحتياطية"

cd "$project_dir"
docker compose --env-file "$env_file" -f "$compose_file" config -q

container_state() {
  service_name=$1
  container_id=$(docker compose --env-file "$env_file" -f "$compose_file" ps -q "$service_name")
  [ -n "$container_id" ] || return 1
  docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    "$container_id" 2>/dev/null
}

verify_running_services() {
  postgres_state=$(container_state postgres) || fail "PostgreSQL غير مشغّل"
  redis_state=$(container_state redis) || fail "Redis غير مشغّل"
  web_state=$(container_state web) || fail "خدمة web غير مشغّلة"
  worker_state=$(container_state worker) || fail "خدمة worker غير مشغّلة"
  caddy_state=$(container_state caddy) || fail "خدمة caddy غير مشغّلة"
  [ "$postgres_state" = healthy ] || fail "PostgreSQL ليس healthy: $postgres_state"
  [ "$redis_state" = healthy ] || fail "Redis ليس healthy: $redis_state"
  [ "$web_state" = healthy ] || fail "web ليس healthy: $web_state"
  [ "$worker_state" = running ] || fail "worker ليس running: $worker_state"
  [ "$caddy_state" = running ] || [ "$caddy_state" = healthy ] || fail "caddy ليس running: $caddy_state"
}

verify_running_services
database_name=$(sed -n 's/^ARCHIVE_DATABASE_NAME=//p' "$env_file" | tail -n 1)
database_user=$(sed -n 's/^ARCHIVE_DATABASE_USER=//p' "$env_file" | tail -n 1)
database_name=${database_name:-archive_admin}
database_user=${database_user:-archive}

active_job_count() {
  result=$(docker compose --env-file "$env_file" -f "$compose_file" exec -T postgres \
    psql -v ON_ERROR_STOP=1 -U "$database_user" -d "$database_name" -tAc \
    "select count(*) from collection_jobs where status in ('queued','running','pause_requested','paused','cancel_requested');" \
    2>/dev/null | tr -d '[:space:]') || return 1
  case "$result" in
    ''|*[!0-9]*) return 1 ;;
  esac
  printf '%s\n' "$result"
}

initial_active=$(active_job_count) || fail "تعذر التحقق من المهام النشطة في PostgreSQL"

install -d -m 700 "$stage_root"
bundle_fingerprint=$(sha256sum "$bundle_dir/SHA256SUMS" | awk '{print substr($1,1,16)}')
stage_dir="$stage_root/syrian-archive-airwars-v${target_version}-$bundle_fingerprint"
if [ ! -d "$stage_dir" ]; then
  stage_temp=$(mktemp -d "$stage_root/.v4-stage-XXXXXXXX")
  cp -a "$bundle_dir/." "$stage_temp/"
  printf '%s\n' "$bundle_fingerprint" > "$stage_temp/.bundle-fingerprint"
  mv "$stage_temp" "$stage_dir"
  stage_temp=
fi
test "$(cat "$stage_dir/.bundle-fingerprint")" = "$bundle_fingerprint" || fail "بصمة staging لا تطابق الحزمة"
cd "$stage_dir"
sha256sum -c SHA256SUMS >/dev/null || fail "فشل تحقق نسخة staging"

stage_image="syrian-archive-airwars-v4-stage:$bundle_fingerprint"
docker build -q -f "$stage_dir/payload/Dockerfile.vps" -t "$stage_image" "$stage_dir/payload" >/dev/null
docker image inspect "$stage_image" >/dev/null
staged_engine=$(docker run --rm "$stage_image" python -c \
  'from archive_pipeline.speed_pilot import ENGINE_VERSION; print(ENGINE_VERSION)')
[ "$staged_engine" = "$target_version" ] || fail "صورة staging لا تحتوي محرك V4.0.0"
docker run --rm \
  -v "$stage_dir/payload/tests:/srv/archive/tests:ro" \
  "$stage_image" \
  python -m unittest discover -s /srv/archive/tests -p 'test_*.py' >/dev/null

if [ "$mode" = stage-only ]; then
  printf '\nV4_STAGE_OK\n'
  printf 'Stage: %s\n' "$stage_dir"
  printf 'Active jobs observed (untouched): %s\n' "$initial_active"
  printf 'No production file or container was changed.\n'
  exit 0
fi

if [ "$mode" = activate-when-idle ]; then
  wait_started=$(date +%s)
  while :; do
    active_jobs=$(active_job_count) || fail "تعذر التحقق من المهام النشطة أثناء الانتظار"
    [ "$active_jobs" -ne 0 ] || break
    elapsed=$(( $(date +%s) - wait_started ))
    [ "$elapsed" -lt "$idle_timeout" ] || fail "انتهت مهلة انتظار خلو المهام بعد ${idle_timeout}ث؛ لم تتغير production"
    printf 'V4_WAITING_FOR_IDLE active_jobs=%s elapsed_seconds=%s\n' "$active_jobs" "$elapsed"
    sleep "$idle_poll"
  done
else
  active_jobs=$initial_active
  if [ "$active_jobs" -ne 0 ]; then
    fail "يوجد $active_jobs مهمة نشطة أو متوقفة. استخدم --stage-only أو --activate-when-idle؛ لم تتغير production."
  fi
fi

# Recheck at the activation boundary before creating a backup or stopping a service.
active_jobs=$(active_job_count) || fail "تعذر التحقق النهائي من المهام النشطة"
[ "$active_jobs" -eq 0 ] || fail "بدأت مهمة جديدة قبل التفعيل؛ لم تتغير production"

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="$backup_root/syrian-archive-airwars-${current_version}-before-v4-$timestamp"
install -d -m 700 "$backup_dir"
backup_paths=()
for path in \
  archive_pipeline control_plane scripts tests deploy docs \
  Dockerfile.vps compose.vps.yaml requirements.txt requirements-vps.txt \
  .env.vps .env.vps.example .archive-version .v4-version .v3-version .v2-version; do
  [ ! -e "$project_dir/$path" ] || backup_paths+=("$path")
done

restore_pre_activation_services() {
  docker compose --env-file "$env_file" -f "$compose_file" \
    start web worker caddy >/dev/null || true
}

# Close the job-creation entry point before touching the worker. If a job won
# the race immediately before this boundary, the worker remains alive and the
# installer restores the web entry point without interrupting that job.
if ! docker compose --env-file "$env_file" -f "$compose_file" stop caddy web >/dev/null; then
  restore_pre_activation_services
  fail "تعذر إغلاق مدخل الإدارة بالكامل؛ أعيدت الخدمات ولم يتغير الكود"
fi
if ! active_after_web_stop=$(active_job_count); then
  restore_pre_activation_services
  fail "تعذر التحقق من الخمول بعد إغلاق مدخل الإدارة"
fi
if [ "$active_after_web_stop" -ne 0 ]; then
  restore_pre_activation_services
  fail "بدأت مهمة عند حد التفعيل؛ أعيد مدخل الإدارة ولم تُقطع المهمة"
fi

# With the web entry point closed and the database still idle, stopping the
# worker can no longer interrupt a task created through the control plane.
if ! docker compose --env-file "$env_file" -f "$compose_file" stop worker >/dev/null; then
  restore_pre_activation_services
  fail "تعذر إيقاف worker بأمان؛ أعيدت الخدمات ولم يتغير الكود"
fi
if ! active_after_worker_stop=$(active_job_count); then
  restore_pre_activation_services
  fail "تعذر التحقق من الخمول بعد إيقاف worker"
fi
if [ "$active_after_worker_stop" -ne 0 ]; then
  restore_pre_activation_services
  fail "ظهرت مهمة خارجية عند حد التفعيل؛ أعيدت الخدمات ولم يتغير الكود"
fi

# Take the final backup only after all writers are quiescent. Every failure in
# this pre-mutation section explicitly restores the production services.
cd "$project_dir"
if ! tar -czf "$backup_dir/code-and-config.tar.gz" "${backup_paths[@]}"; then
  restore_pre_activation_services
  fail "فشل إنشاء نسخة الكود الاحتياطية؛ أعيدت الخدمات"
fi
if ! docker compose --env-file "$env_file" -f "$compose_file" exec -T postgres \
  pg_dump -U "$database_user" -d "$database_name" \
  > "$backup_dir/postgres.sql"; then
  restore_pre_activation_services
  fail "فشل إنشاء نسخة PostgreSQL الاحتياطية؛ أعيدت الخدمات"
fi
if [ ! -s "$backup_dir/code-and-config.tar.gz" ] || [ ! -s "$backup_dir/postgres.sql" ]; then
  restore_pre_activation_services
  fail "النسخة الاحتياطية غير مكتملة؛ أعيدت الخدمات"
fi

replace_directories=(archive_pipeline control_plane scripts tests deploy docs)
replace_files=(Dockerfile.vps compose.vps.yaml requirements.txt requirements-vps.txt .env.vps.example)

capture_activation_diagnostics() {
  diagnostics_dir="$backup_dir/v4-failure-diagnostics"
  if ! install -d -m 700 "$diagnostics_dir"; then
    printf 'WARNING: تعذر إنشاء مجلد سجلات فشل V4؛ ستستمر الاستعادة.\n' >&2
    return 0
  fi
  {
    printf 'captured_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'target_version=%s\n' "$target_version"
    printf 'current_version_before_activation=%s\n' "$current_version"
    docker compose --env-file "$env_file" -f "$compose_file" ps -a
  } > "$diagnostics_dir/compose-state.txt" 2>&1 || true
  for service_name in web worker caddy; do
    docker compose --env-file "$env_file" -f "$compose_file" \
      logs --no-color --timestamps --tail 300 "$service_name" \
      > "$diagnostics_dir/${service_name}.log" 2>&1 || true
    service_id=$(docker compose --env-file "$env_file" -f "$compose_file" ps -q "$service_name" 2>/dev/null || true)
    [ -z "$service_id" ] || docker inspect "$service_id" \
      > "$diagnostics_dir/${service_name}-inspect.json" 2>&1 || true
  done
}

activation_check_failed() {
  printf 'V4_ACTIVATION_CHECK_FAILED check=%s detail=%s\n' "$1" "$2" >&2
  return 1
}

rollback() {
  trap - ERR
  capture_activation_diagnostics
  printf 'فشل تفعيل V4؛ جارٍ إرجاع الكود السابق من %s\n' "$backup_dir" >&2
  cd "$project_dir"
  for path in "${replace_directories[@]}"; do
    rm -rf -- "$project_dir/$path"
  done
  for path in "${replace_files[@]}" .env.vps .archive-version .v4-version .v3-version .v2-version; do
    rm -f -- "$project_dir/$path"
  done
  tar -xzf "$backup_dir/code-and-config.tar.gz" -C "$project_dir"
  docker compose --env-file .env.vps -f compose.vps.yaml build web worker >/dev/null || true
  docker compose --env-file .env.vps -f compose.vps.yaml up -d --no-deps --force-recreate web worker caddy >/dev/null || true
  printf 'تمت محاولة الاستعادة. النسخة الاحتياطية: %s\n' "$backup_dir" >&2
  printf 'سجلات فشل V4: %s\n' "$backup_dir/v4-failure-diagnostics" >&2
  exit 1
}
trap rollback ERR

cd "$project_dir"
for path in "${replace_directories[@]}"; do
  rm -rf -- "$project_dir/$path"
  cp -a "$stage_dir/payload/$path" "$project_dir/$path"
done
for path in "${replace_files[@]}"; do
  cp -a "$stage_dir/payload/$path" "$project_dir/$path"
done

set_env() {
  key=$1
  value=$2
  if grep -q "^${key}=" "$env_file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$env_file"
  fi
}

# Balanced V4 defaults. Social/archive hosts use explicit fair-rate policies;
# the base delay remains a conservative fallback for unknown general sites.
set_env ARCHIVE_COLLECTOR_WORKERS 64
set_env ARCHIVE_COLLECTOR_PER_HOST_WORKERS 4
set_env ARCHIVE_COLLECTOR_SOCIAL_WORKERS 12
set_env ARCHIVE_COLLECTOR_ARCHIVE_WORKERS 12
set_env ARCHIVE_COLLECTOR_CHECKPOINT_EVERY 5000
set_env ARCHIVE_COLLECTOR_DELAY 0.05
set_env ARCHIVE_COLLECTOR_TIMEOUT 6
set_env ARCHIVE_COLLECTOR_FAST_TIMEOUT 3
set_env ARCHIVE_COLLECTOR_RETRIES 1
set_env ARCHIVE_INCIDENT_MODE snapshot_first
set_env ARCHIVE_INLINE_WAYBACK false
set_env ARCHIVE_INCIDENT_CHUNK_SIZE 250
set_env ARCHIVE_SOURCE_CHUNK_SIZE 5000
set_env ARCHIVE_LIVE_UPDATE_INTERVAL 0.5

chmod 600 "$env_file"
install -d -o 10001 -g 10001 "$project_dir/data"
docker compose --env-file "$env_file" -f "$compose_file" config -q
# Existing databases do not always receive indexes newly declared in ORM
# metadata. Create the two dashboard indexes explicitly and idempotently.
docker compose --env-file "$env_file" -f "$compose_file" exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U "$database_user" -d "$database_name" <<'SQL'
CREATE INDEX IF NOT EXISTS ix_collection_items_job_kind_updated
  ON collection_items (job_id, kind, updated_at);
CREATE INDEX IF NOT EXISTS ix_collection_items_job_kind_status
  ON collection_items (job_id, kind, status);
SQL
docker compose --env-file "$env_file" -f "$compose_file" build web worker
docker compose --env-file "$env_file" -f "$compose_file" up -d --no-deps --force-recreate web worker

printf 'V4_ACTIVATION_CHECK_START check=web_health timeout_seconds=%s\n' "$web_ready_timeout"
web_ready=0
check_started=$(date +%s)
attempt=0
while :; do
  attempt=$((attempt + 1))
  web_id=$(docker compose --env-file "$env_file" -f "$compose_file" ps -q web)
  [ -n "$web_id" ] || activation_check_failed web_health "web container is missing"
  state=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$web_id" 2>/dev/null || true)
  if [ "$state" = healthy ]; then
    web_ready=1
    break
  fi
  container_status=$(docker inspect --format='{{.State.Status}}' "$web_id" 2>/dev/null || true)
  case "$container_status" in
    exited|dead|removing)
      activation_check_failed web_health "container_status=$container_status health_status=$state"
      ;;
  esac
  elapsed=$(( $(date +%s) - check_started ))
  [ "$elapsed" -lt "$web_ready_timeout" ] \
    || activation_check_failed web_health "timeout after ${elapsed}s; health_status=$state"
  if [ "$attempt" -eq 1 ] || [ $((attempt % 5)) -eq 0 ]; then
    printf 'V4_ACTIVATION_CHECK_WAIT check=web_health elapsed_seconds=%s state=%s\n' "$elapsed" "${state:-unknown}"
  fi
  sleep "$readiness_poll"
done
[ "$web_ready" -eq 1 ] || activation_check_failed web_health "unexpected readiness state"
printf 'V4_ACTIVATION_CHECK_OK check=web_health\n'

printf 'V4_ACTIVATION_CHECK_START check=engine_version\n'
if ! running_engine=$(docker compose --env-file "$env_file" -f "$compose_file" exec -T web \
  python -c 'from archive_pipeline.speed_pilot import ENGINE_VERSION; print(ENGINE_VERSION)' 2>&1); then
  activation_check_failed engine_version "command failed: $running_engine"
fi
[ "$running_engine" = "$target_version" ] \
  || activation_check_failed engine_version "expected=$target_version actual=$running_engine"
printf 'V4_ACTIVATION_CHECK_OK check=engine_version version=%s\n' "$running_engine"

# A Compose "Started" state only means that the worker process was launched.
# Celery remote control can still need several seconds before the node begins
# consuming control messages. Ping this exact worker node and retry instead of
# interpreting one early timeout as a broken V4 deployment.
printf 'V4_ACTIVATION_CHECK_START check=worker_ping timeout_seconds=%s\n' "$worker_ready_timeout"
worker_ready=0
worker_ping_output=
check_started=$(date +%s)
attempt=0
while :; do
  attempt=$((attempt + 1))
  if worker_ping_output=$(docker compose --env-file "$env_file" -f "$compose_file" exec -T worker \
    python -c 'import os, sys; from control_plane.tasks import celery_app; node="celery@" + os.uname().nodename; replies=celery_app.control.ping(destination=[node], timeout=5); print(replies); sys.exit(0 if replies else 1)' \
    2>&1); then
    worker_ready=1
    break
  fi
  worker_id=$(docker compose --env-file "$env_file" -f "$compose_file" ps -q worker)
  [ -n "$worker_id" ] || activation_check_failed worker_ping "worker container is missing"
  worker_state=$(docker inspect --format='{{.State.Status}}' "$worker_id" 2>/dev/null || true)
  case "$worker_state" in
    exited|dead|removing)
      activation_check_failed worker_ping "container_status=$worker_state output=$worker_ping_output"
      ;;
  esac
  elapsed=$(( $(date +%s) - check_started ))
  [ "$elapsed" -lt "$worker_ready_timeout" ] \
    || activation_check_failed worker_ping "timeout after ${elapsed}s; container_status=$worker_state; output=$worker_ping_output"
  if [ "$attempt" -eq 1 ] || [ $((attempt % 3)) -eq 0 ]; then
    printf 'V4_ACTIVATION_CHECK_WAIT check=worker_ping elapsed_seconds=%s state=%s\n' "$elapsed" "${worker_state:-unknown}"
  fi
  sleep "$readiness_poll"
done
[ "$worker_ready" -eq 1 ] || activation_check_failed worker_ping "unexpected readiness state"
printf 'V4_ACTIVATION_CHECK_OK check=worker_ping response=%s\n' "$worker_ping_output"

docker compose --env-file "$env_file" -f "$compose_file" up -d --no-deps --force-recreate caddy
archive_domain=$(sed -n 's/^ARCHIVE_DOMAIN=//p' "$env_file" | tail -n 1)
archive_host=${archive_domain#http://}
archive_host=${archive_host#https://}
archive_host=${archive_host%%/*}
[ -n "$archive_host" ] || activation_check_failed caddy_health "ARCHIVE_DOMAIN is empty"
printf 'V4_ACTIVATION_CHECK_START check=caddy_health timeout_seconds=%s host=%s\n' "$caddy_ready_timeout" "$archive_host"
caddy_ready=0
check_started=$(date +%s)
attempt=0
while :; do
  attempt=$((attempt + 1))
  if curl -fsS -H "Host: $archive_host" http://127.0.0.1/health 2>/dev/null \
    | grep -q '"status":"ok"'; then
    caddy_ready=1
    break
  fi
  caddy_id=$(docker compose --env-file "$env_file" -f "$compose_file" ps -q caddy)
  [ -n "$caddy_id" ] || activation_check_failed caddy_health "caddy container is missing"
  caddy_state=$(docker inspect --format='{{.State.Status}}' "$caddy_id" 2>/dev/null || true)
  case "$caddy_state" in
    exited|dead|removing)
      activation_check_failed caddy_health "container_status=$caddy_state"
      ;;
  esac
  elapsed=$(( $(date +%s) - check_started ))
  [ "$elapsed" -lt "$caddy_ready_timeout" ] \
    || activation_check_failed caddy_health "timeout after ${elapsed}s; container_status=$caddy_state"
  if [ "$attempt" -eq 1 ] || [ $((attempt % 5)) -eq 0 ]; then
    printf 'V4_ACTIVATION_CHECK_WAIT check=caddy_health elapsed_seconds=%s state=%s\n' "$elapsed" "${caddy_state:-unknown}"
  fi
  sleep "$readiness_poll"
done
[ "$caddy_ready" -eq 1 ] || activation_check_failed caddy_health "unexpected readiness state"
printf 'V4_ACTIVATION_CHECK_OK check=caddy_health\n'

verify_secret postgres_password
verify_secret session_secret
verify_secret admin_password_hash
printf '%s\n' "$target_version" > "$project_dir/.archive-version"
printf '%s\n' "$target_version" > "$project_dir/.v4-version"
chmod 644 "$project_dir/.archive-version" "$project_dir/.v4-version"
trap - ERR

printf '\nV4_INSTALL_OK\n'
printf 'Admin: http://%s/admin\n' "$archive_host"
printf 'Backup: %s\n' "$backup_dir"
printf 'Stage: %s\n' "$stage_dir"
printf 'Data, secrets, PostgreSQL, Redis and the legacy cache were preserved.\n'
