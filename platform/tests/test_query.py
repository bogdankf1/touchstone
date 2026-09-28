"""Read-model behavior against a small published DuckDB generation."""

import json

import duckdb
import pytest
from touchstone_platform.query import open_snapshot
from touchstone_platform.settings import Settings


@pytest.fixture
def published(tmp_path):
    path = tmp_path / "generation-one.duckdb"
    with duckdb.connect(str(path)) as db:
        db.execute("""
            create table mart_runs (
                tenant_id varchar, workflow_id varchar, run_id varchar,
                workflow_version varchar, experiment_version varchar, cohort_version varchar,
                config_version varchar, measurement_mode varchar, dataset_simulated boolean,
                declared_at timestamptz, completeness_known boolean,
                expected_tasks integer, received_tasks integer, completed_tasks integer,
                failed_tasks integer, missing_tasks integer, correct_tasks integer,
                missing_outcomes integer, model_cost decimal(38,12),
                review_cost decimal(38,12), error_cost decimal(38,12), currency varchar,
                latency_population integer, latency_p99_ms double,
                expected_cases integer, passed_cases integer, missing_checks integer,
                error_checks integer, conflicting_checks integer, metrics_complete boolean
            )
        """)
        db.executemany(
            "insert into mart_runs values (" + ",".join("?" for _ in range(30)) + ")",
            [
                (
                    tenant,
                    "workflow",
                    "run",
                    "wv1",
                    "ev1",
                    "cohort1",
                    "cfg1",
                    "measured",
                    True,
                    "2026-09-26T10:00:00Z",
                    True,
                    2,
                    2,
                    2,
                    0,
                    0,
                    2,
                    0,
                    cost,
                    "0.000000000000",
                    "0.000000000000",
                    "USD",
                    2,
                    p99,
                    2,
                    1,
                    1,
                    0,
                    0,
                    False,
                )
                for tenant, cost, p99 in (
                    ("tenant-a", "0.100000000001", 100.0),
                    ("tenant-b", "0.200000000002", 1000.0),
                )
            ]
            + [
                (
                    "tenant-c",
                    "workflow",
                    "run",
                    "wv1",
                    "ev1",
                    "other-cohort",
                    "cfg1",
                    "fabricated",
                    True,
                    "2026-09-26T10:00:00Z",
                    True,
                    1,
                    1,
                    1,
                    0,
                    0,
                    1,
                    0,
                    "9.000000000000",
                    "0.000000000000",
                    "0.000000000000",
                    "USD",
                    1,
                    5000.0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    True,
                ),
                (
                    "tenant-a",
                    "workflow",
                    "complete",
                    "wv1",
                    "ev1",
                    "cohort1",
                    "cfg1",
                    "measured",
                    True,
                    "2026-09-26T10:00:00Z",
                    True,
                    3,
                    3,
                    3,
                    0,
                    0,
                    3,
                    0,
                    "0.300000000003",
                    "0.000000000000",
                    "0.000000000000",
                    "USD",
                    3,
                    100.0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    True,
                ),
            ],
        )
        db.execute("alter table mart_runs add column unexpected_tasks integer default 0")
        db.execute("""
            create table int_tasks (
                tenant_id varchar, workflow_id varchar, run_id varchar, task_id varchar,
                first_started_at timestamptz, terminal_at timestamptz,
                terminal_status varchar, latency_ms double, incomplete boolean
            )
        """)
        db.executemany(
            "insert into int_tasks values (?,?,?,?,?,?,?,?,?)",
            [
                (
                    tenant,
                    "workflow",
                    "run",
                    task,
                    "2026-09-26T10:00:00Z",
                    "2026-09-26T10:00:01Z",
                    "completed",
                    latency,
                    False,
                )
                for tenant, task, latency in (
                    ("tenant-a", "a1", 10.0),
                    ("tenant-a", "a2", 100.0),
                    ("tenant-b", "b1", 20.0),
                    ("tenant-b", "b2", 1000.0),
                )
            ],
        )
        db.execute("""
            create table mart_nodes (
                tenant_id varchar, workflow_id varchar, run_id varchar, task_id varchar,
                node_name varchar, currency varchar, model_cost decimal(38,12),
                call_count integer, incomplete boolean, trace_id varchar,
                evidence_event_id varchar
            )
        """)
        db.execute(
            "insert into mart_nodes values ('tenant-a','workflow','run','a1',"
            "'provider','USD',0.100000000001,1,false,'trace-a','event-a')"
        )
        db.execute("""
            create table mart_contribution_rates (
                tenant_id varchar, workflow_id varchar, run_id varchar, metric_id varchar,
                definition_version varchar, unit varchar, expected_tasks integer,
                declaration_versions integer, task_contributions integer,
                incomplete_contributions integer, unexpected_tasks integer,
                numerator_sum double, denominator_sum double, declared_at timestamptz,
                coverage_known boolean, missing_tasks integer, eligible_numerator double,
                eligible_denominator double, rate double
            )
        """)
        db.execute("""
            insert into mart_contribution_rates values
            ('tenant-a','workflow','run','false_positive','v1','ratio',2,1,1,0,0,
             1,2,'2026-09-26T10:00:00Z',true,1,null,null,null)
        """)
        db.execute("""
            create table stg_events (
                tenant_id varchar, workflow_id varchar, run_id varchar, task_id varchar,
                event_id varchar, event_kind varchar, node_name varchar, trace_id varchar,
                span_id varchar, received_at timestamptz, identity_conflict boolean,
                document_json json
            )
        """)
        db.executemany(
            "insert into stg_events values (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    tenant,
                    "workflow",
                    "run",
                    task,
                    event,
                    "execution",
                    "root",
                    trace,
                    span,
                    "2026-09-26T10:01:00Z",
                    False,
                    json.dumps({"payload": {"prompt": "private", "response_text": "private"}}),
                )
                for tenant, task, event, trace, span in (
                    ("tenant-a", "a1", "event-a", "shared-trace", "span-a"),
                    ("tenant-b", "b1", "event-b", "shared-trace", "span-b"),
                )
            ],
        )
        db.execute("""
            create table int_calls (
                tenant_id varchar, workflow_id varchar, run_id varchar,
                task_id varchar, incomplete boolean, unavailable integer,
                price_table_version varchar
            )
        """)
        db.execute("""
            insert into int_calls values
            ('tenant-a','workflow','run','a1',true,1,'price-v1'),
            ('tenant-b','workflow','run','b1',false,0,'price-v1')
        """)
        db.execute("""
            create table raw_declarations (
                tenant_id varchar, workflow_id varchar, run_id varchar,
                content_sha256 varchar, document_json json
            )
        """)
        db.executemany(
            "insert into raw_declarations values (?,?,?,?,?)",
            [
                (
                    tenant,
                    "workflow",
                    run,
                    f"hash-{tenant}-{run}",
                    json.dumps({"code_revision": "rev1", "dataset_version": "data1"}),
                )
                for tenant, run in (
                    ("tenant-a", "run"),
                    ("tenant-b", "run"),
                    ("tenant-c", "run"),
                    ("tenant-a", "complete"),
                )
            ],
        )
        db.execute("""
            create table raw_rejections (
                tenant_id varchar, workflow_id varchar, run_id varchar,
                task_id varchar, reason varchar
            )
        """)
        db.execute(
            "insert into raw_rejections values ('tenant-a','workflow','run','a1','invalid cost')"
        )
    (tmp_path / "current.json").write_text(
        json.dumps(
            {
                "generation": path.name,
                "warehouse": "DuckDB local preview",
                "published_at": "2026-09-26T10:02:00Z",
                "cutoff": "2026-09-26T10:01:00Z",
                "accepted_measurements": 2,
                "accepted_declarations": 2,
                "rejected_count": 0,
            }
        )
    )
    return Settings(warehouse_dir=tmp_path)


def test_snapshot_aggregate_pools_compatible_rows_and_root_latencies(published):
    with open_snapshot(published) as snapshot:
        summary = snapshot.summary("workflow", "run", aggregate=True)
    assert summary["tenant_ids"] == ["tenant-a", "tenant-b"]
    assert summary["model_cost"] == "0.300000000003"
    assert summary["correct_tasks"] == 4
    assert summary["latency_p99_ms"] == 1000.0
    assert summary["latency_population"] == 4
    assert summary["cpst"] is None  # missing required evaluation checks
    assert len(summary["tenants"]) == 2


def test_complete_run_uses_exact_decimal_division_and_quality_counts(published):
    with open_snapshot(published) as snapshot:
        complete = snapshot.summary("workflow", "complete", tenant_id="tenant-a")
        incomplete = snapshot.summary("workflow", "run", tenant_id="tenant-a")
    assert complete["cpst"] == "0.100000000001"
    assert incomplete["quality"] == {
        "identity_conflicts": 0,
        "incomplete_calls": 1,
        "unavailable_prices": 1,
        "rejected_events": 1,
    }


def test_aggregate_excludes_other_dataset_or_price_version(published):
    with duckdb.connect(str(published.warehouse_dir / "generation-one.duckdb")) as db:
        db.execute("""
            update raw_declarations
            set document_json = '{"code_revision":"rev1","dataset_version":"data2"}'
            where tenant_id = 'tenant-b' and run_id = 'run'
        """)
    with open_snapshot(published) as snapshot:
        assert snapshot.summary("workflow", "run", aggregate=True)["excluded_tenants"] == [
            "tenant-b",
            "tenant-c",
        ]
    with duckdb.connect(str(published.warehouse_dir / "generation-one.duckdb")) as db:
        db.execute("""
            update raw_declarations
            set document_json = '{"code_revision":"rev1","dataset_version":"data1"}'
            where tenant_id = 'tenant-b' and run_id = 'run'
        """)
        db.execute("""
            update int_calls set price_table_version = 'price-v2'
            where tenant_id = 'tenant-b' and run_id = 'run'
        """)
    with open_snapshot(published) as snapshot:
        assert snapshot.summary("workflow", "run", aggregate=True)["excluded_tenants"] == [
            "tenant-b",
            "tenant-c",
        ]


def test_aggregate_excludes_conflicting_declaration_even_when_completeness_known(published):
    with duckdb.connect(str(published.warehouse_dir / "generation-one.duckdb")) as db:
        db.execute("""
            insert into raw_declarations values
            ('tenant-a','workflow','run','conflicting-hash-a',
             '{"code_revision":"rev3","dataset_version":"data1"}'),
            ('tenant-b','workflow','run','conflicting-hash',
             '{"code_revision":"rev2","dataset_version":"data1"}')
        """)
    with open_snapshot(published) as snapshot:
        assert (
            snapshot.summary("workflow", "run", tenant_id="tenant-b")["completeness_known"] is True
        )
        aggregate = snapshot.summary("workflow", "run", aggregate=True)
    assert aggregate["tenant_ids"] == ["tenant-c"]
    assert aggregate["excluded_tenants"] == ["tenant-a", "tenant-b"]
    assert aggregate["model_cost"] == "9.000000000000"


def test_no_observed_contributions_keep_components_unknown_in_tenant_and_aggregate(published):
    with duckdb.connect(str(published.warehouse_dir / "generation-one.duckdb")) as db:
        db.execute("""
            insert into mart_contribution_rates values
            ('tenant-b','workflow','run','false_positive','v1','ratio',2,1,0,0,0,
             null,null,'2026-09-26T10:00:00Z',true,2,null,null,null)
        """)
    with open_snapshot(published) as snapshot:
        tenant = snapshot.summary("workflow", "run", tenant_id="tenant-b")
        aggregate = snapshot.summary("workflow", "run", aggregate=True)
    assert tenant["contribution_rates"][0]["numerator"] is None
    assert tenant["contribution_rates"][0]["denominator"] is None
    assert tenant["contribution_rates"][0]["rate"] is None
    assert aggregate["contribution_rates"][0]["numerator"] == 1.0
    assert aggregate["contribution_rates"][0]["denominator"] == 2.0
    assert aggregate["contribution_rates"][0]["expected_tasks"] == 4
    assert aggregate["contribution_rates"][0]["task_contributions"] == 1
    assert aggregate["contribution_rates"][0]["rate"] is None


def test_snapshot_uses_parameters_for_hostile_run_and_tenant_values(published):
    with open_snapshot(published) as snapshot:
        assert snapshot.summary("workflow", "run' OR 1=1 --", tenant_id="tenant-a") is None
        assert snapshot.summary("workflow", "run", tenant_id="../tenant-b") is None
        assert snapshot.trace("shared-trace", "workflow", "run", "tenant-a")["events"] == [
            {
                "tenant_id": "tenant-a",
                "workflow_id": "workflow",
                "run_id": "run",
                "task_id": "a1",
                "event_id": "event-a",
                "event_kind": "execution",
                "node_name": "root",
                "trace_id": "shared-trace",
                "span_id": "span-a",
                "received_at": "2026-09-26T10:01:00+00:00",
                "identity_conflict": False,
            }
        ]


def test_snapshot_pins_manifest_during_publication(published):
    with open_snapshot(published) as snapshot:
        assert snapshot.metadata()["generation"] == "generation-one.duckdb"
        other = published.warehouse_dir / "generation-two.duckdb"
        with duckdb.connect(str(other)) as db:
            db.execute("create table marker(i integer)")
        (published.warehouse_dir / "current.json").write_text(
            json.dumps(
                {
                    "generation": other.name,
                    "warehouse": "DuckDB local preview",
                    "published_at": "2026-09-26T10:03:00Z",
                    "cutoff": "2026-09-26T10:03:00Z",
                }
            )
        )
        assert snapshot.metadata()["generation"] == "generation-one.duckdb"
        assert (
            snapshot.summary("workflow", "run", tenant_id="tenant-a")["model_cost"]
            == "0.100000000001"
        )
