#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export TOUCHSTONE_CH_PASSWORD="${TOUCHSTONE_CH_PASSWORD:-phase2-smoke-disposable}"
project="${TOUCHSTONE_COMPOSE_PROJECT:-touchstone-phase2-smoke-$$}"
if [[ ! "$project" =~ ^touchstone-phase2-smoke-[a-zA-Z0-9-]+$ ]]; then
  printf 'Refusing non-disposable Compose project: %s\n' "$project" >&2
  exit 2
fi
existing_containers="$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
existing_volumes="$(docker volume ls -q --filter "label=com.docker.compose.project=$project")"
existing_networks="$(docker network ls -q --filter "label=com.docker.compose.project=$project")"
if [[ -n "$existing_containers$existing_volumes$existing_networks" ]]; then
  printf 'Refusing existing Compose project resources: %s\n' "$project" >&2
  exit 2
fi
export TOUCHSTONE_COMPOSE_PROJECT="$project"
compose=(docker compose -p "$TOUCHSTONE_COMPOSE_PROJECT" -f infra/compose.platform.yaml)
cleanup() {
  "${compose[@]}" down --volumes >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${compose[@]}" up -d --build --wait clickhouse collector api dagster dagster-daemon web
collector_endpoint="http://$("${compose[@]}" port collector 4318)"
uv run --frozen --all-packages touchstone-synthetic emit \
  --endpoint "$collector_endpoint" --run-id phase2-ci-fabricated

for attempt in {1..30}; do
  raw_spans="$("${compose[@]}" exec -T clickhouse clickhouse-client \
    --user touchstone --password "$TOUCHSTONE_CH_PASSWORD" \
    --query 'SELECT count() FROM otel.otel_traces')"
  if [[ "$raw_spans" == 44 ]]; then
    break
  fi
  sleep 1
done
[[ "$raw_spans" == 44 ]]

"${compose[@]}" exec -T dagster-daemon dagster job execute \
  -m touchstone_platform.orchestration.definitions -j touchstone_refresh
api_endpoint="http://$("${compose[@]}" port api 8000)"
web_endpoint="http://$("${compose[@]}" port web 3000)"
TOUCHSTONE_SMOKE_API="$api_endpoint" TOUCHSTONE_SMOKE_WEB="$web_endpoint" python3 - <<'PY'
import json
import os
from urllib.parse import urlencode
from urllib.request import urlopen

api = os.environ["TOUCHSTONE_SMOKE_API"]
web = os.environ["TOUCHSTONE_SMOKE_WEB"]
with urlopen(api + "/v1/workflows") as response:
    workflows = json.load(response)["data"]
assert any(row["workflow_id"] == "synthetic-case-triage" for row in workflows)
for run_id, complete in (("phase2-ci-fabricated", True), ("phase2-ci-fabricated-incomplete", False)):
    query = urlencode({"workflow_id": "synthetic-case-triage", "aggregate": "true"})
    with urlopen(f"{api}/v1/runs/{run_id}/summary?{query}") as response:
        summary = json.load(response)["data"]
    assert summary["metrics_complete"] is complete
    assert summary["cpst"] == ("3.050000000000" if complete else None)
    with urlopen(web + "/?" + urlencode({"workflow": "synthetic-case-triage", "run": run_id})) as response:
        assert response.status == 200
print("fabricated OTLP-to-warehouse-to-API-to-web smoke passed")
PY
