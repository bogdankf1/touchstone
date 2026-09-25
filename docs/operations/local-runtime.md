# Local runtime and resource evidence

All transaction data referenced here is simulated. Measurements below come from one development
host on 2026-09-25 and are point-in-time observations, not sizing guarantees.

## Environment

| Item | Observed value |
|---|---|
| Host architecture | Apple arm64 |
| Docker Desktop | client/server 29.8.0; Compose v5.5.1 |
| Docker allocation | 12 CPUs; 8,319,238,144 bytes (7.748 GiB) memory |
| Kubernetes client | kubectl v1.36.1; Kustomize v5.8.1 |
| kind | v0.33.0, installed under ignored `artifacts/tools/` |
| Python | host locked environment 3.12.13; image 3.12.14 |
| uv | 0.11.32 |
| Disk | planning inspection about 80.2 GiB free; human-approved budget about 80 GB; task-time `df` showed 77 GiB free |

The image uses the verified native arm64 indices
`python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9`
and
`ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c`.
The built `touchstone-reckoner:phase0` image ID was
`sha256:e0f3c305da524e14c7464ff6de30ce5bee0d1b2d2610fe564918f63ff0c9ce8e`,
reported as arm64 with user `10001:10001`, Python 3.12.14, and 224,497,364 bytes unpacked.

## Measurements

| Check | Actual observation |
|---|---|
| Full simulated-corpus profiler | 111.06 seconds; 23,822,336-byte peak RSS (about 22.7 MiB) |
| Compose API container | healthy; 41.55 MiB of 256 MiB; 0.36% CPU; 2 PIDs |
| kind node | 729.8 MiB of 7.748 GiB at the sample; 12 CPU and 8,124,256 Ki memory allocatable |
| kind Reckoner container | 41,693,184-byte working set; 40,468,480-byte RSS; 100m/64Mi request; 500m/256Mi limit |
| kind pod | Ready 1/1; zero restarts; loaded image ID `sha256:7395c7c30913ef8f59b5f4e53be8a9827e97551d6f54beec0a19dfde4d2ef178` |

Both deployment paths returned exactly `{"status":"ok"}` from `/health/live`. Compose was stopped
before kind started. The named Compose project and disposable kind cluster were removed after the
checks; no volumes or unrelated Docker resources were deleted.

These small-process measurements do not prove that the future databases, model integrations,
workflow runtime, warehouse tooling, and web application fit together within the Docker
allocation. Each later phase must measure only the services it introduces and revisit concurrency
before running more of the stack.

## Compose procedure

```bash
docker compose -p touchstone-foundation -f infra/compose.yaml config --quiet
docker compose -p touchstone-foundation -f infra/compose.yaml up --build -d --wait
curl --fail http://127.0.0.1:8000/health/live
docker stats --no-stream touchstone-foundation-reckoner-1
docker compose -p touchstone-foundation -f infra/compose.yaml down
```

`down` intentionally omits volume deletion and applies only to the named project.

## kind procedure

Download the official Darwin arm64 kind v0.33.0 binary to ignored artifacts and verify it before
execution:

```bash
mkdir -p artifacts/tools
curl -fL https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-darwin-arm64 -o artifacts/tools/kind
curl -fL https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-darwin-arm64.sha256sum -o artifacts/tools/kind.sha256sum
shasum -a 256 artifacts/tools/kind
cat artifacts/tools/kind.sha256sum
chmod +x artifacts/tools/kind
```

Both checksum values must be
`0c8c7dbe5e23594a198b786c4bc13dacc101fa6196b0cb0b23a1ca44e61f4b4f`.
Use the dedicated ignored kubeconfig so the user's default configuration is untouched:

```bash
artifacts/tools/kind create cluster --name touchstone-foundation --config infra/kind.yaml --kubeconfig artifacts/kind-kubeconfig --wait 60s
artifacts/tools/kind load docker-image touchstone-reckoner:phase0 --name touchstone-foundation
kubectl --kubeconfig artifacts/kind-kubeconfig apply -f infra/k8s/namespace.yaml
kubectl --kubeconfig artifacts/kind-kubeconfig apply -f infra/k8s/reckoner.yaml
kubectl --kubeconfig artifacts/kind-kubeconfig -n touchstone-foundation rollout status deployment/reckoner --timeout=60s
kubectl --kubeconfig artifacts/kind-kubeconfig -n touchstone-foundation port-forward service/reckoner 8000:8000 --address 127.0.0.1
```

While the controlled port-forward is active, call
`curl --fail http://127.0.0.1:8000/health/live`. Then terminate only that port-forward, confirm the
exact cluster name with `artifacts/tools/kind get clusters`, and remove it with:

```bash
artifacts/tools/kind delete cluster --name touchstone-foundation --kubeconfig artifacts/kind-kubeconfig
```
