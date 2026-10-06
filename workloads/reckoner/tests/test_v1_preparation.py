import csv
import json
import sqlite3

import pytest
from reckoner.data.cohort import prepare
from reckoner.v1.data.prepare import prepare_v1
from test_cohort import HEADERS, row, write_source


@pytest.fixture
def inputs(tmp_path):
    source = tmp_path / "source"
    write_source(source)
    path = source / "credit_card_transactions-ibm_v2.csv"
    with path.open("a", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=HEADERS)
        for year in (2017, 2018):
            for label, count, user in [("Yes", 210, "history-high"), ("No", 1820, "history-low")]:
                writer.writerows(row(user, year, label, n) for n in range(count))
    baseline = tmp_path / "baseline"
    prepare(source, baseline)
    return source, baseline


def test_preparation_freezes_identity_separates_oracle_and_preserves_source_rows(inputs, tmp_path):
    source, baseline = inputs
    first = prepare_v1(source, baseline, tmp_path / "one")
    second = prepare_v1(source, baseline, tmp_path / "two")
    assert first == second
    assert first["history"]["retained_records"] == 5081
    assert len(first["samples"]["pilot"]["selected_transaction_ids"]) == 20
    runtime = [
        json.loads(line)
        for line in (tmp_path / "one/runtime_development.jsonl").read_text().splitlines()
    ]
    assert len(runtime) == len({x["transaction_id"] for x in runtime}) == 2000
    assert all("label" not in x and "resolved_at" not in x for x in runtime)
    with sqlite3.connect(tmp_path / "one/history.sqlite") as db:
        assert db.execute("SELECT count(*) FROM history").fetchone()[0] == 5081
        assert "label" not in [r[1] for r in db.execute("PRAGMA table_info(history)")]
    pilot = set(first["samples"]["pilot"]["selected_transaction_ids"])
    assert pilot <= set(first["samples"]["development"]["selected_transaction_ids"])
    assert set(first["samples"]["validation"]["selected_transaction_ids"]).isdisjoint(
        first["samples"]["development"]["selected_transaction_ids"]
    )
    with pytest.raises(ValueError, match="exists"):
        prepare_v1(source, baseline, tmp_path / "one")


def test_source_checksum_change_rejected_before_publication(inputs, tmp_path):
    source, baseline = inputs
    with (source / "credit_card_transactions-ibm_v2.csv").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="checksum"):
        prepare_v1(source, baseline, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_insufficient_stratum_leaves_no_output(tmp_path):
    source = tmp_path / "source"
    write_source(source)
    baseline = tmp_path / "baseline"
    prepare(source, baseline)
    with pytest.raises(ValueError, match="insufficient fraud"):
        prepare_v1(source, baseline, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_source_index_reads_canonical_rows_and_previous_card_across_windows(inputs, tmp_path):
    from reckoner.v1.data.history import SourceHistory

    source, baseline = inputs
    prepared = tmp_path / "prepared"
    prepare_v1(source, baseline, prepared)
    with SourceHistory(prepared, source) as history:
        tx = history.transaction(1)
        rows = list(
            history.card_before(
                tx["tenant_id"], tx["card_id"], "2019-01-01T00:00:00Z", since="2018-12-01T00:00:00Z"
            )
        )
        assert rows == []
        previous = history.previous_card(tx["tenant_id"], tx["card_id"], "2019-01-01T00:00:00Z")
        assert previous["occurred_at"] < "2019-01-01T00:00:00Z"
        assert previous["transaction_id"] != tx["transaction_id"]
        assert "label" not in previous


@pytest.mark.integration
def test_import_owner_and_cutoff_functions_keep_oracles_privileged(inputs, tmp_path, pg):
    import psycopg
    from reckoner.v1.data.prepare import import_v1

    source, baseline = inputs
    prepared = tmp_path / "prepared"
    index = prepare_v1(source, baseline, prepared)
    imported = import_v1(prepared, source, pg.owner_dsn)
    assert imported["bundle_id"] == index["bundle_id"]
    tx = json.loads((prepared / "runtime_development.jsonl").read_text().splitlines()[0])
    with psycopg.connect(pg.runner_dsn) as connection:
        history = connection.execute(
            "SELECT document FROM reckoner.v1_history_before(%s,%s)",
            (tx["tenant_id"], tx["transaction_id"]),
        ).fetchall()
        assert all(r[0]["occurred_at"] < tx["occurred_at"] for r in history)
        cases = connection.execute(
            "SELECT document FROM reckoner.v1_resolved_before(%s,%s)",
            (tx["tenant_id"], tx["transaction_id"]),
        ).fetchall()
        assert all(r[0]["resolved_at"] < tx["occurred_at"] for r in cases)
        validation = [
            json.loads(line)
            for line in (prepared / "runtime_validation.jsonl").read_text().splitlines()
        ]
        early = min(validation, key=lambda item: item["occurred_at"])
        previous = connection.execute(
            "SELECT document FROM reckoner.v1_previous_card(%s,%s)",
            (early["tenant_id"], early["transaction_id"]),
        ).fetchone()
        assert previous is not None
        assert previous[0]["card_id"] == early["card_id"]
        assert previous[0]["occurred_at"] < "2017-12-01T00:00:00Z"
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT * FROM oracle.v1_resolutions")
    with psycopg.connect(pg.api_dsn) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT * FROM oracle.v1_resolutions")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        import_v1(prepared, source, pg.runner_dsn)
    with psycopg.connect(pg.owner_dsn) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "UPDATE reckoner.v1_history SET occurred_at=occurred_at + interval '1 day'"
            )


def test_cli_registers_v1_prepare_and_import_arguments():
    from reckoner.cli import _parser

    args = _parser().parse_args(
        ["v1", "prepare", "--source", "archive", "--baseline-bundle", "baseline", "--output", "new"]
    )
    assert args.v1_command == "prepare"
    args = _parser().parse_args(["v1", "import", "--bundle", "bundle", "--env-file", "owner.env"])
    assert args.v1_command == "import"


def test_source_backed_reader_rejects_source_mutation(inputs, tmp_path):
    from reckoner.v1.data.history import SourceHistory

    source, baseline = inputs
    prepared = tmp_path / "prepared"
    prepare_v1(source, baseline, prepared)
    with (source / "credit_card_transactions-ibm_v2.csv").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="checksum"):
        SourceHistory(prepared, source)


def test_shuffled_archive_is_sorted_at_query_time(inputs, tmp_path):
    import random

    from reckoner.v1.data.history import SourceHistory

    source, _ = inputs
    path = source / "credit_card_transactions-ibm_v2.csv"
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    random.Random(12).shuffle(rows)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(rows)
    baseline = tmp_path / "shuffled-baseline"
    prepare(source, baseline)
    prepared = tmp_path / "prepared"
    index = prepare_v1(source, baseline, prepared)
    query = json.loads(
        (prepared / index["files"]["runtime_validation"]["path"]).read_text().splitlines()[0]
    )
    with SourceHistory(prepared, source) as history:
        transactions = list(
            history.card_before(query["tenant_id"], query["card_id"], query["occurred_at"])
        )
    assert len(transactions) > 100
    assert [tx["occurred_at"] for tx in transactions] == sorted(
        tx["occurred_at"] for tx in transactions
    )
    assert all(tx["occurred_at"] < query["occurred_at"] for tx in transactions)


@pytest.mark.integration
def test_import_rejects_conflicting_existing_canonical_content(inputs, tmp_path, pg):
    import psycopg
    from psycopg.types.json import Jsonb
    from reckoner.v1.data.prepare import import_v1

    source, baseline = inputs
    prepared = tmp_path / "prepared"
    prepare_v1(source, baseline, prepared)
    tx = json.loads((prepared / "runtime_development.jsonl").read_text().splitlines()[0])
    tx["amount_minor"] += 1
    with psycopg.connect(pg.owner_dsn) as connection:
        connection.execute(
            "INSERT INTO reckoner.transactions VALUES (%s,%s,%s)",
            (tx["tenant_id"], tx["transaction_id"], Jsonb(tx)),
        )
    with pytest.raises(ValueError, match="canonical record conflict"):
        import_v1(prepared, source, pg.owner_dsn)
    with psycopg.connect(pg.owner_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM reckoner.v1_history").fetchone()[0] == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    "scenario", ["small-disk", "small-size", "retry-disk", "final-disk", "final-size"]
)
def test_import_resource_guards_cover_small_retries_and_partial_batches(
    scenario, inputs, tmp_path, pg, monkeypatch
):
    import psycopg
    from reckoner.v1.data import prepare as preparation

    source, baseline = inputs
    prepared = tmp_path / "prepared"
    preparation.prepare_v1(source, baseline, prepared)
    if scenario == "retry-disk":
        receipt = preparation.import_v1(prepared, source, pg.owner_dsn)
        assert receipt["imported_history_records"] < 10000
    with psycopg.connect(pg.owner_dsn) as connection:
        before = connection.execute("SELECT count(*) FROM reckoner.v1_history").fetchone()[0]
        initial_size = connection.execute("SELECT pg_database_size(current_database())").fetchone()[
            0
        ]
    derived = sum(path.stat().st_size for path in prepared.iterdir() if path.is_file())
    if scenario.endswith("size"):
        # Empty database fits only the final-batch case; the actual rows then grow it.
        allowance = 1 if scenario == "final-size" else -1
        monkeypatch.setattr(preparation, "MAX_DERIVED", derived + initial_size + allowance)
        message = "20 GiB cap"
    else:
        usage = preparation.shutil.disk_usage(prepared)
        calls = 0

        def disk_usage(_):
            nonlocal calls
            calls += 1
            free = (
                usage.free if scenario == "final-disk" and calls == 1 else preparation.MIN_FREE - 1
            )
            return usage._replace(free=free)

        monkeypatch.setattr(preparation.shutil, "disk_usage", disk_usage)
        message = "15 GiB free disk"
    with pytest.raises(ValueError, match=message):
        preparation.import_v1(prepared, source, pg.owner_dsn)
    with psycopg.connect(pg.owner_dsn) as connection:
        assert (
            connection.execute("SELECT count(*) FROM reckoner.v1_history").fetchone()[0] == before
        )
