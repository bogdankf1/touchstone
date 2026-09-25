from __future__ import annotations

import importlib
import json
import shutil
from decimal import Decimal

import psycopg
import pytest
from conftest import CONFIG_DIR, seed_run
from reckoner.baseline.config import load_config
from reckoner.storage.budget import BudgetExceeded, BudgetLedger
from reckoner.storage.postgres import PostgresRepository

pytestmark = pytest.mark.integration


class FakeProvider:
    """Explicitly fake provider used only by isolated test-mode runs."""

    provider_name = "fake"
    is_fake = True

    def __init__(self, *, count: int = 31, result: dict | None = None):
        self.count = count
        self.result = result or {
            "provider_request_id": "req_fake_task4",
            "requested_model": "anthropic/claude-haiku-4-5-20251001",
            "reported_model": "claude-haiku-4-5-20251001",
            "finish_reason": "stop",
            "content": '{"outcome":"escalate"}',
            "input_tokens": 31,
            "output_tokens": 7,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }
        self.count_calls = 0
        self.generation_calls = 0

    def count_input(self, _request: dict) -> int:
        self.count_calls += 1
        return self.count

    def generate(self, _request: dict) -> dict:
        self.generation_calls += 1
        return dict(self.result)


class UncallableAnthropicProvider:
    """Boundary stub proving paid gates run before provider IO."""

    provider_name = "anthropic"
    is_fake = False

    def count_input(self, _request: dict) -> int:
        raise AssertionError("pilot gate must run before token counting")

    def generate(self, _request: dict) -> dict:
        raise AssertionError("pilot gate must run before generation")


class CountingAnthropicBoundary:
    """Non-fake boundary double for proving paid gates precede generation."""

    provider_name = "anthropic"
    is_fake = False

    def __init__(self):
        self.generation_calls = 0

    def count_input(self, _request: dict) -> int:
        return 31

    def generate(self, _request: dict) -> dict:
        self.generation_calls += 1
        return FakeProvider().result


def _runner():
    return importlib.import_module("reckoner.baseline.runner")


def _create_four_task_run(
    pg,
    run_id: str,
    *,
    execution_mode: str = "test",
    purpose: str = "baseline",
    task_limit: int | None = 4,
) -> None:
    config = load_config(CONFIG_DIR / "baseline-v1.json", CONFIG_DIR / "anthropic-prices-v1.json")
    prices = json.loads((CONFIG_DIR / "anthropic-prices-v1.json").read_text())
    bundle_id = json.loads((pg.bundle / "bundle.json").read_text())["bundle_id"]
    with PostgresRepository(pg.owner_dsn) as repo:
        repo.import_bundle(pg.bundle)
        for name in ("thresholds-tenant-a-v1.json", "thresholds-tenant-b-v1.json"):
            repo.register_threshold_config(json.loads((CONFIG_DIR / name).read_text()))
        repo.create_run(
            run_id,
            purpose,
            config,
            bundle_id,
            price=prices,
            execution_mode=execution_mode,
        )
    if task_limit is None:
        return
    with psycopg.connect(pg.owner_dsn) as connection:
        kept = connection.execute(
            "SELECT tenant_id, task_id FROM reckoner.tasks WHERE run_id = %s "
            "ORDER BY tenant_id, task_id LIMIT %s",
            (run_id, task_limit),
        ).fetchall()
        connection.execute(
            "DELETE FROM reckoner.tasks WHERE run_id = %s "
            "AND (tenant_id, task_id) NOT IN ("
            "SELECT * FROM unnest(%s::text[], %s::text[]))",
            (run_id, [row[0] for row in kept], [row[1] for row in kept]),
        )


def test_preflight_persists_requests_and_runner_never_regenerates_completed_tasks(pg):
    runner = _runner()
    _create_four_task_run(pg, "resume-four")
    provider = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        planned = runner.preflight(repo, provider, "resume-four")
        assert planned["pending"] == 4
        assert planned["reservation_total"] == "0.013676"
        assert all(task["request_sha256"] for task in repo.pending_tasks("resume-four"))

        first = runner.execute_run(repo, provider, "resume-four")
        calls = provider.generation_calls
        second = runner.execute_run(repo, provider, "resume-four")

        assert first == {"completed": 4, "failed": 0, "uncertain": 0, "pending": 0}
        assert second == first
        assert provider.generation_calls == calls == 4
        snapshot = repo.snapshot("resume-four", None)
        assert snapshot["completed"] == 4
        assert {run["status"] for run in snapshot["runs"]} == {"complete"}


def test_restart_after_durable_reservation_marks_uncertain_without_generation(pg, monkeypatch):
    runner = _runner()
    _create_four_task_run(pg, "crash-after-reserve")
    provider = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, provider, "crash-after-reserve")
        original = BudgetLedger.reserve

        def crash_after_reserve(ledger, task, maximum):
            original(ledger, task, maximum)
            raise SystemExit("synthetic crash after reservation")

        monkeypatch.setattr(BudgetLedger, "reserve", crash_after_reserve)
        with pytest.raises(SystemExit, match="after reservation"):
            runner.execute_run(repo, provider, "crash-after-reserve")

    monkeypatch.setattr(BudgetLedger, "reserve", original)
    restarted_provider = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        result = runner.execute_run(repo, restarted_provider, "crash-after-reserve")
        snapshot = repo.snapshot("crash-after-reserve", None)
    assert restarted_provider.generation_calls == 0
    assert result["uncertain"] == 1
    assert snapshot["attempts"][0]["status"] == "uncertain"
    assert snapshot["budget_entries"][0]["status"] == "uncertain"


def test_restart_after_provider_return_does_not_regenerate(pg, monkeypatch):
    runner = _runner()
    _create_four_task_run(pg, "crash-after-response")
    provider = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, provider, "crash-after-response")

        def crash_before_finish(*_args, **_kwargs):
            raise SystemExit("synthetic crash before finish")

        monkeypatch.setattr(repo, "finish_attempt", crash_before_finish)
        with pytest.raises(SystemExit, match="before finish"):
            runner.execute_run(repo, provider, "crash-after-response")
    assert provider.generation_calls == 1

    restarted_provider = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        result = runner.execute_run(repo, restarted_provider, "crash-after-response")
    assert restarted_provider.generation_calls == 0
    assert result["uncertain"] == 1


def test_unresolved_dispatch_in_another_run_blocks_all_new_generation(pg):
    runner = _runner()
    _create_four_task_run(pg, "unresolved-a", task_limit=1)
    _create_four_task_run(pg, "blocked-b", task_limit=1)
    with PostgresRepository(pg.runner_dsn) as repo:
        first_provider = FakeProvider()
        second_provider = FakeProvider()
        runner.preflight(repo, first_provider, "unresolved-a")
        runner.preflight(repo, second_provider, "blocked-b")
        task = repo.pending_tasks("unresolved-a")[0]
        BudgetLedger(repo).reserve(task, Decimal(task["reservation_cost"]))

        result = runner.execute_run(repo, second_provider, "blocked-b")
        unresolved = repo.snapshot("unresolved-a", None)

    assert second_provider.generation_calls == 0
    assert result["pending"] == 1
    assert unresolved["uncertain"] == 1


def test_model_or_cache_uncertainty_blocks_later_runs_globally(pg):
    runner = _runner()
    _create_four_task_run(pg, "mismatch-a", task_limit=1)
    _create_four_task_run(pg, "mismatch-b", task_limit=1)
    mismatched = FakeProvider().result | {"reported_model": "wrong-model"}
    with PostgresRepository(pg.runner_dsn) as repo:
        first_provider = FakeProvider(result=mismatched)
        second_provider = FakeProvider()
        runner.preflight(repo, first_provider, "mismatch-a")
        runner.preflight(repo, second_provider, "mismatch-b")
        assert runner.execute_run(repo, first_provider, "mismatch-a")["uncertain"] == 1
        result = runner.execute_run(repo, second_provider, "mismatch-b")

    assert second_provider.generation_calls == 0
    assert result["pending"] == 1


def test_failure_before_reservation_creates_no_dispatched_attempt(pg, monkeypatch):
    runner = _runner()
    _create_four_task_run(pg, "before-reservation")
    provider = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, provider, "before-reservation")

        def reject_before_reserve(*_args, **_kwargs):
            raise BudgetExceeded("synthetic insufficient budget")

        monkeypatch.setattr(BudgetLedger, "reserve", reject_before_reserve)
        result = runner.execute_run(repo, provider, "before-reservation")
        snapshot = repo.snapshot("before-reservation", None)
    assert result["pending"] == 4
    assert snapshot["attempts"] == []
    assert provider.generation_calls == 0


def test_insufficient_aggregate_budget_blocks_preflight_before_dispatch(pg):
    expensive = seed_run(pg, "prior-spend", "tenant-a")
    with PostgresRepository(pg.runner_dsn) as repo:
        reservation = BudgetLedger(repo).reserve(expensive, Decimal("9.99"))
        BudgetLedger(repo).settle(reservation["call_id"], None, None)

    _create_four_task_run(pg, "insufficient-preflight")
    provider = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        with pytest.raises(BudgetExceeded, match="remaining global funds"):
            _runner().preflight(repo, provider, "insufficient-preflight")
        snapshot = repo.snapshot("insufficient-preflight", None)
        assert snapshot["attempts"] == []
        assert {run["status"] for run in snapshot["runs"]} == {"blocked"}
    assert provider.generation_calls == 0


def test_preflight_or_execution_blocks_missing_price_request_drift_and_code_drift(pg, monkeypatch):
    runner = _runner()
    _create_four_task_run(pg, "immutable-preflight")
    provider = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, provider, "immutable-preflight")

    with psycopg.connect(pg.owner_dsn) as owner:
        owner.execute(
            "UPDATE reckoner.tasks SET request_sha256 = repeat('0', 64) "
            "WHERE run_id = 'immutable-preflight' AND task_id = "
            "(SELECT min(task_id) FROM reckoner.tasks WHERE run_id = 'immutable-preflight')"
        )
    with PostgresRepository(pg.runner_dsn) as repo:
        with pytest.raises(ValueError, match="request identity"):
            runner.execute_run(repo, provider, "immutable-preflight")
    assert provider.generation_calls == 0

    _create_four_task_run(pg, "code-drift")
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, provider, "code-drift")
        monkeypatch.setattr(runner, "_code_revision", lambda: "changed-code")
        with pytest.raises(ValueError, match="code revision"):
            runner.execute_run(repo, provider, "code-drift")

    _create_four_task_run(pg, "missing-price")
    with psycopg.connect(pg.owner_dsn) as owner:
        owner.execute("DELETE FROM reckoner.price_tables")
    with PostgresRepository(pg.runner_dsn) as repo:
        with pytest.raises(ValueError, match="price"):
            runner.preflight(repo, FakeProvider(), "missing-price")


def test_invalid_but_billed_response_is_failed_once_with_usage_preserved(pg):
    runner = _runner()
    _create_four_task_run(pg, "invalid-billed")
    invalid = FakeProvider().result | {"content": "not-json"}
    provider = FakeProvider(result=invalid)
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, provider, "invalid-billed")
        result = runner.execute_run(repo, provider, "invalid-billed")
        snapshot = repo.snapshot("invalid-billed", None)
    assert result == {"completed": 0, "failed": 4, "uncertain": 0, "pending": 0}
    assert provider.generation_calls == 4
    assert all(row["usage"]["input_tokens"] == 31 for row in snapshot["attempts"])
    assert all(row["status"] == "settled" for row in snapshot["budget_entries"])
    assert {run["status"] for run in snapshot["runs"]} == {"incomplete"}


def test_fake_provider_requires_explicit_test_mode(pg):
    runner = _runner()
    _create_four_task_run(pg, "paid-run", execution_mode="paid")
    with PostgresRepository(pg.runner_dsn) as repo:
        with pytest.raises(ValueError, match="fake provider"):
            runner.preflight(repo, FakeProvider(), "paid-run")


def test_paid_baseline_requires_a_matching_successful_twenty_case_pilot(pg):
    runner = _runner()
    _create_four_task_run(pg, "paid-without-pilot", execution_mode="paid")
    with PostgresRepository(pg.runner_dsn) as repo:
        with pytest.raises(ValueError, match="successful 20-case pilot"):
            runner.preflight(repo, UncallableAnthropicProvider(), "paid-without-pilot")


def test_completed_fake_pilot_never_satisfies_paid_baseline_gate(pg):
    runner = _runner()
    _create_four_task_run(pg, "fake-pilot", purpose="pilot", task_limit=None)
    fake = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        assert runner.preflight(repo, fake, "fake-pilot")["pending"] == 20
        assert runner.execute_run(repo, fake, "fake-pilot")["completed"] == 20
    with psycopg.connect(pg.owner_dsn) as owner:
        owner.execute(
            "UPDATE reckoner.runs SET execution_mode = 'paid' WHERE run_id = 'fake-pilot'"
        )

    _create_four_task_run(pg, "paid-after-fake-pilot", execution_mode="paid")
    with PostgresRepository(pg.runner_dsn) as repo:
        with pytest.raises(ValueError, match="successful 20-case pilot"):
            runner.preflight(repo, UncallableAnthropicProvider(), "paid-after-fake-pilot")


def test_paid_pilot_gate_requires_durable_export_bounds_and_dispatch_revalidation(pg, tmp_path):
    runner = _runner()
    otlp = importlib.import_module("reckoner.telemetry.otlp")
    _create_four_task_run(
        pg, "exported-pilot", purpose="pilot", execution_mode="paid", task_limit=None
    )
    pilot_provider = CountingAnthropicBoundary()
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, pilot_provider, "exported-pilot")
        runner.execute_run(repo, pilot_provider, "exported-pilot")
    _create_four_task_run(pg, "paid-after-export", execution_mode="paid", task_limit=1)

    with PostgresRepository(pg.runner_dsn) as repo:
        assert not repo.pilot_gate_satisfied("paid-after-export")
        otlp.export_run(repo, "exported-pilot", tmp_path / "pilot-export")
        assert repo.pilot_gate_satisfied("paid-after-export")

    provider = CountingAnthropicBoundary()
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, provider, "paid-after-export")
    with psycopg.connect(pg.owner_dsn) as owner:
        owner.execute(
            "UPDATE reckoner.telemetry_outbox SET status = 'failed' WHERE run_id = 'exported-pilot'"
        )
    with PostgresRepository(pg.runner_dsn) as repo:
        result = runner.execute_run(repo, provider, "paid-after-export")
    assert provider.generation_calls == 0
    assert result["pending"] == 1


def test_pilot_gate_rejects_missing_frozen_reservation_bounds(pg, tmp_path):
    runner = _runner()
    otlp = importlib.import_module("reckoner.telemetry.otlp")
    _create_four_task_run(
        pg,
        "missing-bounds-pilot",
        purpose="pilot",
        execution_mode="paid",
        task_limit=None,
    )
    pilot_provider = CountingAnthropicBoundary()
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, pilot_provider, "missing-bounds-pilot")
        runner.execute_run(repo, pilot_provider, "missing-bounds-pilot")
        otlp.export_run(repo, "missing-bounds-pilot", tmp_path / "pilot-export")
    _create_four_task_run(pg, "baseline-missing-bounds", execution_mode="paid", task_limit=1)
    with psycopg.connect(pg.owner_dsn) as owner:
        owner.execute(
            "UPDATE reckoner.tasks SET request_document = NULL, request_sha256 = NULL, "
            "input_token_estimate = NULL, reservation_input_tokens = NULL, "
            "reservation_cost = NULL, trace_id = NULL, span_id = NULL, "
            "provider_span_id = NULL, event_id = NULL "
            "WHERE run_id = 'missing-bounds-pilot' AND task_id = "
            "(SELECT min(task_id) FROM reckoner.tasks WHERE run_id = 'missing-bounds-pilot')"
        )
    with PostgresRepository(pg.runner_dsn) as repo:
        assert not repo.pilot_gate_satisfied("baseline-missing-bounds")


def test_code_provenance_is_anchored_to_source_tree_and_detects_executable_change(
    pg, tmp_path, monkeypatch
):
    runner = _runner()
    source = tmp_path / "runner-source.py"
    source.write_text("version = 1\n")
    monkeypatch.setattr(runner, "_relevant_source_files", lambda: [source])
    _create_four_task_run(pg, "source-digest", task_limit=1)
    provider = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, provider, "source-digest")
        monkeypatch.chdir(tmp_path)
        source.write_text("version = 2\n")
        with pytest.raises(ValueError, match="code revision"):
            runner.execute_run(repo, provider, "source-digest")
    assert provider.generation_calls == 0


def test_code_provenance_ignores_caller_working_directory(pg, tmp_path, monkeypatch):
    runner = _runner()
    _create_four_task_run(pg, "other-cwd", task_limit=1)
    provider = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, provider, "other-cwd")
        monkeypatch.chdir(tmp_path)
        assert runner.execute_run(repo, provider, "other-cwd")["completed"] == 1


def test_runtime_bundle_validation_never_requires_oracle_or_archive_files(
    fabricated_bundle, tmp_path
):
    from reckoner import cli

    runtime = tmp_path / "runtime-only"
    runtime.mkdir()
    index = json.loads((fabricated_bundle / "bundle.json").read_text())
    shutil.copy2(fabricated_bundle / "bundle.json", runtime / "bundle.json")
    cohort = index["cohorts"]["pilot"]
    for logical_name in (cohort["runtime_file"], *cohort["manifest_files"]):
        relative = index["files"][logical_name]["path"]
        shutil.copy2(fabricated_bundle / relative, runtime / relative)

    assert cli._runtime_bundle_identity(runtime, "pilot") == index["bundle_id"]
    assert not any("oracle" in path.name for path in runtime.iterdir())


def test_completion_uses_cohort_from_frozen_bundle_when_transactions_overlap(pg):
    runner = _runner()
    _create_four_task_run(pg, "seed-original", task_limit=1)
    overlap_bundle = "b" * 64
    prices = json.loads((CONFIG_DIR / "anthropic-prices-v1.json").read_text())
    config = load_config(CONFIG_DIR / "baseline-v1.json", CONFIG_DIR / "anthropic-prices-v1.json")
    with psycopg.connect(pg.owner_dsn) as owner:
        for tenant_id in ("tenant-a", "tenant-b"):
            cohort_id = f"zzzz-overlap-{tenant_id}"
            owner.execute(
                "INSERT INTO reckoner.cohorts "
                "(tenant_id, cohort_id, purpose, bundle_id, document) "
                "VALUES (%s,%s,'baseline',%s,'{}'::jsonb)",
                (tenant_id, cohort_id, overlap_bundle),
            )
            owner.execute(
                "INSERT INTO reckoner.cohort_members (tenant_id, cohort_id, transaction_id) "
                "SELECT tenant_id, %s, transaction_id FROM reckoner.cohort_members "
                "WHERE tenant_id = %s AND cohort_id = "
                "(SELECT cohort_id FROM reckoner.cohorts WHERE tenant_id = %s "
                "AND purpose = 'baseline' AND bundle_id <> %s LIMIT 1)",
                (cohort_id, tenant_id, tenant_id, overlap_bundle),
            )
    with PostgresRepository(pg.owner_dsn) as repo:
        repo.create_run(
            "overlap-run",
            "baseline",
            config,
            overlap_bundle,
            price=prices,
            execution_mode="test",
        )
    with psycopg.connect(pg.owner_dsn) as owner:
        task = owner.execute(
            "SELECT tenant_id, task_id FROM reckoner.tasks WHERE run_id = 'overlap-run' "
            "ORDER BY tenant_id, task_id LIMIT 1"
        ).fetchone()
        owner.execute(
            "DELETE FROM reckoner.tasks WHERE run_id = 'overlap-run' AND task_id <> %s",
            (task[1],),
        )
    provider = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, provider, "overlap-run")
        runner.execute_run(repo, provider, "overlap-run")
        decision = repo.snapshot("overlap-run", None)["decisions"][0]
    assert decision["cohort_id"] == f"zzzz-overlap-{task[0]}"
