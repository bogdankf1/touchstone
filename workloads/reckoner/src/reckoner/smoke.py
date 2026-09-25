"""Fixed fabricated deployment check, confined to a disposable smoke database."""

import json
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from reckoner.baseline.runner import execute_run, preflight
from reckoner.contracts import content_id, validate_cohort_manifest, validate_document
from reckoner.resources import PACKAGE, SCHEMAS
from reckoner.storage.postgres import PostgresRepository, _insert_document
from reckoner.telemetry.otlp import export_run

RUN_ID = "fabricated-smoke-v1"


def is_smoke_database(dsn: str) -> bool:
    return conninfo_to_dict(dsn).get("dbname", "").startswith("reckoner_smoke_")


class FabricatedProvider:
    """No transport: every fabricated purchase is escalated with synthetic usage."""

    provider_name = "fake"
    is_fake = True

    def count_input(self, request):
        return 31

    def generate(self, request):
        return {
            "provider_request_id": "fake-" + content_id(request),
            "requested_model": request["model"],
            "reported_model": request["model"].split("/", 1)[1],
            "finish_reason": "stop",
            "content": '{"outcome":"escalate"}',
            "input_tokens": 31,
            "output_tokens": 7,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }


def smoke(environment: dict[str, str], output: Path, documents: tuple) -> dict:
    owner_dsn = environment.get("RECKONER_OWNER_DSN", "")
    runner_dsn = environment.get("RECKONER_RUNNER_DSN", "")
    if not owner_dsn or not runner_dsn:
        raise ValueError("invalid environment")
    if not all(is_smoke_database(dsn) for dsn in (owner_dsn, runner_dsn)):
        raise ValueError("smoke requires a dedicated reckoner_smoke_ database")
    owner_target = conninfo_to_dict(owner_dsn)
    runner_target = conninfo_to_dict(runner_dsn)
    if any(owner_target.get(key) != runner_target.get(key) for key in ("host", "port", "dbname")):
        raise ValueError("smoke database targets differ")
    fixture = json.loads((PACKAGE / "fixtures/smoke-v1.json").read_text())
    bundle_id = content_id({"fabricated_fixture": fixture})
    config, price, thresholds = documents
    with PostgresRepository(owner_dsn) as owner:
        with owner._connection.transaction():
            cursor = owner._connection.cursor()
            if cursor.execute(
                "SELECT 1 FROM reckoner.runs WHERE run_id <> %s", (RUN_ID,)
            ).fetchone():
                raise ValueError("smoke database contains another experiment")
            for row in fixture:
                transaction = row["transaction"]
                validate_document(transaction, SCHEMAS / "transaction-v1.schema.json")
                tenant, identity = transaction["tenant_id"], transaction["transaction_id"]
                _insert_document(
                    cursor,
                    "reckoner.transactions",
                    ("tenant_id", "transaction_id"),
                    (tenant, identity),
                    ("tenant_id", "transaction_id"),
                    transaction,
                )
                oracle = {
                    "schema_version": "oracle-v1",
                    "tenant_id": tenant,
                    "transaction_id": identity,
                    "label": row["label"],
                    "oracle_version": "fabricated-smoke-v1",
                    "source_file_sha256": transaction["provenance"]["source_file_sha256"],
                    "source_record": transaction["provenance"]["source_record"],
                }
                validate_document(oracle, SCHEMAS / "oracle-v1.schema.json")
                _insert_document(
                    cursor,
                    "oracle.oracle_labels",
                    ("tenant_id", "transaction_id", "oracle_version", "label"),
                    (tenant, identity, oracle["oracle_version"], row["label"]),
                    ("tenant_id", "transaction_id", "oracle_version"),
                    oracle,
                )
            for tenant in ("tenant-a", "tenant-b"):
                selected = [
                    row["transaction"]["transaction_id"]
                    for row in fixture
                    if row["transaction"]["tenant_id"] == tenant
                ]
                document = json.loads((PACKAGE / "fixtures/smoke-cohort-v1.json").read_text())
                document.update(tenant_id=tenant, selected_transaction_ids=selected)
                document["manifest_id"] = content_id(document)
                validate_cohort_manifest(document, SCHEMAS / "cohort-manifest-v1.schema.json")
                _insert_document(
                    cursor,
                    "reckoner.cohorts",
                    ("tenant_id", "cohort_id", "purpose", "bundle_id"),
                    (tenant, RUN_ID, "baseline", bundle_id),
                    ("tenant_id", "cohort_id"),
                    document,
                )
                for identity in selected:
                    cursor.execute(
                        "INSERT INTO reckoner.cohort_members "
                        "(tenant_id, cohort_id, transaction_id) VALUES (%s,%s,%s) "
                        "ON CONFLICT DO NOTHING",
                        (tenant, RUN_ID, identity),
                    )
            for threshold in thresholds:
                owner.register_threshold_config(threshold)
            owner.create_run(
                RUN_ID, "baseline", config, bundle_id, price=price, execution_mode="test"
            )
    with PostgresRepository(runner_dsn) as runner:
        provider = FabricatedProvider()
        preflight(runner, provider, RUN_ID)
        result = execute_run(runner, provider, RUN_ID)
        export_run(runner, RUN_ID, output)
    return {"run_id": RUN_ID, "provider_call_mode": "fake", **result}
