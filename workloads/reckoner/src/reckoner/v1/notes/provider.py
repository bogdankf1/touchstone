"""LiteLLM transport for pinned note and judge requests; no fallback or retries."""

from reckoner.baseline.pricing import native_request_document
from reckoner.baseline.provider import AnthropicProvider
from reckoner.v1.notes.prompt import MODEL


class NoteProvider(AnthropicProvider):
    def __init__(self, api_key, *, _http_client=None):
        super().__init__(api_key, _http_client=_http_client, timeout_seconds=30)

    @staticmethod
    def _validate_request(request):
        if set(request) != {"model", "system", "messages", "max_tokens", "temperature"}:
            raise ValueError("invalid generation request")
        if request["model"] != MODEL or request["temperature"] != 0:
            raise ValueError("unpinned generation model/settings")
        if type(request["max_tokens"]) is not int or not 1 <= request["max_tokens"] <= 4096:
            raise ValueError("invalid generation output bound")
        native_request_document(request)
