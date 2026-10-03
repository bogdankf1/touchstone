#!/usr/bin/env python3
"""Allow-listed Reckoner v1 Compose/kind smoke and restore check on fabricated simulated data.

Run from the repository root with the host Python (standard library only):

  python3 infra/smoke-reckoner-v1.py compose --instance touchstone-phase3-v1-smoke-<id> \\
      --evidence-dir artifacts/phase3/task12/compose-<id> \\
      --plugin-dir artifacts/phase3/task3/plugins
  python3 infra/smoke-reckoner-v1.py kind --instance touchstone-phase3-v1-kind-<id> \\
      --evidence-dir artifacts/phase3/task12/kind-<id> --plugin-dir ... \\
      --kubeconfig artifacts/phase3/task12/kind-<id>.kubeconfig --kind-bin PATH --kind-sha256 HEX
  python3 infra/smoke-reckoner-v1.py cleanup --evidence-dir DIR

Every resource this script creates is written to DIR/ledger.json BEFORE creation and labelled
with a random sentinel. Cleanup removes only ledger resources whose labels carry that exact
sentinel; preserved Phase 1-3 identifiers are refused outright. It never prunes, never removes
images, never uses `down --volumes`, and never reads the repository `.env` or a provider key:
all credentials are freshly generated, fabricated and disposable. Compose and kind never run
together, and neither runs alongside the preserved Phase 2 stack.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "infra/compose.reckoner-v1.yaml"
K8S = ROOT / "infra/k8s/reckoner-v1"
VERIFY = ROOT / "infra/verify_reckoner_v1_smoke.py"
GDS_JAR = "neo4j-graph-data-science-2026.09.0.jar"
GDS_SHA256 = "74e7026ed7bad144c67473a0e4d47276907b781ee5e283ecab11245498ad2a8e"
DATABASE = "reckoner_smoke_v1"
RUN_ID = "fabricated-smoke-v1"
TENANTS = ("tenant-a", "tenant-b")
NAMESPACE = "touchstone-phase3-v1-smoke"
SENTINEL = "touchstone.smoke.sentinel"
NODE_SENTINEL = "touchstone.smoke/sentinel"
INSTANCE_LABEL = "touchstone.smoke.instance"
INSTANCE = re.compile(r"touchstone-phase3-v1-[a-z0-9][a-z0-9-]{2,30}")
IMAGES = {
    "reckoner": "touchstone-reckoner:phase3",
    "web": "touchstone-web:phase3",
    "platform": "touchstone-platform:phase3",
}
PINNED = {
    "touchstone-pgvector:phase3": "pgvector/pgvector:0.8.6-pg17-bookworm@sha256:"
    "cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f",
    "touchstone-neo4j:phase3": "neo4j:2026.09.0@sha256:"
    "91fb0bf237c41b7b3dcbe84703aa0b82e0d7d067b16e1c8ab21f03fc679edf4e",
}
VOLUME_SUFFIXES = ("postgres", "neo4j", "neo4j-logs", "clickhouse", "collector-queue", "warehouse")
RESTORE_SUFFIXES = ("postgres-restore", "neo4j-reimport")
PRESERVED_VOLUMES = frozenset(
    {
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
)
# Long-running stacks that must not run while this smoke runs (8 GB Docker budget).
EXCLUSIVE_PROJECTS = ("touchstone-phase2-measured", "touchstone-phase3-task")
FREE_FLOOR = 15 * 1024**3


class Refused(RuntimeError):
    """A safety rule refused the operation before any change."""


class DiskFloor(Refused):
    """Free disk fell below the hard 15 GiB floor; the run aborts and cleans up."""


def free_bytes() -> int:
    return shutil.disk_usage(ROOT).free


def guard_disk(evidence: Path, label: str) -> int:
    """Record free disk at a checkpoint and abort below the floor."""
    free = free_bytes()
    record = {"label": label, "free_bytes": free, "below_floor": free < FREE_FLOOR}
    with (Path(evidence) / "disk-guard.jsonl").open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    if free < FREE_FLOOR:
        raise DiskFloor(f"free disk {free} below the 15 GiB floor at {label}")
    return free


def validate_instance(name: str) -> str:
    if not INSTANCE.fullmatch(name or ""):
        raise Refused(f"instance must match {INSTANCE.pattern}: {name!r}")
    return name


def planned_volumes(instance: str, suffixes=VOLUME_SUFFIXES) -> list[str]:
    names = [f"{validate_instance(instance)}-{suffix}" for suffix in suffixes]
    if set(names) & PRESERVED_VOLUMES:
        raise Refused("planned volume collides with a preserved identifier")
    return names


def child_environment(environ, extra=None) -> dict:
    """Never pass an ambient kubeconfig through; everything else is explicit."""
    keep = {k: v for k, v in environ.items() if k != "KUBECONFIG"}
    return {**keep, **(extra or {})}


def run_command(command, *, env=None, stdout_path=None, input_path=None, timeout=1800):
    """Real runner: returns stdout text; raises CalledProcessError (stderr kept for logs)."""
    stdout = open(stdout_path, "wb") if stdout_path else subprocess.PIPE  # noqa: SIM115
    stdin = open(input_path, "rb") if input_path else None  # noqa: SIM115
    try:
        result = subprocess.run(
            command,
            stdout=stdout,
            stdin=stdin,
            stderr=subprocess.PIPE,
            env=child_environment(os.environ, env),
            timeout=timeout,
            check=False,
        )
    finally:
        for handle in (stdout, stdin):
            if hasattr(handle, "close"):
                handle.close()
    output = result.stdout.decode() if isinstance(result.stdout, bytes) else ""
    if result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode, command, output=output, stderr=result.stderr.decode()
        )
    return output


class Ledger:
    """Write-ahead record of every resource this run may create, with its sentinel."""

    def __init__(self, path: Path, data: dict):
        self.path, self.data = path, data

    @classmethod
    def create(cls, evidence: Path, instance: str):
        evidence = Path(evidence)
        evidence.mkdir(parents=True, exist_ok=False)
        data = {
            "schema_version": "reckoner-v1-smoke-ledger-v1",
            "instance": validate_instance(instance),
            "sentinel": secrets.token_hex(8),
            "volumes": [],
            "projects": [],
            "clusters": [],
            "dataset": "fabricated simulated",
        }
        ledger = cls(evidence / "ledger.json", data)
        ledger.save()
        return ledger

    @classmethod
    def load(cls, evidence: Path):
        path = Path(evidence) / "ledger.json"
        return cls(path, json.loads(path.read_text()))

    @property
    def sentinel(self):
        return self.data["sentinel"]

    def add(self, kind: str, name: str):
        if name not in self.data[kind]:
            self.data[kind].append(name)
            self.save()

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")


def _volume_labels(runner, name):
    try:
        inspected = json.loads(runner(["docker", "volume", "inspect", name]))
    except subprocess.CalledProcessError:
        return None
    return inspected[0].get("Labels") or {}


def create_volumes(runner, ledger: Ledger, names):
    for name in names:
        if name in PRESERVED_VOLUMES or not name.startswith(ledger.data["instance"] + "-"):
            raise Refused(f"volume {name} is outside this instance")
        if _volume_labels(runner, name) is not None:
            raise Refused(f"volume {name} already exists; refusing to adopt it")
    for name in names:
        ledger.add("volumes", name)
        runner(
            [
                "docker",
                "volume",
                "create",
                "--label",
                f"{SENTINEL}={ledger.sentinel}",
                "--label",
                f"{INSTANCE_LABEL}={ledger.data['instance']}",
                name,
            ]
        )


def cleanup(runner, ledger: Ledger, kind_bin=None):
    """Stop own projects, then remove own volumes/cluster only after every check passes."""
    for name in ledger.data["volumes"]:
        if name in PRESERVED_VOLUMES or not name.startswith(ledger.data["instance"] + "-"):
            raise Refused(f"ledger names a volume this run cannot own: {name}")
        labels = _volume_labels(runner, name)
        if labels is not None and labels.get(SENTINEL) != ledger.sentinel:
            raise Refused(f"volume {name} does not carry this run's sentinel")
    for project in ledger.data["projects"]:
        if not project.startswith(ledger.data["instance"] + "-"):
            raise Refused(f"ledger names a foreign project: {project}")
        runner(["docker", "compose", "-p", project, "down", "--remove-orphans"])
    for cluster in ledger.data["clusters"]:
        if cluster != ledger.data["instance"] or kind_bin is None:
            raise Refused(f"cluster {cluster} cannot be removed by this run")
        kubeconfig = Path(ledger.data["kubeconfig"])
        # The dedicated kubeconfig was created by this run (validate_kubeconfig refused an
        # existing file); it must still name this cluster's context before it is used.
        if not kubeconfig.is_file() or f"kind-{cluster}" not in kubeconfig.read_text():
            raise Refused(f"kubeconfig {kubeconfig} does not belong to cluster {cluster}")
        kube = Kubectl(kubeconfig, cluster, runner)
        label = "{.items[0].metadata.labels." + NODE_SENTINEL.replace(".", "\\.") + "}"
        if kube("get", "nodes", "-o", "jsonpath=" + label).strip() != ledger.sentinel:
            raise Refused(f"cluster {cluster} does not carry this run's sentinel")
        # Never fall back to the ambient ~/.kube/config.
        runner(
            [str(kind_bin), "delete", "cluster", "--name", cluster, "--kubeconfig", str(kubeconfig)]
        )
        kubeconfig.unlink()
    for name in ledger.data["volumes"]:
        if _volume_labels(runner, name) is not None:
            runner(["docker", "volume", "rm", name])


def verify_file(path: Path, expected: str):
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if digest != expected:
        raise Refused(f"checksum mismatch for {path}")
    return digest


def validate_kubeconfig(path: Path, environ) -> Path:
    path = Path(path).expanduser().absolute()
    ambient = (Path(environ.get("HOME", "~")) / ".kube/config").expanduser().absolute()
    if path == ambient:
        raise Refused("refusing the ambient kubeconfig; pass a dedicated new file")
    if environ.get("KUBECONFIG") and Path(environ["KUBECONFIG"]).absolute() != path:
        raise Refused("KUBECONFIG is set to another configuration; unset it")
    if path.exists():
        raise Refused(f"kubeconfig {path} already exists; refusing to reuse a cluster")
    return path


class Kubectl:
    def __init__(self, kubeconfig: Path, cluster: str, runner=run_command):
        self.kubeconfig, self.context, self.runner = Path(kubeconfig), f"kind-{cluster}", runner

    def __call__(self, *args, **kwargs):
        command = ["kubectl", "--kubeconfig", str(self.kubeconfig), "--context", self.context]
        return self.runner([*command, *map(str, args)], **kwargs)


class Compose:
    def __init__(self, runner, ledger: Ledger, project: str, profile: str, env: dict):
        if not project.startswith(ledger.data["instance"] + "-"):
            raise Refused(f"project {project} is outside this instance")
        self.runner, self.project, self.profile, self.env = runner, project, profile, env
        ledger.add("projects", project)

    def __call__(self, *args, **kwargs):
        command = ["docker", "compose", "-p", self.project, "--profile", self.profile]
        command += ["-f", str(COMPOSE_FILE), *map(str, args)]
        return self.runner(command, env=self.env, **kwargs)

    def run(self, service, *command, **kwargs):
        return self("run", "--rm", "--no-deps", "-T", service, *command, **kwargs)

    def down(self):
        return self("down", "--remove-orphans")


@dataclass
class Options:
    instance: str
    evidence: Path
    plugin_dir: Path
    plugin_sha256: str = GDS_SHA256
    cleanup: bool = False
    sample_interval: float = 2.0


class Sampler:
    """Coarse `docker stats` observations of this instance's containers (not exact peaks)."""

    def __init__(self, runner, path: Path, prefix: str, interval: float):
        self.runner, self.path, self.prefix, self.interval = runner, path, prefix, interval
        self.stop, self.thread = threading.Event(), threading.Thread(target=self._loop)

    def _loop(self):
        started, sample = time.monotonic(), 0
        with self.path.open("a") as handle:
            while not self.stop.is_set():
                try:
                    rows = self.runner(
                        ["docker", "stats", "--no-stream", "--format", "{{json .}}"]
                    ).splitlines()
                except subprocess.CalledProcessError:
                    rows = []
                elapsed = round(time.monotonic() - started, 3)
                for row in rows:
                    item = json.loads(row)
                    if item.get("Name", "").startswith(self.prefix):
                        # One `docker stats` call is one sample round; sum a round for totals.
                        item.update(elapsed_seconds=elapsed, sample=sample)
                        handle.write(json.dumps(item, sort_keys=True) + "\n")
                handle.flush()
                sample += 1
                self.stop.wait(self.interval)

    def __enter__(self):
        if self.interval:
            self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        if self.interval:
            self.thread.join()


UNITS = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "kB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
}


def memory_used(usage: str) -> int:
    """Used side of `docker stats` MemUsage, e.g. ``142.1MiB / 512MiB``."""
    number, unit = re.fullmatch(r"([\d.]+)([A-Za-z]+)", usage.split("/")[0].strip()).groups()
    return int(float(number) * UNITS[unit])


def resource_summary(evidence: Path) -> dict:
    """Sampled maxima per container and per sample round, plus cgroup lifetime peaks."""
    summary = {}
    for path in sorted(Path(evidence).glob("stats-*.jsonl")):
        per, rounds = {}, {}
        for line in path.read_text().splitlines():
            row = json.loads(line)
            used = memory_used(row["MemUsage"])
            per[row["Name"]] = max(per.get(row["Name"], 0), used)
            rounds[row["sample"]] = rounds.get(row["sample"], 0) + used
        summary[path.stem] = {
            "sample_rounds": len(rounds),
            "max_sampled_bytes_per_container": dict(sorted(per.items())),
            "max_sampled_bytes_combined": max(rounds.values(), default=None),
        }
    for path in sorted(Path(evidence).glob("cgroup-*.json")):
        peaks = json.loads(path.read_text())
        summary[path.stem] = {
            name: int(value["memory.peak"]) if value else None for name, value in peaks.items()
        }
    return summary


def write_secrets(directory: Path) -> dict:
    """Fresh fabricated credentials for disposable stores only; never a provider key."""
    directory.mkdir(mode=0o700)
    owner, neo4j, clickhouse = (secrets.token_urlsafe(24) for _ in range(3))
    host = f"postgres:5432/{DATABASE}"
    values = {"RECKONER_OWNER_DSN": f"postgresql://postgres:{owner}@{host}"}
    for kind in ("runner", "evaluator", "api"):
        values[f"RECKONER_{kind.upper()}_DSN"] = (
            f"postgresql://reckoner_{kind}_login:{secrets.token_urlsafe(24)}@{host}"
        )
    files = {
        "postgres.env": {
            "POSTGRES_DB": DATABASE,
            "POSTGRES_USER": "postgres",
            "POSTGRES_PASSWORD": owner,
        },
        "provision.env": values,
        "runner.env": {"RECKONER_RUNNER_DSN": values["RECKONER_RUNNER_DSN"]},
        "api.env": {"RECKONER_API_DSN": values["RECKONER_API_DSN"]},
        "neo4j-server.env": {"NEO4J_AUTH": f"neo4j/{neo4j}"},
        "neo4j.env": {
            "RECKONER_NEO4J_URI": "bolt://neo4j:7687",
            "RECKONER_NEO4J_USER": "neo4j",
            "RECKONER_NEO4J_PASSWORD": neo4j,
        },
        "clickhouse.env": {"TOUCHSTONE_CH_PASSWORD": clickhouse},
    }
    for name, content in files.items():
        path = directory / name
        path.write_text("".join(f"{k}={v}\n" for k, v in content.items()))
        path.chmod(0o600)
    return {"clickhouse": clickhouse, "secret_values": [owner, neo4j, clickhouse]}


def _write(path: Path, text: str):
    path.write_text(text if text.endswith("\n") or not text else text + "\n")


def _receipt(evidence: Path, name: str, call):
    """Run a step whose stdout is a JSON receipt; keep the output even when it fails."""
    started = time.monotonic()
    try:
        output = call()
    except subprocess.CalledProcessError as error:
        _write(evidence / f"{name}.failed.txt", (error.output or "") + (error.stderr or ""))
        raise
    _write(evidence / f"{name}.json", output)
    return {"step": name, "seconds": round(time.monotonic() - started, 3)}


def _json(evidence: Path, name: str) -> dict:
    return json.loads((evidence / f"{name}.json").read_text())


def _peaks(runner, project: str, services, evidence: Path, label: str):
    peaks = {}
    for service in services:
        container = f"{project}-{service}-1"
        try:
            peaks[service] = {
                key: runner(["docker", "exec", container, "cat", f"/sys/fs/cgroup/{key}"]).strip()
                for key in ("memory.peak", "memory.current", "memory.events")
            }
        except subprocess.CalledProcessError:
            peaks[service] = None
    _write(evidence / f"cgroup-{label}.json", json.dumps(peaks, indent=2, sort_keys=True))


def preflight_docker(runner, instance: str, images):
    for image in images:
        runner(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    running = runner(["docker", "ps", "--format", "{{.Names}}"]).split()
    busy = [n for n in running if n.startswith(EXCLUSIVE_PROJECTS) or "phase3-v1" in n]
    kind_nodes = runner(
        ["docker", "ps", "--filter", "label=io.x-k8s.kind.cluster", "--format", "{{.Names}}"]
    ).split()
    if busy or kind_nodes:
        raise Refused(f"other heavy stacks are running: {sorted(busy + kind_nodes)}")
    for existing in runner(["docker", "volume", "ls", "--format", "{{.Name}}"]).split():
        if existing.startswith(instance + "-"):
            raise Refused(f"volume {existing} already exists for this instance")
    free = shutil.disk_usage(ROOT).free
    if free < FREE_FLOOR:
        raise Refused(f"free disk {free} below the 15 GiB floor")


def _disk(runner, evidence: Path, label: str):
    record = {"free_bytes": shutil.disk_usage(ROOT).free}
    try:
        record["docker_system_df"] = runner(["docker", "system", "df", "--format", "{{json .}}"])
    except subprocess.CalledProcessError:
        record["docker_system_df"] = None
    _write(evidence / f"disk-{label}.json", json.dumps(record, indent=2, sort_keys=True))


def _volume_sizes(runner, ledger: Ledger, evidence: Path):
    names = [n for n in ledger.data["volumes"] if _volume_labels(runner, n) is not None]
    command = ["docker", "run", "--rm", "--network=none", "--read-only", "--entrypoint", "du"]
    for index, name in enumerate(names):
        command += ["-v", f"{name}:/audit/{index}:ro"]
    command += [PINNED["touchstone-pgvector:phase3"], "-sk"]
    command += [f"/audit/{index}" for index in range(len(names))]
    sizes = {}
    if names:
        for row in runner(command).splitlines():
            kib, path = row.split()
            sizes[names[int(path.rsplit("/", 1)[1])]] = int(kib) * 1024
    _write(evidence / "volume-sizes.json", json.dumps(sizes, indent=2, sort_keys=True))
    return sizes


def _restore_globals(evidence: Path):
    """Role definitions without passwords; the image's own superuser is not recreated."""
    lines = (evidence / "globals.sql").read_text().splitlines()
    kept = [line for line in lines if not re.search(r"\bROLE postgres\b", line)]
    _write(evidence / "globals-restore.sql", "\n".join(kept))
    return evidence / "globals-restore.sql"


def compose_smoke(options: Options, runner=run_command, preflight=True) -> dict:
    instance = validate_instance(options.instance)
    evidence = Path(options.evidence).absolute()
    ledger = Ledger.create(evidence, instance)
    verify_file(Path(options.plugin_dir) / GDS_JAR, options.plugin_sha256)
    if preflight:
        preflight_docker(runner, instance, [*IMAGES.values(), *PINNED.values()])
    _disk(runner, evidence, "before")
    created = write_secrets(evidence / "secrets")
    data = evidence / "empty-data"
    data.mkdir()
    env = {
        "RECKONER_V1_INSTANCE": instance,
        "RECKONER_V1_POSTGRES_VOLUME": f"{instance}-postgres",
        "RECKONER_V1_NEO4J_VOLUME": f"{instance}-neo4j",
        "RECKONER_SECRET_DIR": str(evidence / "secrets"),
        "RECKONER_V1_EVIDENCE_DIR": str(evidence / "container-output"),
        "RECKONER_V1_DATA_DIR": str(data),
        "RECKONER_GDS_PLUGIN_DIR": str(Path(options.plugin_dir).absolute()),
        "RECKONER_V1_NEO4J_TX_LOG_ROTATION": "16m",
        "TOUCHSTONE_CH_PASSWORD": created["clickhouse"],
    }
    (evidence / "container-output").mkdir()
    verify = ["python", "/verify/verify_reckoner_v1_smoke.py"]
    smoke = ["reckoner", "v1", "smoke"]
    steps = []
    create_volumes(runner, ledger, planned_volumes(instance))
    projects = []
    try:
        # Preparation/GDS profile: Postgres + Neo4j; graph evidence persisted, never scored.
        prepare = Compose(runner, ledger, f"{instance}-prepare", "prepare", env)
        projects.append(prepare)
        with Sampler(runner, evidence / "stats-prepare.jsonl", instance, options.sample_interval):
            started = time.monotonic()
            prepare("up", "-d", "--wait", "postgres", "neo4j")
            steps.append({"step": "prepare-up", "seconds": round(time.monotonic() - started, 3)})
            steps.append(_receipt(evidence, "prepare-migrate", lambda: prepare.run("migrate")))
            for step, service in (("seed", "owner"), ("graph", "owner"), ("evidence", "worker")):
                steps.append(
                    _receipt(
                        evidence,
                        f"prepare-{step}",
                        lambda s=step, v=service: prepare.run(v, *smoke, s, "--env-file", "-"),
                    )
                )
            steps.append(
                _receipt(
                    evidence,
                    "fingerprint-prepare",
                    lambda: prepare.run(
                        "verifier", *verify, "fingerprint", "--graph", "--output", "-"
                    ),
                )
            )
            _peaks(runner, prepare.project, ("postgres", "neo4j"), evidence, "prepare")
        started = time.monotonic()
        prepare.down()
        steps.append({"step": "prepare-down", "seconds": round(time.monotonic() - started, 3)})

        # Online profile: graph stopped; API, console, dashboard API and workflow worker.
        online = Compose(runner, ledger, f"{instance}-online", "online", env)
        projects.append(online)
        endpoints = ["--api", "http://api:8000", "--web", "http://web:3000", "--output", "-"]
        with Sampler(runner, evidence / "stats-online.jsonl", instance, options.sample_interval):
            started = time.monotonic()
            online("up", "-d", "--wait", "api", "web", "platform-api")
            steps.append({"step": "online-up", "seconds": round(time.monotonic() - started, 3)})
            steps.append(
                _receipt(
                    evidence,
                    "online-run",
                    lambda: online.run("worker", *smoke, "run", "--env-file", "-"),
                )
            )
            steps.append(
                _receipt(
                    evidence,
                    "online-run-repeat",
                    lambda: online.run("worker", *smoke, "run", "--env-file", "-"),
                )
            )
            steps.append(
                _receipt(
                    evidence,
                    "online",
                    lambda: online.run("verifier", *verify, "online", *endpoints),
                )
            )
            started = time.monotonic()
            online("restart", "postgres")
            online("up", "-d", "--wait", "api", "web")
            steps.append(
                {"step": "postgres-restart", "seconds": round(time.monotonic() - started, 3)}
            )
            steps.append(
                _receipt(
                    evidence,
                    "online-after-restart",
                    lambda: online.run("verifier", *verify, "online", *endpoints),
                )
            )
            online("stop", "postgres")
            steps.append(
                _receipt(
                    evidence,
                    "stopped",
                    lambda: online.run("verifier", *verify, "stopped", *endpoints),
                )
            )
            online("start", "postgres")
            online("up", "-d", "--wait", "api", "web")
            steps.append(
                _receipt(
                    evidence,
                    "fingerprint-online",
                    lambda: online.run("verifier", *verify, "fingerprint", "--output", "-"),
                )
            )
            dump = ["exec", "-T", "postgres"]
            online(
                *dump,
                "pg_dump",
                "-U",
                "postgres",
                "-d",
                DATABASE,
                "-Fc",
                stdout_path=evidence / "postgres.dump",
            )
            online(
                *dump,
                "pg_dumpall",
                "-U",
                "postgres",
                "--globals-only",
                "--no-role-passwords",
                stdout_path=evidence / "globals.sql",
            )
            _peaks(
                runner,
                online.project,
                ("postgres", "api", "web", "platform-api"),
                evidence,
                "online",
            )
        online.down()

        # Platform refresh profile: outbox -> OTLP collector -> ClickHouse -> warehouse.
        refresh_env = {**env, "RECKONER_V1_POSTGRES_MEMORY": "512m"}
        refresh = Compose(runner, ledger, f"{instance}-refresh", "refresh", refresh_env)
        projects.append(refresh)
        export = [
            "reckoner",
            "v1",
            "telemetry-export",
            "--env-file",
            "-",
            "--endpoint",
            "http://collector:4318",
        ]
        with Sampler(runner, evidence / "stats-refresh.jsonl", instance, options.sample_interval):
            refresh("up", "-d", "--wait", "postgres", "clickhouse", "collector", "platform-api")
            for tenant in TENANTS:
                steps.append(
                    _receipt(
                        evidence,
                        f"refresh-collect-{tenant}",
                        lambda t=tenant: refresh.run(
                            "exporter",
                            "reckoner",
                            "v1",
                            "telemetry-collect",
                            "--env-file",
                            "-",
                            "--tenant-id",
                            t,
                            "--run-id",
                            RUN_ID,
                        ),
                    )
                )
            refresh("stop", "collector")
            steps.append(
                _receipt(
                    evidence,
                    "refresh-export-collector-stopped",
                    lambda: refresh.run("exporter", *export),
                )
            )
            refresh("start", "collector")
            refresh("up", "-d", "--wait", "collector")
            steps.append(
                _receipt(evidence, "refresh-export", lambda: refresh.run("exporter", *export))
            )
            started = time.monotonic()
            refresh.run("refresh", stdout_path=evidence / "refresh-publish.log")
            steps.append(
                {"step": "warehouse-refresh", "seconds": round(time.monotonic() - started, 3)}
            )
            steps.append(
                _receipt(
                    evidence,
                    "refresh",
                    lambda: refresh.run(
                        "verifier",
                        *verify,
                        "refresh",
                        "--platform-api",
                        "http://platform-api:8000",
                        "--output",
                        "-",
                    ),
                )
            )
            _peaks(
                runner,
                refresh.project,
                ("postgres", "clickhouse", "collector", "platform-api"),
                evidence,
                "refresh",
            )
        refresh.down()

        # Restore: Postgres dump into a NEW volume; graph reimported from the immutable source.
        restore_volumes = planned_volumes(instance, RESTORE_SUFFIXES)
        create_volumes(runner, ledger, restore_volumes)
        restore_env = {
            **env,
            "RECKONER_V1_POSTGRES_VOLUME": restore_volumes[0],
            "RECKONER_V1_NEO4J_VOLUME": restore_volumes[1],
        }
        restored = Compose(runner, ledger, f"{instance}-restore", "prepare", restore_env)
        projects.append(restored)
        started = time.monotonic()
        restored("up", "-d", "--wait", "postgres", "neo4j")
        restored(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-q",
            input_path=_restore_globals(evidence),
        )
        restored(
            "exec",
            "-T",
            "postgres",
            "pg_restore",
            "-U",
            "postgres",
            "-d",
            DATABASE,
            "--exit-on-error",
            input_path=evidence / "postgres.dump",
        )
        steps.append(_receipt(evidence, "restore-migrate", lambda: restored.run("migrate")))
        steps.append(
            _receipt(
                evidence,
                "restore-graph",
                lambda: restored.run("owner", *smoke, "graph", "--env-file", "-"),
            )
        )
        steps.append(
            _receipt(
                evidence,
                "fingerprint-restore",
                lambda: restored.run(
                    "verifier", *verify, "fingerprint", "--graph", "--output", "-"
                ),
            )
        )
        steps.append({"step": "restore-total", "seconds": round(time.monotonic() - started, 3)})
        restored.down()
        served = Compose(runner, ledger, f"{instance}-restore", "online", restore_env)
        served("up", "-d", "--wait", "api", "web")
        steps.append(
            _receipt(
                evidence,
                "online-restored",
                lambda: served.run("verifier", *verify, "online", *endpoints),
            )
        )
        served.down()
        comparison = compare_restore(evidence)
        _write(evidence / "restore-comparison.json", json.dumps(comparison, indent=2))
        sizes = _volume_sizes(runner, ledger, evidence)
        _disk(runner, evidence, "after")
        summary = {
            "schema_version": "reckoner-v1-compose-smoke-v1",
            "dataset": "fabricated simulated; no provider calls",
            "instance": instance,
            "steps": steps,
            "volume_bytes": sizes,
            "restore": comparison,
            "resources": resource_summary(evidence),
        }
        _write(evidence / "summary.json", json.dumps(summary, indent=2, sort_keys=True))
        if not comparison["all_equal"]:
            raise Refused("restored or reimported state differs from the original")
    except BaseException:
        for project in projects:
            try:
                project.down()  # Containers and networks only; volumes stay for inspection.
            except subprocess.CalledProcessError:
                pass
        _write(evidence / "failure-steps.json", json.dumps(steps, indent=2))
        raise
    if options.cleanup:
        cleanup(runner, ledger)
    return summary


def compare_restore(evidence: Path) -> dict:
    """Restored Postgres equals the dumped state; reimported source graph equals the import."""
    online = _json(evidence, "fingerprint-online")["postgres"]
    restored = _json(evidence, "fingerprint-restore")
    original_graph = _json(evidence, "fingerprint-prepare")["graph"]
    graph = restored["graph"]
    keys = ("source_nodes", "source_sha256", "relationships", "relationships_sha256")
    result = {
        "postgres_tables_equal": online["tables"] == restored["postgres"]["tables"],
        "postgres_roles_equal": online["roles"] == restored["postgres"]["roles"],
        "postgres_table_count": len(online["tables"]),
        "graph_source_equal": {k: graph[k] for k in keys} == {k: original_graph[k] for k in keys},
        "graph_projection_deterministic_fields_equal": graph["projections"]
        == original_graph["projections"],
        "raw_clickhouse_disaster_restore": "not claimed; only the published warehouse "
        "snapshot procedure is tested elsewhere",
    }
    result["all_equal"] = all(v for k, v in result.items() if k.endswith("_equal"))
    return result


def kind_config(plugin_dir: Path, sentinel: str) -> dict:
    image = re.search(
        r"image: (kindest/node:\S+@sha256:[0-9a-f]{64})", (ROOT / "infra/kind.yaml").read_text()
    ).group(1)
    return {
        "kind": "Cluster",
        "apiVersion": "kind.x-k8s.io/v1alpha4",
        "nodes": [
            {
                "role": "control-plane",
                "image": image,
                # A node label set at creation proves ownership to cleanup.
                "labels": {NODE_SENTINEL: sentinel},
                "extraMounts": [
                    {
                        "hostPath": str(Path(plugin_dir).absolute()),
                        "containerPath": "/touchstone/gds-plugins",
                        "readOnly": True,
                    }
                ],
            }
        ],
    }


def _wait_job(kube, name, timeout=900):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = kube(
            "-n",
            NAMESPACE,
            "get",
            "job",
            name,
            "-o",
            "jsonpath={.status.succeeded},{.status.failed}",
        )
        succeeded, _, failed = state.partition(",")
        if succeeded == "1":
            return True
        if failed and failed != "0":
            return False
        time.sleep(3)
    raise TimeoutError(f"job {name} did not finish")


def _job(kube, evidence: Path, manifest: str, stage: str, name: str, containers=None, label=None):
    kube("-n", NAMESPACE, "delete", "job", name, "--ignore-not-found", "--wait=true")
    kube("apply", "-f", K8S / manifest, "-l", f"touchstone.dev/stage={stage}")
    started = time.monotonic()
    passed = _wait_job(kube, name)
    for container in containers or [None]:
        args = ["-n", NAMESPACE, "logs", f"job/{name}"]
        stem = label or f"kind-{name}"
        target = stem if container is None else f"{stem}-{container}"
        if container:
            args += ["-c", container]
        try:
            _write(evidence / f"{target}.json", kube(*args))
        except subprocess.CalledProcessError as error:
            _write(evidence / f"{target}.failed.txt", error.stderr or "")
    if not passed:
        raise RuntimeError(f"kind job {name} failed")
    return {"step": f"kind-{name}", "seconds": round(time.monotonic() - started, 3)}


def _kind_steps(options, runner, ledger, evidence, config, kubeconfig, kind_bin):
    instance, steps = ledger.data["instance"], []
    started = time.monotonic()
    runner(
        [
            str(kind_bin),
            "create",
            "cluster",
            "--name",
            instance,
            "--config",
            str(config),
            "--kubeconfig",
            str(kubeconfig),
        ]
    )
    steps.append({"step": "kind-create", "seconds": round(time.monotonic() - started, 3)})
    guard_disk(evidence, "after-cluster-create")
    kube = Kubectl(kubeconfig, instance, runner)
    with Sampler(runner, evidence / "stats-kind.jsonl", instance, options.sample_interval):
        started = time.monotonic()
        for image in (IMAGES["reckoner"], IMAGES["web"], *PINNED):
            archive = Path("/private/tmp") / f"{instance}-{image.split(':')[0]}.tar"
            runner(
                ["docker", "image", "save", "--platform", "linux/arm64", "-o", str(archive), image]
            )
            try:
                guard_disk(evidence, f"archive-{image}")
                runner([str(kind_bin), "load", "image-archive", "--name", instance, str(archive)])
            finally:
                archive.unlink(missing_ok=True)
            guard_disk(evidence, f"loaded-{image}")
        steps.append({"step": "kind-load-images", "seconds": round(time.monotonic() - started, 3)})
        kube("apply", "-f", K8S / "namespace.yaml")
        kube("label", "namespace", NAMESPACE, f"{SENTINEL}={ledger.sentinel}")
        for name, source in (
            ("reckoner-v1-postgres", "postgres.env"),
            ("reckoner-v1-provision", "provision.env"),
            ("reckoner-v1-runner", "runner.env"),
            ("reckoner-v1-api", "api.env"),
            ("reckoner-v1-neo4j-server", "neo4j-server.env"),
            ("reckoner-v1-neo4j", "neo4j.env"),
        ):
            kube(
                "-n",
                NAMESPACE,
                "create",
                "secret",
                "generic",
                name,
                f"--from-env-file={evidence / 'secrets' / source}",
            )
        kube(
            "-n",
            NAMESPACE,
            "create",
            "configmap",
            "reckoner-v1-verify",
            f"--from-file=verify_reckoner_v1_smoke.py={VERIFY}",
        )
        started = time.monotonic()
        for manifest in ("postgres.yaml", "neo4j.yaml"):
            kube("apply", "-f", K8S / manifest)
        for name in ("postgres", "neo4j"):
            kube("-n", NAMESPACE, "rollout", "status", f"statefulset/{name}", "--timeout=600s")
        steps.append({"step": "kind-stores-ready", "seconds": round(time.monotonic() - started, 3)})
        guard_disk(evidence, "stores-ready")
        steps.append(
            _job(
                kube,
                evidence,
                "worker.yaml",
                "prepare",
                "reckoner-v1-prepare",
                ["migrate", "seed", "graph", "evidence", "fingerprint"],
            )
        )
        guard_disk(evidence, "prepare-job")
        kube("-n", NAMESPACE, "scale", "statefulset/neo4j", "--replicas=0")
        kube("-n", NAMESPACE, "wait", "--for=delete", "pod/neo4j-0", "--timeout=300s")
        started = time.monotonic()
        for manifest in ("api.yaml", "web.yaml"):
            kube("apply", "-f", K8S / manifest)
        for name in ("api", "web"):
            kube("-n", NAMESPACE, "rollout", "status", f"deployment/{name}", "--timeout=600s")
        steps.append({"step": "kind-online-ready", "seconds": round(time.monotonic() - started, 3)})
        guard_disk(evidence, "online-ready")
        steps.append(_job(kube, evidence, "worker.yaml", "online", "reckoner-v1-run"))
        steps.append(
            _job(
                kube,
                evidence,
                "smoke-job.yaml",
                "verify-online",
                "reckoner-v1-verify-online",
                label="kind-online",
            )
        )
        # Persistent storage and restart: the Postgres pod is replaced on its own volume.
        started = time.monotonic()
        kube("-n", NAMESPACE, "delete", "pod", "postgres-0", "--wait=true")
        kube("-n", NAMESPACE, "rollout", "status", "statefulset/postgres", "--timeout=300s")
        kube(
            "-n",
            NAMESPACE,
            "wait",
            "--for=condition=Ready",
            "pod",
            "-l",
            "app=api",
            "--timeout=300s",
        )
        steps.append(
            {"step": "kind-postgres-restart", "seconds": round(time.monotonic() - started, 3)}
        )
        _write(evidence / "kind-restart.marker", "after postgres pod replacement")
        steps.append(
            _job(
                kube,
                evidence,
                "smoke-job.yaml",
                "verify-online",
                "reckoner-v1-verify-online",
                label="kind-online-after-restart",
            )
        )
        kube("-n", NAMESPACE, "scale", "statefulset/postgres", "--replicas=0")
        kube("-n", NAMESPACE, "wait", "--for=delete", "pod/postgres-0", "--timeout=300s")
        steps.append(
            _job(kube, evidence, "smoke-job.yaml", "verify-stopped", "reckoner-v1-verify-stopped")
        )
        kube("-n", NAMESPACE, "scale", "statefulset/postgres", "--replicas=1")
        kube("-n", NAMESPACE, "rollout", "status", "statefulset/postgres", "--timeout=300s")
        kube(
            "-n",
            NAMESPACE,
            "wait",
            "--for=condition=Ready",
            "pod",
            "-l",
            "app=api",
            "--timeout=300s",
        )
        steps.append(
            _job(kube, evidence, "smoke-job.yaml", "fingerprint", "reckoner-v1-fingerprint")
        )
        _write(evidence / "kind-pods.txt", kube("-n", NAMESPACE, "get", "pods,pvc", "-o", "wide"))
        try:
            _write(
                evidence / "kind-crictl-stats.json",
                runner(
                    ["docker", "exec", f"{instance}-control-plane", "crictl", "stats", "-o", "json"]
                ),
            )
        except subprocess.CalledProcessError:
            pass
    guard_disk(evidence, "end")
    return steps


def _kind_diagnostics(runner, kubeconfig, instance, evidence):
    """Best-effort state capture before any cleanup; never raises."""
    kube = Kubectl(kubeconfig, instance, runner)
    for name, args in (
        ("kind-failure-pods.txt", ("-n", NAMESPACE, "get", "pods,pvc,jobs", "-o", "wide")),
        ("kind-failure-describe.txt", ("-n", NAMESPACE, "describe", "pods")),
        ("kind-failure-events.txt", ("-n", NAMESPACE, "get", "events", "--sort-by=.lastTimestamp")),
    ):
        try:
            _write(evidence / name, kube(*args))
        except (subprocess.CalledProcessError, OSError):
            pass


def kind_smoke(
    options: Options,
    kubeconfig: Path,
    kind_bin: Path,
    kind_sha256: str,
    runner=run_command,
    preflight=True,
) -> dict:
    instance = validate_instance(options.instance)
    kubeconfig = validate_kubeconfig(kubeconfig, os.environ)
    verify_file(kind_bin, kind_sha256)
    evidence = Path(options.evidence).absolute()
    ledger = Ledger.create(evidence, instance)
    verify_file(Path(options.plugin_dir) / GDS_JAR, options.plugin_sha256)
    if preflight:
        preflight_docker(runner, instance, [IMAGES["reckoner"], IMAGES["web"], *PINNED.values()])
        if instance in runner([str(kind_bin), "get", "clusters"]).split():
            raise Refused(f"kind cluster {instance} already exists")
    _disk(runner, evidence, "before")
    write_secrets(evidence / "secrets")
    config = evidence / "kind-config.json"
    _write(config, json.dumps(kind_config(options.plugin_dir, ledger.sentinel), indent=2))
    ledger.data["kubeconfig"] = str(kubeconfig)
    ledger.save()
    for tag, source in PINNED.items():
        runner(["docker", "tag", source, tag])
        if runner(["docker", "image", "inspect", "--format", "{{.Id}}", tag]) != runner(
            ["docker", "image", "inspect", "--format", "{{.Id}}", source]
        ):
            raise Refused(f"{tag} is not the pinned image {source}")
    steps = []
    guard_disk(evidence, "before-cluster")
    ledger.add("clusters", instance)
    try:
        steps += _kind_steps(options, runner, ledger, evidence, config, kubeconfig, kind_bin)
    except BaseException as error:
        _kind_diagnostics(runner, kubeconfig, instance, evidence)
        if options.cleanup or isinstance(error, DiskFloor):
            try:
                cleanup(runner, ledger, kind_bin)
            except (Refused, subprocess.CalledProcessError) as failed:
                # Ownership unproven (for example a half-created cluster): nothing is deleted.
                _write(evidence / "kind-cleanup-refused.txt", repr(failed))
        raise
    _disk(runner, evidence, "after")
    summary = {
        "schema_version": "reckoner-v1-kind-smoke-v1",
        "dataset": "fabricated simulated; no provider calls",
        "instance": instance,
        "kubeconfig": str(kubeconfig),
        "context": f"kind-{instance}",
        "steps": steps,
        "resources": resource_summary(evidence),
    }
    _write(evidence / "summary.json", json.dumps(summary, indent=2, sort_keys=True))
    if options.cleanup:
        cleanup(runner, ledger, kind_bin)
        guard_disk(evidence, "after-cleanup")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("compose", "kind"):
        command = commands.add_parser(name)
        command.add_argument("--instance", required=True)
        command.add_argument("--evidence-dir", type=Path, required=True)
        command.add_argument("--plugin-dir", type=Path, required=True)
        command.add_argument(
            "--cleanup", action="store_true", help="remove this run's own resources after success"
        )
        command.add_argument("--sample-interval", type=float, default=2.0)
    kind = commands.choices["kind"]
    kind.add_argument("--kubeconfig", type=Path, required=True)
    kind.add_argument("--kind-bin", type=Path, required=True)
    kind.add_argument("--kind-sha256", required=True)
    clean = commands.add_parser("cleanup")
    clean.add_argument("--evidence-dir", type=Path, required=True)
    clean.add_argument("--kind-bin", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "cleanup":
            cleanup(run_command, Ledger.load(args.evidence_dir), args.kind_bin)
            return 0
        options = Options(
            args.instance,
            args.evidence_dir,
            args.plugin_dir,
            cleanup=args.cleanup,
            sample_interval=args.sample_interval,
        )
        if args.command == "compose":
            result = compose_smoke(options)
        else:
            result = kind_smoke(options, args.kubeconfig, args.kind_bin, args.kind_sha256)
    except Refused as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"instance": result["instance"], "steps": len(result["steps"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
