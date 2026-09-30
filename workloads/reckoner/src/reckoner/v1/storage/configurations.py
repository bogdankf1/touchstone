"""Immutable server-priced candidates and provider-free stored-score estimates."""

from decimal import Decimal

from jsonschema import ValidationError
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from reckoner.contracts import content_id, validate_threshold_config
from reckoner.resources import SCHEMAS
from reckoner.v1.api.schemas import ConfigurationRequest, OpaqueID
from reckoner.v1.calibration import validate_calibration_context
from reckoner.v1.contracts import validate_v1
from reckoner.v1.workflow.nodes import route
from reckoner.v1.workflow.runner import calibration_context


def models(repo):
    return repo._connection.execute(
        "SELECT * FROM reckoner.api_v1_models ORDER BY purpose,model"
    ).fetchall()


def get_configuration(repo, tenant_id, config_id):
    row = repo._connection.execute(
        "SELECT * FROM reckoner.api_v1_configurations WHERE tenant_id=%s AND config_id=%s",
        (tenant_id, config_id),
    ).fetchone()
    if row is None:
        raise LookupError("configuration not found")
    return row


def create_configuration(repo, document: dict) -> dict:
    request = ConfigurationRequest.model_validate(document)
    config = dict(request.configuration)
    TypeAdapter(OpaqueID).validate_python(config.get("tenant_id"))
    if "config_id" in config or "threshold_config_id" in config:
        raise ValueError("configuration identities are server-generated")
    threshold = {
        "schema_version": "threshold-config-v1",
        "tenant_id": config.get("tenant_id"),
        "currency": "USD",
        "parameters": request.thresholds,
    }
    threshold["config_id"] = content_id(threshold)
    try:
        validate_threshold_config(threshold, SCHEMAS / "threshold-config-v1.schema.json")
    except ValidationError as exc:
        raise ValueError("invalid thresholds") from exc
    config["threshold_config_id"] = threshold["config_id"]
    config["config_id"] = content_id(config)
    config = validate_v1("run-config", config)
    registry = models(repo)
    for part in ("scorer", "note_model", "judge_model"):
        selected = config[part]
        if not any(
            row["provider"] == selected["provider"]
            and row["model"] == selected["model"]
            and row["purpose"] == ("scorer" if part == "scorer" else "note")
            and row["price_table"] == selected["price_table"]
            for row in registry
        ):
            raise ValueError("unsupported or unpriced model")
    artifact = request.calibration
    if config["score_mode"] == "calibrated" and artifact is None:
        artifact = repo._connection.execute(
            "SELECT reckoner.v1_configuration_calibration(%s,%s) AS artifact",
            (config["tenant_id"], config["calibration_id"]),
        ).fetchone()["artifact"]
    qualification = {
        "evidence_mode": request.evidence_mode,
        "data_kind": request.data_kind,
        "calibration": artifact,
    }
    if config["score_mode"] == "calibrated":
        if artifact is None or artifact.get("calibration_id") != config["calibration_id"]:
            raise ValueError("calibration identity mismatch")
        validate_calibration_context(artifact, calibration_context(config, qualification))
    elif request.calibration is not None or config["calibration_id"] is not None:
        raise ValueError("raw score mode requires no calibration")
    with repo._connection.transaction():
        repo._connection.execute(
            "SELECT reckoner.v1_save_configuration(%s,%s,%s)",
            tuple(Jsonb(x) for x in (config, threshold, qualification)),
        )
    return get_configuration(repo, config["tenant_id"], config["config_id"])


def activate_configuration(
    repo, *, tenant_id: str, config_id: str, expected_version: int, idempotency_key: str
) -> dict:
    with repo._connection.transaction():
        return repo._connection.execute(
            "SELECT reckoner.v1_activate_configuration(%s,%s,%s,%s) AS document",
            (tenant_id, config_id, expected_version, idempotency_key),
        ).fetchone()["document"]


def list_configurations(repo, *, tenant_id: str) -> dict:
    items = repo._connection.execute(
        "SELECT * FROM reckoner.api_v1_configurations WHERE tenant_id=%s ORDER BY config_id",
        (tenant_id,),
    ).fetchall()
    activation = repo._connection.execute(
        "SELECT * FROM reckoner.api_v1_activation WHERE tenant_id=%s", (tenant_id,)
    ).fetchone()
    return {
        "items": items,
        "activation": activation or {"tenant_id": tenant_id, "config_id": None, "version": 0},
        "models": models(repo),
    }


def preview_configuration(repo, *, tenant_id: str, run_id: str, config_id: str) -> dict:
    candidate = get_configuration(repo, tenant_id, config_id)
    run = repo._connection.execute(
        "SELECT config_id FROM reckoner.api_v1_runs WHERE tenant_id=%s AND run_id=%s",
        (tenant_id, run_id),
    ).fetchone()
    if run is None:
        raise LookupError("run not found")
    source = get_configuration(repo, tenant_id, run["config_id"])
    # Stored effective scores support threshold-only changes, never a model forecast.
    keys = (
        "feature_version",
        "scaler_id",
        "calibration_id",
        "score_mode",
        "graph_version",
        "retrieval_version",
        "resolution_policy_version",
    )
    comparable = all(
        source["configuration"][k] == candidate["configuration"][k] for k in keys
    ) and all(
        source["configuration"]["scorer"][k] == candidate["configuration"]["scorer"][k]
        for k in ("provider", "model", "question_version")
    )
    counts = {"auto-approve": 0, "auto-decline": 0, "escalate": 0}
    missing = 0
    rows = repo._connection.execute(
        "SELECT * FROM reckoner.api_v1_preview WHERE tenant_id=%s AND run_id=%s",
        (tenant_id, run_id),
    ).fetchall()
    policy = (
        repo._connection.execute(
            "SELECT evidence_mode,data_kind FROM reckoner.api_v1_run_policy "
            "WHERE tenant_id=%s AND run_id=%s",
            (tenant_id, run_id),
        ).fetchone()
        or source["qualification"]
    )
    if candidate["qualification"] is not None:
        if policy is not None:
            comparable = comparable and all(
                policy[key] == candidate["qualification"][key]
                for key in ("evidence_mode", "data_kind")
            )
        elif any(row["probability"] is not None for row in rows):
            # Old unbound scores cannot qualify a new evidence policy.
            comparable = False
    if comparable:
        for row in rows:
            probability = Decimal(row["probability"]) if row["probability"] is not None else None
            missing += probability is None
            outcome = route(
                probability,
                Decimal(row["amount_minor"]) / 100,
                {**candidate["thresholds"]["parameters"], "currency": "USD"},
            )["outcome"]
            counts[outcome] += 1
    return {
        "tenant_id": tenant_id,
        "run_id": run_id,
        "config_id": config_id,
        "estimate": True,
        "status": "available" if comparable else "incompatible_scores",
        "counts": counts if comparable else None,
        "completed_decisions": len(rows),
        "missing_scores": missing if comparable else None,
        "model_cost": None,
        "note_cost": "not_estimated",
        "provider_calls": 0,
    }
