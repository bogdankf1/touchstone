#!/usr/bin/env bash
# Run from the repository root. State/evidence remain until explicit project cleanup.
set -euo pipefail
: "${RECKONER_SECRET_DIR:?set protected disposable credential directory}"
: "${RECKONER_EXPORT_DIR:?set writable evidence directory}"
export RECKONER_BUILD_REVISION="${RECKONER_BUILD_REVISION:-$(git rev-parse HEAD)}"
export RECKONER_IMPORT_DIR="${RECKONER_IMPORT_DIR:-$RECKONER_EXPORT_DIR/empty-import}"
export RECKONER_RUNTIME_DIR="${RECKONER_RUNTIME_DIR:-$RECKONER_EXPORT_DIR/empty-runtime}"
mkdir -p "$RECKONER_EXPORT_DIR" "$RECKONER_IMPORT_DIR" "$RECKONER_RUNTIME_DIR"
# Linux bind mounts need the documented image UID; this image-local operation touches only output.
compose=(docker compose -p touchstone-phase1-smoke -f infra/compose.yaml)
"${compose[@]}" config --quiet
"${compose[@]}" up -d --wait postgres
if [[ "${RECKONER_SKIP_BUILD:-0}" != 1 ]]; then
  "${compose[@]}" build reckoner
fi
"${compose[@]}" run --rm --no-deps --user 0 owner chown 10001:10001 /evidence
"${compose[@]}" run --rm --no-deps owner
"${compose[@]}" up -d --wait reckoner
"${compose[@]}" run --rm --no-deps owner reckoner smoke --env-file - --output /evidence/runner-otlp
"${compose[@]}" run --rm --no-deps evaluator reckoner evaluate --env-file - --run-id fabricated-smoke-v1
"${compose[@]}" run --rm --no-deps runner reckoner export --env-file - --run-id fabricated-smoke-v1 --output /evidence/runner-otlp
"${compose[@]}" run --rm --no-deps evaluator reckoner export-evaluations --env-file - --run-id fabricated-smoke-v1 --output /evidence/evaluator-otlp
"${compose[@]}" run --rm --no-deps evaluator reckoner report --env-file - --run-id fabricated-smoke-v1 --output /evidence/report
"${compose[@]}" restart postgres
"${compose[@]}" up -d --wait postgres reckoner
"${compose[@]}" run --rm --no-deps owner reckoner smoke --env-file - --output /evidence/runner-otlp
python3 infra/verify_smoke.py "$RECKONER_EXPORT_DIR" "http://127.0.0.1:${RECKONER_API_PORT:-8000}"
"${compose[@]}" exec -T postgres psql -U postgres -d reckoner_smoke_compose -Atc 'SELECT version(); SELECT count(*) FROM reckoner.attempts;'
attempt_count="$("${compose[@]}" exec -T postgres psql -U postgres -d reckoner_smoke_compose -Atc 'SELECT count(*) FROM reckoner.attempts;')"
[[ "$attempt_count" == 4 ]]
docker stats --no-stream touchstone-phase1-smoke-postgres-1 touchstone-phase1-smoke-reckoner-1
