"""Reckoner v1 Compose/kind packaging and the allow-listed smoke orchestrator (no Docker run)."""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
INFRA = ROOT / "infra"
COMPOSE = INFRA / "compose.reckoner-v1.yaml"
K8S = INFRA / "k8s/reckoner-v1"
PG_DIGEST = "sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f"
NEO4J_DIGEST = "sha256:91fb0bf237c41b7b3dcbe84703aa0b82e0d7d067b16e1c8ab21f03fc679edf4e"
GIB = 1024**3
DOCKER_BUDGET = 8_319_238_144
PRESERVED = {
    "touchstone-phase1-measured_postgres-data",
    "touchstone-phase1-smoke_postgres-data",
    "touchstone-phase2-measured_clickhouse-data",
    "touchstone-phase2-measured_collector-queue",
    "touchstone-phase2-measured_warehouse-data",
    "touchstone-phase3-task3_task3-neo4j",
    "touchstone-phase3-task3_task3-postgres",
    "touchstone-phase3-task10_clickhouse-data",
    "touchstone-phase3-task10_collector-queue",
    "touchstone-phase3-task11-neo4j",
    "touchstone-phase3-task11-postgres",
}


def script():
    path = INFRA / "smoke-reckoner-v1.py"
    if not path.exists():
        pytest.fail("allow-listed smoke orchestrator is not implemented")
    spec = importlib.util.spec_from_file_location("smoke_reckoner_v1", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ComposeLoader(yaml.SafeLoader):
    """Compose merge tags replace the extended value; plain YAML has no constructor."""


def _override(loader, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_sequence(node, deep=True)


ComposeLoader.add_constructor("!override", _override)


def compose():
    if not COMPOSE.exists():
        pytest.fail("Reckoner v1 Compose file is not implemented")
    document = yaml.load(COMPOSE.read_text(), Loader=ComposeLoader)
    platform = yaml.safe_load((INFRA / "compose.platform.yaml").read_text())["services"]
    services = {}
    for name, service in document["services"].items():
        base = dict(platform[service["extends"]["service"]]) if "extends" in service else {}
        services[name] = {**base, **{k: v for k, v in service.items() if k != "extends"}}
    return document, services


def size(value: str) -> int:
    default = re.fullmatch(r"\$\{[A-Z0-9_]+:-([^}]+)\}", str(value))
    text = default.group(1) if default else str(value)
    number, unit = re.fullmatch(r"(\d+)([mg])", text).groups()
    return int(number) * (1024**2 if unit == "m" else GIB)


def profile_services(services, profile):
    return {name: s for name, s in services.items() if profile in s.get("profiles", [])}


def test_every_service_is_profiled_and_volumes_are_external_instance_scoped():
    document, services = compose()
    assert all(s.get("profiles") for s in services.values()), "no service may start implicitly"
    for name, volume in document["volumes"].items():
        assert volume["external"] is True, name
        assert "${RECKONER_V1_INSTANCE" in volume["name"], name
        assert not any(p in volume["name"] for p in PRESERVED)
    profiles = {p for s in services.values() for p in s["profiles"]}
    assert profiles == {"prepare", "online", "refresh"}
    # Image-declared VOLUME paths must be mounted, or Docker leaves unowned anonymous volumes.
    declared = {
        "postgres": "/var/lib/postgresql/data",
        "neo4j": "/data",
        "clickhouse": "/var/lib/clickhouse",
        "queue-init": "/var/lib/clickhouse",
        "warehouse-init": "/var/lib/clickhouse",
    }
    for name, path in declared.items():
        mounts = [str(v).split(":")[1] for v in services[name].get("volumes", [])]
        assert path in mounts + services[name].get("tmpfs", []), name
    assert "/logs" in [str(v).split(":")[1] for v in services["neo4j"]["volumes"]]


@pytest.mark.parametrize(
    "profile,ceiling,postgres",
    [("prepare", 7 * GIB, None), ("online", 4.5 * GIB, None), ("refresh", 6.4 * GIB, "512m")],
)
def test_profile_memory_ceilings_fit_the_docker_budget(profile, ceiling, postgres):
    """Long-running services plus the largest one-shot job never exceed the profile ceiling."""
    _, services = compose()
    selected = profile_services(services, profile)
    assert all("mem_limit" in s for s in selected.values()), "every service needs a limit"
    one_shot = {
        "migrate",
        "owner",
        "worker",
        "exporter",
        "verifier",
        "queue-init",
        "warehouse-init",
        "refresh",
    }
    limits = {
        name: size(postgres) if name == "postgres" and postgres else size(s["mem_limit"])
        for name, s in selected.items()
    }
    resident = sum(v for k, v in limits.items() if k not in one_shot)
    jobs = max((v for k, v in limits.items() if k in one_shot), default=0)
    assert resident + jobs <= ceiling < DOCKER_BUDGET
    assert not {"dagster", "dagster-daemon"} & set(selected), "Phase 2 stack is not duplicated"
    assert ("neo4j" in selected) is (profile == "prepare"), "graph runs only for preparation"


def test_traffic_waits_for_migration_and_readiness_checks_are_declared():
    _, services = compose()
    api = services["api"]
    assert api["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert api["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "/health/ready" in " ".join(api["healthcheck"]["test"])
    assert services["web"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert "gds.version()" in " ".join(services["neo4j"]["healthcheck"]["test"])
    assert "neo4j" not in api.get("depends_on", {}), "API readiness never depends on the graph"
    assert "collector" not in services["exporter"].get("depends_on", {}), (
        "the outbox, not the collector, is the durable queue; export must see a stopped collector"
    )
    assert services["postgres"]["image"].endswith(PG_DIGEST)
    assert services["neo4j"]["image"].endswith(NEO4J_DIGEST)


def test_credentials_come_only_from_the_protected_runtime_directory():
    document, services = compose()
    text = COMPOSE.read_text()
    assert "PASSWORD:" not in re.sub(r"TOUCHSTONE_CH_PASSWORD", "", text).upper().replace(
        "CLICKHOUSE_PASSWORD", ""
    )
    for name, service in services.items():
        files = service.get("env_file", [])
        for path in [files] if isinstance(files, str) else files:
            assert path.startswith("${RECKONER_SECRET_DIR"), (name, path)
    assert ".env" not in [Path(p).name for p in re.findall(r"[\w./-]+\.env\b", text)]


def manifests():
    if not K8S.is_dir():
        pytest.fail("Reckoner v1 kind manifests are not implemented")
    documents = []
    for path in sorted(K8S.glob("*.yaml")):
        documents += [(path.name, d) for d in yaml.safe_load_all(path.read_text()) if d]
    return documents


def workloads():
    kinds = {"StatefulSet", "Deployment", "Job"}
    return {d["metadata"]["name"]: d for _, d in manifests() if d["kind"] in kinds}


def containers(document):
    spec = document["spec"]
    template = spec.get("template") or spec.get("jobTemplate", {}).get("spec", {}).get("template")
    pod = template["spec"]
    return pod, [*pod.get("initContainers", []), *pod["containers"]]


def test_kind_manifest_files_and_single_dedicated_namespace():
    names = {path.name for path in K8S.glob("*.yaml")}
    assert names == {
        "namespace.yaml",
        "postgres.yaml",
        "neo4j.yaml",
        "worker.yaml",
        "api.yaml",
        "web.yaml",
        "smoke-job.yaml",
    }
    namespaces = {
        d["metadata"].get("namespace") for _, d in manifests() if d["kind"] != "Namespace"
    }
    assert namespaces == {"touchstone-phase3-v1-smoke"}


def test_kind_workloads_pin_images_limit_resources_and_mount_secrets_at_runtime():
    pinned = {"touchstone-pgvector:phase3": PG_DIGEST, "touchstone-neo4j:phase3": NEO4J_DIGEST}
    seen = set()
    for name, document in manifests():
        if document["kind"] not in {"StatefulSet", "Deployment", "Job"}:
            continue
        pod, items = containers(document)
        assert pod["securityContext"]["runAsNonRoot"] is True, name
        for container in items:
            assert container["imagePullPolicy"] == "Never", (name, container["name"])
            image = container["image"]
            seen.add(image)
            assert image in pinned or image in {
                "touchstone-reckoner:phase3",
                "touchstone-web:phase3",
            }, image
            if image in pinned:
                source = document["metadata"]["annotations"]["touchstone.dev/pinned-source"]
                assert source.endswith(pinned[image])
            resources = container["resources"]
            assert resources["requests"]["memory"] and resources["limits"]["memory"]
            assert resources["requests"]["cpu"] and resources["limits"]["cpu"]
            for variable in container.get("env", []):
                assert "value" not in variable or "PASSWORD" not in variable["name"]
                assert "DSN" not in variable["name"] or "valueFrom" in variable
            for source in container.get("envFrom", []):
                assert set(source) == {"secretRef"}
        if document["kind"] == "Job":
            assert document["spec"]["backoffLimit"] == 0
    assert set(pinned) <= seen


def test_kind_storage_probes_and_graph_plugin_mount():
    by_name = workloads()
    for name in ("postgres", "neo4j"):
        claims = by_name[name]["spec"]["volumeClaimTemplates"]
        assert claims and claims[0]["spec"]["accessModes"] == ["ReadWriteOnce"]
    _, (api,) = containers(by_name["api"])
    assert api["readinessProbe"]["httpGet"]["path"] == "/health/ready"
    assert api["livenessProbe"]["httpGet"]["path"] == "/health/live"
    _, (web,) = containers(by_name["web"])
    assert web["readinessProbe"] and web["livenessProbe"]
    pod, _ = containers(by_name["neo4j"])
    plugin = next(v for v in pod["volumes"] if v["name"] == "gds-plugins")
    assert plugin["hostPath"]["path"] == "/touchstone/gds-plugins"
    _, (neo4j,) = containers(by_name["neo4j"])
    mount = next(m for m in neo4j["volumeMounts"] if m["name"] == "gds-plugins")
    assert mount["readOnly"] is True


def test_kind_and_compose_resource_profiles_are_equivalent():
    _, services = compose()
    by_name = workloads()
    pairs = {"postgres": "postgres", "neo4j": "neo4j", "api": "api", "web": "web"}
    for compose_name, kind_name in pairs.items():
        _, items = containers(by_name[kind_name])
        limit = items[-1]["resources"]["limits"]["memory"]
        expected = size(services[compose_name]["mem_limit"])
        number, unit = re.fullmatch(r"(\d+)(Mi|Gi)", limit).groups()
        assert int(number) * (1024**2 if unit == "Mi" else GIB) == expected, compose_name


# --- Allow-listed orchestrator guards -------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "touchstone-phase2-smoke-1",
        "touchstone-phase3-task3",
        "touchstone-phase3-task11",
        "touchstone-phase3-v1",
        "touchstone-phase3-v1-UPPER",
        "touchstone-phase3-v1-../escape",
        "phase3-v1-smoke",
    ],
)
def test_instance_names_outside_the_phase3_v1_namespace_are_refused(name):
    module = script()
    with pytest.raises(module.Refused):
        module.validate_instance(name)


def test_planned_identifiers_never_include_preserved_resources():
    module = script()
    instance = module.validate_instance("touchstone-phase3-v1-smoke-ab12cd")
    planned = module.planned_volumes(instance)
    assert planned and not set(planned) & PRESERVED
    assert all(v.startswith(instance + "-") for v in planned)
    assert module.PRESERVED_VOLUMES == PRESERVED


class FakeDocker:
    """Records commands; reports pre-existing volumes and optional failures."""

    def __init__(self, existing=(), labels=None, fail_on=None):
        self.calls, self.existing, self.labels = [], set(existing), labels or {}
        self.fail_on = fail_on

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        text = " ".join(command)
        if self.fail_on and self.fail_on(text):
            raise subprocess.CalledProcessError(1, command)
        if command[:3] == ["docker", "volume", "inspect"]:
            name = command[-1]
            if name not in self.existing:
                raise subprocess.CalledProcessError(1, command)
            return json.dumps([{"Name": name, "Labels": self.labels.get(name, {})}])
        if command[:3] == ["docker", "volume", "create"]:
            name = command[-1]
            self.existing.add(name)
            label = [c for c in command if c.startswith(module_label())]
            self.labels[name] = dict(item.split("=", 1) for item in label)
            return name
        return ""


def module_label():
    return "touchstone.smoke."


def test_existing_resources_are_refused_and_never_adopted(tmp_path):
    module = script()
    instance = "touchstone-phase3-v1-smoke-ab12cd"
    docker = FakeDocker(existing={instance + "-postgres"})
    ledger = module.Ledger.create(tmp_path / "evidence", instance)
    with pytest.raises(module.Refused, match="already exists"):
        module.create_volumes(docker, ledger, module.planned_volumes(instance))
    assert not [c for c in docker.calls if c[:3] == ["docker", "volume", "create"]]


def test_cleanup_deletes_only_ledger_resources_with_the_exact_sentinel(tmp_path):
    module = script()
    instance = "touchstone-phase3-v1-smoke-ab12cd"
    ledger = module.Ledger.create(tmp_path / "evidence", instance)
    docker = FakeDocker(existing=PRESERVED)
    module.create_volumes(docker, ledger, [instance + "-postgres"])
    # A same-named volume carrying another sentinel is not ours; a preserved one never is.
    docker.labels[instance + "-postgres"]["touchstone.smoke.sentinel"] = "someone-else"
    ledger.data["volumes"].append("touchstone-phase3-task3_task3-postgres")
    with pytest.raises(module.Refused):
        module.cleanup(docker, ledger)
    removed = [c for c in docker.calls if c[:3] == ["docker", "volume", "rm"]]
    assert removed == []


def test_cleanup_removes_its_own_volume_after_checking_labels(tmp_path):
    module = script()
    instance = "touchstone-phase3-v1-smoke-ab12cd"
    ledger = module.Ledger.create(tmp_path / "evidence", instance)
    docker = FakeDocker(existing=PRESERVED)
    module.create_volumes(docker, ledger, [instance + "-postgres"])
    module.cleanup(docker, ledger)
    assert [c for c in docker.calls if c[:3] == ["docker", "volume", "rm"]] == [
        ["docker", "volume", "rm", instance + "-postgres"]
    ]
    assert not any("prune" in c or "rmi" in c for c in map(" ".join, docker.calls))


@pytest.mark.parametrize("ambient", ["default", "env", "existing"])
def test_kind_refuses_ambient_kubectl_configuration(tmp_path, monkeypatch, ambient):
    module = script()
    home = tmp_path / "home"
    (home / ".kube").mkdir(parents=True)
    (home / ".kube/config").write_text("ambient")
    monkeypatch.setenv("HOME", str(home))
    kubeconfig = {
        "default": home / ".kube/config",
        "env": tmp_path / "chosen",
        "existing": tmp_path / "existing",
    }[ambient]
    if ambient == "env":
        monkeypatch.setenv("KUBECONFIG", str(home / ".kube/config"))
    if ambient == "existing":
        kubeconfig.write_text("old cluster")
    with pytest.raises(module.Refused):
        module.validate_kubeconfig(kubeconfig, os.environ)


def test_kubectl_commands_always_name_the_isolated_kubeconfig_and_context(tmp_path):
    module = script()
    kubeconfig = tmp_path / "kubeconfig"
    kube = module.Kubectl(kubeconfig, "touchstone-phase3-v1-kind-ab12cd", runner=FakeDocker())
    kube("get", "pods")
    command = kube.runner.calls[-1]
    assert command[:5] == [
        "kubectl",
        "--kubeconfig",
        str(kubeconfig),
        "--context",
        "kind-touchstone-phase3-v1-kind-ab12cd",
    ]
    assert "KUBECONFIG" not in module.child_environment({"KUBECONFIG": "/x", "PATH": "/bin"})


def _tree_hash(paths):
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path).encode() + path.read_bytes())
    return digest.hexdigest()


def test_failure_midway_preserves_old_volumes_images_and_sources(tmp_path):
    """A failing online profile stops only this run; nothing pre-existing is removed."""
    module = script()
    sources = [p for p in INFRA.rglob("*") if p.is_file()]
    before = _tree_hash(sources)
    instance = "touchstone-phase3-v1-smoke-ab12cd"
    docker = FakeDocker(
        existing=PRESERVED, fail_on=lambda text: "--profile online" in text and " up " in text
    )
    evidence = tmp_path / "evidence"
    plugin = tmp_path / "plugins"
    plugin.mkdir()
    jar = plugin / module.GDS_JAR
    jar.write_bytes(b"fabricated")
    with pytest.raises(subprocess.CalledProcessError):
        module.compose_smoke(
            module.Options(
                instance=instance,
                evidence=evidence,
                plugin_dir=plugin,
                plugin_sha256=hashlib.sha256(b"fabricated").hexdigest(),
                cleanup=False,
                sample_interval=0,
            ),
            runner=docker,
            preflight=False,
        )
    text = [" ".join(c) for c in docker.calls]
    assert not [t for t in text if " volume rm " in f" {t} " or "prune" in t or " rmi " in t]
    assert not [t for t in text if "down" in t and ("--volumes" in t or "-v" in t.split())]
    assert not [t for t in text if "image rm" in t or "kind delete" in t]
    assert any("--profile online" in t and " down" in t for t in text), "own project stopped"
    ledger = json.loads((evidence / "ledger.json").read_text())
    assert set(ledger["volumes"]) == set(module.planned_volumes(instance))
    assert not set(ledger["volumes"]) & PRESERVED
    assert _tree_hash(sources) == before


def test_script_runs_with_the_host_standard_library_only():
    text = (INFRA / "smoke-reckoner-v1.py").read_text()
    imported = set(re.findall(r"^(?:from|import) ([a-z_]+)", text, re.M))
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


def test_committed_langgraph_view_matches_the_compiled_workflow():
    from langgraph.checkpoint.memory import InMemorySaver
    from reckoner.v1.workflow import build_graph

    generated = build_graph(InMemorySaver()).get_graph().draw_mermaid()
    assert (ROOT / "docs/architecture/reckoner-v1-workflow.mmd").read_text() == generated


def test_resource_summary_sums_one_sample_round_and_keeps_cgroup_peaks(tmp_path):
    module = script()
    rows = [
        {"Name": "a", "MemUsage": "100MiB / 1GiB", "sample": 0},
        {"Name": "b", "MemUsage": "1GiB / 4GiB", "sample": 0},
        {"Name": "a", "MemUsage": "300MiB / 1GiB", "sample": 1},
    ]
    (tmp_path / "stats-online.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "cgroup-online.json").write_text(
        json.dumps({"a": {"memory.peak": "5"}, "collector": None})
    )
    summary = module.resource_summary(tmp_path)
    assert summary["stats-online"]["max_sampled_bytes_combined"] == 100 * 2**20 + 2**30
    assert summary["stats-online"]["max_sampled_bytes_per_container"]["a"] == 300 * 2**20
    assert summary["cgroup-online"] == {"a": 5, "collector": None}


def test_kind_aborts_at_the_free_disk_floor_and_deletes_only_its_own_cluster(tmp_path, monkeypatch):
    """Owner bound: free disk below 15 GiB aborts and runs the sentinel-only cleanup."""
    module = script()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KUBECONFIG", raising=False)
    kind = tmp_path / "kind"
    kind.write_bytes(b"fabricated kind")
    plugin = tmp_path / "plugins"
    plugin.mkdir()
    (plugin / module.GDS_JAR).write_bytes(b"fabricated")
    evidence = tmp_path / "evidence"
    instance = "touchstone-phase3-v1-kind-ab12cd"
    readings = iter([30 * GIB, 30 * GIB, 14 * GIB])
    monkeypatch.setattr(module, "free_bytes", lambda: next(readings, 14 * GIB))
    calls = []

    def runner(command, **kwargs):
        calls.append([str(part) for part in command])
        if any(str(part).startswith("jsonpath=") for part in command):
            return json.loads((evidence / "ledger.json").read_text())["sentinel"]
        return ""

    with pytest.raises(module.DiskFloor):
        module.kind_smoke(
            module.Options(
                instance=instance,
                evidence=evidence,
                plugin_dir=plugin,
                plugin_sha256=hashlib.sha256(b"fabricated").hexdigest(),
                cleanup=False,
                sample_interval=0,
            ),
            tmp_path / "kubeconfig",
            kind,
            hashlib.sha256(b"fabricated kind").hexdigest(),
            runner=runner,
            preflight=False,
        )
    deleted = [c for c in calls if c[0] == str(kind) and "delete" in c]
    assert deleted == [[str(kind), "delete", "cluster", "--name", instance]]
    text = [" ".join(c) for c in calls]
    assert not [t for t in text if "volume rm" in t or "prune" in t or " rmi " in t]
    guard = [json.loads(line) for line in (evidence / "disk-guard.jsonl").read_text().splitlines()]
    assert guard[-1]["free_bytes"] < module.FREE_FLOOR and guard[-1]["below_floor"] is True
