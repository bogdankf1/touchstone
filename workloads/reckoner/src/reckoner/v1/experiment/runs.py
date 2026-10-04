"""Run declaration consumes tenant activation as a future-run default only.

With no explicit configuration, a new run freezes the tenant's currently active
configuration into its immutable experiment manifest. An already declared run is
returned as stored: the active pointer is never reread for it. Explicit selection
stays reproducible regardless of activation.
"""

from reckoner.contracts import content_id


def _active(repo, tenant_id):
    row = repo._connection.execute(
        "SELECT config_id, version FROM reckoner.v1_active_configuration WHERE tenant_id=%s",
        (tenant_id,),
    ).fetchone()
    if row is None or row["config_id"] is None:
        raise LookupError("no active configuration for this tenant; supply an explicit one")
    return row["config_id"], row["version"]


def declare_run(
    repo,
    *,
    tenant_id: str,
    run_id: str,
    purpose: str,
    tasks: list[dict],
    dataset_version: str,
    cohort_version: str,
    code_revision: str,
    created_at: str,
    config_id: str | None = None,
    telemetry_mode: str = "workflow",
) -> dict:
    """Owner action: declare (or return) one immutable run with a frozen configuration."""
    stored = repo._connection.execute(
        "SELECT document FROM reckoner.v1_runs WHERE tenant_id=%s AND run_id=%s",
        (tenant_id, run_id),
    ).fetchone()
    if stored is not None:
        manifest = stored["document"]
        if config_id is not None and config_id != manifest["config_id"]:
            raise ValueError("an existing run keeps its frozen configuration")
        if [dict(t) for t in tasks] != manifest["tasks"] or purpose != manifest["purpose"]:
            raise ValueError("an existing run keeps its frozen tasks and purpose")
        return {"manifest": manifest, "config_source": "declared", "activation_version": None}
    source, version = "explicit", None
    if config_id is None:
        config_id, version = _active(repo, tenant_id)
        source = "active"
    manifest = {
        "schema_version": "reckoner-experiment-v1",
        "tenant_id": tenant_id,
        "run_id": run_id,
        "purpose": purpose,
        "config_id": config_id,
        "dataset_simulated": True,
        "dataset_version": dataset_version,
        "cohort_version": cohort_version,
        "code_revision": code_revision,
        "created_at": created_at,
        "tasks": [dict(t) for t in tasks],
    }
    manifest["experiment_id"] = content_id(manifest)
    created = repo.create_run(manifest, config_id, telemetry_mode=telemetry_mode)
    return {"manifest": created, "config_source": source, "activation_version": version}
