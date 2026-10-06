"""Every actual synthesis/judge request has an exact approval and durable settlement."""

import json
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from reckoner.baseline.pricing import observed_cost
from reckoner.contracts import content_id
from reckoner.v1 import pricing
from reckoner.v1.notes.prompt import MODEL
from reckoner.v1.storage.budget import ProviderBudget, attempt_maximum, validate_protocol

NOTE_STAGES = {"note-generation", "note-repair"}
JUDGE_STAGES = {"judge-verdict", "judge-statements", "judge-faithfulness"}


class ApprovalRequired(ValueError):
    def __init__(self, request, stage):
        super().__init__("exact stage request requires approval")
        self.request, self.stage, self.request_sha256 = request, stage, content_id(request)


class BudgetedCalls:
    """One persisted call per task/stage. Reuse responses after any later write failure."""

    def __init__(self, repo, client, task, config, *, kind, budget=None):
        if kind not in {"note", "judge"}:
            raise ValueError("unknown generation purpose")
        self.repo, self.client, self.task, self.config, self.kind = repo, client, task, config, kind
        self.budget = budget or ProviderBudget(repo._connection)
        self.lineage = {}
        if self.budget._connection is not repo._connection:
            raise ValueError("reservation implementation must share the task connection")

    def preflight(self, request, stage, protocol):
        if protocol is None:
            raise ApprovalRequired(request, stage)
        validate_protocol(protocol)
        from reckoner.v1.notes.provider import NoteProvider

        NoteProvider._validate_request(request)
        allowed = NOTE_STAGES if self.kind == "note" else JUDGE_STAGES
        if stage not in allowed:
            raise ValueError("unapproved call stage")
        pinned = self.repo.workflow_document(
            "v1_configs", {"tenant_id": self.task["tenant_id"], "config_id": self.task["config_id"]}
        )
        if pinned != self.config:
            raise ValueError("generation configuration is not pinned")
        model = self.config[self.kind + "_model"]
        if (
            model["model"] != MODEL
            or model["provider"] != "anthropic"
            or model["prompt_version"] != ("note-v1" if self.kind == "note" else "judge-v1")
        ):
            raise ValueError("unsupported generation model/prompt")
        if (
            set(request) != {"model", "system", "messages", "max_tokens", "temperature"}
            or request["model"] != MODEL
            or request["temperature"] != 0
        ):
            raise ValueError("unsupported generation request")
        if (
            protocol["provider"] != "anthropic"
            or protocol["model"] != MODEL
            or protocol["purpose"] != ("online-note" if self.kind == "note" else "judge")
        ):
            raise ValueError("generation purpose/provider mismatch")
        for key in ("tenant_id", "run_id"):
            if protocol[key] != self.task[key]:
                raise ValueError("generation task scope mismatch")
        for key in ("input_token_ceiling", "max_output_tokens", "maximum_attempts"):
            if protocol[key] > self.config["limits"][key]:
                raise ValueError("protocol exceeds frozen execution limits")
        if request["max_tokens"] != protocol["max_output_tokens"]:
            raise ValueError("output limit mismatch")
        if (
            len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode())
            > protocol["input_token_ceiling"]
        ):
            raise ValueError("request exceeds conservative input-token ceiling")
        derivation = protocol.get("derivation")
        if derivation is None:
            approved = {k: self.task[k] for k in ("task_id", "transaction_id")} | {
                "request_sha256": content_id(request)
            }
            if approved not in protocol["tasks"]:
                raise ApprovalRequired(request, stage)
            self.lineage[stage] = None
        else:
            # derived-request-v1: only the exact reconstruction from the persisted parent.
            if self.kind != "judge" or stage != derivation["stage"]:
                raise ValueError("stage is outside the derived-request policy")
            case = {k: self.task[k] for k in ("task_id", "transaction_id")}
            if case | {"request_sha256": None} not in protocol["tasks"]:
                raise ValueError("case is outside the derived-request policy")
            from reckoner.v1.evaluation.derived import expected_request

            expected, parent = expected_request(self.repo, self.task, self.config, derivation)
            if content_id(expected) != content_id(request):
                raise ValueError("derived request differs from its exact reconstruction")
            self.lineage[stage] = parent
        if self.config["limits"]["timeout_seconds"] != 30:
            raise ValueError("generation timeout must be pinned to 30 seconds")
        if stage == "note-repair":
            initial = self.repo._connection.execute(
                "SELECT document FROM reckoner.v1_generation_stages WHERE "
                "tenant_id=%s AND run_id=%s AND task_id=%s AND "
                "stage='note-generation'",
                tuple(self.task[k] for k in ("tenant_id", "run_id", "task_id")),
            ).fetchone()
            if (
                protocol["maximum_attempts"] < 2
                or initial is None
                or initial["document"]["protocol_id"] != protocol["protocol_id"]
            ):
                raise ValueError("repair must use the original bounded protocol allowance")
        if not pricing.matches("anthropic", model["price_table"]):
            raise ValueError("unknown pinned generation pricing")
        return attempt_maximum(
            protocol,
            pricing.cost(
                "anthropic",
                {
                    "input_tokens": protocol["input_token_ceiling"],
                    "output_tokens": protocol["max_output_tokens"],
                },
            ),
        )

    def execute(self, request, *, stage, protocol):
        maximum = self.preflight(request, stage, protocol)
        connection = self.repo._connection
        identity = {k: self.task[k] for k in ("tenant_id", "run_id", "task_id")}
        lock = int(content_id(["generation", *identity.values(), stage])[:15], 16)
        connection.execute("SELECT pg_advisory_lock(%s)", (lock,))
        try:
            previous = connection.execute(
                "SELECT document FROM reckoner.v1_generation_stages WHERE "
                "tenant_id=%s AND run_id=%s AND task_id=%s AND stage=%s",
                (*identity.values(), stage),
            ).fetchone()
            if previous:
                call = previous["document"]
                if (
                    call["request_sha256"] != content_id(request)
                    or call["protocol_id"] != protocol["protocol_id"]
                ):
                    raise ValueError("immutable generation stage mismatch")
                saved = connection.execute(
                    "SELECT score FROM reckoner.v1_provider_responses WHERE "
                    "tenant_id=%s AND call_id=%s",
                    (identity["tenant_id"], call["call_id"]),
                ).fetchone()
                if saved:
                    return saved["score"]
                return self._finish(call, None, "uncertain")
            call = {
                **identity,
                "transaction_id": self.task["transaction_id"],
                **{
                    k: protocol[k]
                    for k in (
                        "provider",
                        "purpose",
                        "model",
                        "input_token_ceiling",
                        "max_output_tokens",
                    )
                },
                "stage": stage,
                "source_run_id": self.task["run_id"],
                "source_task_id": self.task["task_id"],
                "request_sha256": content_id(request),
                "request_document": request,
                "protocol_id": protocol["protocol_id"],
                "derived_from": self.lineage.get(stage),
                "started_at": datetime.now(UTC).isoformat(),
            }
            call["call_id"] = content_id([*identity.values(), stage])
            with connection.transaction():
                self.budget.reserve(call, maximum, protocol, fixture=self.repo.fixture_dispatch)
                connection.execute(
                    "INSERT INTO reckoner.v1_generation_stages "
                    "(tenant_id,run_id,task_id,stage,call_id,document) VALUES "
                    "(%s,%s,%s,%s,%s,%s)",
                    (*identity.values(), stage, call["call_id"], Jsonb(call)),
                )
            try:
                body = self.client.generate(request)
            except Exception:
                # Provider exceptions may contain secrets; only safe category persists.
                return self._finish(call, None, "uncertain")
            return self._finish(call, body, "responded")
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (lock,))

    def _finish(self, call, body, status):
        price = self.config[self.kind + "_model"]["price_table"]
        cost = observed_cost(body, price) if isinstance(body, dict) else None
        usage = (
            {k: body[k] for k in ("input_tokens", "output_tokens")} if cost is not None else None
        )
        # Persist only the bounded response contract, never arbitrary provider diagnostics.
        fields = (
            "content",
            "finish_reason",
            "reported_model",
            "provider_request_id",
            "requested_model",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        )
        safe = {k: body.get(k) for k in fields} if isinstance(body, dict) else None
        result = {
            "tenant_id": call["tenant_id"],
            "call_id": call["call_id"],
            "stage": call["stage"],
            "status": status,
            "body": safe,
            "billing_status": "settled" if cost is not None else "uncertain",
            "cost": format(cost, "f") if cost is not None else None,
            "usage": usage,
            "started_at": call["started_at"],
            "completed_at": datetime.now(UTC).isoformat(),
            "price_table_id": price["price_table_version"],
            "protocol_id": call["protocol_id"],
        }
        with self.repo._connection.transaction():
            self.budget.settle(call["call_id"], usage, cost)
            self.repo._connection.execute(
                "INSERT INTO reckoner.v1_provider_responses "
                "(tenant_id,call_id,category,body,score) VALUES (%s,%s,%s,%s,%s)",
                (call["tenant_id"], call["call_id"], status, Jsonb(safe), Jsonb(result)),
            )
        return result
