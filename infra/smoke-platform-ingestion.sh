#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export UV_PYTHON_INSTALL_DIR=/private/tmp/touchstone-uv-python
export UV_CACHE_DIR=/private/tmp/touchstone-uv-cache
export TOUCHSTONE_CH_PASSWORD="$(openssl rand -hex 16)"
export TOUCHSTONE_COMPOSE_PROJECT="touchstone-task2-smoke-$$"

compose=(docker compose -p "$TOUCHSTONE_COMPOSE_PROJECT" -f infra/compose.platform.yaml)
cleanup() {
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${compose[@]}" up -d --wait
uv run --offline --all-packages pytest platform/tests/test_ingestion_integration.py -q -s
