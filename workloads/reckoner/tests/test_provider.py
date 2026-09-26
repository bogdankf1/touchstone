import copy
import json

import httpx
import pytest
from reckoner.baseline.pricing import observed_cost
from reckoner.baseline.provider import (
    AnthropicProvider,
    InvalidResponse,
    ProviderError,
    parse_decision,
)


def request():
    return {
        "model": "anthropic/claude-haiku-4-5-20251001",
        "system": "Classify this simulated purchase.",
        "messages": [{"role": "user", "content": '{"amount_minor":1000}'}],
        "max_tokens": 256,
        "temperature": 0,
    }


NATIVE_OUTPUT_CONFIG = {
    "format": {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": ["auto-approve", "auto-decline", "escalate"]}
            },
            "required": ["outcome"],
            "additionalProperties": False,
        },
    }
}


def native_request():
    return {**request(), "output_config": NATIVE_OUTPUT_CONFIG}


def result(**changes):
    value = {
        "provider_request_id": "req_fake_123",
        "requested_model": "anthropic/claude-haiku-4-5-20251001",
        "reported_model": "claude-haiku-4-5-20251001",
        "finish_reason": "stop",
        "content": '{"outcome":"escalate"}',
        "input_tokens": 31,
        "output_tokens": 7,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    value.update(changes)
    return value


def anthropic_response():
    return {
        "id": "msg_fake_123",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": '{"outcome":"escalate"}'}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 31, "output_tokens": 7},
    }


def test_count_input_uses_the_native_model_and_exact_prompt_payload_once():
    seen = []

    def handler(http_request):
        seen.append(http_request)
        return httpx.Response(200, json={"input_tokens": 123})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider("sk-ant-fake-test-key", _http_client=client)
        assert provider.count_input(request()) == 123

    assert len(seen) == 1
    assert seen[0].url.path == "/v1/messages/count_tokens"
    assert json.loads(seen[0].content) == {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": '{"amount_minor":1000}'}],
            }
        ],
        "model": "claude-haiku-4-5-20251001",
        "system": request()["system"],
    }


def test_count_failure_blocks_without_a_generation_request_or_retry():
    paths = []

    def handler(http_request):
        paths.append(http_request.url.path)
        return httpx.Response(
            429,
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "wait"}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider("sk-ant-fake-test-key", _http_client=client)
        with pytest.raises(ProviderError, match="token count failed"):
            provider.count_input(request())

    assert paths == ["/v1/messages/count_tokens"]


def test_native_count_sends_schema_once_without_tools_or_cache():
    seen = []

    def handler(http_request):
        seen.append(http_request)
        return httpx.Response(200, json={"input_tokens": 150})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert (
            AnthropicProvider("sk-ant-fake-test-key", _http_client=client).count_input(
                native_request()
            )
            == 150
        )

    assert len(seen) == 1
    assert seen[0].url.path == "/v1/messages/count_tokens"
    assert json.loads(seen[0].content) == {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": '{"amount_minor":1000}'}]}
        ],
        "model": "claude-haiku-4-5-20251001",
        "system": request()["system"],
        "output_config": NATIVE_OUTPUT_CONFIG,
    }


def test_native_count_schema_rejection_fails_closed_without_generation():
    seen = []

    def handler(http_request):
        seen.append(http_request.url.path)
        return httpx.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "unsupported"},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="token count failed"):
            AnthropicProvider("sk-ant-fake-test-key", _http_client=client).count_input(
                native_request()
            )

    assert seen == ["/v1/messages/count_tokens"]


def test_native_generation_sends_exact_schema_once_without_tools_or_cache():
    seen = []

    def handler(http_request):
        seen.append(http_request)
        return httpx.Response(
            200, json=anthropic_response(), headers={"request-id": "req_fake_123"}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        generated = AnthropicProvider("sk-ant-fake-test-key", _http_client=client).generate(
            native_request()
        )

    assert parse_decision(generated) == "escalate"
    assert len(seen) == 1
    assert seen[0].url.path == "/v1/messages"
    assert json.loads(seen[0].content) == {
        "max_tokens": 256,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": '{"amount_minor":1000}'}]}
        ],
        "model": "claude-haiku-4-5-20251001",
        "system": request()["system"],
        "temperature": 0,
        "output_config": NATIVE_OUTPUT_CONFIG,
    }


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value["format"].update(type="json_object"),
        lambda value: value["format"]["schema"]["properties"]["outcome"].update(
            enum=["auto-approve", "auto-decline"]
        ),
        lambda value: value["format"]["schema"].update(additionalProperties=True),
        lambda value: value.update(unexpected=True),
    ],
)
def test_native_generation_rejects_changed_schema_before_dispatch(change):
    seen = []

    def handler(http_request):
        seen.append(http_request)
        return httpx.Response(200, json=anthropic_response())

    altered = native_request()
    altered["output_config"] = copy.deepcopy(NATIVE_OUTPUT_CONFIG)
    change(altered["output_config"])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="output config"):
            AnthropicProvider("sk-ant-fake-test-key", _http_client=client).generate(altered)
    assert seen == []


def test_generate_uses_one_native_anthropic_request_with_retries_and_extras_disabled():
    seen = []

    def handler(http_request):
        seen.append(http_request)
        return httpx.Response(
            200,
            json=anthropic_response(),
            headers={"request-id": "req_fake_123"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider("sk-ant-fake-test-key", _http_client=client)
        generated = provider.generate(request())

    assert generated == result()
    assert len(seen) == 1
    assert seen[0].url.path == "/v1/messages"
    body = json.loads(seen[0].content)
    assert body == {
        "max_tokens": 256,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": '{"amount_minor":1000}'}],
            }
        ],
        "model": "claude-haiku-4-5-20251001",
        "system": request()["system"],
        "temperature": 0,
    }


@pytest.mark.parametrize(
    ("native_field", "invalid_value", "result_field"),
    [
        ("input_tokens", None, "input_tokens"),
        ("input_tokens", True, "input_tokens"),
        ("input_tokens", 31.9, "input_tokens"),
        ("output_tokens", None, "output_tokens"),
        ("output_tokens", False, "output_tokens"),
        ("output_tokens", 7.5, "output_tokens"),
        ("cache_read_input_tokens", None, "cache_read_tokens"),
        ("cache_read_input_tokens", True, "cache_read_tokens"),
        ("cache_creation_input_tokens", None, "cache_creation_tokens"),
        ("cache_creation_input_tokens", False, "cache_creation_tokens"),
    ],
)
def test_generate_preserves_invalid_native_usage_for_local_rejection(
    native_field, invalid_value, result_field
):
    response = anthropic_response()
    response["usage"][native_field] = invalid_value

    def handler(_http_request):
        return httpx.Response(
            200,
            json=response,
            headers={"request-id": "req_fake_123"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        generated = AnthropicProvider("sk-ant-fake-test-key", _http_client=client).generate(
            request()
        )

    assert generated[result_field] == invalid_value
    assert type(generated[result_field]) is type(invalid_value)
    with pytest.raises(InvalidResponse, match="usage"):
        parse_decision(generated)
    assert (
        observed_cost(generated, {"input_per_million": "1.00", "output_per_million": "5.00"})
        is None
    )


@pytest.mark.parametrize(
    ("missing_field", "result_field"),
    [("input_tokens", "input_tokens"), ("output_tokens", "output_tokens")],
)
def test_generate_preserves_missing_required_native_usage_for_local_rejection(
    missing_field, result_field
):
    response = anthropic_response()
    response["usage"].pop(missing_field)

    def handler(_http_request):
        return httpx.Response(
            200,
            json=response,
            headers={"request-id": "req_fake_123"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        generated = AnthropicProvider("sk-ant-fake-test-key", _http_client=client).generate(
            request()
        )

    assert generated[result_field] is None
    with pytest.raises(InvalidResponse, match="usage"):
        parse_decision(generated)
    assert (
        observed_cost(generated, {"input_per_million": "1.00", "output_per_million": "5.00"})
        is None
    )


@pytest.mark.parametrize("status", [429, 500])
def test_generate_does_not_retry_or_switch_models_after_http_failure(status):
    seen = []

    def handler(http_request):
        seen.append(http_request)
        error_type = "rate_limit_error" if status == 429 else "api_error"
        return httpx.Response(
            status,
            json={"type": "error", "error": {"type": error_type, "message": "failed"}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider("sk-ant-fake-test-key", _http_client=client)
        with pytest.raises(ProviderError, match="generation failed"):
            provider.generate(request())

    assert len(seen) == 1
    assert json.loads(seen[0].content)["model"] == "claude-haiku-4-5-20251001"


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.ConnectError("offline")])
def test_generate_does_not_retry_after_transport_failure(failure):
    seen = []

    def handler(http_request):
        seen.append(http_request)
        raise failure

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider("sk-ant-fake-test-key", _http_client=client)
        with pytest.raises(ProviderError, match="generation failed"):
            provider.generate(request())

    assert len(seen) == 1


def test_parse_decision_accepts_only_the_exact_outcome_document():
    assert parse_decision(result()) == "escalate"


@pytest.mark.parametrize(
    ("changes", "case"),
    [
        ({"content": "not JSON"}, "malformed JSON"),
        ({"content": '```json\n{"outcome":"escalate"}\n```'}, "markdown fences"),
        (
            {"content": '{"outcome":"escalate","rationale":"extra"}'},
            "extra response keys",
        ),
        (
            {"content": '{"outcome":"escalate","outcome":"auto-approve"}'},
            "duplicate JSON keys",
        ),
        ({"content": '{"outcome":1}'}, "wrong outcome type"),
        ({"content": '{"outcome":"review"}'}, "unknown outcome"),
        ({"finish_reason": "length"}, "non-stop finish"),
        ({"input_tokens": None}, "missing usage"),
        ({"reported_model": "claude-haiku-latest"}, "model mismatch"),
        ({"cache_read_tokens": 1}, "unexpected cache usage"),
    ],
)
def test_parse_decision_rejects_ambiguous_or_unmeasurable_responses(changes, case):
    with pytest.raises(InvalidResponse):
        parse_decision(result(**changes))


def test_invalid_response_error_never_echoes_provider_content():
    secret = "sk-ant-never-log-this"

    with pytest.raises(InvalidResponse) as raised:
        parse_decision(result(content=secret))

    assert secret not in str(raised.value)
