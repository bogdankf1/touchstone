"""Build and atomically publish verified immutable warehouse generations."""

from __future__ import annotations

import csv
import fcntl
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import clickhouse_connect
import duckdb

from touchstone_platform.extract import iter_declarations, iter_measurements
from touchstone_platform.settings import Settings
from touchstone_platform.staging import StagingReceipt, build_snapshot

DBT_PROJECT = Path(__file__).resolve().parents[2] / "dbt"


class RefreshBusy(RuntimeError):
    """Another refresh holds the exclusive publication lock."""


@dataclass(frozen=True)
class RefreshResult:
    generation: str
    cutoff: str
    accepted_measurements: int
    accepted_declarations: int
    rejected_count: int
    runs: tuple[dict, ...]
    status_warning: str | None = None


def _write_json(path: Path, value: dict, *, after_replace=None) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if after_replace is not None:
            after_replace()
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(
        command, cwd=DBT_PROJECT, env=env, capture_output=True, text=True, check=False
    )
    if result.returncode:
        details = f"{result.stdout[-3000:]} {result.stderr[-1000:]}"
        raise RuntimeError(f"{' '.join(command[:2])} failed: {details}")


def _build_generation(
    settings: Settings, target: Path, cutoff: datetime
) -> tuple[StagingReceipt, tuple[dict, ...]]:
    client = clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_username,
        password=settings.clickhouse_password,
        database=settings.clickhouse_database,
    )
    try:
        receipt = build_snapshot(
            iter_measurements(client, through=cutoff, page_size=settings.extraction_page_size),
            iter_declarations(client, through=cutoff, page_size=settings.extraction_page_size),
            target,
            through=cutoff,
        )
    finally:
        client.close()

    with tempfile.TemporaryDirectory(prefix="dbt-", dir=settings.warehouse_dir) as temporary:
        profiles = Path(temporary) / "profiles.yml"
        profiles.write_text(
            "touchstone:\n  target: local\n  outputs:\n    local:\n"
            f"      type: duckdb\n      path: {target}\n      schema: main\n"
            f"      threads: {settings.dbt_threads}\n"
            f"      settings:\n        memory_limit: '{settings.duckdb_memory_limit}'\n"
        )
        env = os.environ.copy()
        env["DBT_PROFILES_DIR"] = temporary
        _run(
            ["dbt", "build", "--project-dir", str(DBT_PROJECT), "--profiles-dir", temporary],
            env=env,
        )
        semantic_csv = Path(temporary) / "semantic-components.csv"
        _run(
            [
                "mf",
                "query",
                "--metrics",
                "model_cost,correct_tasks,expected_tasks",
                "--group-by",
                "run__tenant_id,run__workflow_id,run__run_id",
                "--decimals",
                "12",
                "--csv",
                str(semantic_csv),
            ],
            env=env,
        )
        with semantic_csv.open(newline="") as stream:
            semantic_rows = {
                (row["run__tenant_id"], row["run__workflow_id"], row["run__run_id"]): row
                for row in csv.DictReader(stream)
            }

    with duckdb.connect(str(target), read_only=True) as connection:
        rows = connection.execute(
            "select tenant_id, workflow_id, run_id, metrics_complete, "
            "expected_tasks, received_tasks, completed_tasks, failed_tasks, "
            "missing_tasks, missing_outcomes, missing_checks, conflicting_checks, "
            "model_cost, correct_tasks "
            "from mart_runs"
        ).fetchall()
    for row in rows:
        if not row[3]:
            continue
        semantic = semantic_rows.get((row[0], row[1], row[2]))
        if semantic is None or (
            Decimal(semantic["model_cost"]) != row[12]
            or int(semantic["correct_tasks"]) != row[13]
            or int(semantic["expected_tasks"]) != row[4]
        ):
            raise RuntimeError("MetricFlow component mismatch with governed mart")
    runs = tuple(
        {
            "tenant_id": row[0],
            "workflow_id": row[1],
            "run_id": row[2],
            "metrics_complete": row[3],
            "expected_tasks": row[4],
            "received_tasks": row[5],
            "completed_tasks": row[6],
            "failed_tasks": row[7],
            "missing_tasks": row[8],
            "missing_outcomes": row[9],
            "missing_checks": row[10],
            "conflicting_checks": row[11],
        }
        for row in rows
    )
    return receipt, runs


def refresh(settings: Settings) -> RefreshResult:
    """Reject overlap; publish only after dbt, Elementary and MetricFlow succeed."""
    settings = replace(settings, warehouse_dir=settings.warehouse_dir.resolve())
    settings.warehouse_dir.mkdir(parents=True, exist_ok=True)
    with (settings.warehouse_dir / "refresh.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RefreshBusy("warehouse refresh already in progress") from error
        cutoff = datetime.now(UTC)
        generation = f"generation-{uuid4().hex}.duckdb"
        working = settings.warehouse_dir / f"working_{uuid4().hex}.duckdb"
        receipt = None

        def failed_status(error: Exception) -> None:
            accepted_measurements = receipt.accepted_measurements if receipt else None
            accepted_declarations = receipt.accepted_declarations if receipt else None
            _write_json(
                settings.warehouse_dir / "refresh-status.json",
                {
                    "state": "failed",
                    "attempted_at": cutoff.isoformat(),
                    "cutoff": cutoff.isoformat(),
                    "accepted_measurements": accepted_measurements,
                    "accepted_declarations": accepted_declarations,
                    "rejected_count": receipt.rejected_count if receipt else None,
                    "error": str(error),
                },
            )

        try:
            receipt, runs = _build_generation(settings, working, cutoff)
            if Path(f"{working}.wal").exists():
                raise RuntimeError("working warehouse still has an open WAL")
            published = settings.warehouse_dir / generation
            os.replace(working, published)
            result = RefreshResult(
                generation,
                receipt.cutoff,
                receipt.accepted_measurements,
                receipt.accepted_declarations,
                receipt.rejected_count,
                runs,
            )
        except Exception as error:
            working.unlink(missing_ok=True)
            failed_status(error)
            raise
        published_manifest = False

        def mark_published() -> None:
            nonlocal published_manifest
            published_manifest = True

        warning = None
        try:
            _write_json(
                settings.warehouse_dir / "current.json",
                {
                    **asdict(result),
                    "published_at": datetime.now(UTC).isoformat(),
                    "warehouse": "DuckDB local preview",
                },
                after_replace=mark_published,
            )
        except Exception as error:
            if not published_manifest:
                failed_status(error)
                raise
            warning = str(error)
        try:
            _write_json(
                settings.warehouse_dir / "refresh-status.json",
                {
                    "state": "published_with_warning" if warning else "succeeded",
                    "attempted_at": cutoff.isoformat(),
                    "generation": generation,
                    "cutoff": receipt.cutoff,
                    "accepted_measurements": receipt.accepted_measurements,
                    "accepted_declarations": receipt.accepted_declarations,
                    "rejected_count": receipt.rejected_count,
                    "warning": warning,
                },
            )
        except Exception as error:
            warning = f"refresh status could not be written: {error}"
        return replace(result, status_warning=warning)


@contextmanager
def open_published_snapshot(settings: Settings, *, manifest: dict | None = None):
    """Pin one request to one immutable generation for its whole read."""
    if manifest is None:
        manifest = json.loads((settings.warehouse_dir / "current.json").read_text())
    generation = manifest["generation"]
    if (
        Path(generation).name != generation
        or not generation.startswith("generation-")
        or not generation.endswith(".duckdb")
    ):
        raise ValueError("invalid published generation")
    with duckdb.connect(str(settings.warehouse_dir / generation), read_only=True) as connection:
        yield connection
