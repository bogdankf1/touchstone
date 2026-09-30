"""Crash-safe scoring: one dispatch layer, persisted gates, protected responses."""

import json
import random
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from psycopg.types.json import Jsonb

from reckoner.contracts import content_id
from reckoner.resources import PROMPTS
from reckoner.storage.budget import ACCOUNTING_LOCK
from reckoner.v1.contracts import validate_v1
from reckoner.v1.providers.jev import JevError, build_request, valid_usage, validate_response
from reckoner.v1.storage.budget import ProviderBudget, validate_protocol


def _configuration(repo, task):
    row = repo._connection.execute(
        "SELECT c.document FROM reckoner.v1_configs c "
        "JOIN reckoner.v1_runs r ON (r.tenant_id,r.config_id)=(c.tenant_id,c.config_id) "
        "WHERE r.tenant_id=%s AND r.run_id=%s AND c.config_id=%s",
        (task["tenant_id"], task["run_id"], task["config_id"]),
    ).fetchone()
    if row is None:
        raise ValueError("pinned scorer configuration is missing")
    return validate_v1("run-config", row["document"])


def _preflight(repo, task, evidence, protocol):
    validate_protocol(protocol)
    evidence = validate_v1("evidence", evidence)
    if (evidence["tenant_id"], evidence["transaction_id"]) != (
        task["tenant_id"],
        task["transaction_id"],
    ):
        raise ValueError("evidence does not match task")
    if evidence["query_time"] != task["transaction"]["occurred_at"]:
        # Equivalent UTC forms are common (Z/+00:00).
        if datetime.fromisoformat(evidence["query_time"]) != datetime.fromisoformat(
            task["transaction"]["occurred_at"]
        ):
            raise ValueError("evidence query time does not match task")
    config = _configuration(repo, task)
    scorer = config["scorer"]
    question = json.loads((PROMPTS / "jev-choice-v1.json").read_text())
    if scorer["question_version"] != question["question_version"]:
        raise ValueError("unknown scorer question version")
    for key in ("input_token_ceiling", "max_output_tokens", "maximum_attempts"):
        if protocol[key] > config["limits"][key]:
            raise ValueError("protocol exceeds pinned configuration")
    if scorer["provider"] != protocol["provider"] or scorer["model"] != protocol["model"]:
        raise ValueError("protocol scorer does not match pinned configuration")
    if config["limits"]["timeout_seconds"] != 30:
        raise ValueError("scorer response timeout must be pinned to 30 seconds")
    request = build_request(task["transaction"], evidence)
    digest = content_id(request)
    # UTF-8 byte count is a conservative upper bound for tokenizer input, without
    # assuming an unavailable provider tokenizer. Includes complete question/state.
    size = len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode())
    if size > protocol["input_token_ceiling"]:
        raise ValueError("request exceeds conservative input bound")
    case = {
        "task_id": task["task_id"],
        "transaction_id": task["transaction_id"],
        "request_sha256": digest,
    }
    if (
        case not in protocol["tasks"]
        or protocol["tenant_id"] != task["tenant_id"]
        or protocol["run_id"] != task["run_id"]
    ):
        raise ValueError("exact request/case is not in protocol")
    price = scorer["price_table"]
    if (
        price["model"] != "jev-1.13.0"
        or price["currency"] != "USD"
        or Decimal(price["input_per_million"]) != Decimal(".042")
        or Decimal(price["output_per_million"]) != 0
    ):
        raise ValueError("unknown scorer pricing prevents dispatch")
    return config, request, digest


def _score(call, config, digest, category, parsed, usage, cost):
    return validate_v1(
        "score",
        {
            "schema_version": "reckoner-score-v1",
            **{k: call[k] for k in ("tenant_id", "run_id", "task_id", "call_id")},
            "raw_probability": parsed["distribution"]["fraud"] if parsed else None,
            "distribution": parsed["distribution"] if parsed else None,
            "confidence": parsed["confidence"] if parsed else None,
            "requested_model": config["scorer"]["model"],
            "reported_model": parsed["model"] if parsed else None,
            "question_version": config["scorer"]["question_version"],
            "input_sha256": digest,
            "usage": usage or {"input_tokens": None, "output_tokens": None},
            "cost": {
                "status": "settled" if cost is not None else "uncertain",
                "amount": format(cost, "f") if cost is not None else None,
                "currency": "USD",
                "price_table_id": config["scorer"]["price_table"]["price_table_version"],
            },
            "attempt_status": "responded"
            if parsed
            else ("uncertain" if category == "uncertain" else "failed"),
            "adjusted_probability": None,
            "calibration_id": None,
        },
    )


def _finish(repo, ledger, call, score, category, body, usage, cost, retry_after):
    with repo._connection.transaction():
        repo._connection.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
        ledger.settle(call["call_id"], usage, cost)
        repo._connection.execute(
            "INSERT INTO reckoner.v1_provider_responses "
            "(tenant_id,call_id,category,body,score) VALUES (%s,%s,%s,%s,%s)",
            (call["tenant_id"], call["call_id"], category, Jsonb(body), Jsonb(score)),
        )
        state = repo._connection.execute(
            "SELECT * FROM reckoner.v1_provider_state WHERE provider=%s FOR UPDATE",
            (call["provider"],),
        ).fetchone()
        if state and state["active_call"] == call["call_id"]:
            failures = (
                state["consecutive_failures"] + 1 if category in {"transient", "connect"} else 0
            )
            opened = datetime.now(UTC) + timedelta(seconds=60) if failures >= 5 else None
            deferred = datetime.now(UTC) + timedelta(seconds=retry_after or 0)
            repo._connection.execute(
                "UPDATE reckoner.v1_provider_state SET active_call=NULL,"
                "consecutive_failures=%s,open_until=%s,"
                "next_dispatch_at=GREATEST(next_dispatch_at,%s) "
                "WHERE provider=%s",
                (failures, opened, deferred, call["provider"]),
            )


def _claim(repo, ledger, call, maximum, protocol):
    with repo._connection.transaction():
        repo._connection.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
        repo._connection.execute(
            "INSERT INTO reckoner.v1_provider_state (provider) VALUES (%s) ON CONFLICT DO NOTHING",
            (call["provider"],),
        )
        state = repo._connection.execute(
            "SELECT * FROM reckoner.v1_provider_state WHERE provider=%s FOR UPDATE",
            (call["provider"],),
        ).fetchone()
        now = datetime.now(UTC)
        if state["active_call"] is not None:
            return "provider has an outstanding dispatch", 0
        if state["open_until"] is not None and state["open_until"] > now:
            return "provider circuit open", 0
        wait = max(0, (state["next_dispatch_at"] - now).total_seconds())
        if wait > 60:
            return "provider retry deferred", 0
        ledger.reserve(call, maximum, protocol)
        # active_call is the single persisted half-open probe and concurrency lease.
        next_time = max(now, state["next_dispatch_at"]) + timedelta(
            seconds=max(0.025, protocol["input_token_ceiling"] / 100000)
        )
        repo._connection.execute(
            "UPDATE reckoner.v1_provider_state SET active_call=%s,"
            "next_dispatch_at=%s WHERE provider=%s",
            (call["call_id"], next_time, call["provider"]),
        )
        return None, wait


def score_task(repo, client, task: dict, evidence: dict, protocol: dict) -> dict:
    """Reuse persisted scores; ambiguous dispatches require explicit reconciliation.

    Skips return {scorer_status: unavailable, degraded_reason: ...}, without a
    fabricated call/score. BudgetExceeded propagates: tasks stay incomplete.
    """
    config, request, digest = _preflight(repo, task, evidence, protocol)
    ledger = ProviderBudget(repo._connection)
    identity = tuple(task[k] for k in ("tenant_id", "run_id", "task_id"))
    lock = int(content_id(list(identity))[:15], 16)
    repo._connection.execute("SELECT pg_advisory_lock(%s)", (lock,))
    try:
        previous = repo._connection.execute(
            "SELECT c.*,r.category,r.score FROM reckoner.v1_provider_calls c "
            "LEFT JOIN reckoner.v1_provider_responses r USING(call_id) "
            "WHERE c.tenant_id=%s AND c.run_id=%s AND c.task_id=%s "
            "AND c.provider='typesafe' ORDER BY c.dispatched_at,c.call_id",
            identity,
        ).fetchall()
        if previous:
            last = previous[-1]
            if (
                last["document"]["request_sha256"] != digest
                or last["protocol_id"] != protocol["protocol_id"]
            ):
                raise ValueError("task scorer request/protocol is already pinned")
            if last["score"] is None:
                score = _score(last, config, digest, "uncertain", None, None, None)
                _finish(repo, ledger, last, score, "uncertain", None, None, None, None)
                return score
            if (
                last["category"] not in {"connect", "transient"}
                or len(previous) >= protocol["maximum_attempts"]
            ):
                return last["score"]
        for number in range(len(previous), protocol["maximum_attempts"]):
            call = {
                **{
                    k: protocol[k]
                    for k in (
                        "tenant_id",
                        "run_id",
                        "provider",
                        "purpose",
                        "model",
                        "input_token_ceiling",
                        "max_output_tokens",
                    )
                },
                "task_id": task["task_id"],
                "transaction_id": task["transaction_id"],
                "call_id": str(uuid.uuid4()),
                "request_sha256": digest,
                "request_document": request,
            }
            maximum = Decimal(protocol["input_token_ceiling"]) * Decimal(".042") / Decimal(1000000)
            skip, wait = _claim(repo, ledger, call, maximum, protocol)
            if skip:
                return {"scorer_status": "unavailable", "degraded_reason": skip}
            if wait:
                time.sleep(wait)
            body, parsed, usage, cost, retry_after = None, None, None, None, None
            try:
                body = client.evaluate(request)
                parsed = validate_response(body)
                category = "responded"
            except JevError as error:
                category, body, retry_after = error.category, error.body, error.retry_after
            if isinstance(body, dict):
                try:
                    usage = valid_usage(body.get("usage"))
                except ValueError:
                    usage = None
            if category == "connect":
                usage = {"input_tokens": 0, "output_tokens": 0}
            if usage is not None:
                cost = Decimal(usage["input_tokens"]) * Decimal(".042") / Decimal(1000000)
            score = _score(call, config, digest, category, parsed, usage, cost)
            retry_delay = (
                max(
                    2**number + random.Random(int(digest, 16) + number).random() * 0.1,
                    retry_after or 0,
                )
                if category in {"connect", "transient"}
                else retry_after
            )
            _finish(repo, ledger, call, score, category, body, usage, cost, retry_delay)
            if (
                category not in {"connect", "transient"}
                or number + 1 >= protocol["maximum_attempts"]
            ):
                return score
            if retry_after is not None and retry_after > 60:
                return score
            time.sleep(retry_delay)
        raise AssertionError("unreachable bounded scoring loop")
    finally:
        repo._connection.execute("SELECT pg_advisory_unlock(%s)", (lock,))
