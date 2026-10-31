#!/usr/bin/env bash
# Update this application's containers from a clean private clone, preserving runtime data.
set -Eeuo pipefail
umask 077
[[ $# -ge 2 && $# -le 3 ]] || { echo "Usage: bash ops/update-server.sh CONFIG_DIR PROJECT [REVISION]" >&2; exit 2; }
SOURCE="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
CONFIG="$(cd -- "$1" && pwd -P)"
PROJECT="$2"
REVISION=origin/main
if [[ $# == 3 ]]; then REVISION="$3"; fi
[[ "$PROJECT" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { echo "Invalid Compose project" >&2; exit 2; }
[[ "$CONFIG" != "$SOURCE" && "$CONFIG" != "$SOURCE/"* ]] || { echo "Keep private configuration outside the clone" >&2; exit 2; }
[[ -f "$CONFIG/.env" && -f "$CONFIG/compose.server-test.yml" ]] || { echo "Private .env/server overlay missing" >&2; exit 2; }
[[ -d "$SOURCE/.git" && -z "$(git -C "$SOURCE" status --porcelain)" ]] || { echo "Source clone must have a clean worktree" >&2; exit 2; }
STATE="$CONFIG/.deploy-state"
mkdir -p "$STATE"
exec 9>"$STATE/lock"
flock -n 9 || { echo "Another update is running" >&2; exit 1; }
export AETHERRAG_ENV_FILE="$CONFIG/.env"
cd -- "$SOURCE"
OLD="$(git rev-parse HEAD)"
git fetch --prune origin main
NEW="$(git rev-parse --verify "$REVISION^{commit}")"
if ! git merge-base --is-ancestor "$NEW" origin/main; then
  echo "Revision must belong to origin/main" >&2; exit 2
fi
if ! git diff --quiet "$OLD" "$NEW" -- backend/app/infrastructure/catalog_migrations.py backend/app/infrastructure/catalog_schema.py; then
  echo "Schema changes need a separate migration/backup plan; automatic update stopped." >&2; exit 2
fi
PREVIOUS="$(docker ps --filter "label=com.docker.compose.project=$PROJECT" --filter label=com.docker.compose.service=api --format '{{.Image}}')"
[[ -n "$PREVIOUS" && "$PREVIOUS" != *$'\n'* ]] || { echo "Expected exactly one running API for this project" >&2; exit 2; }
IMAGE="aetherrag-app:git-$(git rev-parse --short=12 "$NEW")"
OVERLAY="$STATE/image.yml"
write_overlay() {
  printf 'services:\n' > "$OVERLAY"
  for service in api worker migrate integration-tests; do
    printf '  %s:\n    image: "%s"\n' "$service" "$1" >> "$OVERLAY"
  done
}
dc() {
  docker compose -p "$PROJECT" --project-directory "$SOURCE" --env-file "$CONFIG/.env" \
    -f "$SOURCE/docker-compose.yml" -f "$CONFIG/compose.server-test.yml" -f "$OVERLAY" "$@"
}
DEPLOYED=0
rollback() {
  result=$?
  trap - ERR
  git checkout --detach "$OLD" >/dev/null 2>&1 || true
  write_overlay "$PREVIOUS"
  if [[ "$DEPLOYED" == 1 ]]; then
    dc up -d --no-deps --force-recreate --wait --wait-timeout 240 api worker proxy || true
  fi
  echo "Update failed. Previous image/configuration restored where possible; inspect container health." >&2
  exit "$result"
}
trap rollback ERR
git checkout --detach "$NEW"
docker build -t "$IMAGE" .
write_overlay "$IMAGE"
dc config --quiet
DEPLOYED=1
dc up -d --no-deps --force-recreate --wait --wait-timeout 240 api worker proxy
dc exec -T api python -c "import urllib.request; b=urllib.request.urlopen('http://127.0.0.1:8000/docs').read(); assert b'api-docs-init.js' in b; urllib.request.urlopen('http://127.0.0.1:8000/static/api-docs-init.js'); print('API docs: local assets ready')"
printf '%s %s %s\n' "$(date -u +%FT%TZ)" "$NEW" "$IMAGE" >> "$STATE/history"
trap - ERR
echo "Deployed commit: $NEW"
echo "Private configuration and Docker volumes preserved."
