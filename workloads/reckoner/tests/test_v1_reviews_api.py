"""First review wins atomically; retry and attribution never forge simulation."""

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient
from reckoner.api import create_app
from test_v1_cases_api import case_setup

pytestmark = pytest.mark.integration


def action(case_id, **changes):
    return {
        "tenant_id": "tenant-a",
        "case_id": case_id,
        "reviewer_id": "human/alice",
        "verdict": "approve",
        "idempotency_key": "click/1",
        "expected_version": 1,
        **changes,
    }


def test_review_idempotency_conflict_and_atomic_outbox(pg):
    decision = case_setup(pg)
    with TestClient(create_app(pg.api_dsn)) as client:
        body = action(decision["decision_id"])
        first = client.post("/v1/reviews", json=body)
        assert first.status_code == 200
        assert first.json()["recommendation"] is None
        assert first.json()["reviewer_type"] == "human"
        assert first.json()["prior_case_version"] == 1
        assert client.post("/v1/reviews", json=body).json() == first.json()
        assert client.post("/v1/reviews", json={**body, "verdict": "decline"}).status_code == 409
        assert (
            client.post("/v1/reviews", json={**body, "idempotency_key": "click/2"}).status_code
            == 409
        )
        assert (
            client.post("/v1/reviews", json={**body, "reviewer_type": "simulated"}).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/reviews", json={**body, "reviewer_id": "simulated-reviewer"}
            ).status_code
            == 422
        )
        detail = client.get(
            "/v1/case", params={"tenant_id": "tenant-a", "case_id": body["case_id"]}
        ).json()
        assert (detail["status"], detail["version"]) == ("resolved", 2)
        assert detail["review"]["recommendation"] is None
    with psycopg.connect(pg.owner_dsn) as owner:
        assert owner.execute("SELECT count(*) FROM reckoner.v1_reviews").fetchone()[0] == 1
        event = owner.execute("SELECT document,payload,status FROM reckoner.v1_outbox").fetchone()
        assert event[0]["action_id"] == first.json()["action_id"]
        assert event[0]["event_kind"] == "review"
        assert event[2] == "pending"
        assert event[1]


def test_concurrent_reviewers_have_one_winner(pg):
    decision = case_setup(pg)

    def click(i):
        with TestClient(create_app(pg.api_dsn)) as client:
            return client.post(
                "/v1/reviews",
                json=action(
                    decision["decision_id"], idempotency_key=f"click/{i}", reviewer_id=f"human/{i}"
                ),
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(click, range(2))) == [200, 409]


def test_outbox_failure_rolls_back_review_and_version(pg):
    decision = case_setup(pg)
    with psycopg.connect(pg.owner_dsn) as owner:
        owner.execute(
            "CREATE FUNCTION reckoner.reject_test_event() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'fabricated outbox failure'; END $$"
        )
        owner.execute(
            "CREATE TRIGGER reject_test_event BEFORE INSERT ON reckoner.v1_outbox "
            "FOR EACH ROW EXECUTE FUNCTION reckoner.reject_test_event()"
        )
    with TestClient(create_app(pg.api_dsn)) as client:
        assert client.post("/v1/reviews", json=action(decision["decision_id"])).status_code == 503
        detail = client.get(
            "/v1/case", params={"tenant_id": "tenant-a", "case_id": decision["decision_id"]}
        ).json()
        assert (detail["status"], detail["version"]) == ("open", 1)
    with psycopg.connect(pg.owner_dsn) as owner:
        assert owner.execute("SELECT count(*) FROM reckoner.v1_reviews").fetchone()[0] == 0
        assert owner.execute("SELECT count(*) FROM reckoner.v1_outbox").fetchone()[0] == 0


@pytest.mark.parametrize("note_first", [True, False])
def test_note_attachment_and_review_share_case_row_lock(pg, note_first):
    from threading import Event

    from reckoner.v1.storage.repository import V1Repository
    from reckoner.v1.storage.reviews import submit_review
    from v1_fixtures import identified, note_fixture

    decision = case_setup(pg)
    case = decision["decision_id"]
    note = note_fixture()
    note.update(case_id=case, decision_id=case, evidence_id=decision["evidence_id"])
    identified(note, "note_id")
    locked = Event()
    release = Event()

    def attach(hold):
        with V1Repository(pg.runner_dsn) as repo:
            with repo._connection.transaction():
                status = repo._connection.execute(
                    "SELECT reckoner.v1_lock_note_case(%s,%s) AS status", ("tenant-a", case)
                ).fetchone()["status"]
                if hold:
                    locked.set()
                    assert release.wait(10)
                available = status == "pending"
                if available:
                    values = {
                        k: note[k]
                        for k in (
                            "tenant_id",
                            "note_id",
                            "case_id",
                            "decision_id",
                            "evidence_id",
                            "generation_status",
                            "prompt_version",
                            "completed_at",
                        )
                    }
                    repo._insert(
                        "v1_notes",
                        {**values, "document": note},
                        {"tenant_id": "tenant-a", "note_id": note["note_id"]},
                    )
                result = {
                    "tenant_id": "tenant-a",
                    "case_id": case,
                    "note": note,
                    "status": "succeeded",
                    "available_before_review": available,
                }
                repo._insert(
                    "v1_note_results",
                    {"tenant_id": "tenant-a", "case_id": case, "document": result},
                    {"tenant_id": "tenant-a", "case_id": case},
                )
                return result

    def review(hold):
        with V1Repository(pg.api_dsn) as repo:
            with repo._connection.transaction():
                result = submit_review(
                    repo,
                    {
                        "tenant_id": "tenant-a",
                        "case_id": case,
                        "reviewer_id": "human/a",
                        "verdict": "decline",
                    },
                    idempotency_key="lock-test",
                    expected_version=1,
                )
                if hold:
                    locked.set()
                    assert release.wait(10)
                return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attach if note_first else review, True)
        assert locked.wait(10)
        second = pool.submit(review if note_first else attach, False)
        # PostgreSQL confirms the second transaction is blocked on the first,
        # rather than relying on a scheduling sleep as evidence of contention.
        import time

        deadline = time.monotonic() + 10
        with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
            while not owner.execute(
                "SELECT EXISTS(SELECT FROM pg_stat_activity "
                "WHERE datname=current_database() AND wait_event_type='Lock')"
            ).fetchone()[0]:
                assert time.monotonic() < deadline
        release.set()
        results = [first.result(), second.result()]
    captured = results[1 if note_first else 0]
    assert captured["recommendation"] == ("approve" if note_first else None)
    with TestClient(create_app(pg.api_dsn)) as client:
        detail = client.get("/v1/case", params={"tenant_id": "tenant-a", "case_id": case}).json()
        assert detail["note"]["verdict_recommendation"] == "approve"
        assert detail["available_before_review"] is note_first
        assert detail["review"]["recommendation"] == captured["recommendation"]


def test_simulated_cli_uses_evaluator_only_and_is_idempotent(pg, tmp_path):
    from reckoner.cli import _parser
    from reckoner.v1.cli import execute

    decision = case_setup(pg)
    env = tmp_path / "evaluator.env"
    env.write_text(f"RECKONER_EVALUATOR_DSN={pg.evaluator_dsn}\n")
    try:
        args = _parser().parse_args(
            [
                "v1",
                "review-simulated",
                "--tenant-id",
                "tenant-a",
                "--run-id",
                "run-a",
                "--env-file",
                str(env),
            ]
        )
    except SystemExit:
        pytest.fail("review-simulated CLI is missing")
    first = execute(args)
    assert execute(args) == first
    reviewed = first["actions"][0]
    assert reviewed["reviewer_type"] == "simulated"
    assert reviewed["oracle_version"] == "cctd-label-v1"
    assert reviewed["resolution_policy_version"] == "simulated-seven-days-v1"
    with psycopg.connect(pg.owner_dsn) as owner:
        label = owner.execute(
            "SELECT label FROM oracle.oracle_labels WHERE tenant_id=%s AND transaction_id=%s",
            ("tenant-a", decision["transaction_id"]),
        ).fetchone()[0]
        assert reviewed["verdict"] == ("decline" if label == "fraud" else "approve")
        assert owner.execute("SELECT count(*) FROM reckoner.v1_reviews").fetchone()[0] == 1
    for dsn in (pg.api_dsn, pg.runner_dsn):
        with psycopg.connect(dsn) as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    "SELECT reckoner.v1_review(%s,%s,%s,%s,%s,%s,true)",
                    (
                        "tenant-a",
                        decision["decision_id"],
                        "simulated-reviewer",
                        "approve",
                        "simulation-v1",
                        1,
                    ),
                )
    env.write_text(f"RECKONER_EVALUATOR_DSN={pg.evaluator_dsn}\nJEV_API_KEY=not-a-real-key\n")
    with pytest.raises(ValueError):
        execute(args)


def test_available_degraded_note_recommendation_is_captured(pg):
    from reckoner.v1.storage.repository import V1Repository
    from v1_fixtures import identified, note_fixture

    decision = case_setup(pg)
    note = note_fixture()
    note.update(
        case_id=decision["decision_id"],
        decision_id=decision["decision_id"],
        evidence_id=decision["evidence_id"],
        generation_status="degraded",
    )
    identified(note, "note_id")
    with V1Repository(pg.runner_dsn) as repo:
        values = {
            k: note[k]
            for k in (
                "tenant_id",
                "note_id",
                "case_id",
                "decision_id",
                "evidence_id",
                "generation_status",
                "prompt_version",
                "completed_at",
            )
        }
        repo._insert(
            "v1_notes",
            {**values, "document": note},
            {"tenant_id": "tenant-a", "note_id": note["note_id"]},
        )
    with TestClient(create_app(pg.api_dsn)) as client:
        result = client.post("/v1/reviews", json=action(decision["decision_id"]))
        assert result.status_code == 200
        assert result.json()["recommendation"] == "approve"


def test_direct_api_sql_rejects_null_simulation_flag(pg):
    decision = case_setup(pg)
    with psycopg.connect(pg.api_dsn, autocommit=True) as api:
        with pytest.raises(psycopg.errors.CheckViolation):
            api.execute(
                "SELECT reckoner.v1_review(%s,%s,%s,%s,%s,%s,NULL)",
                (
                    "tenant-a",
                    decision["decision_id"],
                    "simulated-reviewer",
                    "approve",
                    "null-flag",
                    1,
                ),
            )
    with psycopg.connect(pg.owner_dsn) as owner:
        assert owner.execute("SELECT count(*) FROM reckoner.v1_reviews").fetchone()[0] == 0
        assert owner.execute("SELECT status,version FROM reckoner.v1_cases").fetchone() == (
            "pending",
            1,
        )
