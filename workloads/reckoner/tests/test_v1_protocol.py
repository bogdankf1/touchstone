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
from reckoner.v1.storage.repository import V1Repository as V1Repo
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


# --- experiment protocols: measured drafts, frozen bodies, ledger validation ------


def jev_price():
    import json

    from reckoner.resources import CONFIG

    return json.loads((CONFIG / "jev-prices-v1.json").read_text())


def fabricated_manifest(cases, *, population="pilot", mode="relational"):
    """A fabricated 3b-shaped manifest; never a published measured manifest."""
    body = {
        "schema_version": "reckoner-evidence-manifest-v1",
        "dataset_simulated": True,
        "preparation_id": "1" * 64,
        "source_snapshot_id": "2" * 64,
        "population": population,
        "mode": mode,
        "sample_id": "3" * 64,
        "case_count": len(cases),
        "cases": [
            {
                "tenant_id": tenant,
                "transaction_id": transaction,
                "evidence_id": content_id([tenant, transaction, mode]),
                "coverage_status": "available",
                "missing": [],
                "projection_id": None,
                "page_rank_converged": None,
                "snapshot_age_seconds": None,
            }
            for tenant, transaction in cases
        ],
    }
    return {**body, "manifest_id": content_id(body)}


def write_manifest(tmp_path, manifest, name="pilot-relational.json"):
    from reckoner.data.artifacts import canonical_json

    path = tmp_path / name
    path.write_bytes(canonical_json(manifest))
    return path


def ledger_snapshot(provider="typesafe", remaining="10", unresolved=(), ledger_id="9" * 64):
    return {
        "schema_version": "reckoner-ledger-snapshot-v1",
        "provider": provider,
        "ledger_id": ledger_id,
        "legacy_ledger_sha256": None,
        "legacy_verified": provider == "typesafe",
        "provider_cap_usd": "10",
        "liability_usd": str(Decimal(10) - Decimal(remaining)),
        "remaining_usd": remaining,
        "unresolved": list(unresolved),
        "billing_basis": "local reservations and settlements; not a provider invoice",
    }


def runs_for(manifest, purpose="pilot"):
    """Fabricated experiment declarations, one per tenant, covering the manifest."""
    from v1_fixtures import HASH

    runs = []
    for tenant in sorted({c["tenant_id"] for c in manifest["cases"]}):
        run = {
            "schema_version": "reckoner-experiment-v1",
            "tenant_id": tenant,
            "run_id": f"{purpose}-{tenant}",
            "purpose": purpose,
            "config_id": HASH,
            "dataset_simulated": True,
            "dataset_version": HASH,
            "cohort_version": HASH,
            "code_revision": "fabricated-revision",
            "created_at": "2026-10-04T00:00:00Z",
            "tasks": [
                {"task_id": f"task-{c['transaction_id']}", "transaction_id": c["transaction_id"]}
                for c in manifest["cases"]
                if c["tenant_id"] == tenant
            ],
        }
        runs.append(identified(run, "experiment_id"))
    return runs


def measured(manifest, runs, size=1500):
    from reckoner.v1.experiment.protocol import request_measurements

    by_tx = {(c["tenant_id"], c["transaction_id"]): c for c in manifest["cases"]}
    entries = []
    for run in runs:
        for task in run["tasks"]:
            case = by_tx[(run["tenant_id"], task["transaction_id"])]
            entries.append(
                (
                    {**task, "tenant_id": run["tenant_id"], "run_id": run["run_id"]},
                    {"evidence_id": case["evidence_id"]},
                    {"fabricated_request": case["evidence_id"], "pad": "x" * size},
                )
            )
    return request_measurements(entries)


PILOT_CASES = [("tenant-a", f"tx-a-{n}") for n in range(16)] + [
    ("tenant-b", f"tx-b-{n}") for n in range(4)
]


def draft_inputs(tmp_path, purpose="pilot", cases=PILOT_CASES, **overrides):
    from reckoner.v1.experiment.protocol import load_evidence_manifest

    manifest = fabricated_manifest(cases, population=purpose)
    loaded = load_evidence_manifest(write_manifest(tmp_path, manifest))
    runs = runs_for(manifest, purpose)
    inputs = {
        "purpose": purpose,
        "approver": FIXTURE,
        "manifests": [loaded],
        "runs": runs,
        "measurements": measured(manifest, runs),
        "price_table": jev_price(),
        "ledger": ledger_snapshot(),
        "code_revision": "fabricated-revision",
    }
    inputs.update(overrides)
    return inputs


FIXTURE = "fabricated-test-fixture"


def pilot(tmp_path, **overrides):
    from reckoner.v1.experiment.protocol import draft_scoring_protocol

    return draft_scoring_protocol(**draft_inputs(tmp_path, **overrides))


def test_published_manifest_is_verified_by_bytes_and_identity(tmp_path):
    from reckoner.v1.experiment.protocol import load_evidence_manifest

    manifest = fabricated_manifest(PILOT_CASES)
    loaded = load_evidence_manifest(write_manifest(tmp_path, manifest))
    assert loaded["manifest"] == manifest and len(loaded["sha256"]) == 64
    altered = deepcopy(manifest)
    altered["cases"][0]["evidence_id"] = "f" * 64
    with pytest.raises(ValueError, match="altered"):
        load_evidence_manifest(write_manifest(tmp_path, altered, "altered.json"))
    repeated = deepcopy(manifest)
    repeated["cases"][1] = repeated["cases"][0]
    with pytest.raises(ValueError, match="repeats"):
        load_evidence_manifest(
            write_manifest(tmp_path, identified(repeated, "manifest_id"), "repeat.json")
        )


@pytest.mark.parametrize(
    "missing",
    ["approver", "manifests", "runs", "measurements", "price_table", "ledger", "code_revision"],
)
def test_draft_refuses_to_emit_while_measured_inputs_are_missing(tmp_path, missing):
    from reckoner.v1.experiment.protocol import MissingInputs, draft_scoring_protocol

    with pytest.raises(MissingInputs) as error:
        draft_scoring_protocol(**draft_inputs(tmp_path, **{missing: None}))
    assert len(error.value.names) == 1


def test_larger_runs_need_the_pilot_token_overhead_measurement(tmp_path):
    from reckoner.v1.experiment.protocol import MissingInputs, draft_scoring_protocol

    with pytest.raises(MissingInputs, match="token_overhead"):
        draft_scoring_protocol(**draft_inputs(tmp_path, purpose="development"))


def test_pilot_draft_pins_manifest_cases_prices_ledger_and_worst_case(tmp_path):
    from reckoner.v1.experiment.protocol import validate_protocol

    draft = pilot(tmp_path)
    receipt = validate_protocol(draft, ledger_snapshot())
    assert receipt["execution_authorized"] is False
    assert receipt["case_count"] == 20
    # 20 cases x 3 attempts x 64,000 billed input tokens x USD 0.042 / 1M.
    assert Decimal(draft["worst_case_usd"]) == Decimal("0.16128") == Decimal(draft["usd_cap"])
    assert draft["bounds"]["input_token_ceiling"] == 32000
    assert {d["tenant_id"] for d in draft["dispatch"]} == {"tenant-a", "tenant-b"}
    assert draft["evidence"][0]["population"] == "pilot"
    assert {c["evidence_mode"] for c in draft["cases"]} == {"relational"}
    assert "approved" not in draft


def overhead(ratio="0.5", extra=200):
    body = {
        "schema_version": "reckoner-token-overhead-v1",
        "source_protocol_sha256": "4" * 64,
        "observations": 20,
        "max_tokens_per_byte": ratio,
        "max_overhead_tokens": extra,
    }
    return {**body, "overhead_id": content_id(body)}


def test_measured_overhead_revises_bounds_before_asking(tmp_path):
    draft = pilot(tmp_path, purpose="development", token_overhead=overhead())
    assert draft["bounds"]["input_token_ceiling"] == draft["bounds"]["billing_token_bound"]
    assert draft["bounds"]["input_token_ceiling"] < 32000
    assert draft["versions"]["token_overhead_id"] == overhead()["overhead_id"]
    with pytest.raises(ValueError, match="state limit"):
        pilot(tmp_path, purpose="validation", token_overhead=overhead(ratio="40"))


def test_draft_rejects_evidence_not_from_the_published_manifest(tmp_path):
    inputs = draft_inputs(tmp_path)
    other = fabricated_manifest(PILOT_CASES, mode="gds-augmented")
    inputs["measurements"] = measured(other, inputs["runs"])
    from reckoner.v1.experiment.protocol import draft_scoring_protocol

    with pytest.raises(ValueError, match="manifest"):
        draft_scoring_protocol(**inputs)


def test_draft_rejects_unmeasured_run_tasks_and_unknown_prices(tmp_path):
    from reckoner.v1.experiment.protocol import draft_scoring_protocol

    inputs = draft_inputs(tmp_path)
    cases = inputs["measurements"]["cases"][1:]

    body = {k: v for k, v in inputs["measurements"].items() if k != "measurement_id"}
    body["cases"] = cases
    inputs["measurements"] = {**body, "measurement_id": content_id(body)}
    with pytest.raises(ValueError, match="declared run task"):
        draft_scoring_protocol(**inputs)
    price = jev_price()
    price["input_per_million"] = "0.043"
    price = identified(price, "price_table_version")
    with pytest.raises(ValueError, match="pricing"):
        draft_scoring_protocol(**draft_inputs(tmp_path, price_table=price))


def rehash(protocol):
    from reckoner.v1.experiment.protocol import identify

    return identify(protocol)


def redispatch(protocol, index, **changes):
    changed = deepcopy(protocol)
    changed["dispatch"][index] = identified(
        {**changed["dispatch"][index], **changes}, "protocol_id"
    )
    return changed


def mutations(draft):
    """Each case must block dispatch even when the body is consistently rehashed."""
    removed = deepcopy(draft)
    removed["cases"] = removed["cases"][1:]
    swapped = redispatch(
        draft,
        0,
        tasks=[{**draft["dispatch"][0]["tasks"][0], "request_sha256": "e" * 64}]
        + draft["dispatch"][0]["tasks"][1:],
    )
    swapped_case = deepcopy(draft)
    swapped_case["cases"][0]["transaction_id"] = "substituted"
    price = deepcopy(draft)
    price["prices"]["input_per_million"] = "0.043"
    identified(price["prices"], "price_table_version")
    no_price = deepcopy(draft)
    no_price["prices"] = None
    model = deepcopy(draft)
    model["model"] = "jev-1.14.0"
    attempts = redispatch(draft, 0, maximum_attempts=2)
    low_cap = deepcopy(draft)
    low_cap["usd_cap"] = "0.1"
    low_worst = deepcopy(draft)
    low_worst["worst_case_usd"] = "0.1"
    over_dispatch = redispatch(draft, 0, usd_cap="0.0001")
    flagged = deepcopy(draft)
    flagged["approved"] = True
    unknown_purpose = deepcopy(draft)
    unknown_purpose["purpose"] = "remaining-credit"
    return {
        "changed case set": rehash(removed),
        "substituted case": rehash(swapped_case),
        "changed request": rehash(swapped),
        "changed prices": rehash(price),
        "missing prices": rehash(no_price),
        "changed model": rehash(model),
        "changed attempt count": rehash(attempts),
        "cap below worst case": rehash(low_cap),
        "declared worst case": rehash(low_worst),
        "dispatch cap below worst case": rehash(over_dispatch),
        "approved flag": flagged,
        "unknown purpose": rehash(unknown_purpose),
    }


@pytest.mark.parametrize(
    "name",
    [
        "changed case set",
        "substituted case",
        "changed prices",
        "missing prices",
        "changed model",
        "changed attempt count",
        "cap below worst case",
        "declared worst case",
        "dispatch cap below worst case",
        "approved flag",
        "unknown purpose",
    ],
)
def test_changed_protocol_contents_block_validation(tmp_path, name):
    from reckoner.v1.experiment.protocol import validate_protocol

    with pytest.raises(ValueError):
        validate_protocol(mutations(pilot(tmp_path))[name], ledger_snapshot())


def test_any_consistent_change_after_approval_breaks_the_sha_binding(tmp_path):
    from reckoner.v1.experiment.protocol import approval_scope, bind_approval, validate_protocol

    draft = pilot(tmp_path)
    approval = approval_fixture(draft["protocol_sha256"], approval_scope(draft))
    changed = mutations(draft)["changed request"]
    # Internally consistent, so only the exact SHA binding (and dispatch preflight,
    # which rebuilds each request from pinned evidence) can reject it.
    validate_protocol(changed, ledger_snapshot())
    with pytest.raises(ValueError, match="SHA-256"):
        bind_approval(approval, changed)


def test_changed_protocol_hash_blocks_validation(tmp_path):
    from reckoner.v1.experiment.protocol import validate_protocol

    draft = pilot(tmp_path)
    draft["usd_cap"] = "1"
    with pytest.raises(ValueError, match="SHA-256"):
        validate_protocol(draft, ledger_snapshot())


@pytest.mark.parametrize(
    "ledger,match",
    [
        (ledger_snapshot(ledger_id="8" * 64), "wrong ledger"),
        (ledger_snapshot(provider="anthropic"), "wrong ledger"),
        (ledger_snapshot(remaining="0.1"), "remainder"),
        (
            ledger_snapshot(unresolved=[{"kind": "unsettled call", "identity": "c"}]),
            "unresolved",
        ),
        ({"provider": "typesafe"}, "snapshot"),
    ],
)
def test_ledger_identity_balance_and_prior_reservations_block(tmp_path, ledger, match):
    from reckoner.v1.experiment.protocol import validate_protocol

    with pytest.raises(ValueError, match=match):
        validate_protocol(pilot(tmp_path), ledger)


def test_approval_must_restate_the_exact_protocol_scope(tmp_path):
    from reckoner.v1.experiment.protocol import approval_scope, bind_approval

    draft = pilot(tmp_path)
    good = approval_fixture(draft["protocol_sha256"], approval_scope(draft))
    assert bind_approval(good, draft) == good
    for change in (
        {"protocol_sha256": "c" * 64},
        {"approver": "someone-else"},
        {"scope": {**approval_scope(draft), "usd_cap": "10"}},
        {"scope": {**approval_scope(draft), "case_count": 19}},
        {"scope": {**approval_scope(draft), "purpose": "development"}},
        {"scope": {**approval_scope(draft), "maximum_attempts": 1}},
    ):
        with pytest.raises(ValueError):
            bind_approval({**good, **change}, draft)


def test_presentation_lists_exact_scope_balance_and_is_not_consent(tmp_path):
    from reckoner.v1.experiment.protocol import present_protocol

    draft = pilot(tmp_path)
    text = present_protocol(draft, ledger_snapshot())
    for expected in (
        draft["protocol_sha256"],
        "jev-1.13.0",
        "Frozen cases: 20 (tenant-a: 16, tenant-b: 4)",
        "hard cap: USD 0.16128",
        "remaining USD 10",
        "not execution",
        "not approval",
        "nonrefundable",
    ):
        assert expected.lower() in text.lower()
    assert "Blocked" not in text
    blocked = present_protocol(draft, ledger_snapshot(remaining="0.01"))
    assert "Blocked" in blocked and "remainder" in blocked
    assert "approval_id" not in text and "owner_statement" not in text


# --- disposable PostgreSQL: measured draft -> external approval -> reservation ----


def experiment_scenario(pg, tmp_path, *, purpose="pilot", per_tenant=(2, 1)):
    """Fabricated runs/evidence/manifest in a disposable database; no provider call."""
    import json

    from reckoner.v1.experiment.protocol import (
        draft_scoring_protocol,
        load_evidence_manifest,
        measure_scoring_requests,
    )
    from test_v1_storage import CONFIG_DIR, setup_run
    from v1_fixtures import config_fixture, evidence_fixture

    setup_run(pg)
    runs, persisted = [], []
    with V1Repo(pg.owner_dsn) as owner:
        for tenant, count_ in zip(("tenant-a", "tenant-b"), per_tenant, strict=True):
            if not count_:
                continue
            threshold = json.loads((CONFIG_DIR / f"thresholds-{tenant}-v1.json").read_text())
            config = config_fixture(tenant=tenant, threshold=threshold["config_id"])
            config["limits"].update(input_token_ceiling=32000, maximum_attempts=3)
            config["scorer"]["price_table"] = jev_price()
            identified(config, "config_id")
            owner.register_config(config)
            rows = owner._connection.execute(
                "SELECT document FROM reckoner.transactions WHERE tenant_id=%s "
                "ORDER BY transaction_id LIMIT %s",
                (tenant, count_),
            ).fetchall()
            run = {
                "schema_version": "reckoner-experiment-v1",
                "tenant_id": tenant,
                "run_id": f"{purpose}-{tenant}",
                "purpose": purpose,
                "config_id": config["config_id"],
                "dataset_simulated": True,
                "dataset_version": "a" * 64,
                "cohort_version": "a" * 64,
                "code_revision": "fabricated-revision",
                "created_at": "2026-10-04T00:00:00Z",
                "tasks": [
                    {"task_id": f"task-{n}", "transaction_id": r["document"]["transaction_id"]}
                    for n, r in enumerate(rows)
                ],
            }
            identified(run, "experiment_id")
            from reckoner.v1.experiment.protocol import PURPOSES

            owner.create_run(run, config["config_id"], telemetry_mode=PURPOSES[purpose][2])
            runs.append(run)
            for row in rows:
                tx = row["document"]
                evidence = evidence_fixture(tenant=tenant, transaction_id=tx["transaction_id"])
                evidence["query_time"] = tx["occurred_at"]
                evidence["cutoffs"]["history_before"] = tx["occurred_at"]
                evidence["cutoffs"]["resolved_before"] = tx["occurred_at"]
                identified(evidence, "evidence_id")
                owner.persist_evidence(evidence)
                persisted.append(evidence)
    body = {
        "schema_version": "reckoner-evidence-manifest-v1",
        "dataset_simulated": True,
        "preparation_id": "1" * 64,
        "source_snapshot_id": "a" * 64,
        "population": purpose,
        "mode": "relational",
        "sample_id": "3" * 64,
        "case_count": len(persisted),
        "cases": [
            {
                "tenant_id": e["tenant_id"],
                "transaction_id": e["transaction_id"],
                "evidence_id": e["evidence_id"],
                "coverage_status": "available",
                "missing": [],
                "projection_id": None,
                "page_rank_converged": None,
                "snapshot_age_seconds": None,
            }
            for e in sorted(persisted, key=lambda e: (e["tenant_id"], e["transaction_id"]))
        ],
    }
    manifest = load_evidence_manifest(
        write_manifest(tmp_path, {**body, "manifest_id": content_id(body)}, "scenario.json")
    )
    by_tx = {(c["tenant_id"], c["transaction_id"]): c for c in manifest["manifest"]["cases"]}
    cases = [
        {
            **task,
            "tenant_id": run["tenant_id"],
            "run_id": run["run_id"],
            "evidence_id": by_tx[(run["tenant_id"], task["transaction_id"])]["evidence_id"],
        }
        for run in runs
        for task in run["tasks"]
    ]
    with V1Repo(pg.runner_dsn) as runner:
        measurements = measure_scoring_requests(runner, cases)
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        ledger = ProviderBudget(owner).snapshot("typesafe")
    draft = draft_scoring_protocol(
        purpose=purpose,
        approver=FIXTURE,
        manifests=[manifest],
        runs=runs,
        measurements=measurements,
        price_table=jev_price(),
        ledger=ledger,
        code_revision="fabricated-revision",
        token_overhead=None if purpose == "pilot" else overhead(),
    )
    return draft, ledger, runs


def fixture_approval(protocol, approval_id=None):
    from reckoner.v1.experiment.protocol import approval_scope

    return approval_fixture(protocol["protocol_sha256"], approval_scope(protocol), approval_id)


def reserve(pg, protocol, approval):
    from reckoner.v1.experiment.protocol import reserve_protocol

    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        return reserve_protocol(protocol, ProviderBudget(owner), approval=approval)


@pytest.mark.integration
def test_measured_draft_reserves_only_with_its_bound_external_approval(pg, tmp_path):
    draft, ledger, runs = experiment_scenario(pg, tmp_path)
    assert ledger["unresolved"] == [] and ledger["remaining_usd"] == "10"
    assert reserve(pg, draft, fixture_approval(draft)) == draft["protocol_sha256"]
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        snapshot = ProviderBudget(owner).snapshot("typesafe")
        assert Decimal(snapshot["remaining_usd"]) == Decimal(10) - Decimal(draft["usd_cap"])
        assert {u["kind"] for u in snapshot["unresolved"]} == {"open protocol"}
        rows = owner.execute(
            "SELECT protocol_id FROM reckoner.v1_protocol_authorizations ORDER BY 1"
        ).fetchall()
    assert [r[0] for r in rows] == sorted(d["protocol_id"] for d in draft["dispatch"])
    # A second recording of the same protocol is refused (its own envelope is open).
    with pytest.raises(ValueError, match="unresolved|duplicate approval|already approved"):
        reserve(pg, draft, fixture_approval(draft))


@pytest.mark.integration
def test_reservation_rejects_unbound_approvals_and_changed_declarations(pg, tmp_path):
    from reckoner.v1.experiment.protocol import approval_scope, identify

    draft, ledger, runs = experiment_scenario(pg, tmp_path)
    wrong_scope = approval_fixture(
        draft["protocol_sha256"], {**approval_scope(draft), "usd_cap": "1"}
    )
    with pytest.raises(ValueError, match="scope"):
        reserve(pg, draft, wrong_scope)
    changed = deepcopy(draft)
    changed["runs"][0]["experiment_id"] = "f" * 64
    changed = identify(changed)
    with pytest.raises(ValueError, match="declared run"):
        reserve(pg, changed, fixture_approval(changed))
    other_ledger = deepcopy(draft)
    other_ledger["ledger"]["ledger_id"] = "8" * 64
    other_ledger = identify(other_ledger)
    with pytest.raises(ValueError, match="wrong ledger"):
        reserve(pg, other_ledger, fixture_approval(other_ledger))
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        assert owner.execute("SELECT count(*) FROM reckoner.v1_protocols").fetchone()[0] == 0
    reserve(pg, draft, fixture_approval(draft))
