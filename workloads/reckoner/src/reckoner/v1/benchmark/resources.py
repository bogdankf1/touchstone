"""Resource guards, disk reconciliation, volume sizing and coarse service sampling."""

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

GIB = 1024**3
FREE_FLOOR = 15 * GIB
DERIVED_CAP = 20 * GIB
RECONCILIATION_SCHEMA = "phase3-disk-reconciliation-v1"
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}


class ResourceGuardError(ValueError):
    pass


def parse_stores(declared: list[str], measured: dict) -> dict:
    """Declared sizes are explicit operator inputs; measured sizes come from the volumes."""
    stores = {name: {"bytes": size, "source": "measured"} for name, size in measured.items()}
    for item in declared:
        name, separator, value = item.partition("=")
        if not separator or not name or not value.isdigit():
            raise ValueError("declared store size must be NAME=BYTES")
        if name in stores:
            raise ValueError(f"store {name} declared twice")
        stores[name] = {"bytes": int(value), "source": "declared"}
    return dict(sorted(stores.items()))


def resource_guard(
    *,
    free_bytes: int,
    artifact_bytes: int,
    stores: dict,
    free_floor: int = FREE_FLOOR,
    derived_cap: int = DERIVED_CAP,
) -> dict:
    record = {
        "free_bytes": free_bytes,
        "artifact_bytes": artifact_bytes,
        "stores": stores,
        "derived_bytes": artifact_bytes + sum(s["bytes"] for s in stores.values()),
        "free_floor_bytes": free_floor,
        "derived_cap_bytes": derived_cap,
    }
    if free_bytes < free_floor:
        raise ResourceGuardError(f"free disk below floor: {json.dumps(record)}")
    if record["derived_bytes"] > derived_cap:
        raise ResourceGuardError(f"derived data above cap: {json.dumps(record)}")
    return record


def disk_reconciliation(
    *,
    artifact_bytes: int,
    volume_bytes: dict,
    free_bytes: int,
    containers: list,
    free_floor: int = FREE_FLOOR,
    derived_cap: int = DERIVED_CAP,
) -> dict:
    record = {
        "schema_version": RECONCILIATION_SCHEMA,
        "artifact_bytes": artifact_bytes,
        "volume_bytes": dict(sorted(volume_bytes.items())),
        "total_derived_bytes": artifact_bytes + sum(volume_bytes.values()),
        "free_bytes": free_bytes,
        "free_floor_bytes": free_floor,
        "derived_cap_bytes": derived_cap,
        "containers": containers,
    }
    if free_bytes < free_floor or record["total_derived_bytes"] > derived_cap:
        raise ResourceGuardError(f"resource limits exceeded: {json.dumps(record)}")
    return record


def parse_du(rows, volumes: list[str]) -> dict:
    sizes = {}
    for row in rows:
        kibibytes, path = row.split(maxsplit=1)
        sizes[volumes[int(path.rsplit("/", 1)[-1])]] = int(kibibytes) * 1024
    if len(sizes) != len(volumes):
        raise ValueError("volume measurement incomplete")
    return sizes


def tree_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in Path(root).rglob("*") if p.is_file())


def volume_bytes(volumes: dict, image: str) -> dict:
    """Measure named volumes read-only, so stopped stores are measured, never assumed."""
    names = sorted(volumes)
    command = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--memory=128m",
        "--entrypoint",
        "du",
    ]
    for index, name in enumerate(names):
        command += ["-v", f"{volumes[name]}:/audit/{index}:ro"]
    command += [image, "-sk", *[f"/audit/{index}" for index in range(len(names))]]
    rows = subprocess.check_output(command, text=True).splitlines()
    return parse_du(rows, names)


def memory_bytes(usage: str) -> int:
    """Parse the used side of docker stats MemUsage, e.g. ``2.056GiB / 4GiB``."""
    used = usage.split("/", 1)[0].strip()
    for unit, factor in sorted(UNITS.items(), key=lambda item: -len(item[0])):
        number = used.removesuffix(unit)
        if number != used:
            try:
                return int(float(number) * factor)
            except ValueError:
                break
    raise ValueError(f"unrecognised memory usage {usage!r}")


def sample_maxima(rows: list[dict]) -> dict:
    maxima = {}
    for row in rows:
        maxima[row["Name"]] = max(maxima.get(row["Name"], 0), memory_bytes(row["MemUsage"]))
    return dict(sorted(maxima.items()))


class Guard:
    def __init__(self, args):
        if args.store_volume and not args.du_image:
            raise ValueError("--store-volume requires --du-image")
        self.args = args
        self.volumes = {}
        for item in args.store_volume:
            name, separator, volume = item.partition("=")
            if not separator or not name or not volume or name in self.volumes:
                raise ValueError(f"store volume must be a unique NAME=VOLUME, got {item!r}")
            self.volumes[name] = volume

    def __call__(self, connection=None) -> dict:
        measured = volume_bytes(self.volumes, self.args.du_image) if self.volumes else {}
        record = resource_guard(
            free_bytes=shutil.disk_usage(self.args.output_dir).free,
            artifact_bytes=tree_bytes(self.args.artifact_root),
            stores=parse_stores(self.args.store_bytes, measured),
            free_floor=self.args.free_floor_bytes,
            derived_cap=self.args.derived_cap_bytes,
        )
        if connection is not None:
            record["database_bytes"] = connection.execute(
                "SELECT pg_database_size(current_database())"
            ).fetchone()[0]
        return record


class Sampler:
    """Coarse docker stats observations; they are samples, not exact peaks."""

    def __init__(self, path: Path, containers: list[str], interval: float):
        self.path, self.containers, self.interval = path, containers, interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run)
        self.started = time.perf_counter()
        self.error = None

    def _run(self):
        try:
            self._sample()
        except BaseException as error:  # Re-raised by __exit__; never silently lost.
            self.error = error

    def _sample(self):
        with self.path.open("x") as handle:
            while True:  # At least one sample, then one per interval until stopped.
                raw = subprocess.check_output(
                    ["docker", "stats", "--no-stream", "--format", "{{json .}}", *self.containers],
                    text=True,
                )
                elapsed = time.perf_counter() - self.started
                for line in raw.splitlines():
                    handle.write(json.dumps({"elapsed_seconds": elapsed, **json.loads(line)}))
                    handle.write("\n")
                handle.flush()
                if self.stop_event.wait(self.interval):
                    break

    def __enter__(self):
        if self.containers:
            self.thread.start()
        return self

    def __exit__(self, exc_type, *_):
        self.stop_event.set()
        if self.containers:
            self.thread.join()
        if self.error is not None and exc_type is None:
            raise RuntimeError("service sampling failed; resource evidence incomplete") from (
                self.error
            )
