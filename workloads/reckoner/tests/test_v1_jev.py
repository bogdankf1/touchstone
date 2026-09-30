"""Fabricated HTTP boundary tests; no provider credential or network inference."""

import importlib
import json
from copy import deepcopy

import httpx
import pytest
from reckoner.v1.contracts import validate_v1
from v1_fixtures import evidence_fixture, score_fixture


def provider():
    try:
        return importlib.import_module("reckoner.v1.providers.jev")
    except ModuleNotFoundError:
        pytest.fail("Jev provider is not implemented")


def response():
    return {
        "model": "jev-1.13.0",
        "answers": {
            "risk": {
                "type": "choice",
                "choice": "legitimate",
                "probabilities": {"fraud": 0.2, "legitimate": 0.8},
                "confidence": 0.8,
            }
        },
        "usage": {"input_tokens": 2000, "output_tokens": 0},
    }


def test_single_http_attempt_auth_binary_request_and_explicit_timeouts():
    module = provider()

    def handle(request):
        assert request.url == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["authorization"] == "Bearer fabricated-only"
        assert request.extensions["timeout"]["connect"] == 5
        assert request.extensions["timeout"]["read"] == 30
        body = json.loads(request.content)
        assert body["model"] == "jev-1.13.0"
        assert set(body["questions"]["risk"]["criteria"]) == {"fraud", "legitimate"}
        return httpx.Response(200, json=response())

    client = module.JevClient("fabricated-only", transport=httpx.MockTransport(handle))
    request = module.build_request(
        {
            "tenant_id": "a",
            "transaction_id": "opaque|id",
            "amount_minor": 2000,
            "currency": "USD",
            "occurred_at": "2019-01-08T00:00:00Z",
            "oracle": "fraud",
            "card_number": "secret",
            "source_extra": "bad",
        },
        {**evidence_fixture(), "oracle": "fraud", "unknown": "bad"},
    )
    assert all(value not in json.dumps(request) for value in ("oracle", "secret", "source_extra"))
    assert module.validate_response(client.evaluate(request))["distribution"] == {
        "fraud": "0.2",
        "legitimate": "0.8",
    }


@pytest.mark.parametrize(
    "change",
    [
        {"fraud": True, "legitimate": 0.8},
        {"fraud": float("nan"), "legitimate": 0.8},
        {"fraud": -0.1, "legitimate": 1.1},
        {"fraud": 0.2, "legitimate": 0.7},
        {"fraud": 0.2, "legitimate": 0.8, "other": 0},
    ],
)
def test_invalid_distributions_never_become_scores(change):
    module = provider()
    body = response()
    body["answers"]["risk"]["probabilities"] = change
    with pytest.raises(module.JevError, match="invalid_response"):
        module.validate_response(body)


@pytest.mark.parametrize("field,value", [("model", "jev-latest"), ("usage", None)])
def test_unexpected_model_rejected_and_usage_absence_remains_unknown(field, value):
    module = provider()
    body = response()
    body[field] = value
    if field == "model":
        with pytest.raises(module.JevError):
            module.validate_response(body)
    else:
        assert module.validate_response(body)["usage"] is None


@pytest.mark.parametrize(
    "status,category", [(401, "auth"), (422, "request"), (429, "transient"), (529, "transient")]
)
def test_error_response_is_categorized_without_body_in_error_text(status, category):
    module = provider()
    client = module.JevClient(
        "fabricated-only",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="private body", headers={"Retry-After": "7"})
        ),
    )
    with pytest.raises(module.JevError) as error:
        client.evaluate({"model": "jev-1.13.0"})
    assert error.value.category == category
    assert error.value.retry_after == 7
    assert "private body" not in str(error.value)
    assert error.value.body == "private body"


@pytest.mark.parametrize(
    "exception,category", [(httpx.ConnectError, "connect"), (httpx.ReadTimeout, "uncertain")]
)
def test_transport_error_delivery_is_explicit(exception, category):
    module = provider()
    calls = []

    def handle(request):
        calls.append(request)
        raise exception("sensitive details")

    client = module.JevClient("fabricated-only", transport=httpx.MockTransport(handle))
    with pytest.raises(module.JevError) as error:
        client.evaluate({})
    assert error.value.category == category
    assert len(calls) == 1
    assert "sensitive" not in str(error.value)


def test_html_success_body_is_a_recorded_invalid_response():
    module = provider()
    client = module.JevClient(
        "fabricated-only",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="<html>private</html>")),
    )
    with pytest.raises(module.JevError, match="invalid_response"):
        client.evaluate({})


@pytest.mark.parametrize(
    "legitimate,accepted",
    [
        ("0.800001", True),
        ("0.799999", True),
        ("0.80000100000000000000000000001", False),
        ("0.79999899999999999999999999999", False),
    ],
)
def test_shared_score_tolerance_preserves_raw_decimals(legitimate, accepted):
    record = score_fixture(legitimate=legitimate)
    if accepted:
        assert validate_v1("score", record)["distribution"]["legitimate"] == legitimate
    else:
        with pytest.raises(ValueError):
            validate_v1("score", record)


def test_selected_choice_and_confidence_are_validated():
    module = provider()
    for key, value in [("choice", "fraud"), ("confidence", True), ("confidence", 1.1)]:
        body = deepcopy(response())
        body["answers"]["risk"][key] = value
        with pytest.raises(module.JevError):
            module.validate_response(body)


def test_neighbourhood_references_preserve_tenant_with_opaque_identical_ids():
    from reckoner.v1.evidence.assemble import neighbourhood

    rows = [
        {
            "transaction": {
                "tenant_id": tenant,
                "transaction_id": "same|id",
                "account_id": "a",
                "card_id": "c",
                "merchant_id": "m",
            }
        }
        for tenant in ("a|b", "a")
    ]
    display = neighbourhood(rows)
    assert len(set(display["transaction_refs"])) == 2
    assert all(len(ref) == 64 for ref in display["transaction_refs"])


def test_http_decimal_precision_does_not_round_outside_tolerance_into_a_score():
    module = provider()
    text = (
        '{"model":"jev-1.13.0","answers":{"risk":{"type":"choice",'
        '"choice":"legitimate","confidence":0.8,"probabilities":{"fraud":0.2,'
        '"legitimate":0.80000100000000000000000000001}}},'
        '"usage":{"input_tokens":1,"output_tokens":0}}'
    )
    client = module.JevClient(
        "fabricated-only", transport=httpx.MockTransport(lambda _: httpx.Response(200, text=text))
    )
    with pytest.raises(module.JevError):
        module.validate_response(client.evaluate({}))
