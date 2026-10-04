"""Bounded provider-request projection of persisted evidence (fabricated data only).

Only the provider request is bounded: persisted evidence stays byte-identical, and note
validation still checks claims against the full persisted evidence.
"""

import json
from copy import deepcopy

import pytest
from reckoner.contracts import content_id
from v1_fixtures import (
    config_fixture,
    decision_fixture,
    evidence_fixture,
    identified,
    note_fixture,
    score_fixture,
)

TX = {
    "tenant_id": "tenant-a",
    "transaction_id": "transaction-a",
    "occurred_at": "2019-01-08T00:00:00Z",
    "amount_minor": 2000,
    "currency": "USD",
}


def ref(n):
    return content_id(["fabricated-ref", n])


def indicator(identity, count, rank=1):
    return {
        "rank": rank,
        "indicator_id": identity,
        "description": f"Fabricated observation for {identity}.",
        "method": f"risk-indicators-v1: fabricated; severity={1 / rank:.12g}",
        # Persisted order is not assumed: the projection sorts canonically.
        "evidence_refs": [ref(n) for n in reversed(range(count))],
    }


def comparable(n):
    return {
        "tenant_id": "tenant-a",
        "transaction_id": f"comparable-{n}",
        "verdict": "approve",
        "resolved_at": "2018-12-01T00:00:00Z",
        "resolution_policy_version": "simulated-seven-days-v1",
        "similarity": "0.5",
        "evidence_refs": [ref(10_000 + n)],
    }


def evidence_with(*indicators, comparables=5):
    evidence = evidence_fixture()
    evidence["risk_indicators"] = list(indicators)
    evidence["comparable_cases"] = [comparable(n) for n in range(comparables)]
    return identified(evidence, "evidence_id")


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def test_large_indicator_is_projected_with_count_hash_and_ordered_exemplars():
    from reckoner.v1 import projection
    from reckoner.v1.providers.jev import build_request

    evidence = evidence_with(indicator("merchant-exposure", 2944))
    stored = deepcopy(evidence)
    request = build_request(TX, evidence)
    assert evidence == stored  # persisted evidence is never altered
    sent = request["state"]["evidence"]["risk_indicators"][0]
    full = sorted(stored["risk_indicators"][0]["evidence_refs"])
    assert sent == {
        "rank": 1,
        "indicator_id": "merchant-exposure",
        "description": stored["risk_indicators"][0]["description"],
        "method": stored["risk_indicators"][0]["method"],
        "evidence_ref_count": 2944,
        "evidence_refs_sha256": content_id(full),
        "evidence_refs": full[: projection.REF_SAMPLE],
        "evidence_refs_truncated": True,
    }
    # The Jev Choice contract in this repository documents no extra state keys, so the
    # projection version is recorded with the call and the protocol, never sent.
    assert "request_projection" not in request["state"]
    assert set(request["state"]) == {"dataset_simulated", "transaction", "evidence"}
    assert projection.VERSION == "bounded-evidence-v1"
    assert size(request) < 8000


def test_small_indicator_keeps_every_reference_and_states_no_truncation():
    from reckoner.v1.providers.jev import build_request

    evidence = evidence_with(indicator("amount-ratio", 5))
    sent = build_request(TX, evidence)["state"]["evidence"]["risk_indicators"][0]
    assert sent["evidence_refs"] == sorted(evidence["risk_indicators"][0]["evidence_refs"])
    assert sent["evidence_ref_count"] == 5 and sent["evidence_refs_truncated"] is False


def test_worst_case_request_stays_far_below_the_conservative_ceiling():
    from reckoner.v1.providers.jev import build_request

    evidence = evidence_with(
        *(indicator(name, 20_000, rank) for rank, name in enumerate(("a", "b", "c"), 1))
    )
    assert size(build_request(TX, evidence)) <= 16000  # half of the 32,000 bound


def test_comparables_beyond_the_bounded_display_fail_closed():
    from reckoner.v1.providers.jev import build_request

    with pytest.raises(ValueError, match="bounded request projection"):
        build_request(TX, evidence_with(comparables=6))
    evidence = evidence_with(comparables=1)
    evidence["comparable_cases"][0]["evidence_refs"].append(ref(99_999))
    with pytest.raises(ValueError, match="bounded request projection"):
        build_request(TX, identified(evidence, "evidence_id"))


def test_projection_adds_no_field_beyond_counts_hashes_and_flags():
    from reckoner.v1 import projection

    evidence = evidence_with(indicator("card-burst", 40))
    evidence["risk_indicators"][0]["oracle"] = "fraud"  # never copied, even if present
    (item,) = projection.indicators(evidence)
    assert set(item) == {
        "rank",
        "indicator_id",
        "description",
        "method",
        "evidence_ref_count",
        "evidence_refs_sha256",
        "evidence_refs",
        "evidence_refs_truncated",
    }


def note_case(count):
    evidence = evidence_with(indicator("merchant-exposure", count), comparables=0)
    score = score_fixture()
    note = note_fixture()
    note["evidence_id"] = evidence["evidence_id"]
    note["confidence"] = {"value": score["confidence"], "meaning": "jev_distribution_concentration"}
    note["entity_neighbourhood"] = {
        "summary": "Neighbourhood unavailable.",
        "evidence_refs": [evidence["evidence_id"]],
    }
    note["what_would_change_verdict"][0]["evidence_refs"] = [evidence["evidence_id"]]
    return note, evidence, score


def test_note_request_is_bounded_and_validation_checks_the_full_persisted_evidence():
    from reckoner.v1 import projection
    from reckoner.v1.notes import build_note_request, validate_note

    note, evidence, score = note_case(2944)
    request = build_note_request(
        decision_fixture(evidence=evidence), evidence, score, config_fixture()
    )
    context = json.loads(request["messages"][0]["content"])
    assert context["request_projection"] == projection.VERSION
    (sent,) = context["risk_indicators"]
    assert sent["evidence_ref_count"] == 2944 and sent["evidence_refs_truncated"] is True
    assert len(sent["evidence_refs"]) == projection.REF_SAMPLE
    full = evidence["risk_indicators"][0]
    copied = {k: sent[k] for k in ("rank", "indicator_id", "description", "method")}
    note["risk_indicators"] = [{**copied, "evidence_refs": sent["evidence_refs"]}]
    # An action may cite a reference outside the exemplars: it is checked against the
    # full persisted list, not the bounded request.
    note["what_would_change_verdict"][0]["evidence_refs"] = [max(full["evidence_refs"])]
    assert max(full["evidence_refs"]) not in sent["evidence_refs"]
    identified(note, "note_id")
    assert validate_note(note, evidence, score) == note


@pytest.mark.parametrize("change", ["invented", "other-subset", "method"])
def test_note_indicator_claims_outside_the_persisted_evidence_are_rejected(change):
    from reckoner.v1.notes import validate_note

    note, evidence, score = note_case(2944)
    full = sorted(evidence["risk_indicators"][0]["evidence_refs"])
    item = {k: evidence["risk_indicators"][0][k] for k in ("indicator_id", "description")}
    item.update(rank=1, method=evidence["risk_indicators"][0]["method"], evidence_refs=full[:32])
    if change == "invented":
        item["evidence_refs"] = [*full[:31], ref(123_456_789)]
    if change == "other-subset":
        item["evidence_refs"] = full[100:132]  # real, but not what was supplied or persisted
    if change == "method":
        item["method"] = "attribution"
    note["risk_indicators"] = [item]
    identified(note, "note_id")
    with pytest.raises(ValueError):
        validate_note(note, evidence, score)
