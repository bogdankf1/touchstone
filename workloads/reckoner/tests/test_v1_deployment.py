"""Reckoner v1 Compose/kind packaging and the allow-listed smoke orchestrator (no Docker run)."""

import hashlib
import importlib.util
import json
import os
import re
import shutil
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
    sys.modules[spec.name] = module  # dataclasses resolve deferred annotations via the module
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
        # Required, never defaulted: the script validates names against preserved volumes.
        assert re.fullmatch(
            r"\$\{RECKONER_V1_[A-Z0-9_]+:\?[^}]+\}(-[a-z0-9-]+)?", volume["name"]
        ), name
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
    [("prepare", 7 * GIB, None), ("online", 4.5 * GIB, None), ("refresh", 5.875 * GIB, "512m")],
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
    assert resident + jobs == ceiling < DOCKER_BUDGET
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


REFRESH = K8S / "refresh"
CH_DIGEST = "sha256:0152dd511befe6a2c2ef53e930726179669b08116da78500b37c51c96ff5ee77"
COLLECTOR_DIGEST = "sha256:45392d534c1edcc809c2d112394029246bc679d2ae5ea7081414a1fc74f2c621"


def refresh_overlay():
    """The kind refresh stage, rendered exactly as `kubectl apply -k` would apply it."""
    if not (REFRESH / "kustomization.yaml").exists():
        pytest.fail("kind refresh overlay is not implemented")
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.fail("rendering the kind refresh overlay requires kubectl (kustomize)")
    rendered = subprocess.run(
        [kubectl, "kustomize", str(REFRESH)], check=True, capture_output=True, text=True
    ).stdout
    documents = [d for d in yaml.safe_load_all(rendered) if d]
    job = yaml.safe_load((REFRESH / "refresh-job.yaml").read_text())
    return documents, job


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


def test_cleanup_refuses_a_same_named_volume_carrying_another_sentinel(tmp_path):
    module = script()
    instance = "touchstone-phase3-v1-smoke-ab12cd"
    ledger = module.Ledger.create(tmp_path / "evidence", instance)
    docker = FakeDocker(existing=PRESERVED)
    module.create_volumes(docker, ledger, [instance + "-postgres"])
    docker.labels[instance + "-postgres"]["touchstone.smoke.sentinel"] = "someone-else"
    with pytest.raises(module.Refused, match="sentinel"):
        module.cleanup(docker, ledger)
    assert not [c for c in docker.calls if c[:3] == ["docker", "volume", "rm"]]


def test_cleanup_refuses_a_preserved_volume_named_in_the_ledger(tmp_path):
    module = script()
    instance = "touchstone-phase3-v1-smoke-ab12cd"
    ledger = module.Ledger.create(tmp_path / "evidence", instance)
    docker = FakeDocker(existing=PRESERVED)
    module.create_volumes(docker, ledger, [instance + "-postgres"])
    ledger.data["volumes"].append("touchstone-phase3-task3_task3-postgres")
    with pytest.raises(module.Refused, match="cannot own"):
        module.cleanup(docker, ledger)
    assert not [c for c in docker.calls if c[:3] == ["docker", "volume", "rm"]]


def kind_ledger(module, tmp_path, instance="touchstone-phase3-v1-kind-ab12cd"):
    ledger = module.Ledger.create(tmp_path / "evidence", instance)
    kubeconfig = tmp_path / "kind.kubeconfig"
    kubeconfig.write_text(f"current-context: kind-{instance}\n")
    ledger.data["kubeconfig"] = str(kubeconfig)
    ledger.add("clusters", instance)
    return ledger, kubeconfig


def test_cleanup_refuses_a_kind_cluster_without_this_runs_node_label(tmp_path):
    module = script()
    ledger, kubeconfig = kind_ledger(module, tmp_path)
    calls = []

    def runner(command, **kwargs):
        calls.append([str(c) for c in command])
        return "another-run" if any("jsonpath=" in str(c) for c in command) else ""

    with pytest.raises(module.Refused, match="sentinel"):
        module.cleanup(runner, ledger, kind_bin=tmp_path / "kind")
    assert not [c for c in calls if "delete" in c] and kubeconfig.exists()


def test_cleanup_deletes_its_cluster_through_the_dedicated_kubeconfig_then_removes_it(tmp_path):
    module = script()
    ledger, kubeconfig = kind_ledger(module, tmp_path)
    calls = []

    def runner(command, **kwargs):
        calls.append([str(c) for c in command])
        return ledger.sentinel if any("jsonpath=" in str(c) for c in command) else ""

    module.cleanup(runner, ledger, kind_bin=tmp_path / "kind")
    deleted = [c for c in calls if "delete" in c]
    assert deleted == [
        [
            str(tmp_path / "kind"),
            "delete",
            "cluster",
            "--name",
            ledger.data["instance"],
            "--kubeconfig",
            str(kubeconfig),
        ]
    ]
    assert not kubeconfig.exists()


def test_cleanup_refuses_a_kubeconfig_that_does_not_name_its_cluster(tmp_path):
    module = script()
    ledger, kubeconfig = kind_ledger(module, tmp_path)
    kubeconfig.write_text("current-context: someone-elses-cluster\n")
    with pytest.raises(module.Refused, match="kubeconfig"):
        module.cleanup(lambda command, **kwargs: ledger.sentinel, ledger, kind_bin=tmp_path / "k")
    assert kubeconfig.exists()


def test_kind_refuses_a_pre_existing_cluster_before_creating_anything(tmp_path, monkeypatch):
    module = script()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KUBECONFIG", raising=False)
    kind = tmp_path / "kind"
    kind.write_bytes(b"fabricated kind")
    plugin = tmp_path / "plugins"
    plugin.mkdir()
    (plugin / module.GDS_JAR).write_bytes(b"fabricated")
    instance = "touchstone-phase3-v1-kind-ab12cd"
    calls = []

    def runner(command, **kwargs):
        calls.append([str(c) for c in command])
        return instance if command[1:3] == ["get", "clusters"] else ""

    with pytest.raises(module.Refused, match="already exists"):
        module.kind_smoke(
            module.Options(
                instance,
                tmp_path / "evidence",
                plugin,
                hashlib.sha256(b"fabricated").hexdigest(),
                sample_interval=0,
            ),
            tmp_path / "kubeconfig",
            kind,
            hashlib.sha256(b"fabricated kind").hexdigest(),
            runner=runner,
        )
    assert not [c for c in calls if "create" in c]


@pytest.mark.parametrize(
    "running",
    ["touchstone-phase3-v1-kind-ab12cd-control-plane", "touchstone-phase3-v1-smoke-1-online-api-1"],
)
def test_compose_and_kind_never_run_together(running):
    """Either stack's containers make the other's pre-flight refuse."""
    module = script()

    def runner(command, **kwargs):
        if command[:2] == ["docker", "ps"]:
            kind_filter = "label=io.x-k8s.kind.cluster" in command
            return running if (not kind_filter or "control-plane" in running) else ""
        return ""

    with pytest.raises(module.Refused, match="running"):
        module.preflight_docker(runner, "touchstone-phase3-v1-new-ab12cd", [])


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


def test_failure_midway_preserves_old_volumes_images_and_sources(tmp_path, monkeypatch):
    """A failing online profile stops only this run; nothing pre-existing is removed."""
    module = script()
    monkeypatch.setattr(module, "free_bytes", lambda: 30 * GIB)
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
    # Profile-independent down of every own project, including the one that failed.
    for stage in ("prepare", "online"):
        assert f"docker compose -p {instance}-{stage} down --remove-orphans" in text
    guard = (evidence / "disk-guard.jsonl").read_text()
    assert '"label": "before-volumes"' in guard and '"label": "prepare-done"' in guard
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
        if "create" in command and "--kubeconfig" in command:
            Path(command[command.index("--kubeconfig") + 1]).write_text(f"kind-{instance}")
        if command[:2] == ["docker", "info"]:
            return "aarch64"
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
    kubeconfig = str((tmp_path / "kubeconfig").absolute())
    assert deleted == [
        [str(kind), "delete", "cluster", "--name", instance, "--kubeconfig", kubeconfig]
    ]
    text = [" ".join(c) for c in calls]
    assert not [t for t in text if "volume rm" in t or "prune" in t or " rmi " in t]
    guard = [json.loads(line) for line in (evidence / "disk-guard.jsonl").read_text().splitlines()]
    assert guard[-1]["free_bytes"] < module.FREE_FLOOR and guard[-1]["below_floor"] is True


def test_verifier_receipt_on_stdout_is_one_json_document_without_interleaved_stderr(
    monkeypatch, capsys
):
    """kind captures stdout and stderr together; a stdout receipt must stand alone."""
    spec = importlib.util.spec_from_file_location(
        "verify_v1", INFRA / "verify_reckoner_v1_smoke.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "stopped", lambda args: {"checks": {"api_cases_503": True}})
    assert module.main(["stopped", "--api", "http://a", "--web", "http://w", "--output", "-"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["failed"] == [] and captured.err == ""


def test_repeated_kind_jobs_keep_every_receipt(tmp_path):
    module = script()

    class Kube:
        def __call__(self, *args):
            if "jsonpath={.status.succeeded},{.status.failed}" in args:
                return "1,"
            if "logs" in args:
                return '{"check": "online", "failed": []}'
            return ""

    for label in ("kind-online", "kind-online-after-restart"):
        module._job(
            Kube(),
            tmp_path,
            "smoke-job.yaml",
            "verify-online",
            "reckoner-v1-verify-online",
            label=label,
        )
    assert {p.name for p in tmp_path.iterdir()} == {
        "kind-online.json",
        "kind-online-after-restart.json",
    }


def test_kind_refresh_stage_reuses_the_platform_manifests_in_its_own_namespace():
    kustomization = yaml.safe_load((REFRESH / "kustomization.yaml").read_text())
    assert "../../platform" in kustomization["resources"]
    for path in K8S.rglob("*.yaml"):
        if path.name == "kustomization.yaml":
            continue
        for document in filter(None, yaml.safe_load_all(path.read_text())):
            # Platform services are reused from infra/k8s/platform, never redefined here.
            assert document["metadata"]["name"] not in {"clickhouse", "collector"}, path
    documents, job = refresh_overlay()
    names = {(d["kind"], d["metadata"]["name"]) for d in documents}
    assert {
        ("Deployment", "clickhouse"),
        ("Deployment", "collector"),
        ("Deployment", "api"),
    } <= names
    assert not {("Deployment", "dagster"), ("Deployment", "dagster-daemon")} & names
    assert {d["metadata"].get("namespace") for d in documents if d["kind"] != "Namespace"} == {
        "touchstone-phase3-v1-refresh"
    }
    images = {
        c["image"]
        for d in documents
        if d["kind"] == "Deployment"
        for c in d["spec"]["template"]["spec"]["containers"]
    }
    assert images == {
        "touchstone-clickhouse:phase3",
        "touchstone-collector:phase3",
        "touchstone-platform:phase3",
    }
    pinned = {d["metadata"]["name"]: d["metadata"].get("annotations", {}) for d in documents}
    assert pinned["clickhouse"]["touchstone.dev/pinned-source"].endswith(CH_DIGEST)
    assert pinned["collector"]["touchstone.dev/pinned-source"].endswith(COLLECTOR_DIGEST)
    assert job["metadata"]["namespace"] == "touchstone-phase3-v1-refresh"
    assert job["spec"]["template"]["spec"]["containers"][0]["command"] == ["touchstone", "refresh"]
    assert job["spec"]["backoffLimit"] == 0


def test_console_dashboard_url_points_at_the_refresh_stage_read_api():
    _, (web,) = containers(workloads()["web"])
    url = {e["name"]: e.get("value") for e in web["env"]}["TOUCHSTONE_API_URL"]
    assert url == "http://api.touchstone-phase3-v1-refresh:8000"
    documents, _ = refresh_overlay()
    assert ("Service", "api") in {(d["kind"], d["metadata"]["name"]) for d in documents}


def test_kind_refresh_profile_matches_compose_refresh_ceilings():
    _, services = compose()
    documents, job = refresh_overlay()
    deployments = {d["metadata"]["name"]: d for d in documents if d["kind"] == "Deployment"}
    export = workloads()["reckoner-v1-export"]
    pairs = {
        "clickhouse": deployments["clickhouse"],
        "collector": deployments["collector"],
        "platform-api": deployments["api"],
        "refresh": job,
        "exporter": export,
    }
    for compose_name, document in pairs.items():
        _, items = containers(document)
        limit = items[-1]["resources"]["limits"]["memory"]
        number, unit = re.fullmatch(r"(\d+)(Mi|Gi)", limit).groups()
        expected = size(services[compose_name]["mem_limit"])
        assert int(number) * (1024**2 if unit == "Mi" else GIB) == expected, compose_name


def test_disk_guard_also_enforces_the_approved_derived_data_cap(tmp_path, monkeypatch):
    module = script()
    readings = iter([30 * GIB, 21 * GIB])
    monkeypatch.setattr(module, "free_bytes", lambda: next(readings))
    budget = module.DerivedBudget(baseline_bytes=18 * GIB, cap_bytes=26 * GIB)
    assert module.guard_disk(tmp_path, "start", budget) == 30 * GIB
    with pytest.raises(module.DiskFloor, match="derived"):
        module.guard_disk(tmp_path, "loaded", budget)  # 18 + (30 - 21) = 27 GiB > 26 GiB
    record = json.loads((tmp_path / "disk-guard.jsonl").read_text().splitlines()[-1])
    assert record["derived_bytes"] == 27 * GIB and record["above_derived_cap"] is True


def test_ci_runs_the_v1_smoke_and_benchmark_store_integration_suites():
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yaml").read_text())["jobs"]
    job = jobs["neo4j-integration"]
    commands = " ".join(step.get("run", "") for step in job["steps"])
    for suite in ("test_v1_graph.py", "test_v1_smoke.py", "test_v1_benchmark_stores.py"):
        assert f"workloads/reckoner/tests/{suite}" in commands
    for name in (
        "RECKONER_TEST_SAMPLE_CONTAINERS",
        "RECKONER_TEST_AUDIT_PREFIX",
        "RECKONER_TEST_DU_IMAGE",
    ):
        assert job["env"][name]


def test_structurizr_model_is_structurally_consistent():
    """Not a Structurizr parse: balanced blocks and every referenced identifier is defined."""
    text = (ROOT / "docs/architecture/workspace.dsl").read_text()
    code = re.sub(r'"[^"]*"', '""', text)
    assert code.count("{") == code.count("}")
    defined = set(re.findall(r"^\s*(\w+)\s*=\s*(?:person|softwareSystem|container)\b", text, re.M))
    for source, target in re.findall(r"^\s*(\w+)\s*->\s*(\w+)", text, re.M):
        assert {source, target} <= defined, (source, target)
    for name in re.findall(r"^\s*include\s+(\w+)\s*$", text, re.M):
        assert name in defined, name
    views = re.findall(r'^\s*(?:container|systemContext)\s+(\w+)\s+"(\w+)"', text, re.M)
    assert len({key for _, key in views}) == len(views), "view keys must be unique"
    assert "ReckonerV1" in {key for _, key in views}


def test_host_scripts_run_on_the_host_python_39():
    """macOS's /usr/bin/python3 is 3.9: PEP 604 annotations must stay unevaluated."""
    for name in ("smoke-reckoner-v1.py", "verify_reckoner_v1_smoke.py"):
        text = (INFRA / name).read_text()
        if re.search(r"\w+ \| None", text):
            assert "from __future__ import annotations" in text, name
    host = Path("/usr/bin/python3")
    if host.exists():
        result = subprocess.run(
            [str(host), str(INFRA / "smoke-reckoner-v1.py"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_v1", INFRA / "verify_reckoner_v1_smoke.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_credential_checks_fail_when_no_secret_is_known_or_any_leaks():
    module = verifier()
    assert module.no_credentials(["body"], ["s3cret"]) is True
    assert module.no_credentials(["body with s3cret"], ["s3cret"]) is False
    assert module.no_credentials(["body"], []) is False  # nothing to check is not a pass


def test_readiness_check_requires_the_latest_packaged_migration():
    from reckoner.storage.postgres import PACKAGED_MIGRATIONS

    module = verifier()
    assert module.ready_is_latest({"status": "ready", "migration": PACKAGED_MIGRATIONS[-1]})
    assert not module.ready_is_latest({"status": "ready", "migration": PACKAGED_MIGRATIONS[0]})


def test_stopped_check_inspects_every_error_body_for_credentials(monkeypatch):
    module = verifier()
    bodies = {
        "/health/live": (200, '{"status":"ok"}'),
        "/health/ready": (503, '{"detail":"database unavailable"}'),
        "/v1/cases": (503, '{"detail":"database unavailable"}'),
        "/api/reckoner/cases": (503, '{"detail":"postgresql://x:s3cret@h/db"}'),
    }

    def get(url, **kwargs):
        return next(v for k, v in bodies.items() if k in url.split("?")[0][-25:])

    monkeypatch.setattr(module, "_get", get)
    monkeypatch.setattr(module, "_secrets", lambda: ["s3cret"])
    result = module.stopped(type("Args", (), {"api": "http://a", "web": "http://w"})())
    assert result["checks"]["no_credentials_in_error"] is False


# --- Fix round 2: orchestrator hardening -------------------------------------------------


def write_ledger(tmp_path, **changes):
    module = script()
    instance = "touchstone-phase3-v1-smoke-ab12cd"
    ledger = module.Ledger.create(tmp_path / "evidence", instance)
    ledger.data.update(changes)
    ledger.save()
    return module, tmp_path / "evidence"


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"instance": "touchstone-phase3-task3"}, "instance"),
        ({"sentinel": "not-hex"}, "sentinel"),
        ({"projects": ["touchstone-phase3-v1-smoke-ab12cd-evil"]}, "project"),
        ({"clusters": ["touchstone-phase3-v1-other-cluster"]}, "cluster"),
        ({"kubeconfig": "/Users/someone/.kube/config"}, "kubeconfig"),
    ],
)
def test_a_tampered_ledger_is_refused_on_load(tmp_path, changes, message):
    module, evidence = write_ledger(tmp_path, **changes)
    with pytest.raises(module.Refused, match=message):
        module.Ledger.load(evidence)


def test_cleanup_refuses_a_project_whose_containers_carry_another_sentinel(tmp_path):
    module, evidence = write_ledger(tmp_path)
    ledger = module.Ledger.load(evidence)
    project = ledger.data["instance"] + "-online"
    ledger.add("projects", project)
    calls = []

    def runner(command, **kwargs):
        calls.append(" ".join(map(str, command)))
        if command[:3] == ["docker", "ps", "-a"]:
            return "someone-else\n"
        return ""

    with pytest.raises(module.Refused, match="sentinel"):
        module.cleanup(runner, ledger)
    assert not [c for c in calls if " down" in c]


def test_cleanup_stops_its_project_after_the_container_sentinel_check(tmp_path):
    module, evidence = write_ledger(tmp_path)
    ledger = module.Ledger.load(evidence)
    project = ledger.data["instance"] + "-online"
    ledger.add("projects", project)
    calls = []

    def runner(command, **kwargs):
        calls.append(" ".join(map(str, command)))
        if command[:3] == ["docker", "ps", "-a"]:
            assert f"label=com.docker.compose.project={project}" in command
            return ledger.sentinel + "\n" + ledger.sentinel + "\n"
        return ""

    module.cleanup(runner, ledger)
    assert f"docker compose -p {project} down --remove-orphans" in calls


def test_compose_services_carry_the_run_sentinel_label():
    _, services = compose()
    for name, service in services.items():
        assert service["labels"]["touchstone.smoke.sentinel"].startswith(
            "${RECKONER_V1_SENTINEL"
        ), name


def test_pinned_tag_pointing_at_another_image_is_refused_before_retagging():
    module = script()
    calls = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[:3] == ["docker", "image", "inspect"]:
            return "sha256:other" if command[-1].startswith("touchstone-") else "sha256:pinned"
        return ""

    with pytest.raises(module.Refused, match="different image"):
        module.tag_pinned_images(runner)
    assert not [c for c in calls if c[:2] == ["docker", "tag"]]


def test_absent_pinned_tags_are_created_and_then_verified():
    module = script()
    tags, calls = {}, []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[:3] == ["docker", "image", "inspect"]:
            name = command[-1]
            if name.startswith("touchstone-"):
                if name not in tags:
                    raise subprocess.CalledProcessError(1, command)
                return tags[name]
            return "sha256:" + name.rsplit(":", 1)[-1][:12]
        if command[:2] == ["docker", "tag"]:
            tags[command[3]] = "sha256:" + command[2].rsplit(":", 1)[-1][:12]
        return ""

    module.tag_pinned_images(runner)
    assert set(tags) == set(module.PINNED)


@pytest.mark.parametrize("command", ["compose", "kind"])
@pytest.mark.parametrize("given", ["--derived-cap-bytes", "--derived-baseline-bytes"])
def test_derived_budget_options_must_be_given_together(tmp_path, command, given, capsys):
    module = script()
    argv = [
        command,
        "--instance",
        "touchstone-phase3-v1-x-ab12cd",
        "--evidence-dir",
        str(tmp_path / "evidence"),
        "--plugin-dir",
        str(tmp_path),
        given,
        "1",
    ]
    if command == "kind":
        argv += [
            "--kubeconfig",
            str(tmp_path / "k"),
            "--kind-bin",
            str(tmp_path / "kind"),
            "--kind-sha256",
            "0" * 64,
        ]
    with pytest.raises(SystemExit) as raised:
        module.main(argv)
    assert raised.value.code == 2 and not (tmp_path / "evidence").exists()


def test_cleanup_with_kind_bin_requires_its_checksum(tmp_path):
    module = script()
    with pytest.raises(SystemExit):
        module.main(["cleanup", "--evidence-dir", str(tmp_path), "--kind-bin", str(tmp_path)])


def test_evidence_inside_the_tracked_tree_is_refused_unless_git_ignored(tmp_path):
    module = script()
    with pytest.raises(module.Refused, match="tracked"):
        module.validate_evidence_dir(ROOT / "docs" / "smoke-evidence")
    assert module.validate_evidence_dir(ROOT / "artifacts/phase3/task12/x")
    assert module.validate_evidence_dir(tmp_path / "evidence")


def test_receipt_scan_rejects_any_secret_value_outside_the_secret_directory(tmp_path):
    module = script()
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets/postgres.env").write_text("POSTGRES_PASSWORD=s3cret\n")
    (tmp_path / "online.json").write_text('{"ok": true}')
    module.scan_receipts(tmp_path, ["s3cret"])
    (tmp_path / "leak.txt").write_text("postgresql://x:s3cret@h/db")
    with pytest.raises(module.Refused, match="leak.txt"):
        module.scan_receipts(tmp_path, ["s3cret"])
    with pytest.raises(module.Refused, match="no secret"):
        module.scan_receipts(tmp_path, [])


def test_write_secrets_returns_every_generated_credential(tmp_path):
    module = script()
    created = module.write_secrets(tmp_path / "secrets")
    text = "".join(p.read_text() for p in (tmp_path / "secrets").iterdir())
    for value in created["secret_values"]:
        assert value in text
    assert len(created["secret_values"]) == 6  # owner, runner, evaluator, api, neo4j, clickhouse


def test_memory_parser_tolerates_unavailable_docker_stats_rows(tmp_path):
    module = script()
    assert module.memory_used("-- / --") is None
    rows = [
        {"Name": "a", "MemUsage": "--", "sample": 0},
        {"Name": "a", "MemUsage": "2MiB / 1GiB", "sample": 1},
    ]
    (tmp_path / "stats-x.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert module.resource_summary(tmp_path)["stats-x"]["max_sampled_bytes_combined"] == 2 * 2**20


def test_pinned_images_agree_across_script_compose_and_kind():
    module = script()
    platform = yaml.load((INFRA / "compose.platform.yaml").read_text(), Loader=ComposeLoader)
    _, services = compose()
    compose_images = {services["postgres"]["image"], services["neo4j"]["image"]}
    compose_images |= {platform["services"][n]["image"] for n in ("clickhouse", "collector")}
    annotations = {
        d["metadata"].get("annotations", {}).get("touchstone.dev/pinned-source")
        for _, d in manifests()
    } | {
        d["metadata"].get("annotations", {}).get("touchstone.dev/pinned-source")
        for d in refresh_overlay()[0]
    }
    assert set(module.PINNED.values()) == compose_images == annotations - {None}


def full_kind_runner(tmp_path, instance, secrets_seen):
    """Answers every command of a successful kind run; records argv for inspection."""
    calls, tags = [], {}

    def runner(command, **kwargs):
        command = [str(c) for c in command]
        calls.append(command)
        if "create" in command and "--kubeconfig" in command and command[1] == "create":
            Path(command[command.index("--kubeconfig") + 1]).write_text(f"kind-{instance}")
        if command[:3] == ["docker", "info", "--format"]:
            return "aarch64"
        if command[:3] == ["docker", "image", "inspect"]:
            name = command[-1]
            if name.startswith("touchstone-") and name.split(":")[0] in {
                t.split(":")[0] for t in module_pinned()
            }:
                if name not in tags:
                    raise subprocess.CalledProcessError(1, command)
                return tags[name]
            return "sha256:" + name[-12:]
        if command[:2] == ["docker", "tag"]:
            tags[command[3]] = "sha256:" + command[2][-12:]
        if command[:3] == ["docker", "image", "save"]:
            Path(command[command.index("-o") + 1]).write_bytes(b"archive")
        if any(c.startswith("jsonpath={.status") for c in command):
            return "1,"
        if any(c.startswith("jsonpath={.items[0]") for c in command):
            evidence = tmp_path / "evidence"
            return json.loads((evidence / "ledger.json").read_text())["sentinel"]
        if "logs" in command:
            return "{}"
        return ""

    return runner, calls


def module_pinned():
    return script().PINNED


def test_a_complete_fake_kind_run_never_puts_a_secret_on_argv_and_cleans_up(tmp_path, monkeypatch):
    module = script()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setattr(module, "free_bytes", lambda: 30 * GIB)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    kind = tmp_path / "kind"
    kind.write_bytes(b"fabricated kind")
    plugin = tmp_path / "plugins"
    plugin.mkdir()
    (plugin / module.GDS_JAR).write_bytes(b"fabricated")
    instance = "touchstone-phase3-v1-kind-ab12cd"
    runner, calls = full_kind_runner(tmp_path, instance, [])
    archives = []
    real_mkdtemp = module.tempfile.mkdtemp
    monkeypatch.setattr(
        module.tempfile, "mkdtemp", lambda **k: archives.append(real_mkdtemp(**k)) or archives[-1]
    )
    secrets_seen = {}
    real_write = module.write_secrets

    def capture(directory):
        created = real_write(directory)
        secrets_seen["values"] = created["secret_values"]
        return created

    monkeypatch.setattr(module, "write_secrets", capture)
    module.kind_smoke(
        module.Options(
            instance,
            tmp_path / "evidence",
            plugin,
            hashlib.sha256(b"fabricated").hexdigest(),
            cleanup=True,
            sample_interval=0,
        ),
        tmp_path / "kubeconfig",
        kind,
        hashlib.sha256(b"fabricated kind").hexdigest(),
        runner=runner,
        preflight=False,
    )
    argv = " ".join(" ".join(c) for c in calls)
    assert secrets_seen["values"] and not [v for v in secrets_seen["values"] if v in argv]
    saves = [c for c in calls if c[:3] == ["docker", "image", "save"]]
    assert saves and all(c[c.index("--platform") + 1] == "linux/arm64" for c in saves)
    assert archives and not any(Path(a).exists() for a in archives)
    kubectl = [c for c in calls if c[0] == "kubectl"]
    assert all(
        c[1:5]
        == [
            "--kubeconfig",
            str((tmp_path / "kubeconfig").absolute()),
            "--context",
            f"kind-{instance}",
        ]
        for c in kubectl
    )
    assert not (tmp_path / "evidence/secrets").exists()
    assert not (tmp_path / "kubeconfig").exists()


def runbook_commands():
    text = (ROOT / "docs/operations/reckoner-v1-runbook.md").read_text()
    return "\n".join(re.findall(r"```bash\n(.*?)```", text, re.S))


def test_runbook_container_variables_expand_inside_the_container():
    """A host-side "$POSTGRES_DB" is empty, which would silently dump the `postgres` DB."""
    commands = runbook_commands()
    for line in commands.splitlines():
        if "exec" in line and "$POSTGRES_" in line:
            assert re.search(r"sh -c '[^']*\$POSTGRES_", line), line


def test_runbook_volume_creation_checks_every_name_before_creating_any():
    commands = runbook_commands()
    body = commands[commands.index("create_v1_volumes()") :]
    assert "|| return 1" in body and "exit 1" not in body
    assert body.index("docker volume inspect") < body.index("docker volume create")


@pytest.mark.parametrize("listing", ["\n", "SENTINEL\n\n", "\nSENTINEL\n"])
def test_cleanup_refuses_project_containers_without_any_sentinel_label(tmp_path, listing):
    """An unlabelled container is not ours: an empty label must refuse, never vanish."""
    module, evidence = write_ledger(tmp_path)
    ledger = module.Ledger.load(evidence)
    project = ledger.data["instance"] + "-online"
    ledger.add("projects", project)
    calls = []

    def runner(command, **kwargs):
        calls.append(" ".join(map(str, command)))
        if command[:3] == ["docker", "ps", "-a"]:
            return listing.replace("SENTINEL", ledger.sentinel)
        return ""

    with pytest.raises(module.Refused, match="sentinel"):
        module.cleanup(runner, ledger)
    assert not [c for c in calls if " down" in c]


def kind_inputs(module, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setattr(module, "free_bytes", lambda: 30 * GIB)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    kind = tmp_path / "kind"
    kind.write_bytes(b"fabricated kind")
    plugin = tmp_path / "plugins"
    plugin.mkdir()
    (plugin / module.GDS_JAR).write_bytes(b"fabricated")
    return kind, plugin


def test_kind_refuses_a_kubeconfig_that_cleanup_could_not_later_accept(tmp_path, monkeypatch):
    module = script()
    kind, plugin = kind_inputs(module, tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere" / "nested" / "kubeconfig"
    calls = []
    with pytest.raises(module.Refused, match="kubeconfig"):
        module.kind_smoke(
            module.Options(
                "touchstone-phase3-v1-kind-ab12cd",
                tmp_path / "evidence",
                plugin,
                hashlib.sha256(b"fabricated").hexdigest(),
                sample_interval=0,
            ),
            elsewhere,
            kind,
            hashlib.sha256(b"fabricated kind").hexdigest(),
            runner=lambda command, **k: calls.append(command) or "",
            preflight=False,
        )
    assert calls == [] and not (tmp_path / "evidence").exists()


def test_a_failed_receipt_scan_still_deletes_the_cluster_with_cleanup(tmp_path, monkeypatch):
    module = script()
    kind, plugin = kind_inputs(module, tmp_path, monkeypatch)
    instance = "touchstone-phase3-v1-kind-ab12cd"
    runner, calls = full_kind_runner(tmp_path, instance, [])

    def leak(evidence, values):
        raise module.Refused("leak.txt contains a generated credential")

    monkeypatch.setattr(module, "scan_receipts", leak)
    with pytest.raises(module.Refused, match="leak.txt"):
        module.kind_smoke(
            module.Options(
                instance,
                tmp_path / "evidence",
                plugin,
                hashlib.sha256(b"fabricated").hexdigest(),
                cleanup=True,
                sample_interval=0,
            ),
            tmp_path / "kubeconfig",
            kind,
            hashlib.sha256(b"fabricated kind").hexdigest(),
            runner=runner,
            preflight=False,
        )
    assert [c for c in calls if c[0] == str(kind) and "delete" in c]


EVIDENCE_COMPOSE = INFRA / "compose.reckoner-v1.evidence.yaml"
EVIDENCE_INPUTS = {
    "RECKONER_V1_BUNDLE_DIR": "/inputs/bundle",
    "RECKONER_V1_SOURCE_DIR": "/inputs/source",
    "RECKONER_V1_BASELINE_DIR": "/inputs/baseline",
    "RECKONER_V1_SCALER_DIR": "/inputs/scaler",
}


def test_neo4j_transaction_log_retention_defaults_to_the_documented_neo4j_value():
    _, services = compose()
    environment = services["neo4j"]["environment"]
    assert environment["NEO4J_db_tx__log_rotation_retention__policy"] == (
        "${RECKONER_V1_NEO4J_TX_LOG_RETENTION:-2 days 2G}"
    )


def test_evidence_inputs_are_required_read_only_binds_only_when_the_override_is_loaded():
    if not EVIDENCE_COMPOSE.exists():
        pytest.fail("evidence preparation Compose override is not implemented")
    override = yaml.safe_load(EVIDENCE_COMPOSE.read_text())
    assert set(override) == {"services"} and set(override["services"]) == {"owner", "neo4j"}
    mounts = override["services"]["owner"]["volumes"]
    assert len(mounts) == len(EVIDENCE_INPUTS)
    assert set(override["services"]["owner"]) == {"volumes", "mem_limit"}
    for mount in mounts:
        match = re.fullmatch(r"\$\{([A-Z0-9_]+):\?[^}]+\}:(/inputs/[a-z]+):ro", mount)
        assert match, mount
        assert EVIDENCE_INPUTS[match.group(1)] == match.group(2)
    # The job's memory is chosen per pass: Pass R runs without Neo4j (Postgres 2g + job 3g),
    # Pass G with it (Postgres 1g + Neo4j 4g + job 2g), both within the 7 GiB profile.
    assert re.fullmatch(
        r"\$\{RECKONER_V1_EVIDENCE_JOB_MEMORY:\?[^}]+\}",
        override["services"]["owner"]["mem_limit"],
    )
    # The base file never names these inputs, so every other profile renders without them.
    base = COMPOSE.read_text()
    assert not [
        name for name in [*EVIDENCE_INPUTS, "RECKONER_V1_EVIDENCE_JOB_MEMORY"] if name in base
    ]
    _, services = compose()
    assert "prepare" in services["owner"]["profiles"] and services["owner"]["mem_limit"] == "1g"


def test_ci_runs_the_rolling_evidence_graph_suite_on_an_empty_store():
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yaml").read_text())["jobs"]
    steps = jobs["neo4j-integration"]["steps"]
    runs = [step.get("run", "") for step in steps]
    index = next(i for i, run in enumerate(runs) if "test_v1_evidence_rolling_graph.py" in run)
    assert "-m neo4j_integration" in runs[index]
    assert steps[index]["env"]["RECKONER_TEST_EVIDENCE_NEO4J_URI"].startswith("bolt://127.0.0.1")
    # The rolling graph refuses the store the other suites mark: recreate it empty first.
    assert any("down --volumes" in run for run in runs[:index])
    assert "up -d --wait" in runs[index - 1]
    assert "test_v1_evidence_rolling_graph.py" not in " ".join(runs[:index])


def test_evidence_graph_transactions_are_bounded_by_a_generous_server_timeout():
    override = yaml.safe_load(EVIDENCE_COMPOSE.read_text())
    environment = override["services"]["neo4j"]["environment"]
    assert environment["NEO4J_db_transaction_timeout"] == "${RECKONER_V1_NEO4J_TX_TIMEOUT:-30m}"


def test_runbook_documents_a_bounded_retry_of_the_idempotent_run():
    text = (ROOT / "docs/operations/reckoner-v1-runbook.md").read_text()
    block = text[text.index("v1e_retry()") :]
    block = block[: block.index("}") + 1]
    for needed in ("attempt", "exit", "-ge", "return"):
        assert needed in block, needed
