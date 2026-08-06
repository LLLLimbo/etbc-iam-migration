#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
IAM_WORKSPACE=$(cd "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.integration.yml"
COMPOSE_PROJECT=iam-python-tenant-migration-it
compose=(docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE")

cleanup() {
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

random_secret() {
  docker run --rm --entrypoint sh docker.1ms.run/alpine:3.20 -ec \
    "tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40"
}

export MYSQL_ROOT_PASSWORD
export ETBC_PASSWORD
MYSQL_ROOT_PASSWORD=$(random_secret)
ETBC_PASSWORD=$(random_secret)

cleanup
SKIP_TESTS=1 bash "$IAM_WORKSPACE/.agents/skills/how-to-build-iam-management-service/scripts/build-iam-management-service.sh"
SKIP_TESTS=1 bash "$IAM_WORKSPACE/.agents/skills/how-to-build-iam-auth-center-service/scripts/build-iam-auth-center-service.sh"
"${compose[@]}" up --build --abort-on-container-exit --exit-code-from migration-test
