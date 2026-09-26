"""Bounded Anthropic provider boundary for the Reckoner baseline."""

from __future__ import annotations

import json
import os
import warnings
from typing import Any

import httpx

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"

import litellm  # noqa: E402
from anthropic import Anthropic  # noqa: E402
from litellm.llms.custom_httpx.http_handler import HTTPHandler  # noqa: E402

from reckoner.baseline.pricing import MAX_OUTPUT_TOKENS, native_model_id, native_request_document

OUTCOMES = {"auto-approve", "auto-decline", "escalate"}
_REQUEST_FIELDS = {"model", "system", "messages", "max_tokens", "temperature"}


class ProviderError(RuntimeError):
    """A safe provider failure category with no remote response details."""


class InvalidResponse(ValueError):
    """A provider response that cannot become a measured decision."""


class _DuplicateKey(ValueError):
    pass


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError


def _valid_count(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def parse_decision(result: dict) -> str:
    """Validate a complete measured response and return its single outcome."""
    if not isinstance(result.get("provider_request_id"), str) or not result["provider_request_id"]:
        raise InvalidResponse("provider request identity is unavailable")
    requested_model = result.get("requested_model")
    reported_model = result.get("reported_model")
    if not isinstance(requested_model, str) or not isinstance(reported_model, str):
        raise InvalidResponse("invalid model identity")
    if native_model_id(requested_model) != reported_model:
        raise InvalidResponse("unexpected reported model")
    if result.get("finish_reason") != "stop":
        raise InvalidResponse("response did not finish normally")
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    ):
        if not _valid_count(result.get(field)):
            raise InvalidResponse("response usage is unavailable")
    if result["cache_read_tokens"] or result["cache_creation_tokens"]:
        raise InvalidResponse("unexpected cache usage")

    content = result.get("content")
    if not isinstance(content, str):
        raise InvalidResponse("invalid response content")
    try:
        document = json.loads(
            content,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        raise InvalidResponse("invalid decision document") from None
    if not isinstance(document, dict) or set(document) != {"outcome"}:
        raise InvalidResponse("invalid decision document")
    outcome = document["outcome"]
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise InvalidResponse("invalid decision outcome")
    return outcome


def _field(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class _NativeUsageHTTPHandler(HTTPHandler):
    """Retain Anthropic usage before LiteLLM coerces it into integer defaults."""

    def __init__(self, *, timeout: float, client: httpx.Client | None = None):
        super().__init__(timeout=timeout, client=client)
        self.native_usage: dict[str, Any] | None = None

    def reset_native_usage(self) -> None:
        self.native_usage = None

    def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        response = super().post(*args, **kwargs)
        try:
            body = response.json()
        except (json.JSONDecodeError, TypeError, ValueError):
            return response
        usage = body.get("usage") if isinstance(body, dict) else None
        self.native_usage = dict(usage) if isinstance(usage, dict) else None
        return response


def _native_usage_field(usage: dict[str, Any] | None, field: str, *, absent: Any = None) -> Any:
    if usage is None:
        return None
    return usage[field] if field in usage else absent


class AnthropicProvider:
    """Count and generate one pinned Anthropic request without retries or fallback."""

    provider_name = "anthropic"
    is_fake = False

    def __init__(self, api_key: str, *, _http_client: httpx.Client | None = None):
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("Anthropic API key is required")
        self._api_key = api_key
        self._anthropic = Anthropic(
            api_key=api_key,
            max_retries=0,
            timeout=60,
            http_client=_http_client,
        )
        self._completion_client = _NativeUsageHTTPHandler(timeout=60, client=_http_client)

    @staticmethod
    def _validate_request(request: dict) -> None:
        if set(request) not in (_REQUEST_FIELDS, _REQUEST_FIELDS | {"output_config"}):
            raise ValueError("invalid provider request fields")
        model = request.get("model")
        if not isinstance(model, str) or not model.startswith("anthropic/"):
            raise ValueError("an explicit Anthropic route is required")
        if request.get("max_tokens") != MAX_OUTPUT_TOKENS:
            raise ValueError("invalid output-token limit")
        if request.get("temperature") != 0:
            raise ValueError("invalid generation temperature")
        native_request_document(request)

    def count_input(self, request: dict) -> int:
        """Ask Anthropic to estimate input tokens for the exact model prompt."""
        self._validate_request(request)
        native = native_request_document(request)
        try:
            count_request = {
                "model": native["model"],
                "system": native["system"],
                "messages": native["messages"],
            }
            if "output_config" in native:
                count_request["output_config"] = native["output_config"]
            response = self._anthropic.messages.count_tokens(**count_request)
        except Exception:
            raise ProviderError("token count failed") from None
        count = _field(response, "input_tokens")
        if not _valid_count(count):
            raise ProviderError("token count failed")
        return count

    def generate(self, request: dict) -> dict[str, Any]:
        """Make exactly one non-streaming LiteLLM generation request."""
        self._validate_request(request)
        arguments = {
            **request,
            "api_key": self._api_key,
            "stream": False,
            "num_retries": 0,
            "max_retries": 0,
            "timeout": 60,
        }
        self._completion_client.reset_native_usage()
        arguments["client"] = self._completion_client
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="Item 'summary' on TypedDict class 'ChatCompletionReasoningItem'.*",
                    category=UserWarning,
                    module=r"pydantic\._internal\._generate_schema",
                )
                response = litellm.completion(**arguments)
        except Exception:
            raise ProviderError("generation failed") from None

        choices = _field(response, "choices", [])
        choice = choices[0] if isinstance(choices, (list, tuple)) and choices else None
        message = _field(choice, "message")
        native_usage = self._completion_client.native_usage
        hidden = _field(response, "_hidden_params", {})
        headers = _field(hidden, "additional_headers", {})
        provider_request_id = _field(headers, "llm_provider-request-id") or _field(
            headers, "llm_provider-x-request-id"
        )
        return {
            "provider_request_id": provider_request_id,
            "requested_model": request["model"],
            "reported_model": _field(response, "model"),
            "finish_reason": _field(choice, "finish_reason"),
            "content": _field(message, "content"),
            "input_tokens": _native_usage_field(native_usage, "input_tokens"),
            "output_tokens": _native_usage_field(native_usage, "output_tokens"),
            "cache_read_tokens": _native_usage_field(
                native_usage, "cache_read_input_tokens", absent=0
            ),
            "cache_creation_tokens": _native_usage_field(
                native_usage, "cache_creation_input_tokens", absent=0
            ),
        }
