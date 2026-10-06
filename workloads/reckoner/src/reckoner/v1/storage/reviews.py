"""One database transaction owns first action, recommendation and outbox identity."""

from reckoner.v1.api.schemas import ReviewRequest


def submit_review(repo, action: dict, *, idempotency_key: str, expected_version: int) -> dict:
    request = ReviewRequest.model_validate(
        {**action, "idempotency_key": idempotency_key, "expected_version": expected_version}
    )
    return _submit(
        repo,
        request.tenant_id,
        request.case_id,
        request.reviewer_id,
        request.verdict,
        request.idempotency_key,
        request.expected_version,
        False,
    )


def _submit(repo, tenant, case, actor, verdict, key, version, simulated):
    with repo._connection.transaction():
        return repo._connection.execute(
            "SELECT reckoner.v1_review(%s,%s,%s,%s,%s,%s,%s) AS document",
            (tenant, case, actor, verdict, key, version, simulated),
        ).fetchone()["document"]


def review_simulated(repo, *, tenant_id: str, run_id: str) -> dict:
    # The procedure verifies evaluator membership and derives the verdict itself.
    rows = repo._connection.execute(
        "SELECT case_id FROM reckoner.api_v1_case_details "
        "WHERE tenant_id=%s AND run_id=%s ORDER BY case_id",
        (tenant_id, run_id),
    ).fetchall()
    exists = repo._connection.execute(
        "SELECT 1 FROM reckoner.v1_runs WHERE tenant_id=%s AND run_id=%s", (tenant_id, run_id)
    ).fetchone()
    if exists is None:
        raise LookupError("run not found")
    actions = []
    for row in rows:
        actions.append(
            _submit(
                repo,
                tenant_id,
                row["case_id"],
                "simulated-reviewer",
                "approve",
                "simulation-v1",
                1,
                True,
            )
        )
    return {
        "tenant_id": tenant_id,
        "run_id": run_id,
        "reviewer_type": "simulated",
        "actions": actions,
    }
