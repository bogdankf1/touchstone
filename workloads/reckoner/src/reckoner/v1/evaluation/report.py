"""Complete-population gate arithmetic and immutable local artifacts."""

from decimal import Decimal

from psycopg.types.json import Jsonb

from reckoner.contracts import content_id


def aggregate(rows, expected_count, authorized):
    complete = len(rows) == expected_count
    schema = sum(r["schema"] == "pass" for r in rows)
    agreements = sum(r.get("agreement") is True for r in rows)
    values = [Decimal(str(r["faithfulness"])) for r in rows if r.get("faithfulness") is not None]
    evaluated = (
        complete and expected_count > 0 and all(r["status"] in {"pass", "fail"} for r in rows)
    )
    mean = (
        float(sum(values) / len(values))
        if len(values) == expected_count and expected_count
        else None
    )
    agreement = agreements / expected_count if expected_count else None
    status = "not-evaluated" if not expected_count or not authorized else "failed"
    if (
        evaluated
        and schema == expected_count
        and agreement >= 0.90
        and mean is not None
        and mean >= 0.90
    ):
        status = "passed"
    return {
        "status": status,
        "expected_count": expected_count,
        "observed_count": len(rows),
        "missing_count": max(0, expected_count - len(rows)),
        "schema_valid_count": schema,
        "schema_validity": schema / expected_count if expected_count else None,
        "agreement_rate": agreement,
        "mean_faithfulness": mean,
        "cases": rows,
        "quality_acceptance": "pending separately approved measured evaluation",
        "dataset_simulated": True,
    }


def persist_report(budget, protocol, report):
    from reckoner.v1.storage.budget import ProviderBudget

    if not isinstance(budget, ProviderBudget):
        return report
    stages = list(protocol.get("stages", {}).values()) or [protocol]
    identities = {(p["tenant_id"], p["run_id"]) for p in stages}
    if len(identities) != 1:
        raise ValueError("evaluation protocol scope mismatch")
    tenant, run = identities.pop()
    report = {
        **report,
        "tenant_id": tenant,
        "run_id": run,
        "protocol_ids": [p["protocol_id"] for p in stages],
    }
    report["evaluation_id"] = content_id(report)
    with budget._connection.transaction():
        budget._connection.execute(
            "INSERT INTO reckoner.v1_note_evaluations "
            "(tenant_id,evaluation_id,run_id,document) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (tenant, report["evaluation_id"], run, Jsonb(report)),
        )
    return report
