"""Postgres rolling working set on a fabricated simulated source (disposable stores only)."""

import json
import uuid
from argparse import Namespace
from datetime import timedelta

import psycopg
import pytest
from psycopg import sql
from reckoner.contracts import content_id
from test_v1_evidence_preparation import SNAPSHOT, build_inputs, fabricated_scaler

pytestmark = pytest.mark.integration

FIRST_DAYS = "2017-01-12"


@pytest.fixture(scope="module")
def inputs(tmp_path_factory):
    return build_inputs(tmp_path_factory.mktemp("rolling-pg-inputs"))


@pytest.fixture(scope="module")
def manifest(inputs):
    from reckoner.v1.evidence.preparation import entity_manifest

    source, _, bundle = inputs
    return entity_manifest(bundle, source)


def identities(manifest):
    return [
        {
            "tenant_id": e["tenant_id"],
            "merchant_id": e["identity"],
            "identity": e["shared_identity"],
        }
        for e in manifest["entities"]
        if e["kind"] == "merchant"
    ]


@pytest.fixture
def stores(inputs, pg, manifest):
    """Working-set stores on fresh databases; every one is dropped by its own sentinel."""
    from reckoner.v1.data.history import SourceHistory
    from reckoner.v1.evidence.rolling import (
        RollingStore,
        SourceDays,
        drop_working_set,
        ensure_working_set,
        with_database,
        working_set_name,
    )

    source, _, bundle = inputs
    created, readers = [], []

    def make(guard=None):
        preparation = uuid.uuid4().hex * 2
        owner = ensure_working_set(pg.owner_dsn, preparation)
        created.append(preparation)
        history = SourceHistory(bundle, source)
        days = SourceDays(history, bundle)
        readers.append((history, days))
        store = RollingStore(
            owner,
            with_database(pg.runner_dsn, working_set_name(preparation)),
            source=days,
            scaler=fabricated_scaler(),
            snapshot_id=SNAPSHOT,
            guard=guard,
        )
        store.initialize(identities(manifest))
        store.preparation_id = preparation
        return store

    yield make
    for history, days in readers:
        days.close()
        history.close()
    for preparation in created:
        drop_working_set(pg.owner_dsn, preparation)


def config():
    scaler = fabricated_scaler()
    return {"scaler_id": scaler["scaler_id"], "scaler": scaler}


def development_days(inputs):
    from reckoner.v1.evidence.preparation import query_schedule

    _, baseline, bundle = inputs
    return query_schedule(bundle, baseline, {"development": ("relational",)})


def cases_on(inputs, day):
    for entry in development_days(inputs):
        if entry["day"] >= day:
            return entry["day"], [c["transaction"] for c in entry["cases"]]
    raise AssertionError("no development day")


def history_ids(dsn):
    with psycopg.connect(dsn) as connection:
        return {
            r[0]: r[1]
            for r in connection.execute(
                "SELECT transaction_id, occurred_at FROM reckoner.v1_history"
            )
        }


def fresh_window(pg, store, day, transactions, manifest):
    """An independent store holding exactly [day - 97d, day + 1d) and previous-card rows."""
    from reckoner.v1.data.history import instant
    from reckoner.v1.evidence.postgres import PostgresEvidence, import_evidence
    from reckoner.v1.evidence.preparation import iso, window
    from reckoner.v1.evidence.rolling import (
        ensure_working_set,
        runtime_dsn,
        with_database,
        working_set_name,
    )

    preparation = uuid.uuid4().hex * 2
    owner = ensure_working_set(pg.owner_dsn, preparation)
    history = store.source.history
    start, end = window(day)
    pairs = store.source.records(start, end)
    for tx in transactions:
        previous = history.previous_card(tx["tenant_id"], tx["card_id"], tx["occurred_at"])
        if previous is not None and instant(previous["occurred_at"]) < start:
            for row in history.card_before(
                tx["tenant_id"], tx["card_id"], tx["occurred_at"], since=previous["occurred_at"]
            ):
                pairs.append((row, store.source.resolution(row)))
    records = []
    for tx, resolution in pairs:
        cutoff = instant(tx["occurred_at"])
        records.append(
            {
                "transaction": tx,
                "resolution": resolution,
                "history": {
                    "status": "available",
                    "transactions": list(
                        history.card_before(
                            tx["tenant_id"],
                            tx["card_id"],
                            tx["occurred_at"],
                            since=iso(cutoff - timedelta(days=30)),
                        )
                    ),
                    "previous": history.previous_card(
                        tx["tenant_id"], tx["card_id"], tx["occurred_at"]
                    ),
                },
            }
        )
    coverage = [
        {
            "tenant_id": tenant,
            "history_from": iso(start),
            "history_until": iso(end),
            "previous_card_complete": True,
            "source_snapshot_id": SNAPSHOT,
        }
        for tenant in ("tenant-a", "tenant-b")
    ]
    import_evidence(owner, [], records, identities(manifest), coverage, fabricated_scaler())
    relational = PostgresEvidence(
        runtime_dsn(with_database(pg.runner_dsn, working_set_name(preparation)))
    )
    return preparation, [relational.for_task({"transaction": tx}, config()) for tx in transactions]


def test_rolling_documents_equal_a_fresh_store_holding_exactly_the_window(
    inputs, pg, stores, manifest
):
    from reckoner.v1.evidence.preparation import window
    from reckoner.v1.evidence.rolling import drop_working_set

    store = stores()
    for day in ("2017-04-20", "2017-05-10", "2017-06-01"):
        store.advance_to(day)
        store.evict_before(window(day)[0])
    day, transactions = cases_on(inputs, "2017-06-15")
    store.advance_to(day)
    store.evict_before(window(day)[0])
    store.import_previous_cards(transactions)
    rolling = store.evidence(transactions, config())
    preparation, fresh = fresh_window(pg, store, day, transactions, manifest)
    try:
        assert rolling == fresh
    finally:
        drop_working_set(pg.owner_dsn, preparation)
    assert {d["coverage"]["status"] for d in rolling} == {"available"}
    assert any(d["comparable_cases"] for d in rolling)
    assert all(d["neighbourhood"]["total_transactions"] for d in rolling)
    assert all(d["source_snapshot_ids"]["postgres"] == SNAPSHOT for d in rolling)


def test_eviction_never_claims_evicted_rows_and_earlier_queries_become_unavailable(inputs, stores):
    from reckoner.v1.evidence.preparation import PreparationFault, check_documents, iso, window

    store = stores()
    early, early_cases = cases_on(inputs, "2017-02-01")
    store.advance_to(early)
    store.evict_before(window(early)[0])
    before = store.evidence(early_cases[:1], config())
    assert before[0]["coverage"]["status"] == "available"
    late = "2017-06-20"
    store.advance_to(late)
    store.evict_before(window(late)[0])
    coverage = store.coverage()
    start, end = window(late)
    assert {iso(c["history_from"]) for c in coverage.values()} == {iso(start)}
    assert {iso(c["history_until"]) for c in coverage.values()} == {iso(end)}
    stored = history_ids(store.owner_dsn)
    assert min(stored.values()) >= start
    expected = {tx["transaction_id"] for tx, _ in store.source.records(start, end)}
    assert set(stored) == expected
    with pytest.raises(ValueError, match="monotonic"):
        store.evict_before(start - timedelta(days=1))
    after = store.evidence(early_cases[:1], config())
    assert after[0]["coverage"] == {
        "status": "unavailable",
        "missing": ["historical coverage unavailable"],
    }
    case = {
        "tenant_id": early_cases[0]["tenant_id"],
        "transaction_id": early_cases[0]["transaction_id"],
    }
    with pytest.raises(PreparationFault):
        check_documents(early, [(case, "relational", after[0])])


def card_one_query(store):
    from datetime import UTC, datetime

    start = datetime(2017, 7, 20, tzinfo=UTC)
    rows = [tx for tx, _ in store.source.records(start, start + timedelta(days=1))]
    card = content_id(
        {
            "dataset": "cctd",
            "entity": "card",
            "tenant_id": rows[0]["tenant_id"],
            "source_user": "history-low",
            "source_card": "1",
        }
    )
    matches = [tx for tx in rows if tx["card_id"] == card]
    if not matches:  # history-low may be assigned to either tenant
        other = "tenant-b" if rows[0]["tenant_id"] == "tenant-a" else "tenant-a"
        card = content_id(
            {
                "dataset": "cctd",
                "entity": "card",
                "tenant_id": other,
                "source_user": "history-low",
                "source_card": "1",
            }
        )
        matches = [tx for tx in rows if tx["card_id"] == card]
    (query,) = matches
    return query


def test_previous_card_row_outside_the_window_is_imported_before_assembly(stores):
    from reckoner.v1.evidence.preparation import window

    store = stores()
    query = card_one_query(store)
    store.advance_to("2017-07-20")
    store.evict_before(window("2017-07-20")[0])
    missing = store.evidence([query], config())[0]
    assert missing["features"]["seconds_since_previous"] is None
    imported = store.import_previous_cards([query])
    assert imported["rows"] == 1
    document = store.evidence([query], config())[0]
    assert document["features"]["seconds_since_previous"] == str(float(169 * 86400))
    assert document["coverage"]["status"] == "available"


def test_in_band_budget_or_free_floor_breach_stops_before_commit(stores, tmp_path):
    from reckoner.v1.benchmark.resources import ResourceGuardError
    from reckoner.v1.evidence.rolling import StoreGuard

    for guard in (
        StoreGuard(free_path=tmp_path, max_store_bytes=1),
        StoreGuard(free_path=tmp_path, free_floor_bytes=10**18),
    ):
        store = stores()
        store.guard = guard
        with pytest.raises(ResourceGuardError):
            store.advance_to("2017-01-05")
        assert store.coverage() == {}
        assert history_ids(store.owner_dsn) == {}


def test_runner_identity_has_no_oracle_usage(stores):
    identity = stores().runtime_identity()
    assert identity["current_user"] == "reckoner_runner"
    assert identity["oracle_usage"] is False


def test_drop_working_set_refuses_a_database_without_this_preparations_comment(pg):
    from reckoner.v1.evidence.rolling import (
        drop_working_set,
        ensure_working_set,
        working_set_name,
    )

    preparation = uuid.uuid4().hex * 2
    name = working_set_name(preparation)
    with psycopg.connect(pg.owner_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            with pytest.raises(ValueError, match="refusing"):
                drop_working_set(pg.owner_dsn, preparation)
            with pytest.raises(ValueError, match="refusing"):
                ensure_working_set(pg.owner_dsn, preparation)
            connection.execute(
                sql.SQL("COMMENT ON DATABASE {} IS 'reckoner-evidence-working-set:other'").format(
                    sql.Identifier(name)
                )
            )
            with pytest.raises(ValueError, match="refusing"):
                drop_working_set(pg.owner_dsn, preparation)
            with pytest.raises(ValueError, match="refusing"):
                drop_working_set(pg.owner_dsn, preparation, name="postgres")
        finally:
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
    ensure_working_set(pg.owner_dsn, preparation)
    assert drop_working_set(pg.owner_dsn, preparation)["dropped"] == name


def test_persisted_relational_returns_only_declared_manifest_documents(inputs, pg, stores):
    from reckoner.v1.evidence.preparation import build_manifests, window
    from reckoner.v1.evidence.rolling import (
        PersistedRelational,
        import_queries,
        persist_documents,
    )

    store = stores()
    day, transactions = cases_on(inputs, "2017-01-03")
    store.advance_to(day)
    store.evict_before(window(day)[0])
    documents = store.evidence(transactions, config())
    import_queries(pg.owner_dsn, transactions)
    persist_documents(pg.runner_dsn, documents)
    schedule = [
        {
            "day": day,
            "cases": [
                {
                    "tenant_id": tx["tenant_id"],
                    "transaction_id": tx["transaction_id"],
                    "population": "validation",
                    "modes": ["relational"],
                    "pilot": False,
                }
                for tx in transactions[1:]
            ],
        }
    ]
    declaration = {
        "preparation_id": "p" * 64,
        "source_snapshot_id": SNAPSHOT,
        "populations": {"validation": {"sample_id": "v" * 64}},
    }
    (manifest,) = build_manifests(declaration, schedule, documents[1:], mode="relational")
    base = PersistedRelational(pg.runner_dsn, [manifest])
    assert base.for_task({"transaction": transactions[1]}, {}) == documents[1]
    with pytest.raises(LookupError):
        base.for_task({"transaction": transactions[0]}, {})
    moved = {k: v for k, v in manifest.items() if k != "manifest_id"}
    moved["source_snapshot_id"] = "7" * 64
    other = PersistedRelational(pg.runner_dsn, [{**moved, "manifest_id": content_id(moved)}])
    with pytest.raises(ValueError, match="manifest"):
        other.for_task({"transaction": transactions[1]}, {})
    with pytest.raises(ValueError, match="identity"):
        PersistedRelational(pg.runner_dsn, [{**moved, "manifest_id": manifest["manifest_id"]}])


# --- the CLI pass ------------------------------------------------------------------


def _environment(monkeypatch, inputs, pg, runner=None):
    source, baseline, _ = inputs
    monkeypatch.setenv("RECKONER_OWNER_DSN", pg.owner_dsn)
    monkeypatch.setenv("RECKONER_RUNNER_DSN", runner or pg.runner_dsn)
    monkeypatch.setenv("RECKONER_SOURCE_DIR", str(source))
    monkeypatch.setenv("RECKONER_BASELINE_BUNDLE", str(baseline))


def _declare(inputs, tmp_path):
    from reckoner.v1.evidence.steps import run

    _, _, bundle = inputs
    tmp_path.mkdir(parents=True, exist_ok=True)
    scaler = tmp_path / "scaler.json"
    scaler.write_text(json.dumps(fabricated_scaler()))
    result = run(
        Namespace(
            evidence_step="declare",
            bundle=bundle,
            scaler=scaler,
            output_root=tmp_path / "evidence",
            population=None,
            env_file="-",
        )
    )
    return tmp_path / "evidence" / result["preparation_id"][:12] / "declaration.json", result


def _run(declaration, through=FIRST_DAYS):
    from reckoner.v1.evidence.steps import run

    return run(
        Namespace(
            evidence_step="run",
            declaration=declaration,
            evidence_pass="relational",
            through=through,
            env_file="-",
            free_floor_bytes=0,
            max_store_bytes=None,
            free_path=None,
        )
    )


def _evidence(dsn):
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            "SELECT tenant_id, transaction_id, evidence_id FROM reckoner.v1_evidence "
            "ORDER BY 1, 2, 3"
        ).fetchall()


def _second_operational_database(pg):
    from reckoner.storage.migrate import migrate
    from reckoner.v1.evidence.rolling import with_database

    name = "reckoner_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(pg.owner_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    owner = with_database(pg.owner_dsn, name)
    migrate(owner)
    return name, owner, with_database(pg.runner_dsn, name)


def _drop_database(pg, name):
    with psycopg.connect(pg.owner_dsn, autocommit=True) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (name,)
        )
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def test_crash_before_persist_resumes_to_identical_evidence_without_duplicates(
    inputs, pg, tmp_path, monkeypatch
):
    from reckoner.v1.evidence import rolling
    from reckoner.v1.evidence.rolling import drop_working_set
    from reckoner.v1.evidence.steps import RunLock

    _environment(monkeypatch, inputs, pg)
    declaration, declared = _declare(inputs, tmp_path)
    original = rolling.persist_documents
    calls = []

    def crash(dsn, documents):
        calls.append(len(documents))
        if len(calls) == 3:
            raise RuntimeError("injected crash after import, before persist")
        return original(dsn, documents)

    monkeypatch.setattr(rolling, "persist_documents", crash)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            _run(declaration)
        partial = _evidence(pg.owner_dsn)
        assert partial and len(partial) == sum(calls[:2])
        monkeypatch.setattr(rolling, "persist_documents", original)
        receipts = declaration.parent / "days-relational.jsonl"
        receipts.unlink()
        with RunLock(pg.owner_dsn, declared["preparation_id"]):
            with pytest.raises(RuntimeError, match="lock"):
                _run(declaration)
        result = _run(declaration)
        assert result["runtime_identity"]["oracle_usage"] is False
        from reckoner.v1.evidence.steps import run as run_step

        drop = Namespace(evidence_step="drop-working-set", declaration=declaration, env_file="-")
        with RunLock(pg.owner_dsn, declared["preparation_id"]):
            with pytest.raises(RuntimeError, match="lock"):
                run_step(drop)  # never terminates a concurrent run's working set
        assert _evidence(pg.owner_dsn)
        assert result["complete_days"] == 2 and result["reconstructed_receipts"] == 2
        resumed = _evidence(pg.owner_dsn)
        keys = [(t, x) for t, x, _ in resumed]
        assert len(keys) == len(set(keys))
        logged = [json.loads(line) for line in receipts.read_text().splitlines()]
        assert [r["receipt_reconstructed"] for r in logged[:2]] == [True, True]
        assert {r["day"] for r in logged} == {
            e["day"] for e in development_days(inputs) if e["day"] <= FIRST_DAYS
        }
        assert all(r["coverage"]["statuses"] == {"available": r["cases"]} for r in logged[2:])
    finally:
        drop_working_set(pg.owner_dsn, declared["preparation_id"])
    name, owner, runner = _second_operational_database(pg)
    try:
        monkeypatch.setenv("RECKONER_OWNER_DSN", owner)
        monkeypatch.setenv("RECKONER_RUNNER_DSN", runner)
        clean, _ = _declare(inputs, tmp_path / "clean")
        _run(clean)
        assert _evidence(owner) == resumed
    finally:
        drop_working_set(owner, declared["preparation_id"])
        _drop_database(pg, name)


def test_query_document_differing_from_its_source_row_fails_with_no_partial_rows(
    inputs, pg, tmp_path, monkeypatch
):
    from reckoner.v1.evidence import preparation
    from reckoner.v1.evidence.rolling import drop_working_set

    _environment(monkeypatch, inputs, pg)
    declaration, declared = _declare(inputs, tmp_path)
    original = preparation.query_schedule

    def tampered(*args, **kwargs):
        schedule = original(*args, **kwargs)
        case = schedule[0]["cases"][0]
        case["transaction"] = {**case["transaction"], "amount_minor": 1}
        return schedule

    monkeypatch.setattr(preparation, "query_schedule", tampered)
    try:
        with pytest.raises(ValueError, match="source canonical"):
            _run(declaration)
        assert _evidence(pg.owner_dsn) == []
        with psycopg.connect(pg.owner_dsn) as connection:
            assert (
                connection.execute("SELECT count(*) FROM reckoner.transactions").fetchone()[0] == 0
            )
    finally:
        drop_working_set(pg.owner_dsn, declared["preparation_id"])
