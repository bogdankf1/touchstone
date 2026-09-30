"""Static judges prove gate arithmetic only; they are never quality receipts."""

import importlib
from copy import deepcopy

import pytest
from test_v1_note_generation import sample


def api():
    try:
        return importlib.import_module("reckoner.v1.evaluation.notes")
    except ModuleNotFoundError:
        pytest.fail("note evaluation is not implemented")


class Judge:
    provenance = {
        "provider": "fabricated",
        "model": "fixture",
        "prompt_version": "fixture",
        "library_version": "fixture",
    }

    def __init__(self, values):
        self.values = iter(values)
        self.inputs = []

    def evaluate(self, note, **kwargs):
        self.inputs.append((note, kwargs))
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


def cases(n=10):
    note, evidence, score = sample()
    from v1_fixtures import identified

    result = []
    for i in range(n):
        item = deepcopy(note)
        item["case_id"] = str(i)
        identified(item, "note_id")
        result.append(
            {
                "tenant_id": "tenant-a",
                "case_id": str(i),
                "note": item,
                "evidence": evidence,
                "score": score,
                "oracle_verdict": "approve",
                "raw_transaction": {"secret": "RAW"},
            }
        )
    return result


def test_inclusive_gates_and_note_only_judge_boundary():
    verdict = Judge(["approve"] * 9 + ["decline"])
    faith = Judge([0.90] * 10)
    result = api().evaluate_note_fixtures(
        cases(),
        {"verdict": verdict, "faithfulness": faith},
        10,
        protocol={"approved": True},
        budget=object(),
    )
    assert result["status"] == "passed"
    assert result["agreement_rate"] == 0.9 and result["mean_faithfulness"] == 0.9
    assert all(
        "oracle_verdict" not in kwargs and "evidence" not in kwargs for _, kwargs in verdict.inputs
    )
    assert all("raw_transaction" not in note for note, _ in verdict.inputs)
    assert len(result["cases"]) == 10


@pytest.mark.parametrize("problem", ["missing", "error", "nan", "bad-schema", "missing-oracle"])
def test_missing_and_errors_block_passing(problem):
    items = cases(2)
    if problem == "missing":
        items.pop()
    if problem == "bad-schema":
        items[0]["note"] = None
    if problem == "missing-oracle":
        items[0].pop("oracle_verdict")
    faith = Judge(
        [RuntimeError("judge unavailable"), 1]
        if problem == "error"
        else ([float("nan"), 1] if problem == "nan" else [1, 1])
    )
    result = api().evaluate_note_fixtures(
        items,
        {"verdict": Judge(["approve"] * 2), "faithfulness": faith},
        2,
        protocol={"approved": True},
        budget=object(),
    )
    assert result["status"] != "passed"


def test_empty_population_and_unapproved_evaluation_are_not_evaluated():
    result = api().evaluate_note_fixtures([], {}, 0)
    assert result["status"] == "not-evaluated"
    verdict = Judge([])
    result = api().evaluate_note_fixtures(cases(1), {"verdict": verdict}, 1)
    assert result["status"] == "not-evaluated" and verdict.inputs == []


def test_duplicate_case_cannot_hide_missing_expected_case():
    items = cases(2)
    items[1] = items[0]
    with pytest.raises(ValueError):
        api().evaluate_note_fixtures(items, {}, 2)


def test_actual_framework_adapters_share_harness_budget():
    # A mismatched reservation object cannot satisfy an evaluation authorization.
    verdict = Judge(["approve"])
    verdict.calls = type("Calls", (), {"budget": object()})()
    result = api().evaluate_note_fixtures(
        cases(1),
        {"verdict": verdict, "faithfulness": Judge([1])},
        1,
        protocol={"approved": True},
        budget=object(),
    )
    assert result["status"] == "failed" and verdict.inputs == []


def test_note_cannot_substitute_another_declared_case():
    items = cases(1)
    items[0]["note"]["case_id"] = "other-case"
    from v1_fixtures import identified

    identified(items[0]["note"], "note_id")
    result = api().evaluate_note_fixtures(
        items,
        {"verdict": Judge(["approve"]), "faithfulness": Judge([1])},
        1,
        protocol={"approved": True},
        budget=object(),
    )
    assert result["status"] == "failed"


def test_per_case_factories_receive_only_identity_and_cover_complete_suite():
    seen = []

    def factory(identity):
        seen.append(identity)
        return Judge(["approve"])

    result = api().evaluate_note_fixtures(
        cases(2),
        {"verdict": factory, "faithfulness": lambda identity: Judge([1])},
        2,
        protocol={"approved": True},
        budget=object(),
    )
    assert result["status"] == "passed"
    assert seen == [{"tenant_id": "tenant-a", "case_id": str(i)} for i in range(2)]


def test_harness_uses_actual_deepeval_metric_comparison_locally():
    from reckoner.v1.evaluation.judges import NoteVerdict
    from test_v1_note_generation import Calls, body
    from v1_fixtures import config_fixture

    protocol = {"approved": True}
    judge = NoteVerdict(Calls([body('{"verdict":"approve"}')]), config_fixture(), protocol)
    result = api().evaluate_note_fixtures(
        cases(1),
        {"verdict": judge, "faithfulness": Judge([1])},
        1,
        protocol=protocol,
        budget=object(),
    )
    assert result["status"] == "passed"
    assert judge.score == 1 and judge.is_successful()
