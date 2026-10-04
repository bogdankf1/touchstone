"""Paid-run protocol consent: external SHA-bound approvals, no boolean bypass.

Every approval in these tests is an explicitly fabricated fixture
(`fabricated-test-fixture`), never an owner's words. No provider key, network
or paid call is used; HTTP transports are fabricated and must not be reached.
"""

from copy import deepcopy
from decimal import Decimal
from threading import Barrier, Thread

import httpx
import psycopg
import pytest
from reckoner.contracts import content_id
from reckoner.storage.budget import BudgetExceeded
from reckoner.v1.storage.budget import ProviderBudget, validate_approval_record, validate_protocol
from test_v1_budget import call, protocol
from v1_fixtures import (
    approval_fixture,
    authorize,
    dispatch_scope,
    fixture_protocol_document,
    identified,
)

TASK = {"tenant_id": "tenant-a", "run_id": "run-a", "task_id": "task-a", "transaction_id": "t-a"}


# --- offline: no boolean bypass ---------------------------------------------------


@pytest.mark.parametrize("value", [True, False, "true", 1])
def test_dispatch_protocol_rejects_any_approved_flag(value):
    p = protocol(TASK)
    assert "approved" not in p
    validate_protocol(p)
    with pytest.raises(ValueError, match="exact"):
        validate_protocol(identified({**p, "approved": value}, "protocol_id"))


def test_dispatch_protocol_identity_and_derived_requests_are_strict():
    p = protocol(TASK)
    changed = deepcopy(p)
    changed["usd_cap"] = "0.02"
    with pytest.raises(ValueError, match="identity"):
        validate_protocol(changed)
    bare = deepcopy(p)
    bare["tasks"][0]["request_sha256"] = None
    with pytest.raises(ValueError, match="exact requests"):
        validate_protocol(identified(bare, "protocol_id"))


def test_note_generation_and_evaluation_refuse_protocol_flags():
    from reckoner.v1.evaluation.notes import evaluate_note_fixtures
    from reckoner.v1.notes.generate import generate_note

    class NoCalls:
        def execute(self, *args, **kwargs):  # pragma: no cover - must not be reached
            pytest.fail("flagged protocol reached dispatch")

    with pytest.raises(ValueError, match="consent"):
        generate_note({}, {}, {}, NoCalls(), {}, protocol={"approved": True})
    with pytest.raises(ValueError, match="consent"):
        evaluate_note_fixtures([], {}, 0, protocol={"approved": True}, budget=object())


def scope():
    return dispatch_scope([protocol(TASK)])


def test_valid_fabricated_approval_record_is_detached_and_accepted():
    record = approval_fixture("b" * 64, scope())
    assert validate_approval_record(record) == record
    assert validate_approval_record(record) is not record


@pytest.mark.parametrize(
    "change",
    [
        {"approved": True},
        {"scope": {**scope(), "usd_cap": "remaining"}},
        {"scope": {**scope(), "usd_cap": "*"}},
        {"scope": {**scope(), "usd_cap": "remaining credit"}},
        {"scope": {**scope(), "usd_cap": "-1"}},
        {"scope": {**scope(), "usd_cap": "0"}},
        {"scope": {**scope(), "usd_cap": 0.01}},
        {"scope": {**scope(), "case_count": "all"}},
        {"scope": {**scope(), "case_count": True}},
        {"scope": {**scope(), "model": "*"}},
        {"scope": {**scope(), "provider": "any"}},
        {"scope": {**scope(), "extra": "wildcard"}},
        {"protocol_sha256": "*"},
        {"protocol_sha256": "B" * 64},
        {"approval_id": ""},
        {"approver": ""},
        {"owner_statement": "   "},
        {"approved_at": "yesterday"},
        {"approved_at": "2026-10-04T00:00:00"},
        {"schema_version": "reckoner-protocol-approval-v0"},
    ],
)
def test_approval_record_rejects_wildcards_flags_and_unbound_scope(change):
    with pytest.raises(ValueError):
        validate_approval_record({**approval_fixture("b" * 64, scope()), **change})


def test_approval_record_requires_every_field():
    record = approval_fixture("b" * 64, scope())
    for key in record:
        with pytest.raises(ValueError):
            validate_approval_record({k: v for k, v in record.items() if k != key})


# --- disposable PostgreSQL: approvals are recorded externally by the owner -------


def scored(pg, attempts=1):
    from test_v1_budget import scoring

    return scoring(pg, attempts=attempts, authorized=False)


def no_http():
    from reckoner.v1.providers.jev import JevClient

    return JevClient(
        "fabricated-only",
        transport=httpx.MockTransport(lambda _: pytest.fail("unapproved dispatch")),
    )


def count(repo, table):
    return repo._connection.execute(f"SELECT count(*) AS n FROM reckoner.{table}").fetchone()["n"]


@pytest.mark.integration
def test_structurally_valid_but_unrecorded_protocol_cannot_dispatch(pg):
    repo, task, evidence, p, attempts, _ = scored(pg)
    with repo:
        with pytest.raises(BudgetExceeded, match="recorded approval"):
            attempts.score_task(repo, no_http(), task, evidence, p)
        with pytest.raises(BudgetExceeded, match="recorded approval"):
            ProviderBudget(repo._connection).reserve(call(task, p), Decimal(".000084"), p)
        assert count(repo, "v1_provider_calls") == 0
        assert count(repo, "v1_protocols") == 0


@pytest.mark.integration
def test_runner_cannot_record_approvals_envelopes_or_authorizations(pg):
    repo, task, evidence, p, attempts, _ = scored(pg)
    document = fixture_protocol_document([p])
    approval = approval_fixture(document["protocol_sha256"], dispatch_scope([p]))
    with repo:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ProviderBudget(repo._connection).authorize(document, [p], approval=approval)
        for statement in (
            "INSERT INTO reckoner.v1_protocols (tenant_id,protocol_id,provider,run_id,"
            "maximum_cost,document) VALUES ('tenant-a','x','typesafe','x',0,'{}')",
            "INSERT INTO reckoner.v1_protocol_approvals (approval_id,protocol_sha256,document) "
            "VALUES ('x','" + "a" * 64 + "','{}')",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                repo._connection.execute(statement)
        assert count(repo, "v1_protocols") == 0


@pytest.mark.integration
def test_owner_recorded_approval_reserves_full_envelope_before_dispatch(pg):
    repo, task, evidence, p, attempts, jev = scored(pg)
    document, approval = authorize(pg.owner_dsn, p)
    with repo:
        ledger = ProviderBudget(repo._connection)
        assert ledger.remaining("typesafe") == Decimal("10") - Decimal(p["usd_cap"])
        stored = repo._connection.execute(
            "SELECT a.document AS approval, x.document AS protocol, z.protocol_id "
            "FROM reckoner.v1_protocol_authorizations z "
            "JOIN reckoner.v1_protocol_approvals a ON a.approval_id=z.approval_id "
            "JOIN reckoner.v1_experiment_protocols x ON x.protocol_sha256=z.protocol_sha256"
        ).fetchone()
        assert stored == {
            "approval": approval,
            "protocol": document,
            "protocol_id": p["protocol_id"],
        }
        from test_v1_jev import response

        client = jev.JevClient(
            "fabricated-only",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response())),
        )
        assert attempts.score_task(repo, client, task, evidence, p)["attempt_status"] == (
            "responded"
        )


@pytest.mark.integration
def test_duplicate_approval_identity_and_second_approval_block_reservation(pg):
    repo, task, evidence, p, attempts, _ = scored(pg)
    authorize(pg.owner_dsn, p, approval_id="fabricated-approval-1")
    other = identified({**deepcopy(p), "usd_cap": "0.02"}, "protocol_id")
    with pytest.raises(ValueError, match="duplicate approval"):
        authorize(pg.owner_dsn, other, approval_id="fabricated-approval-1")
    document = fixture_protocol_document([p])
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        with pytest.raises(ValueError, match="already approved"):
            ProviderBudget(owner).authorize(
                document,
                [p],
                approval=approval_fixture(
                    document["protocol_sha256"], dispatch_scope([p]), "fabricated-approval-2"
                ),
            )
    with repo:
        assert count(repo, "v1_protocol_approvals") == 1
        assert count(repo, "v1_protocols") == 1
        with pytest.raises(BudgetExceeded, match="recorded approval"):
            attempts.score_task(repo, no_http(), task, evidence, other)


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation",
    [
        "sha_mismatch",
        "document_changed",
        "scope_case_count",
        "scope_cap",
        "scope_model",
        "attempts",
    ],
)
def test_approval_must_bind_the_exact_protocol_sha_and_bounds(pg, mutation):
    repo, task, evidence, p, attempts, _ = scored(pg)
    document = fixture_protocol_document([p])
    approval = approval_fixture(document["protocol_sha256"], dispatch_scope([p]))
    if mutation == "sha_mismatch":
        approval["protocol_sha256"] = "c" * 64
    elif mutation == "document_changed":
        document["dispatch_protocol_ids"] = ["changed"]
    elif mutation == "scope_case_count":
        approval["scope"]["case_count"] = 2
    elif mutation == "scope_cap":
        approval["scope"]["usd_cap"] = "0.005"
    elif mutation == "scope_model":
        approval["scope"]["model"] = "jev-1.14.0"
    else:
        approval["scope"]["maximum_attempts"] = 0
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        with pytest.raises(ValueError):
            ProviderBudget(owner).authorize(document, [p], approval=approval)
    with repo:
        assert count(repo, "v1_protocol_approvals") == 0
        assert count(repo, "v1_protocols") == 0


@pytest.mark.integration
def test_unresolved_prior_reservation_blocks_a_new_approved_protocol(pg):
    repo, task, evidence, p, attempts, _ = scored(pg)
    authorize(pg.owner_dsn, p)
    second = identified({**deepcopy(p), "usd_cap": "0.02"}, "protocol_id")
    with repo:
        ledger = ProviderBudget(repo._connection)
        ledger.reserve(call(task, p, "in-flight"), Decimal(".000084"), p)
        with pytest.raises(BudgetExceeded, match="unresolved"):
            authorize(pg.owner_dsn, second)
        ledger.settle("in-flight", None, None)
        ledger.close(p["protocol_id"])
        with pytest.raises(BudgetExceeded, match="unresolved"):
            authorize(pg.owner_dsn, second)
        ledger.settle("in-flight", {"input_tokens": 10, "output_tokens": 0}, Decimal("0.00000042"))
        authorize(pg.owner_dsn, second)


@pytest.mark.integration
def test_open_unclosed_protocol_blocks_the_next_one_until_closed(pg):
    repo, task, evidence, p, attempts, _ = scored(pg)
    authorize(pg.owner_dsn, p)
    second = identified({**deepcopy(p), "usd_cap": "0.02"}, "protocol_id")
    with pytest.raises(BudgetExceeded, match="unresolved"):
        authorize(pg.owner_dsn, second)
    with repo:
        ProviderBudget(repo._connection).close(p["protocol_id"])
    authorize(pg.owner_dsn, second)


@pytest.mark.integration
def test_concurrent_approvals_cannot_both_take_the_last_provider_cent(pg):
    repo, task, evidence, p, attempts, _ = scored(pg)
    first = identified({**deepcopy(p), "usd_cap": "9.99"}, "protocol_id")
    authorize(pg.owner_dsn, first)
    with repo:
        ledger = ProviderBudget(repo._connection)
        ledger.reserve(call(task, first, "spent"), Decimal("9.99"), first)
        ledger.settle("spent", {"input_tokens": 1, "output_tokens": 0}, Decimal("9.99"))
        ledger.close(first["protocol_id"])
        assert ledger.remaining("typesafe") == Decimal("0.01")
    outcomes, errors = [], []
    barrier = Barrier(2)

    def race(number):
        candidate = identified(
            {**deepcopy(p), "usd_cap": "0.01", "maximum_attempts": number}, "protocol_id"
        )
        try:
            barrier.wait()
            authorize(pg.owner_dsn, candidate)
        except BudgetExceeded:
            outcomes.append("blocked")
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)
        else:
            outcomes.append("reserved")

    threads = [Thread(target=race, args=(n,)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert errors == []
    assert sorted(outcomes) == ["blocked", "reserved"]


def test_fixture_documents_are_labelled_fabricated():
    document = fixture_protocol_document([protocol(TASK)])
    assert document["schema_version"] == "fabricated-test-protocol"
    assert document["protocol_sha256"] == content_id(
        {k: v for k, v in document.items() if k != "protocol_sha256"}
    )
    assert approval_fixture("b" * 64, scope())["approver"] == "fabricated-test-fixture"
