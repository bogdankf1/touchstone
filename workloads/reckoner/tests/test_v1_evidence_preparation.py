"""Offline real-archive evidence preparation: schedule, identities, policy, resume, publish.

All data is a small fabricated simulated source prepared by the real `prepare_v1` path.
"""

import csv
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from reckoner.contracts import content_id
from reckoner.data.adapter import adapt_row
from reckoner.data.cohort import prepare
from reckoner.v1.data.history import historical_resolution, instant
from reckoner.v1.data.prepare import prepare_v1
from test_cohort import HEADERS, row, write_source

SNAPSHOT = "5" * 64


def _dated(user, when, label, ordinal, *, card="0", merchant=None, amount="$20.01"):
    item = row(user, when.year, label, ordinal, amount=amount)
    item.update(
        Card=card,
        Month=str(when.month),
        Day=str(when.day),
        Time=f"{ordinal % 24:02d}:{(ordinal * 7) % 60:02d}",
        **({"Merchant Name": merchant} if merchant else {}),
    )
    return item


# A second card whose next use is more than 97 days after its previous one, and a third
# card that first appears on a mid-March 2018 query day (absent from that day's projection).
EXTRA_CARDS = [
    ("history-low", "1", date(2017, 2, 1), "1001"),
    ("history-low", "1", date(2017, 7, 20), "1001"),
    ("history-low", "2", date(2018, 3, 15), "1002"),
    ("history-low", "2", date(2018, 3, 20), "1002"),
    # Merchant 2001 appears in one tenant on 2018-02-01 and in the other on 2018-03-05:
    # their shared-identity link is first observed on 2018-03-05 at 10:00.
    ("history-low", "0", date(2018, 2, 1), "2001"),
    ("history-high", "0", date(2018, 3, 5), "2001"),
]


def write_rolling_source(source):
    """Two simulated years spread over every month; merchants are shared across tenants."""
    write_source(source)
    rows = []
    for year in (2017, 2018):
        start = date(year, 1, 1)
        for label, count, user in [("Yes", 210, "history-high"), ("No", 1820, "history-low")]:
            for n in range(count):
                rows.append(_dated(user, start + timedelta(days=n * 350 // count), label, n))
    for n, (user, card, when, merchant) in enumerate(EXTRA_CARDS):
        item = _dated(user, when, "No", 9000 + n, card=card, merchant=merchant)
        item["Time"] = "10:00"
        rows.append(item)
    with (source / "credit_card_transactions-ibm_v2.csv").open("a", newline="") as stream:
        csv.DictWriter(stream, fieldnames=HEADERS).writerows(rows)
    with (source / "sd254_cards.csv").open("a", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["User", "CARD INDEX"])
        writer.writerows({"User": "history-low", "CARD INDEX": c} for c in ("1", "2"))


def build_inputs(root):
    source = root / "source"
    write_rolling_source(source)
    baseline = root / "baseline"
    prepare(source, baseline)
    bundle = root / "bundle"
    prepare_v1(source, baseline, bundle)
    return source, baseline, bundle


@pytest.fixture(scope="module")
def inputs(tmp_path_factory):
    return build_inputs(tmp_path_factory.mktemp("rolling-inputs"))


def fabricated_scaler():
    from reckoner.v1.smoke import smoke_scaler

    return smoke_scaler()


def tx(tenant="tenant-a", ident="t1", when="2017-03-01T12:00:00Z", **changes):
    return {"tenant_id": tenant, "transaction_id": ident, "occurred_at": when, **changes}


# --- schedule -----------------------------------------------------------------


def test_schedule_groups_cases_by_exact_utc_day_and_flags_the_pilot_subset():
    from reckoner.v1.evidence.preparation import schedule_cases

    development = [
        tx(ident="d1", when="2017-03-01T00:00:00Z"),
        tx(ident="d2", when="2017-03-01T23:59:59Z"),
        tx(ident="d3", when="2017-03-02T00:00:00+00:00"),
        tx(tenant="tenant-b", ident="d4", when="2017-03-01T23:30:00-01:00"),
    ]
    schedule = schedule_cases(
        {"development": development, "validation": [tx(ident="v1", when="2018-01-01T05:00:00Z")]},
        {"development": ("relational",), "validation": ("relational", "gds-augmented")},
        pilot={("tenant-a", "d2")},
    )
    assert [entry["day"] for entry in schedule] == ["2017-03-01", "2017-03-02", "2018-01-01"]
    first = schedule[0]["cases"]
    assert [(c["tenant_id"], c["transaction_id"]) for c in first] == [
        ("tenant-a", "d1"),
        ("tenant-a", "d2"),
    ]
    assert [c["pilot"] for c in first] == [False, True]
    assert schedule[1]["cases"][0]["transaction_id"] in {"d3", "d4"}
    assert {c["transaction_id"] for c in schedule[1]["cases"]} == {"d3", "d4"}
    assert schedule[2]["cases"][0]["modes"] == ["relational", "gds-augmented"]
    assert schedule[2]["cases"][0]["population"] == "validation"


@pytest.mark.parametrize(
    "populations,pilot,message",
    [
        ({"development": [tx(), tx()]}, set(), "duplicate"),
        ({"development": [tx()], "validation": [tx()]}, set(), "duplicate"),
        ({"development": [tx()]}, {("tenant-a", "absent")}, "pilot"),
    ],
)
def test_schedule_rejects_duplicates_cross_population_ids_and_a_foreign_pilot(
    populations, pilot, message
):
    from reckoner.v1.evidence.preparation import schedule_cases

    modes = {name: ("relational",) for name in populations}
    with pytest.raises(ValueError, match=message):
        schedule_cases(populations, modes, pilot=pilot)


def test_query_schedule_reads_frozen_runtime_files_and_verifies_the_baseline_bundle(
    inputs, tmp_path
):
    import shutil

    from reckoner.v1.evidence.preparation import DEFAULT_POPULATIONS, query_schedule

    source, baseline, bundle = inputs
    schedule = query_schedule(bundle, baseline, DEFAULT_POPULATIONS)
    cases = [c for entry in schedule for c in entry["cases"]]
    counts = {name: sum(c["population"] == name for c in cases) for name in DEFAULT_POPULATIONS}
    assert counts == {"development": 2000, "validation": 2000, "cohort-2019": 1000}
    assert sum(c["pilot"] for c in cases) == 20
    assert all(c["population"] == "development" for c in cases if c["pilot"])
    assert all(c["transaction"]["transaction_id"] == c["transaction_id"] for c in cases)
    other = tmp_path / "other-baseline"
    shutil.copytree(baseline, other)
    index = json.loads((other / "bundle.json").read_text())
    index["source_files"] = {}
    (other / "bundle.json").write_text(json.dumps(index))
    with pytest.raises(ValueError):
        query_schedule(bundle, other, DEFAULT_POPULATIONS)


# --- window boundaries and identities -------------------------------------------


def test_working_set_window_keeps_a_midnight_query_lookback_and_drops_one_second_earlier():
    from reckoner.v1.evidence.preparation import in_window, window

    start, end = window("2017-06-15")
    assert start.isoformat() == "2017-03-10T00:00:00+00:00"
    assert end.isoformat() == "2017-06-16T00:00:00+00:00"
    assert in_window("2017-03-10T00:00:00Z", "2017-06-15")
    assert not in_window("2017-03-09T23:59:59Z", "2017-06-15")
    assert in_window("2017-06-15T23:59:59Z", "2017-06-15")
    assert not in_window("2017-06-16T00:00:00Z", "2017-06-15")


def _declare(inputs, scaler, populations):
    from reckoner.v1.evidence.preparation import declare_preparation, query_schedule

    source, baseline, bundle = inputs
    return declare_preparation(
        bundle=bundle,
        baseline_bundle=baseline,
        scaler=scaler,
        entity_manifest_id="e" * 64,
        schedule=query_schedule(bundle, baseline, populations),
        populations=populations,
    )


def test_preparation_identity_tracks_inputs_while_the_snapshot_does_not(inputs):
    from reckoner.v1.evidence.preparation import DEFAULT_POPULATIONS

    scaler = fabricated_scaler()
    first = _declare(inputs, scaler, DEFAULT_POPULATIONS)
    assert first["preparation_id"] == content_id(
        {k: v for k, v in first.items() if k != "preparation_id"}
    )
    again = _declare(inputs, scaler, DEFAULT_POPULATIONS)
    assert again == first
    fewer = _declare(inputs, scaler, {"development": ("relational",)})
    more = _declare(
        inputs,
        scaler,
        {**DEFAULT_POPULATIONS, "development": ("relational", "gds-augmented")},
    )
    other_body = {k: v for k, v in scaler.items() if k != "scaler_id"}
    other_body["median"] = [value + 1 for value in other_body["median"]]
    rescaled = _declare(
        inputs, {**other_body, "scaler_id": content_id(other_body)}, DEFAULT_POPULATIONS
    )
    ids = {d["preparation_id"] for d in (first, fewer, more, rescaled)}
    assert len(ids) == 4
    snapshots = {d["source_snapshot_id"] for d in (first, fewer, more, rescaled)}
    assert snapshots == {first["source_snapshot_id"]}
    assert first["scaler_id"] == scaler["scaler_id"]
    assert first["populations"]["pilot"]["case_count"] == 20
    assert first["dataset_simulated"] is True and first["provider_calls"] == 0


def test_source_snapshot_identity_depends_only_on_source_bundle_manifest_and_policy():
    from reckoner.v1.evidence.preparation import source_snapshot_id

    base = source_snapshot_id("b" * 64, "s" * 64, "e" * 64)
    assert base == content_id(
        {
            "working_set": "rolling-97d-v1",
            "bundle_id": "b" * 64,
            "source_sha256": "s" * 64,
            "entity_manifest_id": "e" * 64,
            "resolution_policy": "simulated-seven-days-v1",
        }
    )
    assert source_snapshot_id("c" * 64, "s" * 64, "e" * 64) != base
    assert source_snapshot_id("b" * 64, "s" * 64, "f" * 64) != base


def test_entity_manifest_has_full_history_first_observations_ownership_and_shared_identity(
    inputs,
):
    from reckoner.v1.data.history import SourceHistory
    from reckoner.v1.evidence.preparation import entity_manifest

    source, baseline, bundle = inputs
    manifest = entity_manifest(bundle, source)
    assert manifest["entity_manifest_id"] == content_id(
        {k: v for k, v in manifest.items() if k != "entity_manifest_id"}
    )
    entities = {(e["tenant_id"], e["kind"], e["identity"]): e for e in manifest["entities"]}
    with SourceHistory(bundle, source) as history:
        rows = [
            history.transaction(r[0])
            for r in history.connection.execute("SELECT source_record FROM history")
        ]
    for kind in ("account", "card", "merchant"):
        first = {}
        for item in rows:
            key = (item["tenant_id"], kind, item[f"{kind}_id"])
            candidate = (item["occurred_at"], item["provenance"]["source_record"])
            first[key] = min(first.get(key, candidate), candidate)
        for key, (when, record) in first.items():
            assert entities[key]["first_observed_at"] == when
            assert entities[key]["first_source_record"] == record
            assert entities[key]["key"] == content_id(list(key))
    assert len(entities) == len(
        {(r["tenant_id"], k, r[f"{k}_id"]) for r in rows for k in ("account", "card", "merchant")}
    )
    for item in rows:
        card = entities[(item["tenant_id"], "card", item["card_id"])]
        assert card["account_identity"] == item["account_id"]
    # One tenant-free identity for the same source merchant name in both tenants.
    shared = content_id({"dataset": "cctd", "entity": "merchant", "source_merchant": "1001"})
    holders = {e["tenant_id"] for e in manifest["entities"] if e.get("shared_identity") == shared}
    assert holders == {"tenant-a", "tenant-b"}
    assert all(("shared_identity" in e) == (e["kind"] == "merchant") for e in manifest["entities"])
    later = [e for e in manifest["entities"] if e["kind"] == "card"]
    assert "2018-03-15T10:00:00Z" in {e["first_observed_at"] for e in later}


def test_entity_reconciliation_reports_every_mismatch_against_a_prior_manifest():
    from reckoner.v1.evidence.preparation import reconcile_entity_manifest

    ours = {
        "entities": [
            {
                "tenant_id": "a",
                "kind": "card",
                "identity": "x",
                "first_observed_at": "T1",
                "first_source_record": 1,
            },
            {
                "tenant_id": "a",
                "kind": "card",
                "identity": "y",
                "first_observed_at": "T2",
                "first_source_record": 2,
            },
        ]
    }
    prior = {
        "entities": [
            {
                "tenant_id": "a",
                "kind": "card",
                "identity": "x",
                "occurred_at": "T1",
                "source_record": 1,
                "key": 7,
            },
            {
                "tenant_id": "a",
                "kind": "card",
                "identity": "y",
                "occurred_at": "T0",
                "source_record": 2,
                "key": 8,
            },
            {
                "tenant_id": "b",
                "kind": "card",
                "identity": "z",
                "occurred_at": "T0",
                "source_record": 3,
                "key": 9,
            },
        ]
    }
    result = reconcile_entity_manifest(ours, prior)
    assert result["matching"] == 1
    assert result["first_observation_mismatches"] == 1
    assert result["only_in_prior"] == 1 and result["only_in_ours"] == 0


# --- missing-reason policy ------------------------------------------------------


def evidence(
    missing=(),
    status=None,
    projection=None,
    tenant="tenant-a",
    ident="q",
    when="2018-03-15T10:00:00Z",
):
    from reckoner.v1.evidence.assemble import document

    tx_ = {"tenant_id": tenant, "transaction_id": ident, "occurred_at": when, "amount_minor": 100}
    coverage = {
        "status": status or ("partial" if missing else "available"),
        "missing": list(missing),
    }
    return document(tx_, SNAPSHOT, coverage, {}, projection=projection)


def receipt(cutoff="2018-03-15T00:00:00Z", query="2018-03-15T10:00:00Z", **changes):
    from reckoner.v1.evidence.neo4j import PARAMETERS

    body = {
        "cutoff": cutoff,
        "window_days": 30,
        "algorithm": "louvain-page-rank-v1",
        "gds_version": "2026.09.0",
        "parameters": PARAMETERS,
        "node_count": 3,
        "edge_count": 2,
        "covered_accounts": 1,
        "covered_cards": 1,
        "covered_merchants": 1,
        "build_seconds": 0.5,
        "page_rank_converged": False,
        "page_rank_iterations": 20,
        "community_count": 1,
        "cross_tenant": True,
        **changes,
    }
    age = (instant(query) - instant(cutoff)).total_seconds()
    return {**body, "projection_id": content_id(body), "snapshot_age_seconds": str(age)}


def test_intrinsic_reasons_are_persisted_and_preparation_faults_fail_the_day():
    from reckoner.v1.evidence.preparation import PreparationFault, check_documents

    absent = evidence(["query card absent from GDS projection"], projection=receipt())
    first = {("tenant-a", "late-card"): "2018-03-15T10:00:00Z"}
    late = {"tenant_id": "tenant-a", "transaction_id": "q", "transaction": {"card_id": "late-card"}}
    summary = check_documents(
        "2018-03-15",
        [
            (late, "gds-augmented", absent),
            ({"tenant_id": "tenant-a", "transaction_id": "r"}, "relational", evidence()),
        ],
        first_observed=first,
    )
    assert summary["intrinsic"] == {"query card absent from GDS projection": 1}
    for mode, document in [
        ("relational", evidence(["eligible comparable vectors unavailable"])),
        ("relational", evidence(["historical coverage unavailable"], status="unavailable")),
        ("relational", evidence(["shared-merchant scope undeclared"])),
        ("relational", evidence(["shared-merchant scope coverage unavailable: tenant-b"])),
        ("relational", evidence(["query card absent from GDS projection"])),
        ("gds-augmented", evidence(["GDS projection unavailable before query"])),
        ("gds-augmented", evidence(["graph unavailable: service unreachable"])),
        ("gds-augmented", evidence(["graph unavailable"])),
        ("gds-augmented", evidence(["something new"], projection=receipt())),
        ("gds-augmented", evidence(projection=receipt(cutoff="2018-03-14T00:00:00Z"))),
        ("relational", evidence(projection=receipt())),
    ]:
        case = {"tenant_id": "tenant-a", "transaction_id": "bad"}
        with pytest.raises(PreparationFault) as raised:
            check_documents("2018-03-15", [(case, mode, document)])
        assert raised.value.failures[0]["transaction_id"] == "bad"


# --- resume -----------------------------------------------------------------


def schedule_fixture():
    def case(ident, population="validation", modes=("relational", "gds-augmented")):
        return {
            "tenant_id": "tenant-a",
            "transaction_id": ident,
            "population": population,
            "modes": list(modes),
            "pilot": False,
        }

    return [
        {"day": "2018-01-01", "cases": [case("a"), case("b")]},
        {"day": "2018-01-02", "cases": [case("c")]},
        {"day": "2018-01-03", "cases": [case("d", "development", ("relational",))]},
    ]


def fact(ident, mode="relational", snapshot=SNAPSHOT, cutoff=None, evidence_id=None):
    return {
        "tenant_id": "tenant-a",
        "transaction_id": ident,
        "mode": mode,
        "snapshot": snapshot,
        "graph_cutoff": cutoff,
        "evidence_id": evidence_id or content_id([ident, mode]),
    }


def test_resume_planner_trusts_persisted_documents_and_reconstructs_missing_receipts():
    from reckoner.v1.evidence.preparation import plan_run

    facts = [fact("a"), fact("b"), fact("c", snapshot="9" * 64)]
    plan = plan_run(
        schedule_fixture(),
        facts,
        receipts=[],
        mode="relational",
        through="2018-12-31",
        snapshot=SNAPSHOT,
    )
    assert plan["complete"] == ["2018-01-01"]
    assert plan["reconstruct"] == ["2018-01-01"]
    assert plan["pending"] == ["2018-01-02", "2018-01-03"]
    graph = plan_run(
        schedule_fixture(),
        facts + [fact("a", "gds-augmented", cutoff="2018-01-01T00:00:00Z")],
        receipts=[{"day": "2018-01-01"}],
        mode="gds-augmented",
        through="2018-01-02",
        snapshot=SNAPSHOT,
    )
    assert graph["complete"] == [] and graph["pending"] == ["2018-01-01", "2018-01-02"]
    wrong_day = plan_run(
        schedule_fixture(),
        [fact(i, "gds-augmented", cutoff="2017-12-31T00:00:00Z") for i in ("a", "b")],
        receipts=[],
        mode="gds-augmented",
        through="2018-01-01",
        snapshot=SNAPSHOT,
    )
    assert wrong_day["pending"] == ["2018-01-01"]
    receipt_ = {"day": "2018-01-01", "pass": "relational"}
    done = plan_run(
        schedule_fixture(),
        facts,
        receipts=[receipt_],
        mode="relational",
        through="2018-01-01",
        snapshot=SNAPSHOT,
    )
    assert done == {"complete": ["2018-01-01"], "reconstruct": [], "pending": []}


def test_reconstructed_receipts_come_from_persisted_facts_and_are_marked():
    from reckoner.v1.evidence.preparation import reconstruct_receipt

    entry = schedule_fixture()[0]
    result = reconstruct_receipt(entry, [fact("a"), fact("b")], mode="relational")
    assert result["receipt_reconstructed"] is True
    assert result["day"] == "2018-01-01" and result["cases"] == 2
    assert result["evidence_ids"] == sorted([fact("a")["evidence_id"], fact("b")["evidence_id"]])


@pytest.mark.parametrize(
    "receipts,metrics,referenced,expected",
    [
        ([], 0, False, "build"),
        ([("tenant-a", "p"), ("tenant-b", "p")], 3, False, "reuse"),
        ([("tenant-a", "p")], 3, False, "rebuild"),
        ([("tenant-a", "p"), ("tenant-b", "q")], 3, False, "rebuild"),
        ([("tenant-a", "p"), ("tenant-b", "p")], 2, False, "rebuild"),
        ([("tenant-a", "p")], 3, True, "refuse"),
        ([("tenant-a", "p"), ("tenant-b", "p")], 2, True, "refuse"),
    ],
)
def test_partial_projection_receipts_are_rebuilt_only_when_unreferenced(
    receipts, metrics, referenced, expected
):
    from reckoner.v1.evidence.preparation import projection_action

    rows = [{"tenant_id": t, "projection_id": p, "node_count": 3} for t, p in receipts]
    assert (
        projection_action(rows, ("tenant-a", "tenant-b"), metrics, referenced=referenced)
        == expected
    )


# --- publication --------------------------------------------------------------


def declaration():
    body = {
        "source_snapshot_id": SNAPSHOT,
        "populations": {
            "validation": {"sample_id": "v" * 64, "modes": ["relational", "gds-augmented"]},
            "development": {"sample_id": "d" * 64, "modes": ["relational"]},
            "pilot": {"sample_id": "p" * 64, "modes": ["relational"], "parent": "development"},
        },
    }
    return {**body, "preparation_id": content_id(body)}


def persisted(ident, day="2018-01-01", gds=False, missing=()):
    when = f"{day}T10:00:00Z"
    projection = receipt(cutoff=f"{day}T00:00:00Z", query=when) if gds else None
    document = evidence(missing, projection=projection, ident=ident, when=when)
    return document


def test_publish_writes_content_addressed_manifests_and_refuses_overwrite(tmp_path):
    from reckoner.v1.evidence.preparation import publish

    schedule = schedule_fixture()
    schedule[2]["cases"][0]["pilot"] = True
    documents = [persisted(i) for i in "abcd"]
    written = publish(declaration(), schedule, documents, tmp_path, mode="relational")
    names = sorted(item["population"] for item in written)
    assert names == ["development", "pilot", "validation"]
    validation = json.loads((tmp_path / "manifests/validation-relational.json").read_text())
    assert validation["manifest_id"] == content_id(
        {k: v for k, v in validation.items() if k != "manifest_id"}
    )
    assert [c["transaction_id"] for c in validation["cases"]] == ["a", "b", "c"]
    assert validation["cases"][0]["evidence_id"] == documents[0]["evidence_id"]
    pilot = json.loads((tmp_path / "manifests/pilot-relational.json").read_text())
    assert [c["transaction_id"] for c in pilot["cases"]] == ["d"]
    again = publish(declaration(), schedule, documents, tmp_path, mode="relational")
    assert again == written
    (tmp_path / "manifests/validation-relational.json").write_text("{}")
    with pytest.raises(FileExistsError):
        publish(declaration(), schedule, documents, tmp_path, mode="relational")


@pytest.mark.parametrize("problem", ["missing", "extra", "duplicate"])
def test_publish_refuses_missing_extra_or_duplicate_cases(tmp_path, problem):
    from reckoner.v1.evidence.preparation import publish

    documents = [persisted(i) for i in "abcd"]
    if problem == "missing":
        documents.pop()
    elif problem == "extra":
        documents.append(persisted("zz"))
    else:
        other = evidence(["eligible comparable vectors unavailable"], ident="a")
        documents.append(other)
    with pytest.raises(ValueError, match=problem):
        publish(declaration(), schedule_fixture(), documents, tmp_path, mode="relational")
    assert not (tmp_path / "manifests").exists()


def test_gds_manifest_carries_projection_convergence_and_snapshot_age(tmp_path):
    from reckoner.v1.evidence.preparation import publish

    documents = [persisted(i, gds=True) for i in "ab"] + [persisted("c", "2018-01-02", True)]
    documents[1] = persisted("b", gds=True, missing=["query card absent from GDS projection"])
    written = publish(declaration(), schedule_fixture(), documents, tmp_path, mode="gds-augmented")
    assert [w["population"] for w in written] == ["validation"]
    manifest = json.loads((tmp_path / "manifests/validation-gds-augmented.json").read_text())
    first, second = manifest["cases"][:2]
    assert first["page_rank_converged"] is False and first["snapshot_age_seconds"] == "36000.0"
    assert first["projection_id"] == documents[0]["graph_projection"]["projection_id"]
    assert second["coverage_status"] == "partial"
    assert second["missing"] == ["query card absent from GDS projection"]


# --- CLI ------------------------------------------------------------------------


def test_cli_environment_allowlist_rejects_unknown_keys(tmp_path, monkeypatch):
    from reckoner.v1.evidence.steps import EVIDENCE_KEYS, read_environment

    for key in EVIDENCE_KEYS:  # the host shell may export real store DSNs; isolate the test
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / "evidence.env"
    env.write_text("RECKONER_OWNER_DSN=postgresql://o\n# comment\nRECKONER_RUNNER_DSN=x\n")
    assert read_environment(env) == {
        "RECKONER_OWNER_DSN": "postgresql://o",
        "RECKONER_RUNNER_DSN": "x",
    }
    env.write_text("RECKONER_OWNER_DSN=x\nJEV_API_KEY=secret\n")
    with pytest.raises(ValueError, match="allow"):
        read_environment(env)
    monkeypatch.setenv("JEV_API_KEY", "secret")
    monkeypatch.setenv("RECKONER_SOURCE_DIR", "/data/source")
    assert read_environment("-") == {"RECKONER_SOURCE_DIR": "/data/source"}


def test_output_directory_is_reused_only_for_the_same_preparation(tmp_path):
    from reckoner.v1.evidence.steps import output_directory

    first = declaration()
    path = output_directory(tmp_path, first)
    assert path == tmp_path / first["preparation_id"][:12]
    assert json.loads((path / "declaration.json").read_text()) == first
    assert output_directory(tmp_path, first) == path
    changed = {k: v for k, v in first.items() if k != "preparation_id"}
    changed["source_snapshot_id"] = "6" * 64
    other = {**changed, "preparation_id": first["preparation_id"]}
    with pytest.raises(ValueError, match="preparation"):
        output_directory(tmp_path, other)


def test_cli_registers_the_evidence_group():
    from reckoner.cli import _parser

    parser = _parser()
    args = parser.parse_args(
        [
            "v1",
            "evidence",
            "run",
            "--declaration",
            "d.json",
            "--pass",
            "relational",
            "--through",
            "2017-01-31",
            "--env-file",
            "-",
        ]
    )
    assert (args.v1_command, args.evidence_step, args.evidence_pass) == (
        "evidence",
        "run",
        "relational",
    )
    for step in ("declare", "publish", "drop-working-set", "graph-check"):
        assert parser.parse_args(_minimal(step)).evidence_step == step


def _minimal(step):
    common = ["--env-file", "-"]
    if step == "declare":
        return [
            "v1",
            "evidence",
            step,
            "--bundle",
            "b",
            "--scaler",
            "s",
            "--output-root",
            "o",
            *common,
        ]
    if step == "publish":
        return ["v1", "evidence", step, "--declaration", "d", "--pass", "relational", *common]
    if step == "graph-check":
        return ["v1", "evidence", step, "--declaration", "d", "--day", "2018-06-01", *common]
    return ["v1", "evidence", step, "--declaration", "d", *common]


# --- Neo4j ownership (offline, recording fake driver) ----------------------------


class FakeSession:
    def __init__(self, store, log):
        self.store, self.log = store, log

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def run(self, query, **params):
        self.log.append(query)
        store = self.store

        class Result:
            def single(self_inner):
                if "SmokeStore" in query or "DisposableStore" in query:
                    return {"n": store["foreign_markers"]}
                if "EvidenceStore" in query and "RETURN" in query:
                    return {"n": len(store["markers"]), "ids": store["markers"]}
                return {"n": store["nodes"]}

            def consume(self_inner):
                return None

        return Result()


class FakeDriver:
    def __init__(self, **store):
        self.store = {"foreign_markers": 0, "markers": [], "nodes": 0, **store}
        self.log = []

    def session(self, **_):
        return FakeSession(self.store, self.log)


WRITES = ("CREATE", "MERGE", "DELETE", "SET ", "gds.")


@pytest.mark.parametrize(
    "store",
    [
        {"foreign_markers": 1, "nodes": 1},  # SmokeStore / DisposableStore marker
        {"nodes": 123814},  # unmarked, e.g. the preserved Task 3 graph
        {"markers": ["other-preparation"], "nodes": 10},
    ],
)
def test_rolling_graph_refuses_marked_unowned_or_foreign_stores_before_any_write(store):
    from reckoner.v1.evidence.rolling_graph import RollingGraph

    driver = FakeDriver(**store)
    with pytest.raises(ValueError, match="refus"):
        RollingGraph(driver, preparation_id="this-preparation", snapshot_id=SNAPSHOT).claim()
    assert not [q for q in driver.log if any(w in q for w in WRITES)]


def test_rolling_graph_claims_only_an_empty_store():
    from reckoner.v1.evidence.rolling_graph import RollingGraph

    driver = FakeDriver()
    RollingGraph(driver, preparation_id="this-preparation", snapshot_id=SNAPSHOT).claim()
    assert [q for q in driver.log if "CREATE (:EvidenceStore" in q]


def test_graph_rows_carry_seeded_keys_and_declared_shared_identity():
    from reckoner.v1.evidence.rolling_graph import graph_rows

    source = row("u", 2018, "No", 3)
    item = adapt_row(source, source_sha256="a" * 64, source_record=4, tenant_id="tenant-a")[
        "transaction"
    ]
    resolution = historical_resolution(item, "legitimate", "simulated-seven-days-v1")
    identities = {("tenant-a", item["merchant_id"]): "shared-x"}
    (result,) = graph_rows([(item, resolution)], identities)
    assert result["card_key"] == content_id(["tenant-a", "card", item["card_id"]])
    assert result["merchant_identity"] == "shared-x"
    assert json.loads(result["document"]) == item
    assert result["resolved_at"] == resolution["resolved_at"]


def test_incremental_card_history_vectors_equal_complete_source_vectors(inputs):
    """Day-by-day vectors from the per-card cache equal `candidate_vectors` on full history."""
    from datetime import UTC, datetime

    from reckoner.v1.benchmark.queries import candidate_vectors
    from reckoner.v1.data.history import SourceHistory
    from reckoner.v1.evidence.rolling import CardHistory, SourceDays

    source, _, bundle = inputs
    scaler = fabricated_scaler()
    with SourceHistory(bundle, source) as history:
        days = SourceDays(history, bundle)
        cache = CardHistory(history, scaler)
        start = datetime(2017, 1, 20, tzinfo=UTC)
        compared = 0
        for offset in range(0, 200, 1):
            day = start + timedelta(days=offset)
            pairs = days.records(day, day + timedelta(days=1))
            positive = [tx for tx, _ in pairs if tx["amount_minor"] > 0]
            expected = {
                tx["transaction_id"]: vector
                for tx, vector in candidate_vectors(positive, history, scaler)
            }
            actual = {tx["transaction_id"]: vector for tx, vector in cache.vectors(pairs)}
            assert actual == expected, day
            compared += len(actual)
        days.close()
    assert compared > 500


def test_isolated_calls_run_in_a_fresh_child_process_and_propagate_failures():
    """Assembly runs per day in a spawned child so its native memory returns on exit."""
    import os

    from reckoner.v1.evidence.rolling import isolated

    assert isolated(os.getpid) != os.getpid()
    with pytest.raises(ValueError):
        isolated(int, "not a number")


def test_intrinsic_absence_requires_the_cards_first_observation_on_or_after_the_cutoff():
    """Missing GDSMetric nodes for an older card are a preparation fault, not intrinsic."""
    from reckoner.v1.evidence.preparation import PreparationFault, check_documents

    absent = evidence(["query card absent from GDS projection"], projection=receipt())
    case = {"tenant_id": "tenant-a", "transaction_id": "q", "transaction": {"card_id": "c1"}}
    for first in (
        {("tenant-a", "c1"): "2018-03-14T23:59:00Z"},  # observed before the cutoff
        {},  # unknown card
        None,  # no manifest supplied
    ):
        with pytest.raises(PreparationFault) as raised:
            check_documents("2018-03-15", [(case, "gds-augmented", absent)], first_observed=first)
        assert (
            "first observed before the projection cutoff" in raised.value.failures[0]["reasons"][0]
        )
    on_cutoff = {("tenant-a", "c1"): "2018-03-15T00:00:00Z"}
    check_documents("2018-03-15", [(case, "gds-augmented", absent)], first_observed=on_cutoff)


def test_publish_one_complete_population_while_later_ones_are_still_partial(tmp_path):
    from reckoner.v1.evidence.preparation import publish

    schedule = schedule_fixture()
    schedule[2]["cases"][0]["pilot"] = True
    development = [persisted("d")]
    written = publish(
        declaration(),
        schedule,
        development,
        tmp_path,
        mode="relational",
        populations=["development"],
    )
    assert sorted(w["population"] for w in written) == ["development", "pilot"]
    published = {
        name: (tmp_path / f"manifests/{name}-relational.json").read_bytes()
        for name in ("development", "pilot")
    }
    with pytest.raises(ValueError, match="missing"):
        publish(
            declaration(),
            schedule,
            development,
            tmp_path,
            mode="relational",
            populations=["validation"],
        )
    assert not (tmp_path / "manifests/validation-relational.json").exists()
    with pytest.raises(ValueError, match="extra"):
        publish(
            declaration(),
            schedule,
            [*development, persisted("zz")],
            tmp_path,
            mode="relational",
            populations=["development"],
        )
    everything = [persisted(i) for i in "abc"] + development
    publish(
        declaration(), schedule, everything, tmp_path, mode="relational", populations=["validation"]
    )
    publish(declaration(), schedule, everything, tmp_path, mode="relational")
    for name, payload in published.items():
        assert (tmp_path / f"manifests/{name}-relational.json").read_bytes() == payload
    # The pilot follows its development parent.
    other = tmp_path / "pilot-only"
    names = publish(
        declaration(), schedule, development, other, mode="relational", populations=["pilot"]
    )
    assert sorted(w["population"] for w in names) == ["development", "pilot"]
    with pytest.raises(ValueError, match="undeclared"):
        publish(
            declaration(),
            schedule,
            development,
            other,
            mode="relational",
            populations=["cohort-2019"],
        )


def test_stage_summary_counts_coverage_non_convergence_and_snapshot_ages():
    from reckoner.v1.evidence.preparation import stage_summary

    documents = [persisted(i, gds=True) for i in "ab"] + [
        persisted("c", gds=True, missing=["query card absent from GDS projection"]),
        persisted("d"),
    ]
    summary = stage_summary(documents, mode="gds-augmented")
    assert summary["documents"] == 3
    assert summary["statuses"] == {"available": 2, "partial": 1}
    assert summary["missing"] == {"query card absent from GDS projection": 1}
    assert summary["page_rank_non_converged"] == 3
    assert summary["snapshot_age_seconds"] == {
        "min": "36000.0",
        "median": "36000.0",
        "max": "36000.0",
    }
    relational = stage_summary(documents, mode="relational")
    assert relational["documents"] == 1 and relational["page_rank_non_converged"] == 0


@pytest.mark.parametrize(
    "value,accepted",
    [("1", False), ("16106127359", False), ("16106127360", True), ("21474836480", True)],
)
def test_cli_free_floor_can_only_be_raised(value, accepted):
    from reckoner.cli import _parser

    arguments = [
        "v1",
        "evidence",
        "run",
        "--declaration",
        "d",
        "--pass",
        "relational",
        "--through",
        "2017-01-31",
        "--env-file",
        "-",
        "--free-floor-bytes",
        value,
    ]
    if accepted:
        assert _parser().parse_args(arguments).free_floor_bytes == int(value)
    else:
        with pytest.raises(SystemExit):
            _parser().parse_args(arguments)
    assert _parser().parse_args(arguments[:-2]).free_floor_bytes == 15 * 1024**3


def test_isolated_raises_promptly_when_the_child_is_killed():
    """A cgroup OOM kill of the assembly child must fail the run, never hang it."""
    import signal
    import time

    from reckoner.v1.evidence.rolling import isolated

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="assembly child process died"):
        isolated(signal.raise_signal, signal.SIGKILL)
    assert time.monotonic() - started < 60


def test_preparation_fault_survives_pickling_across_processes():
    import pickle

    from reckoner.v1.evidence.preparation import PreparationFault

    fault = PreparationFault("2018-03-15", [{"transaction_id": "q", "reasons": ["x"]}])
    copy = pickle.loads(pickle.dumps(fault))
    assert (copy.day, copy.failures, str(copy)) == (fault.day, fault.failures, str(fault))


def test_store_guard_also_checks_the_container_vm_filesystem(tmp_path):
    from reckoner.v1.benchmark.resources import ResourceGuardError
    from reckoner.v1.evidence.rolling import StoreGuard

    record = StoreGuard(
        free_path=tmp_path, free_floor_bytes=0, vm_free_path=tmp_path, vm_free_floor_bytes=0
    )()
    assert record["vm_free_bytes"] > 0 and record["vm_free_floor_bytes"] == 0
    with pytest.raises(ResourceGuardError, match="VM"):
        StoreGuard(
            free_path=tmp_path,
            free_floor_bytes=0,
            vm_free_path=tmp_path,
            vm_free_floor_bytes=10**18,
        )()


def test_cli_vm_free_floor_defaults_to_the_hard_floor_and_can_only_be_raised():
    from reckoner.cli import _parser

    base = [
        "v1",
        "evidence",
        "run",
        "--declaration",
        "d",
        "--pass",
        "relational",
        "--through",
        "2017-01-31",
        "--env-file",
        "-",
    ]
    args = _parser().parse_args(base)
    assert (args.vm_free_path, args.vm_free_floor_bytes) == (Path("/"), 15 * 1024**3)
    with pytest.raises(SystemExit):
        _parser().parse_args([*base, "--vm-free-floor-bytes", "1"])


def test_assembly_connections_carry_a_generous_statement_timeout():
    from psycopg.conninfo import conninfo_to_dict
    from reckoner.v1.evidence.rolling import STATEMENT_TIMEOUT_MS, runtime_dsn

    options = conninfo_to_dict(runtime_dsn("postgresql://u:p@h/db"))["options"]
    assert "-c role=reckoner_runner" in options
    assert f"-c statement_timeout={STATEMENT_TIMEOUT_MS}" in options
    assert STATEMENT_TIMEOUT_MS >= 600_000  # far above the measured worst case (≈9 s/case)


def _history_db(path, stamps):
    import sqlite3

    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE history(source_record INTEGER PRIMARY KEY, occurred_at TEXT, "
            "card_key INTEGER, account_key INTEGER, merchant_key INTEGER)"
        )
        db.executemany("INSERT INTO history VALUES (?,?,1,1,1)", enumerate(stamps, start=1))
    return sqlite3.connect(path)


def test_day_ranges_refuse_timestamps_that_do_not_compare_as_instants(tmp_path):
    """Day selection compares text; only the uniform `YYYY-MM-DDTHH:MM:SSZ` form is safe."""
    import sqlite3
    from datetime import UTC, datetime

    from reckoner.v1.evidence.preparation import _first_observations
    from reckoner.v1.evidence.rolling import SourceDays

    with sqlite3.connect(tmp_path / "resolutions.sqlite") as db:
        db.execute("CREATE TABLE resolutions(source_record INTEGER PRIMARY KEY, label TEXT)")

    class History:
        def __init__(self, connection):
            self.connection = connection

        def transaction(self, ordinal):
            raise AssertionError("rows must be refused before they are read")

    start = datetime(2017, 1, 1, tzinfo=UTC)
    bad = _history_db(tmp_path / "bad.sqlite", ["2017-01-01T10:00:00.5Z"])
    with pytest.raises(ValueError, match="timestamp"):
        SourceDays(History(bad), tmp_path).records(start, start + timedelta(days=1))
    with pytest.raises(ValueError, match="timestamp"):
        _first_observations(bad, "card_key")
    good = _history_db(tmp_path / "good.sqlite", ["2017-01-01T10:00:00Z"])
    assert _first_observations(good, "card_key") == {1: ("2017-01-01T10:00:00Z", 1)}


def test_progress_lines_are_timestamped_and_flushed(capsys):
    import re

    from reckoner.v1.evidence.steps import _log

    _log("start 2017-01-01 (4 cases)")
    line = capsys.readouterr().err
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ start 2017-01-01 \(4 cases\)\n", line)


def _retry_cases():
    import psycopg
    from neo4j.exceptions import ClientError, ServiceUnavailable, SessionExpired, TransientError
    from reckoner.v1.benchmark.resources import ResourceGuardError
    from reckoner.v1.evidence.preparation import PreparationFault
    from reckoner.v1.evidence.rolling import ChildDied
    from reckoner.v1.evidence.steps import RunLockHeld

    stop = [
        PreparationFault("2018-03-15", [{"transaction_id": "q", "reasons": ["x"]}]),
        ResourceGuardError("free disk below floor"),
        ValueError("inputs no longer reproduce the declared preparation"),
        ValueError("refusing a store owned by another preparation or purpose"),
        FileExistsError("refusing to overwrite a different manifest"),
        RunLockHeld("another evidence run holds this preparation's lock"),
        psycopg.errors.UniqueViolation("conflict"),
        ClientError("syntax"),
    ]
    retry = [
        ChildDied("assembly child process died"),
        psycopg.OperationalError("server closed the connection unexpectedly"),
        ServiceUnavailable("graph stopped"),
        SessionExpired("session expired"),
        TransientError("deadlock"),
        ConnectionResetError("reset by peer"),
    ]
    return [(e, True) for e in stop] + [(e, False) for e in retry]


@pytest.mark.parametrize(
    "error,hard_stop",
    _retry_cases(),
    ids=lambda v: type(v).__name__ if not isinstance(v, bool) else "",
)
def test_hard_stops_exit_with_the_no_retry_status_and_transient_failures_stay_retryable(
    error, hard_stop, monkeypatch, capsys
):
    """The retry wrapper stops on status 3; a transient failure exits non-zero but not 3."""
    from argparse import Namespace

    from reckoner.v1.evidence import steps

    def fail(args):
        raise error

    monkeypatch.setattr(steps, "_dispatch", fail)
    args = Namespace(evidence_step="run")
    if hard_stop:
        with pytest.raises(SystemExit) as raised:
            steps.run(args)
        assert raised.value.code == steps.NO_RETRY == 3
        assert "no retry" in capsys.readouterr().err
    else:
        with pytest.raises(type(error)):
            steps.run(args)
        assert "retryable" in capsys.readouterr().err


def test_the_cli_returns_the_no_retry_status_through_main(monkeypatch):
    import psycopg
    from reckoner.cli import main
    from reckoner.v1.evidence import steps

    arguments = ["v1", "evidence", "drop-working-set", "--declaration", "d.json", "--env-file", "-"]

    def fault(args):
        raise ValueError("refusing to drop: owner comment differs")

    monkeypatch.setattr(steps, "_dispatch", fault)
    with pytest.raises(SystemExit) as raised:
        main(arguments)
    assert raised.value.code == 3

    def transient(args):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(steps, "_dispatch", transient)
    assert main(arguments) not in (0, 3)


def test_guard_defaults_to_the_hard_vm_floor_when_the_option_is_absent(tmp_path):
    from argparse import Namespace

    from reckoner.v1.benchmark.resources import FREE_FLOOR
    from reckoner.v1.evidence.steps import _guard

    guard = _guard(
        Namespace(free_path=None, free_floor_bytes=FREE_FLOOR, max_store_bytes=None), tmp_path
    )
    assert guard.vm_free_floor_bytes == FREE_FLOOR
