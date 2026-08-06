#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
IMAGE_NAME=etbc-iam-migration:unit

docker build --tag "$IMAGE_NAME" "$SCRIPT_DIR"
docker run --rm \
  --volume "$SCRIPT_DIR:/workspace:ro" \
  --workdir /workspace \
  --entrypoint python \
  "$IMAGE_NAME" \
  -m pytest tests/unit -q -p no:cacheprovider
docker run --rm --user 65532:65532 --entrypoint python "$IMAGE_NAME" -m etbc_migration --help >/dev/null
