"""Validate generic OTLP measurement and declaration documents."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from touchstone_platform.resources import SCHEMAS


@dataclass(frozen=True)
class EventIdentity:
    tenant_id: str
    workflow_id: str
    run_id: str
    event_id: str


@dataclass(frozen=True)
class ValidatedEvent:
    identity: EventIdentity
    task_id: str
    trace_id: str
    span_id: str
    received_at: str
    content_sha256: str
    document: dict[str, Any]

    @property
    def tenant_id(self) -> str:
        return self.identity.tenant_id

    @property
    def workflow_id(self) -> str:
        return self.identity.workflow_id

    @property
    def run_id(self) -> str:
        return self.identity.run_id


@lru_cache(maxsize=2)
def _validator(version: str) -> Draft202012Validator:
    schema = json.loads(SCHEMAS.joinpath(f"{version}.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate(document: dict[str, Any], version: str) -> None:
    _validator(version).validate(document)


def canonical_sha256(document: dict[str, Any]) -> str:
    body = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def validate_event(document: dict, received_at: str) -> ValidatedEvent:
    _validate(document, "measurement-v1")
    if not FormatChecker().conforms(received_at, "date-time"):
        raise ValidationError("received_at must be a date-time")
    identity = EventIdentity(
        document["tenant_id"], document["workflow_id"], document["run_id"], document["event_id"]
    )
    return ValidatedEvent(
        identity=identity,
        task_id=document["task_id"],
        trace_id=document["trace_id"],
        span_id=document["span_id"],
        received_at=received_at,
        content_sha256=canonical_sha256(document),
        document=copy.deepcopy(document),
    )


def validate_event_context(
    event: ValidatedEvent,
    span_attributes: Mapping[str, Any],
    trace_id: str,
    span_id: str,
) -> None:
    """Check envelope identity against the enclosing OTLP span at extraction time."""
    for field in ("tenant_id", "workflow_id", "workflow_version", "run_id", "task_id"):
        if span_attributes.get(f"touchstone.{field}") != event.document[field]:
            raise ValueError(f"measurement {field} disagrees with enclosing span")
    if span_attributes.get("touchstone.simulated") is not event.document["simulated"]:
        raise ValueError("measurement simulated disagrees with enclosing span")
    if trace_id != event.trace_id:
        raise ValueError("measurement trace_id disagrees with enclosing span")
    if span_id != event.span_id:
        raise ValueError("measurement span_id disagrees with enclosing span")


def validate_declaration(document: dict) -> dict:
    _validate(document, "run-declaration-v1")
    if document["expected_task_count"] != len(document["expected_task_ids"]):
        raise ValidationError("expected_task_count must match distinct expected_task_ids")
    suite_ids = [suite["suite_id"] for suite in document["evaluation_suites"]]
    if len(suite_ids) != len(set(suite_ids)):
        raise ValidationError("evaluation suite IDs must be distinct")
    return copy.deepcopy(document)


def declarations_conflict(existing: dict, new: dict) -> bool:
    """Detect changed declaration content for one tenant/workflow/run identity."""
    old = validate_declaration(existing)
    current = validate_declaration(new)
    identity_keys = ("tenant_id", "workflow_id", "run_id")
    if any(old[key] != current[key] for key in identity_keys):
        return False
    return canonical_sha256(old) != canonical_sha256(current)
