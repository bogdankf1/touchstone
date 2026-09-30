"""Immutable configurations, monotonic activation and provider-free preview."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import psycopg
import pytest
from conftest import CONFIG_DIR
from fastapi.testclient import TestClient
from reckoner.api import create_app
from test_v1_cases_api import case_setup
from v1_fixtures import config_fixture

pytestmark = pytest.mark.integration


def candidate():
    config = config_fixture()
    config.pop("config_id")
    config.pop("threshold_config_id")
    config["scorer"]["price_table"] = json.loads((CONFIG_DIR / "jev-prices-v1.json").read_text())
    for key in ("note_model", "judge_model"):
        config[key]["price_table"] = json.loads(
            (CONFIG_DIR / "anthropic-prices-v1.json").read_text()
        )
    return {
        "configuration": config,
        "thresholds": json.loads((CONFIG_DIR / "thresholds-tenant-a-v1.json").read_text())[
            "parameters"
        ],
        "evidence_mode": "relational",
        "data_kind": "fabricated",
        "calibration": None,
    }


def test_save_activate_history_preview_and_run_pinning(pg):
    decision = case_setup(pg)
    with TestClient(create_app(pg.api_dsn)) as client:
        body = candidate()
        saved = client.post("/v1/configurations", json=body)
        assert saved.status_code == 200
        config_id = saved.json()["configuration"]["config_id"]
        assert client.post("/v1/configurations", json=body).json() == saved.json()
        history = client.get("/v1/configurations", params={"tenant_id": "tenant-a"}).json()
        assert history["activation"] == {"tenant_id": "tenant-a", "config_id": None, "version": 0}
        assert len(history["models"]) == 2
        activation = {
            "tenant_id": "tenant-a",
            "config_id": config_id,
            "expected_version": 0,
            "idempotency_key": "activate/1",
        }
        first = client.post("/v1/configurations/activate", json=activation)
        assert first.status_code == 200
        assert first.json()["version"] == 1
        assert client.post("/v1/configurations/activate", json=activation).json() == first.json()
        assert (
            client.post(
                "/v1/configurations/activate", json={**activation, "idempotency_key": "stale"}
            ).status_code
            == 409
        )
        preview = client.get(
            "/v1/configurations/preview",
            params={"tenant_id": "tenant-a", "run_id": "run-a", "config_id": config_id},
        )
        assert preview.status_code == 200
        assert preview.json()["estimate"] is True
        assert preview.json()["model_cost"] is None
        assert preview.json()["counts"] == {"auto-approve": 0, "auto-decline": 0, "escalate": 1}
        assert preview.json()["missing_scores"] == 1
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        assert (
            owner.execute("SELECT config_id FROM reckoner.v1_runs").fetchone()[0]
            == decision["config_id"]
        )
        assert owner.execute("SELECT count(*) FROM reckoner.v1_provider_calls").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute("DELETE FROM reckoner.v1_configs WHERE config_id=%s", (config_id,))


def test_invalid_model_price_and_calibration_are_rejected(pg):
    case_setup(pg)
    with TestClient(create_app(pg.api_dsn)) as client:
        for field, value in [("model", "unsupported/fake"), ("price_table", {})]:
            body = candidate()
            body["configuration"]["note_model"][field] = value
            assert client.post("/v1/configurations", json=body).status_code == 422
        body = candidate()
        body["configuration"]["score_mode"] = "calibrated"
        body["configuration"]["calibration_id"] = "a" * 64
        assert client.post("/v1/configurations", json=body).status_code == 422
        body = candidate()
        body["thresholds"]["t_high"] = "0.01"
        assert client.post("/v1/configurations", json=body).status_code == 422


def test_activation_race_and_aba_cannot_restore_stale_editor(pg):
    case_setup(pg)
    with TestClient(create_app(pg.api_dsn)) as client:
        body = candidate()
        a = client.post("/v1/configurations", json=body).json()["configuration"]["config_id"]
        body["thresholds"]["review_cost"] = "5.00"
        b = client.post("/v1/configurations", json=body).json()["configuration"]["config_id"]

    def activate(pair):
        key, config_id = pair
        with TestClient(create_app(pg.api_dsn)) as client:
            return client.post(
                "/v1/configurations/activate",
                json={
                    "tenant_id": "tenant-a",
                    "config_id": config_id,
                    "expected_version": 0,
                    "idempotency_key": key,
                },
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(activate, [("a", a), ("b", b)]))
    assert sorted(r.status_code for r in results) == [200, 409]
    winner = next(r.json() for r in results if r.status_code == 200)
    with TestClient(create_app(pg.api_dsn)) as client:
        for version, target in [
            (1, b if winner["config_id"] == a else a),
            (2, winner["config_id"]),
        ]:
            assert (
                client.post(
                    "/v1/configurations/activate",
                    json={
                        "tenant_id": "tenant-a",
                        "config_id": target,
                        "expected_version": version,
                        "idempotency_key": f"next/{version}",
                    },
                ).status_code
                == 200
            )
        assert (
            client.post(
                "/v1/configurations/activate",
                json={
                    "tenant_id": "tenant-a",
                    "config_id": a,
                    "expected_version": 1,
                    "idempotency_key": "stale-after-aba",
                },
            ).status_code
            == 409
        )

        replay = client.post(
            "/v1/configurations/activate",
            json={
                "tenant_id": "tenant-a",
                "config_id": winner["config_id"],
                "expected_version": 0,
                "idempotency_key": "a" if winner["config_id"] == a else "b",
            },
        )
        assert replay.json() == winner
        current = client.get("/v1/configurations", params={"tenant_id": "tenant-a"}).json()
        assert current["activation"]["version"] == 3
        assert current["activation"]["config_id"] == winner["config_id"]


def test_calibration_qualification_cannot_transfer_to_changed_evidence_or_question(pg):
    from reckoner.v1.calibration import fit_calibration
    from test_v1_calibration import rehash, sample

    case_setup(pg)
    rows = sample()
    for row in rows:
        row["context"]["scorer"]["question_version"] = "binary-v1"
    artifact = fit_calibration(rows)
    artifact["qualification"] = {
        "status": "selected",
        "validation_id": "b" * 64,
        "raw_brier": "0.04",
        "candidate_brier": "0.001",
        "raw_log_loss": "0.2",
        "candidate_log_loss": "0.02",
    }
    rehash(artifact)
    register_selected(pg, artifact)
    body = candidate()
    body["configuration"].update(
        scaler_id="a" * 64, score_mode="calibrated", calibration_id=artifact["calibration_id"]
    )
    body["calibration"] = artifact
    with TestClient(create_app(pg.api_dsn)) as client:
        assert client.post("/v1/configurations", json=body).status_code == 200
        for name, value in [
            ("feature_version", "changed"),
            ("graph_version", "changed"),
            ("retrieval_version", "changed"),
        ]:
            bad = deepcopy(body)
            bad["configuration"][name] = value
            assert client.post("/v1/configurations", json=bad).status_code == 422
        bad = deepcopy(body)
        bad["configuration"]["scorer"]["question_version"] = "changed"
        assert client.post("/v1/configurations", json=bad).status_code == 422
        bad = deepcopy(body)
        bad["evidence_mode"] = "gds-augmented"
        assert client.post("/v1/configurations", json=bad).status_code == 422


def test_fake_registry_entry_does_not_claim_an_unsupported_contract(pg):
    from psycopg.types.json import Jsonb
    from v1_fixtures import price_fixture

    case_setup(pg)
    price = price_fixture("fake/model")
    with psycopg.connect(pg.owner_dsn) as owner:
        owner.execute(
            "INSERT INTO reckoner.v1_model_registry VALUES (%s,%s,%s,%s,%s,%s)",
            ("tenant-a", "anthropic", "fake/model", "note", False, Jsonb(price)),
        )
    with TestClient(create_app(pg.api_dsn)) as client:
        history = client.get("/v1/configurations", params={"tenant_id": "tenant-a"}).json()
        assert all(row["model"] != "fake/model" for row in history["models"])
        body = candidate()
        body["configuration"]["note_model"].update(model="fake/model", price_table=price)
        assert client.post("/v1/configurations", json=body).status_code == 422


def test_configuration_storage_interface_uses_api_role(pg):
    from reckoner.v1.storage.configurations import create_configuration
    from reckoner.v1.storage.repository import V1Repository

    case_setup(pg)
    with V1Repository(pg.api_dsn) as repo:
        saved = create_configuration(repo, candidate())
        assert saved["configuration"]["tenant_id"] == "tenant-a"


def test_preview_refuses_changed_score_context_and_does_not_return_calibration_payload(pg):
    case_setup(pg)
    body = candidate()
    body["configuration"]["feature_version"] = "changed"
    with TestClient(create_app(pg.api_dsn)) as client:
        saved = client.post("/v1/configurations", json=body).json()
        preview = client.get(
            "/v1/configurations/preview",
            params={"tenant_id": "tenant-a", "run_id": "run-a", "config_id": saved["config_id"]},
        ).json()
        assert preview["status"] == "incompatible_scores"
        assert preview["counts"] is None
        assert preview["model_cost"] is None


def test_permission_boundary_and_configuration_tenant_scope(pg):
    case_setup(pg)
    with TestClient(create_app(pg.api_dsn)) as client:
        saved = client.post("/v1/configurations", json=candidate()).json()
        assert (
            client.post(
                "/v1/configurations/activate",
                json={
                    "tenant_id": "tenant-b",
                    "config_id": saved["config_id"],
                    "expected_version": 0,
                    "idempotency_key": "cross-tenant",
                },
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/v1/configurations/preview",
                params={
                    "tenant_id": "tenant-b",
                    "run_id": "run-a",
                    "config_id": saved["config_id"],
                },
            ).status_code
            == 404
        )
    for dsn in (pg.api_dsn, pg.runner_dsn):
        with psycopg.connect(dsn, autocommit=True) as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM oracle.oracle_labels")
            for table in (
                "v1_active_configuration",
                "v1_configuration_activations",
                "v1_configuration_details",
                "v1_reviews",
            ):
                assert not connection.execute(
                    "SELECT has_table_privilege(current_user,%s,'INSERT,UPDATE,DELETE')",
                    (f"reckoner.{table}",),
                ).fetchone()[0]
    with psycopg.connect(pg.api_dsn, autocommit=True) as api:
        for table in ("v1_provider_responses", "v1_provider_calls", "v1_note_results", "v1_outbox"):
            assert not api.execute(
                "SELECT has_table_privilege(current_user,%s,'SELECT')", (f"reckoner.{table}",)
            ).fetchone()[0]
    with psycopg.connect(pg.evaluator_dsn) as evaluator:
        assert evaluator.execute("SELECT count(*) FROM oracle.oracle_labels").fetchone()[0] > 0


def test_configuration_view_does_not_expose_raw_calibration_artifact(pg):
    from psycopg.types.json import Jsonb

    decision = case_setup(pg)
    with psycopg.connect(pg.owner_dsn) as owner:
        owner.execute(
            "INSERT INTO reckoner.v1_configuration_details VALUES(%s,%s,%s)",
            (
                "tenant-a",
                decision["config_id"],
                Jsonb(
                    {
                        "evidence_mode": "relational",
                        "data_kind": "fabricated",
                        "calibration": {
                            "calibration_id": "a" * 64,
                            "provider_response": "secret",
                            "qualification": {"status": "selected", "examples": "secret"},
                        },
                    }
                ),
            ),
        )
    with TestClient(create_app(pg.api_dsn)) as client:
        response = client.get("/v1/configurations", params={"tenant_id": "tenant-a"})
        assert response.status_code == 200
        assert "secret" not in response.text
        assert "provider_response" not in response.text


def test_preview_checks_frozen_run_evidence_mode_and_replays_score_without_calls(pg):
    from test_v1_graph_workflow import execute, prepared

    repo, task, _, settings, calls = prepared(pg)
    with repo:
        execute(repo, task, settings)
    assert len(calls) == 1
    with TestClient(create_app(pg.api_dsn)) as client:
        changed = candidate()
        changed["evidence_mode"] = "gds-augmented"
        saved = client.post("/v1/configurations", json=changed).json()
        preview = client.get(
            "/v1/configurations/preview",
            params={
                "tenant_id": "tenant-a",
                "run_id": task["run_id"],
                "config_id": saved["config_id"],
            },
        ).json()
        assert preview["status"] == "incompatible_scores"
        assert preview["counts"] is None
        thresholds = candidate()
        thresholds["thresholds"]["t_high"] = "0.10"
        saved = client.post("/v1/configurations", json=thresholds).json()
        preview = client.get(
            "/v1/configurations/preview",
            params={
                "tenant_id": "tenant-a",
                "run_id": task["run_id"],
                "config_id": saved["config_id"],
            },
        ).json()
        assert preview["status"] == "available"
        assert preview["counts"] == {"auto-approve": 0, "auto-decline": 1, "escalate": 0}
    assert len(calls) == 1
    with psycopg.connect(pg.owner_dsn) as owner:
        assert owner.execute("SELECT count(*) FROM reckoner.v1_provider_calls").fetchone()[0] == 1


def test_threshold_edit_reuses_stored_selected_calibration_without_upload(pg):
    from reckoner.v1.calibration import fit_calibration
    from test_v1_calibration import rehash, sample

    case_setup(pg)
    rows = sample()
    for row in rows:
        row["context"]["scorer"]["question_version"] = "binary-v1"
    artifact = fit_calibration(rows)
    artifact["qualification"] = {
        "status": "selected",
        "validation_id": "b" * 64,
        "raw_brier": "0.04",
        "candidate_brier": "0.001",
        "raw_log_loss": "0.2",
        "candidate_log_loss": "0.02",
    }
    rehash(artifact)
    register_selected(pg, artifact)
    body = candidate()
    body["configuration"].update(
        scaler_id="a" * 64, score_mode="calibrated", calibration_id=artifact["calibration_id"]
    )
    body["calibration"] = artifact
    with TestClient(create_app(pg.api_dsn)) as client:
        first = client.post("/v1/configurations", json=body)
        assert first.status_code == 200
        body.pop("calibration")
        body["thresholds"]["review_cost"] = "5.00"
        saved = client.post("/v1/configurations", json=body)
        assert saved.status_code == 200
        assert saved.json()["config_id"] != first.json()["config_id"]
        assert saved.json()["configuration"]["calibration_id"] == artifact["calibration_id"]
        assert "coefficients" not in saved.text
        for field, value in [
            ("calibration_id", "f" * 64),
            ("feature_version", "changed"),
            ("scaler_id", "b" * 64),
        ]:
            changed = deepcopy(body)
            changed["configuration"][field] = value
            assert client.post("/v1/configurations", json=changed).status_code == 422
        changed = deepcopy(body)
        changed["evidence_mode"] = "gds-augmented"
        assert client.post("/v1/configurations", json=changed).status_code == 422
        changed = deepcopy(body)
        changed["configuration"]["tenant_id"] = "tenant-b"
        assert client.post("/v1/configurations", json=changed).status_code == 422


def test_settings_reuse_calibration_after_owner_registers_existing_bound_run_artifact(pg):
    from test_v1_graph_workflow import calibrated_setup, execute

    repo, task, settings, _ = calibrated_setup(pg)
    with repo:
        execute(repo, task, settings)
    register_selected(pg, settings["calibration"])
    body = candidate()
    body.pop("calibration")
    body["configuration"].update(
        scaler_id="a" * 64,
        score_mode="calibrated",
        calibration_id=settings["calibration"]["calibration_id"],
    )
    body["thresholds"]["review_cost"] = "5.00"
    with TestClient(create_app(pg.api_dsn)) as client:
        saved = client.post("/v1/configurations", json=body)
        assert saved.status_code == 200
        assert (
            saved.json()["configuration"]["calibration_id"]
            == settings["calibration"]["calibration_id"]
        )
        assert "coefficients" not in saved.text


def test_configuration_tenant_id_uses_the_same_bounded_query_identity_contract(pg):
    body = candidate()
    body["configuration"]["tenant_id"] = "x" * 257
    with TestClient(create_app(pg.api_dsn)) as client:
        assert client.post("/v1/configurations", json=body).status_code == 422


def test_model_registry_is_tenant_keyed_and_settings_use_only_selected_tenant(pg):
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        columns = owner.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='reckoner' AND table_name='v1_model_registry'"
        ).fetchall()
        assert ("tenant_id",) in columns
        owner.execute(
            "UPDATE reckoner.v1_model_registry SET contract_supported=false "
            "WHERE tenant_id='tenant-b'"
        )
    with TestClient(create_app(pg.api_dsn)) as client:
        assert (
            len(client.get("/v1/configurations", params={"tenant_id": "tenant-a"}).json()["models"])
            == 2
        )
        assert (
            client.get("/v1/configurations", params={"tenant_id": "tenant-b"}).json()["models"]
            == []
        )
        assert client.post("/v1/configurations", json=candidate()).status_code == 200
        other = candidate()
        other["configuration"]["tenant_id"] = "tenant-b"
        assert client.post("/v1/configurations", json=other).status_code == 422


def sql_configuration():
    from v1_fixtures import identified

    body = candidate()
    threshold = identified(
        {
            "schema_version": "threshold-config-v1",
            "tenant_id": "tenant-a",
            "currency": "USD",
            "parameters": body["thresholds"],
        },
        "config_id",
    )
    config = identified(
        {**body["configuration"], "threshold_config_id": threshold["config_id"]}, "config_id"
    )
    qualification = {key: body[key] for key in ("evidence_mode", "data_kind", "calibration")}
    return config, threshold, qualification


@pytest.mark.parametrize(
    "defect",
    [
        "threshold_order",
        "config_id",
        "threshold_id",
        "unknown_config_key",
        "missing_config_key",
        "limit_type",
        "missing_qualification",
        "raw_with_artifact",
        "unregistered_calibration",
        "sql_null_config",
        "json_null_config",
        "null_model",
        "null_score_mode",
        "null_threshold_value",
        "null_qualification_policy",
    ],
)
def test_direct_api_sql_cannot_save_or_activate_invalid_configuration(pg, defect):
    from psycopg.types.json import Jsonb
    from v1_fixtures import identified

    config, threshold, qualification = sql_configuration()
    if defect == "threshold_order":
        threshold["parameters"]["t_high"] = "0.001"
        identified(threshold, "config_id")
        config["threshold_config_id"] = threshold["config_id"]
    elif defect == "threshold_id":
        threshold["config_id"] = "f" * 64
        config["threshold_config_id"] = threshold["config_id"]
    elif defect == "unknown_config_key":
        config["unknown"] = "not part of the contract"
    elif defect == "missing_config_key":
        config.pop("workflow_version")
    elif defect == "limit_type":
        config["limits"]["maximum_attempts"] = "1"
    elif defect == "missing_qualification":
        qualification = {}
    elif defect == "raw_with_artifact":
        qualification["calibration"] = {"calibration_id": "f" * 64}
    elif defect == "unregistered_calibration":
        config.update(score_mode="calibrated", calibration_id="f" * 64)
        qualification["calibration"] = {"calibration_id": "f" * 64}
    elif defect == "null_model":
        config["scorer"] = None
    elif defect == "null_score_mode":
        config["score_mode"] = None
    elif defect == "null_threshold_value":
        threshold["parameters"]["amount_aware"] = None
        identified(threshold, "config_id")
        config["threshold_config_id"] = threshold["config_id"]
    elif defect == "null_qualification_policy":
        qualification["data_kind"] = None
    identified(config, "config_id")
    if defect == "config_id":
        config["config_id"] = "f" * 64
    payload = tuple(Jsonb(doc) for doc in (config, threshold, qualification))
    if defect == "sql_null_config":
        payload = (None, payload[1], payload[2])
    elif defect == "json_null_config":
        payload = (Jsonb(None), payload[1], payload[2])
    with psycopg.connect(pg.api_dsn, autocommit=True) as api:
        with pytest.raises(psycopg.errors.CheckViolation):
            api.execute("SELECT reckoner.v1_save_configuration(%s,%s,%s)", payload)
        with pytest.raises(psycopg.errors.NoDataFound):
            api.execute(
                "SELECT reckoner.v1_activate_configuration(%s,%s,%s,%s)",
                ("tenant-a", config["config_id"], 0, "invalid"),
            )
        assert api.execute("SELECT count(*) FROM reckoner.api_v1_configurations").fetchone()[0] == 0


def selected_artifact():
    from reckoner.v1.calibration import fit_calibration
    from test_v1_calibration import rehash, sample

    rows = sample()
    for row in rows:
        row["context"]["scorer"]["question_version"] = "binary-v1"
    artifact = fit_calibration(rows)
    artifact["qualification"] = {
        "status": "selected",
        "validation_id": "b" * 64,
        "raw_brier": 0.04,
        "candidate_brier": 0.001,
        "raw_log_loss": 0.2,
        "candidate_log_loss": 0.02,
    }
    return rehash(artifact)


def register_selected(pg, artifact):
    from reckoner.v1.storage import configurations
    from reckoner.v1.storage.repository import V1Repository

    if not hasattr(configurations, "register_calibration"):
        pytest.fail("owner-only calibration registration is missing")
    with V1Repository(pg.owner_dsn) as owner:
        configurations.register_calibration(
            owner, tenant_id="tenant-a", artifact=artifact, context=artifact["context"]
        )


def test_owner_registers_selected_artifact_and_api_cannot_forge_context_or_artifact(pg):
    from psycopg.types.json import Jsonb
    from reckoner.v1.storage import configurations
    from reckoner.v1.storage.repository import V1Repository
    from v1_fixtures import identified

    artifact = selected_artifact()
    register_selected(pg, artifact)
    with V1Repository(pg.api_dsn) as api:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            configurations.register_calibration(
                api, tenant_id="tenant-a", artifact=artifact, context=artifact["context"]
            )
    config, threshold, qualification = sql_configuration()
    config.update(
        scaler_id="a" * 64, score_mode="calibrated", calibration_id=artifact["calibration_id"]
    )
    qualification["calibration"] = artifact
    identified(config, "config_id")
    with psycopg.connect(pg.api_dsn, autocommit=True) as api:
        for part in ("context", "artifact", "qualification"):
            bad_config, bad_q = deepcopy(config), deepcopy(qualification)
            if part == "context":
                bad_config["feature_version"] = "changed"
            elif part == "artifact":
                bad_q["calibration"]["coefficients"]["a"] = "999"
            else:
                bad_q["data_kind"] = "simulated-cctd"
            identified(bad_config, "config_id")
            with pytest.raises(psycopg.errors.CheckViolation):
                api.execute(
                    "SELECT reckoner.v1_save_configuration(%s,%s,%s)",
                    tuple(Jsonb(doc) for doc in (bad_config, threshold, bad_q)),
                )
        api.execute(
            "SELECT reckoner.v1_save_configuration(%s,%s,%s)",
            tuple(Jsonb(doc) for doc in (config, threshold, qualification)),
        )
        activated = api.execute(
            "SELECT reckoner.v1_activate_configuration(%s,%s,%s,%s)",
            ("tenant-a", config["config_id"], 0, "valid"),
        ).fetchone()[0]
        assert activated["version"] == 1


def test_activation_rechecks_persisted_configuration_qualification(pg):
    from psycopg.types.json import Jsonb

    config, threshold, _ = sql_configuration()
    with psycopg.connect(pg.owner_dsn) as owner:
        owner.execute(
            "INSERT INTO reckoner.threshold_configs VALUES(%s,%s,%s)",
            ("tenant-a", threshold["config_id"], Jsonb(threshold)),
        )
        owner.execute(
            "INSERT INTO reckoner.v1_configs VALUES(%s,%s,%s,%s,%s)",
            ("tenant-a", config["config_id"], threshold["config_id"], "reckoner-v1", Jsonb(config)),
        )
        owner.execute(
            "INSERT INTO reckoner.v1_configuration_details VALUES(%s,%s,%s)",
            ("tenant-a", config["config_id"], Jsonb({})),
        )
    with psycopg.connect(pg.api_dsn, autocommit=True) as api:
        with pytest.raises(psycopg.errors.CheckViolation):
            api.execute(
                "SELECT reckoner.v1_activate_configuration(%s,%s,%s,%s)",
                ("tenant-a", config["config_id"], 0, "unqualified"),
            )
        assert api.execute("SELECT count(*) FROM reckoner.api_v1_activation").fetchone()[0] == 0


def test_owner_registration_rejects_unqualified_or_mismatched_artifacts(pg):
    from reckoner.v1.storage.configurations import register_calibration
    from reckoner.v1.storage.repository import V1Repository
    from test_v1_calibration import rehash

    artifact = selected_artifact()
    with V1Repository(pg.owner_dsn) as owner:
        for defect in ("identity", "selection", "coefficients", "context"):
            changed, context = deepcopy(artifact), deepcopy(artifact["context"])
            if defect == "identity":
                changed["calibration_id"] = "f" * 64
            elif defect == "selection":
                changed["qualification"]["candidate_brier"] = changed["qualification"]["raw_brier"]
                rehash(changed)
            elif defect == "coefficients":
                changed["coefficients"]["a"] = "NaN"
                rehash(changed)
            else:
                context["feature_version"] = "different"
            with pytest.raises(ValueError):
                register_calibration(owner, tenant_id="tenant-a", artifact=changed, context=context)
        assert (
            owner._connection.execute(
                "SELECT count(*) AS n FROM reckoner.v1_selected_calibrations"
            ).fetchone()["n"]
            == 0
        )
    for dsn in (pg.runner_dsn, pg.evaluator_dsn):
        with V1Repository(dsn) as repo:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                register_calibration(
                    repo, tenant_id="tenant-a", artifact=artifact, context=artifact["context"]
                )


def test_direct_sql_configuration_hash_matches_unicode_and_escaped_strings(pg):
    from psycopg.types.json import Jsonb
    from v1_fixtures import identified

    config, threshold, qualification = sql_configuration()
    config["feature_version"] = 'features/é/☃/"quoted"/\\path'
    identified(config, "config_id")
    with psycopg.connect(pg.api_dsn, autocommit=True) as api:
        api.execute(
            "SELECT reckoner.v1_save_configuration(%s,%s,%s)",
            tuple(Jsonb(doc) for doc in (config, threshold, qualification)),
        )
        saved = api.execute("SELECT configuration FROM reckoner.api_v1_configurations").fetchone()[
            0
        ]
        assert saved == config
