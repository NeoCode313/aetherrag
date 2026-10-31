#!/usr/bin/env bash
# Local evaluator deployment: no remote host and no external API required.
set -euo pipefail
cd -- "$(dirname -- "$0")"
ROOT="$(pwd -P)"
export AETHERRAG_ENV_FILE="$ROOT/.env"
PROJECT=aetherrag-evaluation
IMAGE=aetherrag-app:local
action=start
if [[ "$#" -gt 0 ]]; then action="$1"; fi
dc() { docker compose -p "$PROJECT" --env-file "$ROOT/.env" "$@"; }
prepare() {
  docker info >/dev/null
  docker build -t "$IMAGE" .
  if [[ ! -f .env ]]; then
    docker run --rm -it --network none --read-only --cap-drop ALL \
      --security-opt no-new-privileges:true --tmpfs /tmp:rw,size=64m \
      --user "$(id -u):$(id -g)" --mount "type=bind,source=$ROOT,target=/setup" \
      --workdir /setup --entrypoint python "$IMAGE" \
      /workspace/backend/scripts/setup_env.py --output /setup/.env
  fi
  dc config --quiet
}
case "$action" in
  prepare) prepare ;;
  start) prepare; dc up -d --no-build --wait --wait-timeout 240 ;;
  test)
    [[ -f .env ]] || { printf '%s\n' 'Run start first.' >&2; exit 1; }
    read -r -s -p 'Local application password: ' STACK_TEST_PASSWORD
    printf '\n'
    [[ -n "$STACK_TEST_PASSWORD" ]] || exit 1
    export STACK_TEST_PASSWORD
    trap 'unset STACK_TEST_PASSWORD' EXIT
    dc --profile test run --rm integration-tests
    ;;
  password)
    [[ -f .env ]] || { printf '%s\n' 'Run start first.' >&2; exit 1; }
    docker run --rm -it --network none --read-only --cap-drop ALL \
      --security-opt no-new-privileges:true --tmpfs /tmp:rw,size=64m \
      --user "$(id -u):$(id -g)" --mount "type=bind,source=$ROOT,target=/setup" \
      --workdir /setup --entrypoint python "$IMAGE" \
      /workspace/backend/scripts/change_operator_password.py --env-file /setup/.env
    dc up -d --no-deps --force-recreate --wait --wait-timeout 240 api worker
    ;;
  stop) dc stop ;;
  *) printf '%s\n' 'Usage: bash start-local.sh [start|prepare|test|password|stop]' >&2; exit 1 ;;
esac
