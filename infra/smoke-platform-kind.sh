#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
kind_bin="${KIND_BIN:-$(command -v kind || true)}"
if [[ -z "$kind_bin" || ! -x "$kind_bin" ]]; then
  printf 'kind executable unavailable; install kind or set KIND_BIN to an executable path\n' >&2
  exit 2
fi
cluster="${TOUCHSTONE_KIND_CLUSTER:-touchstone-phase2-smoke-$$}"
kubeconfig="${TOUCHSTONE_KIND_KUBECONFIG:-/private/tmp/$cluster.kubeconfig}"
namespace=touchstone-phase2-smoke
password="${TOUCHSTONE_CH_PASSWORD:-phase2-kind-disposable}"
image_platform="${TOUCHSTONE_KIND_IMAGE_PLATFORM:-linux/arm64}"
kubectl=(kubectl --kubeconfig "$kubeconfig")

for image in touchstone-platform:phase2 touchstone-web:phase2 touchstone-synthetic:phase2; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    printf 'Missing local image %s; build it before running the kind smoke\n' "$image" >&2
    exit 2
  fi
done

docker tag clickhouse/clickhouse-server:25.8@sha256:0152dd511befe6a2c2ef53e930726179669b08116da78500b37c51c96ff5ee77 touchstone-clickhouse:phase2
docker tag otel/opentelemetry-collector-contrib:0.136.0@sha256:45392d534c1edcc809c2d112394029246bc679d2ae5ea7081414a1fc74f2c621 touchstone-collector:phase2
if ! docker container inspect "$cluster-control-plane" >/dev/null 2>&1; then
  "$kind_bin" create cluster --name "$cluster" --config infra/kind.yaml --kubeconfig "$kubeconfig"
fi
for image in touchstone-platform:phase2 touchstone-web:phase2 touchstone-synthetic:phase2 \
  touchstone-clickhouse:phase2 touchstone-collector:phase2; do
  archive="/private/tmp/$cluster-${image%%:*}.tar"
  docker image save --platform "$image_platform" -o "$archive" "$image"
  "$kind_bin" load image-archive --name "$cluster" "$archive"
  rm "$archive"
done

"${kubectl[@]}" apply -f infra/k8s/platform/namespace.yaml
"${kubectl[@]}" -n "$namespace" create secret generic platform-db \
  --from-literal="password=$password"
"${kubectl[@]}" -n "$namespace" create configmap platform-clickhouse-init \
  --from-file=init.sql=platform/clickhouse/init.sql
"${kubectl[@]}" -n "$namespace" create configmap platform-collector-config \
  --from-file=config.yaml=platform/collector/config.yaml
for manifest in clickhouse collector warehouse api web; do
  "${kubectl[@]}" apply -f "infra/k8s/platform/$manifest.yaml"
done
for deployment in clickhouse collector dagster dagster-daemon api web; do
  "${kubectl[@]}" -n "$namespace" rollout status "deployment/$deployment" --timeout=240s
done

"${kubectl[@]}" apply -f infra/k8s/platform/smoke-job.yaml
"${kubectl[@]}" -n "$namespace" wait --for=condition=complete job/synthetic-smoke --timeout=240s
"${kubectl[@]}" -n "$namespace" logs job/synthetic-smoke
"${kubectl[@]}" -n "$namespace" exec deployment/dagster-daemon -- dagster job execute \
  -m touchstone_platform.orchestration.definitions -j touchstone_refresh
"${kubectl[@]}" -n "$namespace" exec -i deployment/dagster-daemon -- python - <<'PY'
import json
from urllib.parse import urlencode
from urllib.request import urlopen

query = urlencode({"workflow_id": "synthetic-case-triage", "aggregate": "true"})
for run, complete in (("phase2-kind-fabricated", True), ("phase2-kind-fabricated-incomplete", False)):
    with urlopen(f"http://api:8000/v1/runs/{run}/summary?{query}") as response:
        result = json.load(response)["data"]
    assert result["metrics_complete"] is complete
    assert result["cpst"] == ("3.050000000000" if complete else None)
    with urlopen("http://web:3000/?" + urlencode({"workflow": "synthetic-case-triage", "run": run})) as response:
        assert response.status == 200
print("kind fabricated OTLP-to-dashboard smoke passed")
PY
"${kubectl[@]}" -n "$namespace" get pods -o wide
printf 'Kind cluster: %s\nDedicated kubeconfig: %s\n' "$cluster" "$kubeconfig"
