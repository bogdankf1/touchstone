"""One direct HTTP attempt; protected callers own persistence and retries."""

import json
import math
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from email.utils import parsedate_to_datetime

import httpx

from reckoner.resources import PROMPTS
from reckoner.v1 import projection
from reckoner.v1.contracts import validate_v1

MODEL = "jev-1.13.0"


class JevError(RuntimeError):
    def __init__(self, category, *, body=None, retry_after=None):
        super().__init__(category)
        self.category = category
        self.body = body
        self.retry_after = retry_after


def build_request(transaction: dict, evidence: dict) -> dict:
    """Project explicit canonical fields; never serialize an unrestricted source row."""
    transaction_keys = (
        "tenant_id",
        "transaction_id",
        "occurred_at",
        "amount_minor",
        "currency",
        "payment_channel",
        "merchant_category_code",
        "processing_errors",
    )
    evidence_keys = (
        "evidence_id",
        "query_time",
        "source_snapshot_ids",
        "cutoffs",
        "coverage",
        "features",
        "risk_indicators",
        "comparable_cases",
        "graph_projection",
    )
    # In score_task, evidence first passes the strict shared contract. This projection
    # also makes direct construction exclude arbitrary top-level input keys.
    validated = validate_v1(
        "evidence",
        {
            key: evidence[key]
            for key in (
                *evidence_keys,
                "schema_version",
                "tenant_id",
                "transaction_id",
                "neighbourhood",
            )
            if key in evidence
        },
    )
    summary = {key: validated[key] for key in evidence_keys if key in validated}
    # Only the request is bounded (count, hash and ordered exemplars); evidence is unchanged.
    # The projection version is recorded with each call and protocol, not sent: the Jev
    # Choice contract documented here defines no further state keys.
    summary["risk_indicators"] = projection.indicators(validated)
    summary["comparable_cases"] = projection.comparables(validated)
    return {
        "model": MODEL,
        "state": {
            "dataset_simulated": True,
            "transaction": {
                key: transaction[key] for key in transaction_keys if key in transaction
            },
            "evidence": summary,
        },
        "questions": {"risk": json.loads((PROMPTS / "jev-choice-v1.json").read_text())["question"]},
    }


def probability(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid probability")
    number = Decimal(str(value))
    if not number.is_finite() or not 0 <= number <= 1:
        raise ValueError("invalid probability")
    return format(number, "f")


def valid_usage(usage):
    if usage is None:
        return None
    if not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens"}:
        raise ValueError("invalid usage")
    if any(type(value) is not int or value < 0 for value in usage.values()):
        raise ValueError("invalid usage")
    return usage


def validate_response(body: dict) -> dict:
    try:
        if body["model"] != MODEL or set(body["answers"]) != {"risk"}:
            raise ValueError("invalid model or question")
        answer = body["answers"]["risk"]
        distribution = answer["probabilities"]
        if answer["type"] != "choice" or set(distribution) != {"fraud", "legitimate"}:
            raise ValueError("invalid alternatives")
        distribution = {key: probability(value) for key, value in distribution.items()}
        with localcontext() as context:
            context.prec = max(map(len, distribution.values())) + 10
            if abs(sum(map(Decimal, distribution.values())) - 1) > Decimal("0.000001"):
                raise ValueError("invalid probability sum")
        if answer["choice"] not in distribution or Decimal(distribution[answer["choice"]]) != max(
            map(Decimal, distribution.values())
        ):
            raise ValueError("inconsistent choice")
        return {
            "distribution": distribution,
            "confidence": probability(answer["confidence"]),
            "model": body["model"],
            "usage": valid_usage(body.get("usage")),
        }
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise JevError("invalid_response", body=body) from exc


def _invalid_constant(value):
    raise ValueError("nonfinite JSON value")


def retry_hint(value):
    if value is None:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


class JevClient:
    def __init__(self, api_key: str, *, transport=None):
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("JEV_API_KEY is required")
        self._api_key = api_key
        self._transport = transport

    def evaluate(self, request: dict) -> dict:
        try:
            with httpx.Client(
                transport=self._transport or httpx.HTTPTransport(retries=0),
                timeout=httpx.Timeout(30, connect=5),
                follow_redirects=False,
            ) as client:
                response = client.post(
                    "https://api.typesafe.ai/v1/systemone",
                    json=request,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise JevError("connect") from exc
        except httpx.TransportError as exc:
            # Read/write failure may follow delivery; do not issue another billed attempt.
            raise JevError("uncertain") from exc
        if response.status_code != 200:
            category = {401: "auth", 422: "request", 429: "transient", 529: "transient"}.get(
                response.status_code, "failed"
            )
            raise JevError(
                category,
                body=response.text,
                retry_after=retry_hint(response.headers.get("Retry-After")),
            )
        try:
            return response.json(parse_float=str, parse_constant=_invalid_constant)
        except ValueError as exc:
            raise JevError("invalid_response", body=response.text) from exc
