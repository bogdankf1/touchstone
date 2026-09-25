"""Resumable sequential baseline execution."""

from __future__ import annotations

import hashlib
import secrets
import subprocess
import time
from datetime import UTC, datetime
from decimal import Decimal

from reckoner.baseline.pricing import (
    native_request_bytes,
    reservation_cost,
    reservation_input_bound,
)
from reckoner.baseline.prompt import build_request
from reckoner.storage.budget import BudgetExceeded, BudgetLedger


def _code_revision() -> str:
    """Return the immutable source revision recorded for a measured run."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("code revision is unavailable") from error
    revision = result.stdout.strip()
    if not revision:
        raise ValueError("code revision is unavailable")
    return revision


def _validate_provider(context: dict, provider: object) -> None:
    is_fake = getattr(provider, "is_fake", None)
    name = getattr(provider, "provider_name", None)
    if context["execution_mode"] == "test":
        if is_fake is not True or name != "fake":
            raise ValueError("test runs require an explicitly fake provider")
    else:
        if is_fake is True:
            raise ValueError("fake provider requires explicit test mode")
        if name != context["config"]["provider"]:
            raise ValueError("provider does not match run configuration")


def _counts(repo, run_id: str) -> dict[str, int]:
    snapshot = repo.snapshot(run_id, None)
    return {key: int(snapshot[key]) for key in ("completed", "failed", "uncertain", "pending")}


def preflight(repo, provider, run_id: str) -> dict:
    """Count and freeze every pending request without generating content."""
    context = repo.run_context(run_id)
    _validate_provider(context, provider)
    if (
        context["execution_mode"] == "paid"
        and context["purpose"] == "baseline"
        and not repo.pilot_gate_satisfied(run_id)
    ):
        raise ValueError("baseline requires a successful 20-case pilot")
    revision = _code_revision()
    if context["preflight_code_revision"] not in {None, revision}:
        raise ValueError("preflight code revision changed")

    plans = []
    for task in repo.pending_tasks(run_id):
        request = build_request(
            task["transaction"],
            context["config"],
            context["thresholds"][task["tenant_id"]],
        )
        request_sha256 = hashlib.sha256(native_request_bytes(request)).hexdigest()
        if task["request"] is not None:
            if task["request"] != request or task["request_sha256"] != request_sha256:
                raise ValueError("immutable preflight request identity mismatch")
            plans.append(
                {
                    "tenant_id": task["tenant_id"],
                    "task_id": task["task_id"],
                    "request": request,
                    "request_sha256": request_sha256,
                    "input_token_estimate": task["input_token_estimate"],
                    "reservation_input_tokens": task["reservation_input_tokens"],
                    "reservation_cost": Decimal(task["reservation_cost"]),
                    "trace_id": task["trace_id"],
                    "span_id": task["span_id"],
                    "event_id": task["event_id"],
                }
            )
            continue
        estimate = provider.count_input(request)
        bound = reservation_input_bound(request, estimate)
        maximum = reservation_cost(request, estimate, context["price"])
        plans.append(
            {
                "tenant_id": task["tenant_id"],
                "task_id": task["task_id"],
                "request": request,
                "request_sha256": request_sha256,
                "input_token_estimate": estimate,
                "reservation_input_tokens": bound,
                "reservation_cost": maximum,
                "trace_id": secrets.token_hex(16),
                "span_id": secrets.token_hex(8),
                "event_id": secrets.token_hex(16),
            }
        )

    total = sum((plan["reservation_cost"] for plan in plans), Decimal(0))
    ledger = BudgetLedger(repo)
    if total > ledger.remaining():
        repo.set_run_status(run_id, "blocked")
        raise BudgetExceeded("remaining global funds do not cover preflight")
    if context["purpose"] == "pilot" and total > ledger.pilot_remaining():
        repo.set_run_status(run_id, "blocked")
        raise BudgetExceeded("remaining pilot funds do not cover preflight")
    repo.persist_preflight(run_id, revision, plans)
    return {
        "pending": len(plans),
        "reservation_total": format(total, "f"),
        "tasks": [
            {
                "tenant_id": plan["tenant_id"],
                "task_id": plan["task_id"],
                "request_sha256": plan["request_sha256"],
                "input_token_estimate": plan["input_token_estimate"],
                "reservation_cost": format(plan["reservation_cost"], "f"),
            }
            for plan in plans
        ],
    }


def execute_run(repo, provider, run_id: str) -> dict:
    """Dispatch each preflighted task once and return explicit terminal counts."""
    with repo.exclusive_runner():
        context = repo.run_context(run_id)
        _validate_provider(context, provider)
        revision = _code_revision()
        if context["preflight_code_revision"] is None:
            raise ValueError("run has no validated preflight")
        if context["preflight_code_revision"] != revision:
            raise ValueError("preflight code revision changed")

        if repo.reconcile_dispatched(run_id):
            return _counts(repo, run_id)
        state = _counts(repo, run_id)
        if state["uncertain"]:
            return state

        tasks = repo.pending_tasks(run_id)
        for task in tasks:
            if task["request"] is None or task["reservation_cost"] is None:
                raise ValueError("run has no validated preflight")
            request_sha256 = hashlib.sha256(native_request_bytes(task["request"])).hexdigest()
            if request_sha256 != task["request_sha256"]:
                raise ValueError("immutable preflight request identity mismatch")
        total = sum((Decimal(task["reservation_cost"]) for task in tasks), Decimal(0))
        ledger = BudgetLedger(repo)
        if total > ledger.remaining():
            repo.set_run_status(run_id, "blocked")
            return _counts(repo, run_id)
        if context["purpose"] == "pilot" and total > ledger.pilot_remaining():
            repo.set_run_status(run_id, "blocked")
            return _counts(repo, run_id)

        repo.set_run_status(run_id, "running")

        for task in tasks:
            try:
                reservation = ledger.reserve(task, Decimal(task["reservation_cost"]))
            except BudgetExceeded:
                repo.set_run_status(run_id, "blocked")
                return _counts(repo, run_id)
            started_at = datetime.now(UTC)
            started_clock = time.monotonic()
            try:
                result = provider.generate(task["request"])
                error_category = None
            except Exception:
                result = None
                error_category = "provider_charge_unknown"
            timing = {
                "started_at": started_at.isoformat(),
                "ended_at": datetime.now(UTC).isoformat(),
                "duration_ms": (time.monotonic() - started_clock) * 1000,
            }
            try:
                repo.finish_attempt(reservation, result, error_category, timing)
            except Exception:
                repo.mark_attempt_uncertain(reservation["call_id"], "telemetry_persistence_failed")
                break
            if result is None:
                break
            if _counts(repo, run_id)["uncertain"]:
                break
        repo.refresh_run_status(run_id)
        return _counts(repo, run_id)
