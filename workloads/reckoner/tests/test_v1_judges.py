"""Exercise installed DeepEval/Ragas interfaces through offline injected calls."""

import importlib
import json

import pytest
from test_v1_note_generation import Calls, body, sample
from v1_fixtures import config_fixture


def api():
    try:
        return importlib.import_module("reckoner.v1.evaluation.judges")
    except ModuleNotFoundError:
        pytest.fail("framework judge adapters are not implemented")


def test_deepeval_custom_metric_only_exposes_note_to_judge():
    module = api()
    from deepeval.metrics import BaseMetric
    from deepeval.test_case import LLMTestCase

    calls = Calls([body('{"verdict":"approve"}')])
    judge = module.NoteVerdict(calls, config_fixture(), {"approved": True})
    assert isinstance(judge, BaseMetric)
    note, _, _ = sample()
    test = LLMTestCase(
        input="Read the note.",
        actual_output=json.dumps({k: note[k] for k in module.CONTENT_FIELDS}),
        expected_output="decline",
    )
    assert judge.measure(test) == 0
    assert judge.is_successful() is False
    assert calls.stages == ["judge-verdict"]


def test_ragas_runs_both_real_framework_stages():
    module = api()
    note, evidence, _ = sample()
    calls = Calls(
        [
            body('{"statements":["Neighbourhood unavailable."]}'),
            body(
                '{"statements":[{"statement":"Neighbourhood '
                'unavailable.","reason":"Explicit in context.","verdict":1}]}'
            ),
        ]
    )
    judge = module.RagasFaithfulness(calls, config_fixture(), {"approved": True})
    assert judge.evaluate(note, evidence=evidence) == 1
    assert calls.stages == ["judge-statements", "judge-faithfulness"]
    assert judge.provenance["library_version"] == "0.4.3"


@pytest.mark.parametrize("response", ["not-json", '{"statements":[]}'])
def test_ragas_invalid_or_claim_free_output_never_passes_or_retries(response):
    module = api()
    note, evidence, _ = sample()
    calls = Calls([body(response)])
    judge = module.RagasFaithfulness(calls, config_fixture(), {"approved": True})
    with pytest.raises(ValueError):
        judge.evaluate(note, evidence=evidence)
    assert calls.stages == ["judge-statements"]
