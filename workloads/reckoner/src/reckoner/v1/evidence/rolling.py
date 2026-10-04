"""Postgres rolling working set for real-archive relational evidence (simulated data).

The working set holds [D - 97 days, D + 1 day) for the query day D being prepared, plus
the previous-card rows of D's queries when they fall outside it. Every cutoff function
filters strictly by query time, so later rows are harmless and evicting before D - 97
days keeps every row a query on D can read. Each day import (rows, resolutions, vectors,
then raising `history_until`) and each eviction (raising `history_from`, then deleting)
is one transaction, so coverage never claims rows that are absent.
"""

import json
import shutil
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from time import perf_counter

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from reckoner.contracts import content_id
from reckoner.v1.benchmark.queries import candidate_vectors
from reckoner.v1.benchmark.resources import FREE_FLOOR, ResourceGuardError
from reckoner.v1.contracts import validate_v1
from reckoner.v1.data.history import POLICY, historical_resolution, instant
from reckoner.v1.evidence.assemble import query_transaction
from reckoner.v1.evidence.postgres import PostgresEvidence
from reckoner.v1.evidence.preparation import (
    TENANTS,
    UNIFORM_INSTANT,
    day_start,
    iso,
    window,
)

WS_PREFIX = "reckoner_ws_"
WS_COMMENT = "reckoner-evidence-working-set:"
RUNTIME_ROLE = "reckoner_runner"
# A hung statement fails instead of blocking a run. Measured worst case is about 9 s for a
# whole case (several statements); ten minutes per statement is far above it.
STATEMENT_TIMEOUT_MS = 600_000
DAY = timedelta(days=1)


def working_set_name(preparation_id: str) -> str:
    return WS_PREFIX + preparation_id[:12]


def with_database(dsn: str, database: str) -> str:
    values = conninfo_to_dict(dsn)
    values["dbname"] = database
    return make_conninfo(**values)


def runtime_dsn(dsn: str) -> str:
    """Runner role plus a generous statement timeout, through libpq options."""
    from reckoner.v1.benchmark.steps import runtime_conninfo

    values = conninfo_to_dict(runtime_conninfo(dsn, RUNTIME_ROLE))
    values["options"] = f"{values['options']} -c statement_timeout={STATEMENT_TIMEOUT_MS}"
    return make_conninfo(**values)


def progress(message: str) -> None:
    """Timestamped, flushed progress line on stderr (also from assembly children)."""
    from datetime import UTC, datetime

    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {message}", file=sys.stderr, flush=True)


def _comment(connection, name):
    return connection.execute(
        "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=%s", (name,)
    ).fetchone()


def ensure_working_set(owner_dsn: str, preparation_id: str) -> str:
    """Create (or reopen) this preparation's working-set database, migrated and marked."""
    from reckoner.storage.migrate import migrate

    name = working_set_name(preparation_id)
    with psycopg.connect(owner_dsn, autocommit=True) as connection:
        found = _comment(connection, name)
        if found is None:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        if found is None or found[0] is None:
            # A crash between CREATE DATABASE and COMMENT leaves this exact name uncommented;
            # only this preparation's derived name is ever adopted.
            connection.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(name), sql.Literal(WS_COMMENT + preparation_id)
                )
            )
        elif found[0] != WS_COMMENT + preparation_id:
            raise ValueError(f"refusing working-set database {name}: owner comment differs")
    dsn = with_database(owner_dsn, name)
    migrate(dsn)
    return dsn


def drop_working_set(owner_dsn: str, preparation_id: str, name: str | None = None) -> dict:
    """Drop only `reckoner_ws_<prep12>` whose comment names this exact preparation."""
    expected = working_set_name(preparation_id)
    name = expected if name is None else name
    if name != expected:
        raise ValueError(f"refusing to drop {name}: not this preparation's working set")
    with psycopg.connect(owner_dsn, autocommit=True) as connection:
        found = _comment(connection, name)
        if found is None:
            return {"dropped": None, "reason": "absent"}
        if found[0] != WS_COMMENT + preparation_id:
            raise ValueError(f"refusing to drop {name}: owner comment differs")
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname=%s AND pid <> pg_backend_pid()",
            (name,),
        )
        connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
    return {"dropped": name, "comment": WS_COMMENT + preparation_id}


class ChildDied(RuntimeError):
    """The assembly child exited without a result (for example an out-of-memory kill)."""


def isolated(function, *args):
    """Run one call in a freshly spawned process and return its result.

    Assembly reads full neighbour and resolution documents, a transient of several hundred
    megabytes per day, and a long-lived process retains about 2.5 MB of native memory per
    assembled case. A child that exits returns all of it, so a multi-day pass stays flat.
    A child that dies (a cgroup OOM kill is SIGKILL) fails the call at once; a worker pool
    would silently replace it and wait forever.
    """
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool

    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=1, mp_context=context) as pool:
        try:
            return pool.submit(function, *args).result()
        except BrokenProcessPool as error:
            raise ChildDied(
                "assembly child process died before returning (for example an out-of-memory "
                "kill); nothing for this day was persisted"
            ) from error


def relational_documents(runner_dsn: str, transactions, config) -> list[dict]:
    relational = PostgresEvidence(runner_dsn)
    documents = []
    for number, tx in enumerate(transactions, start=1):
        documents.append(relational.for_task({"transaction": tx}, config))
        progress(f"assembled relational case {number}/{len(transactions)}")
    return documents


class StoreGuard:
    """In-band check run before every commit: free disk floor and cluster byte budget."""

    def __init__(
        self,
        *,
        free_path,
        free_floor_bytes=FREE_FLOOR,
        max_store_bytes=None,
        vm_free_path=None,
        vm_free_floor_bytes=FREE_FLOOR,
    ):
        """`free_path` is a host bind (APFS); `vm_free_path` is the job container's own
        filesystem, on the Docker Desktop VM disk where the store volumes live."""
        self.free_path = Path(free_path)
        self.free_floor_bytes = free_floor_bytes
        self.max_store_bytes = max_store_bytes
        self.vm_free_path = Path(vm_free_path) if vm_free_path else None
        self.vm_free_floor_bytes = vm_free_floor_bytes

    def __call__(self, connection=None) -> dict:
        record = {
            "free_bytes": shutil.disk_usage(self.free_path).free,
            "free_floor_bytes": self.free_floor_bytes,
        }
        if connection is not None:
            databases, wal = connection.execute(
                "SELECT (SELECT sum(pg_database_size(datname))::bigint FROM pg_database), "
                "(SELECT coalesce(sum(size), 0)::bigint FROM pg_ls_waldir())"
            ).fetchone()
            record.update(
                database_bytes=databases, wal_bytes=wal, max_store_bytes=self.max_store_bytes
            )
            if self.max_store_bytes is not None and databases + wal > self.max_store_bytes:
                raise ResourceGuardError(f"store byte budget exceeded: {json.dumps(record)}")
        if self.vm_free_path is not None:
            record.update(
                vm_free_bytes=shutil.disk_usage(self.vm_free_path).free,
                vm_free_floor_bytes=self.vm_free_floor_bytes,
            )
            if record["vm_free_bytes"] < self.vm_free_floor_bytes:
                raise ResourceGuardError(f"VM free disk below floor: {json.dumps(record)}")
        if record["free_bytes"] < self.free_floor_bytes:
            raise ResourceGuardError(f"free disk below floor: {json.dumps(record)}")
        return record


class SourceDays:
    """Privileged owner reader of canonical rows and simulated labels by time range."""

    def __init__(self, history, bundle: Path):
        self.history = history
        self.labels = sqlite3.connect(
            f"file:{Path(bundle) / 'resolutions.sqlite'}?mode=ro", uri=True
        )

    def close(self):
        self.labels.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def resolution(self, tx: dict) -> dict:
        row = self.labels.execute(
            "SELECT label FROM resolutions WHERE source_record=?",
            (tx["provenance"]["source_record"],),
        ).fetchone()
        if row is None:
            raise LookupError("historical resolution unavailable")
        return historical_resolution(tx, row[0], POLICY)

    def records(self, start, end) -> list[tuple[dict, dict]]:
        # Day ranges compare text, which equals instant order only for one uniform form.
        rows = self.history.connection.execute(
            "SELECT source_record, occurred_at FROM history WHERE occurred_at>=? "
            "AND occurred_at<? ORDER BY occurred_at, source_record",
            (iso(start), iso(end)),
        ).fetchall()
        odd = [stamp for _, stamp in rows if not UNIFORM_INSTANT.fullmatch(stamp)]
        if (
            odd
            or self.history.connection.execute(
                "SELECT 1 FROM history WHERE occurred_at>=? AND occurred_at<? "
                "AND length(occurred_at) <> 20 LIMIT 1",
                (iso(start - DAY), iso(end + DAY)),
            ).fetchone()
        ):
            raise ValueError(f"non-uniform source timestamp format near {iso(start)}: {odd[:3]}")
        pairs = []
        for ordinal, _ in rows:
            tx = self.history.transaction(ordinal)
            pairs.append((tx, self.resolution(tx)))
        return pairs

    def canonical(self, tx: dict) -> dict:
        return self.history.transaction(tx["provenance"]["source_record"])


SLIM = ("tenant_id", "transaction_id", "card_id", "occurred_at", "amount_minor")


def _slim(tx):
    return {key: tx[key] for key in SLIM}


class CardHistory:
    """Per-card strict history for feature vectors, fed day by day in time order.

    Equivalent to `candidate_vectors` (benchmark.queries) over complete source history: a
    card is loaded from the source on first use (its rows in the 30 days before that use and
    the latest earlier row); afterwards every imported row of that card is appended in time
    order. A vector uses only rows strictly before its transaction. Calling
    `candidate_vectors` per day instead re-reads each card's whole history every day.
    """

    def __init__(self, history, scaler):
        self.history, self.scaler, self.cards = history, scaler, {}

    def _load(self, tenant, card, at):
        lower = iso(at - timedelta(days=30))
        rows = sorted(
            (_slim(r) for r in self.history.card_before(tenant, card, iso(at), since=lower)),
            key=lambda r: (r["occurred_at"], r["transaction_id"]),
        )
        previous = self.history.previous_card(tenant, card, lower)
        return {"rows": rows, "previous": _slim(previous) if previous else None, "cursor": at}

    def vectors(self, pairs) -> list[tuple[dict, list[float]]]:
        from reckoner.v1.evidence.features import feature_vector

        result = []
        ordered = sorted(
            (tx for tx, _ in pairs), key=lambda tx: (tx["occurred_at"], tx["transaction_id"])
        )
        for tx in ordered:
            at, key = instant(tx["occurred_at"]), (tx["tenant_id"], tx["card_id"])
            entry = self.cards.get(key)
            if entry is None:
                entry = self.cards[key] = self._load(*key, at)
            elif at < entry["cursor"]:
                raise ValueError("card history must be fed in time order")
            lower = at - timedelta(days=30)
            while entry["rows"] and instant(entry["rows"][0]["occurred_at"]) < lower:
                entry["previous"] = entry["rows"].pop(0)
            earlier = [r for r in entry["rows"] if instant(r["occurred_at"]) < at]
            if tx["amount_minor"] > 0:
                history = {
                    "status": "available",
                    "transactions": earlier,
                    "previous": earlier[-1] if earlier else entry["previous"],
                }
                result.append((tx, feature_vector(tx, history, self.scaler)))
            entry["rows"].append(_slim(tx))
            entry["cursor"] = at
        return result


STAGE = (
    "CREATE TEMP TABLE ws_stage (tenant_id text, transaction_id text, occurred_at timestamptz, "
    "account_id text, card_id text, merchant_id text, document jsonb, resolved_at timestamptz, "
    "resolution jsonb) ON COMMIT DROP"
)
STAGE_COPY = (
    "COPY ws_stage (tenant_id, transaction_id, occurred_at, account_id, card_id, merchant_id, "
    "document, resolved_at, resolution) FROM STDIN"
)
VECTOR_STAGE = (
    "CREATE TEMP TABLE ws_vectors (tenant_id text, transaction_id text, features vector(11)) "
    "ON COMMIT DROP"
)
CONFLICTS = """
SELECT s.transaction_id FROM ws_stage s
 JOIN reckoner.transactions t USING (tenant_id, transaction_id)
 WHERE t.document <> s.document
UNION ALL
SELECT s.transaction_id FROM ws_stage s JOIN reckoner.v1_history h USING (tenant_id, transaction_id)
 WHERE h.document <> s.document
UNION ALL
SELECT s.transaction_id FROM ws_stage s
 JOIN oracle.v1_resolutions r USING (tenant_id, transaction_id)
 WHERE r.resolution_policy_version = %s AND r.document <> s.resolution
LIMIT 1
"""


class RollingStore:
    def __init__(
        self, owner_dsn, runner_dsn, *, source, scaler, snapshot_id, tenants=TENANTS, guard=None
    ):
        self.owner_dsn = owner_dsn
        self.runner_dsn = runtime_dsn(runner_dsn)
        self.source, self.scaler, self.snapshot_id = source, scaler, snapshot_id
        self.tenants = tuple(tenants)
        self.guard = guard or (lambda connection=None: {})
        self.cards = CardHistory(source.history, scaler)

    # -- static declarations -------------------------------------------------------

    def initialize(self, identities: list[dict]) -> int:
        """Shared-merchant declarations for every merchant, idempotent with conflict check."""
        with psycopg.connect(self.owner_dsn) as connection:
            connection.execute(
                "CREATE TEMP TABLE ws_identities (tenant_id text, merchant_id text, identity text) "
                "ON COMMIT DROP"
            )
            with (
                connection.cursor() as cursor,
                cursor.copy("COPY ws_identities FROM STDIN") as copy,
            ):
                for item in identities:
                    copy.write_row((item["tenant_id"], item["merchant_id"], item["identity"]))
            conflict = connection.execute(
                "SELECT s.merchant_id FROM ws_identities s JOIN reckoner.v1_merchant_identities m "
                "USING (tenant_id, merchant_id) WHERE m.identity <> s.identity LIMIT 1"
            ).fetchone()
            if conflict:
                raise ValueError("shared-merchant declaration conflict")
            inserted = connection.execute(
                "INSERT INTO reckoner.v1_merchant_identities SELECT * FROM ws_identities "
                "ON CONFLICT DO NOTHING"
            ).rowcount
            self.guard(connection)
        return inserted

    def coverage(self) -> dict:
        with psycopg.connect(self.owner_dsn, row_factory=dict_row) as connection:
            return {
                row["tenant_id"]: row
                for row in connection.execute("SELECT * FROM reckoner.v1_evidence_coverage")
            }

    def runtime_identity(self) -> dict:
        from reckoner.v1.benchmark.steps import check_runtime_identity, runtime_identity

        with psycopg.connect(self.runner_dsn) as connection:
            return check_runtime_identity(runtime_identity(connection), RUNTIME_ROLE)

    # -- writes ------------------------------------------------------------------

    def _write(self, connection, pairs, *, in_order=True) -> dict:
        timings = {}
        started = perf_counter()
        if in_order:
            vectors = self.cards.vectors(pairs)
        else:  # rows older than the window (previous-card rows) bypass the day cache
            positive = [tx for tx, _ in pairs if tx["amount_minor"] > 0]
            vectors = list(candidate_vectors(positive, self.source.history, self.scaler))
        timings["vector_seconds"] = perf_counter() - started
        started = perf_counter()
        connection.execute(STAGE)
        with connection.cursor() as cursor, cursor.copy(STAGE_COPY) as copy:
            for tx, resolution in pairs:
                copy.write_row(
                    (
                        tx["tenant_id"],
                        tx["transaction_id"],
                        tx["occurred_at"],
                        tx["account_id"],
                        tx["card_id"],
                        tx["merchant_id"],
                        json.dumps(tx),
                        resolution["resolved_at"],
                        json.dumps(resolution),
                    )
                )
        if connection.execute(CONFLICTS, (POLICY,)).fetchone():
            raise ValueError("working-set canonical record conflict")
        connection.execute(
            "INSERT INTO reckoner.transactions SELECT tenant_id, transaction_id, document "
            "FROM ws_stage ON CONFLICT DO NOTHING"
        )
        rows = connection.execute(
            "INSERT INTO reckoner.v1_history SELECT tenant_id, transaction_id, occurred_at, "
            "account_id, card_id, merchant_id, document FROM ws_stage ON CONFLICT DO NOTHING"
        ).rowcount
        connection.execute(
            "INSERT INTO oracle.v1_resolutions SELECT tenant_id, transaction_id, %s, resolved_at, "
            "resolution FROM ws_stage ON CONFLICT DO NOTHING",
            (POLICY,),
        )
        connection.execute(VECTOR_STAGE)
        with connection.cursor() as cursor, cursor.copy("COPY ws_vectors FROM STDIN") as copy:
            for tx, vector in vectors:
                copy.write_row((tx["tenant_id"], tx["transaction_id"], str(vector)))
        if connection.execute(
            "SELECT 1 FROM ws_vectors s "
            "JOIN reckoner.v1_vectors v USING (tenant_id, transaction_id) "
            "WHERE v.scaler_id=%s AND v.features <> s.features LIMIT 1",
            (self.scaler["scaler_id"],),
        ).fetchone():
            raise ValueError("working-set vector conflict")
        stored = connection.execute(
            "INSERT INTO reckoner.v1_vectors SELECT tenant_id, transaction_id, %s, 'features-v1', "
            "features FROM ws_vectors ON CONFLICT DO NOTHING",
            (self.scaler["scaler_id"],),
        ).rowcount
        timings["write_seconds"] = perf_counter() - started
        return {"rows": rows, "vectors": stored, "source_rows": len(pairs), **timings}

    def import_day(self, start) -> dict:
        """One transaction: the day's rows, labels and vectors, then raise `history_until`."""
        end = start + DAY
        began = perf_counter()
        pairs = self.source.records(start, end)
        read = perf_counter() - began
        try:
            return self._import_day(start, end, pairs, read)
        except BaseException:
            # The day's rows may already be in the card cache; a retry reloads from source.
            self.cards = CardHistory(self.source.history, self.scaler)
            raise

    def _import_day(self, start, end, pairs, read) -> dict:
        with psycopg.connect(self.owner_dsn) as connection:
            stats = self._write(connection, pairs)
            for tenant in self.tenants:
                moved = connection.execute(
                    "INSERT INTO reckoner.v1_evidence_coverage AS c (tenant_id, history_from, "
                    "history_until, previous_card_complete, source_snapshot_id) "
                    "VALUES (%s,%s,%s,true,%s) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET history_until=EXCLUDED.history_until "
                    "WHERE c.history_until=%s AND c.source_snapshot_id=EXCLUDED.source_snapshot_id "
                    "AND c.previous_card_complete RETURNING tenant_id",
                    (tenant, start, end, self.snapshot_id, start),
                ).fetchone()
                if moved is None:
                    raise ValueError(f"working-set coverage is not contiguous at {iso(start)}")
            guard = self.guard(connection)
        return {"day": iso(start), "read_seconds": read, **stats, "guard": guard}

    def advance_to(self, day: str, progress=None) -> list[dict]:
        target = day_start(day) + DAY
        coverage = self.coverage()
        if coverage:
            untils = {row["history_until"] for row in coverage.values()}
            if set(coverage) != set(self.tenants) or len(untils) != 1:
                raise ValueError("working-set coverage differs between tenants")
            cursor = untils.pop()
        else:
            cursor = window(day)[0]
        imported = []
        while cursor < target:
            imported.append(self.import_day(cursor))
            if progress:
                progress(f"imported source day {iso(cursor)[:10]}: {imported[-1]['rows']} rows")
            cursor += DAY
        return imported

    def evict_before(self, boundary) -> dict:
        """Raise `history_from` first, then delete older rows, in one transaction."""
        started = perf_counter()
        with psycopg.connect(self.owner_dsn) as connection:
            moved = connection.execute(
                "UPDATE reckoner.v1_evidence_coverage SET history_from=%s "
                "WHERE history_from<=%s AND history_until>%s RETURNING tenant_id",
                (boundary, boundary, boundary),
            ).fetchall()
            if sorted(r[0] for r in moved) != sorted(self.tenants):
                raise ValueError(f"cannot evict before {iso(boundary)}: coverage is not monotonic")
            vectors = connection.execute(
                "DELETE FROM reckoner.v1_vectors v USING reckoner.v1_history h "
                "WHERE (v.tenant_id, v.transaction_id)=(h.tenant_id, h.transaction_id) "
                "AND h.occurred_at<%s",
                (boundary,),
            ).rowcount
            connection.execute(
                "DELETE FROM oracle.v1_resolutions r USING reckoner.v1_history h "
                "WHERE (r.tenant_id, r.transaction_id)=(h.tenant_id, h.transaction_id) "
                "AND h.occurred_at<%s",
                (boundary,),
            )
            rows = connection.execute(
                "WITH gone AS (DELETE FROM reckoner.v1_history WHERE occurred_at<%s "
                "RETURNING tenant_id, transaction_id) DELETE FROM reckoner.transactions t "
                "USING gone g "
                "WHERE (t.tenant_id, t.transaction_id)=(g.tenant_id, g.transaction_id)",
                (boundary,),
            ).rowcount
            guard = self.guard(connection)
        deleted = perf_counter() - started
        started = perf_counter()
        with psycopg.connect(self.owner_dsn, autocommit=True) as connection:
            connection.execute(
                "VACUUM (ANALYZE) reckoner.v1_vectors, oracle.v1_resolutions, "
                "reckoner.v1_history, reckoner.transactions"
            )
        return {
            "boundary": iso(boundary),
            "rows": rows,
            "vectors": vectors,
            "delete_seconds": deleted,
            "vacuum_seconds": perf_counter() - started,
            "guard": guard,
        }

    def import_previous_cards(self, transactions) -> dict:
        """A query's previous card row may be older than the window; import it (and ties)."""
        coverage = self.coverage()
        pairs = {}
        for tx in transactions:
            lower = coverage[tx["tenant_id"]]["history_from"]
            previous = self.source.history.previous_card(
                tx["tenant_id"], tx["card_id"], tx["occurred_at"]
            )
            if previous is None or instant(previous["occurred_at"]) >= lower:
                continue
            for row in self.source.history.card_before(
                tx["tenant_id"], tx["card_id"], tx["occurred_at"], since=previous["occurred_at"]
            ):
                if instant(row["occurred_at"]) < lower:
                    pairs[(row["tenant_id"], row["transaction_id"])] = (
                        row,
                        self.source.resolution(row),
                    )
        if not pairs:
            return {"rows": 0, "vectors": 0, "source_rows": 0}
        with psycopg.connect(self.owner_dsn) as connection:
            stats = self._write(connection, [pairs[k] for k in sorted(pairs)], in_order=False)
            stats["guard"] = self.guard(connection)
        return stats

    def verify_queries(self, transactions) -> None:
        """Runtime documents must equal their source-derived canonical rows."""
        for tx in transactions:
            if self.source.canonical(tx) != tx:
                raise ValueError(
                    "query runtime document differs from its source canonical row: "
                    + tx["transaction_id"]
                )

    def evidence(self, transactions, config) -> list[dict]:
        # A query evicted with its day must still assemble, as explicitly unavailable, and a
        # previous-card row outside the window is imported first (not left to the caller).
        import_queries(self.owner_dsn, transactions)
        self.previous = self.import_previous_cards(transactions)
        return isolated(relational_documents, self.runner_dsn, transactions, config)


# --- operational database ---------------------------------------------------------


def import_queries(owner_dsn: str, transactions) -> int:
    """Owner import of canonical query documents into the database `owner_dsn` names
    (operational or working set), refusing any conflicting stored document."""
    with psycopg.connect(owner_dsn) as connection:
        inserted = 0
        for tx in transactions:
            key = (tx["tenant_id"], tx["transaction_id"])
            inserted += connection.execute(
                "INSERT INTO reckoner.transactions VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (*key, json.dumps(tx)),
            ).rowcount
            stored = connection.execute(
                "SELECT document FROM reckoner.transactions "
                "WHERE tenant_id=%s AND transaction_id=%s",
                key,
            ).fetchone()[0]
            if stored != tx:
                raise ValueError("operational canonical record conflict")
    return inserted


def persist_documents(runner_dsn: str, documents) -> list[str]:
    from reckoner.v1.storage.repository import V1Repository

    with V1Repository(runtime_dsn(runner_dsn)) as repo:
        return [repo.persist_evidence(document) for document in documents]


SUMMARY_SQL = """
SELECT jsonb_build_object(
  'tenant_id', e.tenant_id, 'transaction_id', e.transaction_id, 'evidence_id', e.evidence_id,
  'coverage', d.coverage, 'source_snapshot_ids', d.source_snapshot_ids,
  'graph_projection', CASE WHEN d.graph_projection IS NULL THEN NULL ELSE
    jsonb_build_object(
      'cutoff', d.graph_projection->'cutoff',
      'projection_id', d.graph_projection->'projection_id',
      'page_rank_converged', d.graph_projection->'page_rank_converged',
      'snapshot_age_seconds', d.graph_projection->'snapshot_age_seconds') END,
  'bytes', pg_column_size(e.document))
FROM reckoner.v1_evidence e
-- One decompression per document: the needed top-level fields are extracted once.
CROSS JOIN LATERAL jsonb_to_record(e.document)
  AS d(coverage jsonb, source_snapshot_ids jsonb, graph_projection jsonb)
WHERE d.source_snapshot_ids->>'postgres' = %s
ORDER BY e.tenant_id, e.transaction_id, e.evidence_id
"""


def persisted_summaries(runner_dsn: str, snapshot_id: str) -> list[dict]:
    """Bounded projections of persisted documents (never whole bodies in memory)."""
    with psycopg.connect(runtime_dsn(runner_dsn)) as connection:
        return [row[0] for row in connection.execute(SUMMARY_SQL, (snapshot_id,))]


def facts(summaries) -> list[dict]:
    return [
        {
            "tenant_id": s["tenant_id"],
            "transaction_id": s["transaction_id"],
            "evidence_id": s["evidence_id"],
            "snapshot": s["source_snapshot_ids"]["postgres"],
            "mode": "gds-augmented" if s["graph_projection"] else "relational",
            "graph_cutoff": (s["graph_projection"] or {}).get("cutoff"),
        }
        for s in summaries
    ]


class PersistedRelational:
    """Base adapter for the graph pass: the already-persisted relational document only."""

    def __init__(self, dsn: str, manifests: list[dict]):
        self.dsn = runtime_dsn(dsn)
        self.entries = {}
        for manifest in manifests:
            if manifest["manifest_id"] != content_id(
                {k: v for k, v in manifest.items() if k != "manifest_id"}
            ):
                raise ValueError("relational manifest identity mismatch")
            if manifest["mode"] != "relational":
                raise ValueError("graph base requires a relational manifest")
            for case in manifest["cases"]:
                key = (case["tenant_id"], case["transaction_id"])
                self.entries[key] = (case["evidence_id"], manifest["source_snapshot_id"])

    def for_task(self, task: dict, config: dict) -> dict:
        tx = query_transaction(task)
        key = (tx["tenant_id"], tx["transaction_id"])
        if key not in self.entries:
            raise LookupError("case is not in the declared relational manifest")
        evidence_id, snapshot = self.entries[key]
        with psycopg.connect(self.dsn) as connection:
            row = connection.execute(
                "SELECT document FROM reckoner.v1_evidence WHERE tenant_id=%s AND evidence_id=%s",
                (tx["tenant_id"], evidence_id),
            ).fetchone()
        if row is None:
            raise LookupError("declared relational evidence is not persisted")
        document = validate_v1("evidence", row[0])
        if (
            document["transaction_id"] != tx["transaction_id"]
            or document["query_time"] != tx["occurred_at"]
            or document["source_snapshot_ids"]["postgres"] != snapshot
            or document.get("graph_projection") is not None
            or document["evidence_id"]
            != content_id({k: v for k, v in document.items() if k != "evidence_id"})
        ):
            raise ValueError("persisted relational evidence does not match its manifest")
        return document
