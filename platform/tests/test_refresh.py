"""Atomic local publication keeps readers on a complete generation."""

import json
import os
import stat
from pathlib import Path

import duckdb
import pytest
from test_metrics import declaration, event
from touchstone_platform.refresh import (
    RefreshBusy,
    open_published_snapshot,
    refresh,
)
from touchstone_platform.settings import Settings
from touchstone_platform.staging import StagingReceipt


def settings(tmp_path):
    return Settings(warehouse_dir=tmp_path)


def publish_fixture(directory: Path, generation: str, value: int):
    warehouse = directory / f"generation-{generation}.duckdb"
    with duckdb.connect(str(warehouse)) as connection:
        connection.execute("create table marker(value integer)")
        connection.execute("insert into marker values (?)", [value])
    (directory / "current.json").write_text(json.dumps({"generation": warehouse.name}))
    return warehouse


def test_reader_keeps_old_generation_after_failed_build(tmp_path, monkeypatch):
    publish_fixture(tmp_path, "old", 1)
    with open_published_snapshot(settings(tmp_path)) as old:

        def fail(*args, **kwargs):
            raise RuntimeError("dbt failed")

        monkeypatch.setattr("touchstone_platform.refresh._build_generation", fail)
        with pytest.raises(RuntimeError, match="dbt failed"):
            refresh(settings(tmp_path))
        assert old.execute("select value from marker").fetchone()[0] == 1
        with open_published_snapshot(settings(tmp_path)) as current:
            assert current.execute("select value from marker").fetchone()[0] == 1
    status = json.loads((tmp_path / "refresh-status.json").read_text())
    assert status["state"] == "failed"


def test_crash_before_publish_keeps_previous_manifest(tmp_path, monkeypatch):
    publish_fixture(tmp_path, "old", 1)

    def crash_after_build(_settings, target, _cutoff):
        with duckdb.connect(str(target)) as connection:
            connection.execute("create table marker(value integer)")
            connection.execute("insert into marker values (2)")
        raise RuntimeError("verification failed")

    monkeypatch.setattr("touchstone_platform.refresh._build_generation", crash_after_build)
    with pytest.raises(RuntimeError, match="verification failed"):
        refresh(settings(tmp_path))
    assert (
        json.loads((tmp_path / "current.json").read_text())["generation"] == "generation-old.duckdb"
    )


def test_overlapping_refresh_is_rejected(tmp_path, monkeypatch):
    publish_fixture(tmp_path, "old", 1)

    def nested(_settings, _target, _cutoff):
        with pytest.raises(RefreshBusy):
            refresh(settings(tmp_path))
        raise RuntimeError("stop outer")

    monkeypatch.setattr("touchstone_platform.refresh._build_generation", nested)
    with pytest.raises(RuntimeError, match="stop outer"):
        refresh(settings(tmp_path))


def test_single_response_uses_one_generation(tmp_path):
    publish_fixture(tmp_path, "old", 1)
    with open_published_snapshot(settings(tmp_path)) as reader:
        newer = publish_fixture(tmp_path, "new", 2)
        assert newer.exists()
        assert reader.execute("select value from marker").fetchone()[0] == 1
        assert reader.execute("select value from marker").fetchone()[0] == 1
    with open_published_snapshot(settings(tmp_path)) as reader:
        assert reader.execute("select value from marker").fetchone()[0] == 2


def test_refresh_publishes_verified_generation_with_shared_cutoff(tmp_path, monkeypatch):
    cutoffs = []

    class Client:
        def close(self):
            pass

    monkeypatch.setattr(
        "touchstone_platform.refresh.clickhouse_connect.get_client", lambda **_kwargs: Client()
    )

    def measurements(_client, *, through, page_size):
        cutoffs.append(through)
        return iter([event("execution", "a"), event("outcome", "a")])

    def declarations(_client, *, through, page_size):
        cutoffs.append(through)
        return iter([declaration(("a",))])

    monkeypatch.setattr("touchstone_platform.refresh.iter_measurements", measurements)
    monkeypatch.setattr("touchstone_platform.refresh.iter_declarations", declarations)
    result = refresh(settings(tmp_path))
    assert cutoffs[0] == cutoffs[1]
    assert result.accepted_measurements == 2
    assert result.accepted_declarations == 1
    with open_published_snapshot(settings(tmp_path)) as connection:
        assert connection.execute("select correct_tasks from mart_runs").fetchone()[0] == 1
    manifest = json.loads((tmp_path / "current.json").read_text())
    assert manifest["generation"] == result.generation
    assert manifest["runs"][0]["tenant_id"] == "tenant-fixture-a"
    assert json.loads((tmp_path / "refresh-status.json").read_text())["state"] == "succeeded"


def test_dagster_schedule_is_disabled_until_enabled_by_deployment(monkeypatch):
    from touchstone_platform.orchestration.definitions import make_definitions

    disabled = make_definitions(Settings(schedule_enabled=False))
    enabled = make_definitions(Settings(schedule_enabled=True))
    assert len(disabled.schedules) == 0
    assert len(enabled.schedules) == 1
    assert enabled.schedules[0].cron_schedule == "*/15 * * * *"


def test_semantic_component_mismatch_blocks_publication(tmp_path, monkeypatch):
    import touchstone_platform.refresh as module

    class Client:
        def close(self):
            pass

    monkeypatch.setattr(module.clickhouse_connect, "get_client", lambda **_kwargs: Client())
    monkeypatch.setattr(
        module,
        "iter_measurements",
        lambda *_args, **_kwargs: iter(
            [
                event("execution", "a"),
                event("outcome", "a"),
                event("provider_usage", "a", cost_amount="0.1"),
            ]
        ),
    )
    monkeypatch.setattr(
        module, "iter_declarations", lambda *_args, **_kwargs: iter([declaration(("a",))])
    )
    actual_run = module._run

    def wrong_semantic_result(command, *, env=None):
        if command[0] == "mf":
            csv_path = Path(command[command.index("--csv") + 1])
            csv_path.write_text(
                "run__tenant_id,run__workflow_id,run__run_id,model_cost,correct_tasks,expected_tasks\n"
                "tenant-fixture-a,reckoner,run-fixture-1,999,1,1\n"
            )
            return
        actual_run(command, env=env)

    monkeypatch.setattr(module, "_run", wrong_semantic_result)
    with pytest.raises(RuntimeError, match="MetricFlow component mismatch"):
        refresh(settings(tmp_path))
    assert not (tmp_path / "current.json").exists()
    assert json.loads((tmp_path / "refresh-status.json").read_text())["state"] == "failed"


def test_relative_warehouse_directory_is_resolved_before_dbt(tmp_path, monkeypatch):
    import touchstone_platform.refresh as module

    class Client:
        def close(self):
            pass

    monkeypatch.setattr(module.clickhouse_connect, "get_client", lambda **_kwargs: Client())
    monkeypatch.setattr(
        module,
        "iter_measurements",
        lambda *_args, **_kwargs: iter(
            [
                event("execution", "a"),
                event("outcome", "a"),
            ]
        ),
    )
    monkeypatch.setattr(
        module, "iter_declarations", lambda *_args, **_kwargs: iter([declaration(("a",))])
    )
    relative = Path(os.path.relpath(tmp_path, Path.cwd()))
    result = refresh(Settings(warehouse_dir=relative))
    assert (tmp_path / result.generation).exists()
    with open_published_snapshot(Settings(warehouse_dir=relative)) as connection:
        assert connection.execute("select correct_tasks from mart_runs").fetchone()[0] == 1


def _fake_generation(_settings, target, cutoff):
    with duckdb.connect(str(target)) as connection:
        connection.execute("create table marker(value integer)")
        connection.execute("insert into marker values (2)")
    return StagingReceipt(cutoff.isoformat(), 1, 1, 0), ()


def test_manifest_replace_failure_does_not_publish(tmp_path, monkeypatch):
    import touchstone_platform.refresh as module

    publish_fixture(tmp_path, "old", 1)
    monkeypatch.setattr(module, "_build_generation", _fake_generation)
    original_replace = module.os.replace

    def fail_manifest_replace(source, destination):
        if Path(destination).name == "current.json":
            raise OSError("manifest replacement failed")
        return original_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_manifest_replace)
    with pytest.raises(OSError, match="manifest replacement failed"):
        refresh(settings(tmp_path))
    assert (
        json.loads((tmp_path / "current.json").read_text())["generation"] == "generation-old.duckdb"
    )
    assert json.loads((tmp_path / "refresh-status.json").read_text())["state"] == "failed"


def test_manifest_directory_fsync_failure_is_post_commit(tmp_path, monkeypatch):
    import touchstone_platform.refresh as module

    publish_fixture(tmp_path, "old", 1)
    monkeypatch.setattr(module, "_build_generation", _fake_generation)
    original_fsync = module.os.fsync
    failed = False

    def fail_first_directory_fsync(fd):
        nonlocal failed
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not failed:
            failed = True
            raise OSError("directory fsync failed")
        return original_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", fail_first_directory_fsync)
    result = refresh(settings(tmp_path))
    assert result.status_warning is not None
    assert json.loads((tmp_path / "current.json").read_text())["generation"] == result.generation
    assert json.loads((tmp_path / "refresh-status.json").read_text())["state"] != "failed"


def test_status_write_failure_cannot_relabel_published_generation_failed(tmp_path, monkeypatch):
    import touchstone_platform.refresh as module

    publish_fixture(tmp_path, "old", 1)
    monkeypatch.setattr(module, "_build_generation", _fake_generation)
    original_write = module._write_json

    def fail_success_status(path, value, **kwargs):
        if Path(path).name == "refresh-status.json" and value["state"] == "succeeded":
            raise OSError("status write failed")
        return original_write(path, value, **kwargs)

    monkeypatch.setattr(module, "_write_json", fail_success_status)
    result = refresh(settings(tmp_path))
    assert result.status_warning is not None
    assert json.loads((tmp_path / "current.json").read_text())["generation"] == result.generation
    assert not (tmp_path / "refresh-status.json").exists()
