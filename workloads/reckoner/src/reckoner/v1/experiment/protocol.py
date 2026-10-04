"""Paid-run protocols: immutable bodies, measured drafts, external SHA-bound consent.

A protocol pins the exact frozen cases, evidence manifests, run declarations,
prices, provider/ledger identity, token/attempt bounds, worst-case cost, USD cap,
purpose and approver. Its SHA-256 is the content identity of that body. Consent is
never part of it: an approval record restating the exact scope is supplied
separately and recorded by the owner (`reserve_protocol`). Drafting or presenting
a protocol is not execution and never authorizes a provider call. All workload
data is simulated.
"""

import hashlib
import json
import math
from collections import Counter
from decimal import Decimal
from pathlib import Path

from reckoner.contracts import content_id
from reckoner.resources import PROMPTS
from reckoner.v1.storage.budget import (
    HEX,
    PROVIDERS,
    money,
    validate_approval_record,
)
from reckoner.v1.storage.budget import validate_protocol as validate_dispatch

SCHEMA = "reckoner-paid-protocol-v1"
MEASUREMENTS = "reckoner-request-measurements-v1"
OVERHEAD = "reckoner-token-overhead-v1"
LEDGER = "reckoner-ledger-snapshot-v1"
# experiment purpose -> provider, allowed dispatch purposes, telemetry topology
PURPOSES = {
    "pilot": ("typesafe", {"pilot"}, "scoring-only"),
    "development": ("typesafe", {"development"}, "scoring-only"),
    "validation": ("typesafe", {"validation"}, "scoring-only"),
    "graph-comparison": ("typesafe", {"graph-comparison"}, "scoring-only"),
    "final": ("typesafe", {"final"}, "workflow"),
    "note-judge-pilot": ("anthropic", {"online-note", "judge"}, "workflow"),
    "final-notes": ("anthropic", {"online-note"}, "workflow"),
    "final-judges": ("anthropic", {"judge"}, "workflow"),
}
# Pinned published prices (USD per million tokens); unknown/changed prices block.
PRICES = {
    "typesafe": (Decimal("0.042"), Decimal(0)),
    "anthropic": (Decimal(1), Decimal(5)),
}
FIELDS = {
    "schema_version",
    "dataset_simulated",
    "purpose",
    "provider",
    "model",
    "telemetry_mode",
    "approver",
    "statement_of_purpose",
    "expected_outputs",
    "uncertainty_handling",
    "evidence",
    "runs",
    "cases",
    "versions",
    "prices",
    "ledger",
    "bounds",
    "worst_case_usd",
    "usd_cap",
    "dispatch",
    "protocol_sha256",
}
BOUNDS = {
    "input_token_ceiling",
    "billing_token_bound",
    "max_output_tokens",
    "maximum_attempts",
    "retry_assumption",
}
CASE = {"tenant_id", "run_id", "task_id", "transaction_id", "evidence_id", "evidence_mode"}
RUN = {"tenant_id", "run_id", "experiment_id", "config_id", "purpose", "telemetry_mode"}
PIN = {
    "population",
    "mode",
    "manifest_id",
    "manifest_sha256",
    "preparation_id",
    "source_snapshot_id",
    "case_count",
}
UNCERTAINTY = (
    "Every attempt reserves its maximum before dispatch. A timeout or missing usage "
    "keeps that maximum as an uncertain, nonrefundable liability until reconciled; "
    "it is never assumed zero, and the protocol stops before exceeding its cap. Actual "
    "usage above a reservation blocks further dispatch until reconciled."
)
RETRY = (
    "At most {attempts} attempts per case, only after 429/529/connect failures, with "
    "persisted backoff; 401/422 and ambiguous timeouts are never retried. Each attempt "
    "is reserved and priced separately."
)


class MissingInputs(ValueError):
    """A draft needs measured inputs that do not exist yet."""

    def __init__(self, names):
        self.names = sorted(names)
        super().__init__("protocol draft needs measured inputs: " + ", ".join(self.names))


def _body(protocol):
    return {k: v for k, v in protocol.items() if k != "protocol_sha256"}


def identify(body: dict) -> dict:
    body = {k: v for k, v in body.items() if k != "protocol_sha256"}
    return {**body, "protocol_sha256": content_id(body)}


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


def attempt_maximum(provider, billing_token_bound, max_output_tokens) -> Decimal:
    """Worst-case USD for one attempt at the pinned published price."""
    input_price, output_price = PRICES[provider]
    return (
        Decimal(billing_token_bound) * input_price + Decimal(max_output_tokens) * output_price
    ) / Decimal(1000000)


def dispatch_worst_case(provider, dispatch, bounds) -> Decimal:
    return (
        len(dispatch["tasks"])
        * dispatch["maximum_attempts"]
        * attempt_maximum(provider, bounds["billing_token_bound"], dispatch["max_output_tokens"])
    )


def _price(provider, model, table):
    if not isinstance(table, dict):
        raise ValueError("missing pinned prices block dispatch")
    body = {k: v for k, v in table.items() if k != "price_table_version"}
    input_price, output_price = PRICES[provider]
    try:
        known = (
            table.get("schema_version") == "price-table-v1"
            and table["price_table_version"] == content_id(body)
            and table["model"] == model
            and table["currency"] == "USD"
            and Decimal(table["input_per_million"]) == input_price
            and Decimal(table["output_per_million"]) == output_price
        )
    except (KeyError, TypeError, ArithmeticError) as exc:
        raise ValueError("unknown pinned pricing blocks dispatch") from exc
    if not known:
        raise ValueError("unknown or changed pinned pricing blocks dispatch")


def _strings(value, *, allow_empty=False):
    return (
        isinstance(value, list)
        and (allow_empty or value)
        and all(isinstance(v, str) and v.strip() for v in value)
    )


def validate_protocol(protocol: dict, ledger: dict) -> dict:
    """Validate an immutable protocol against a reconciled provider ledger snapshot.

    Missing/changed prices, a changed case set, model or attempt count, over-limit
    cost, the wrong ledger, an unresolved prior reservation or a changed protocol hash
    each raise. Returns a validation receipt; it is not an approval.
    """
    if not isinstance(protocol, dict) or set(protocol) != FIELDS:
        raise ValueError("an exact paid-run protocol body is required")
    if protocol["protocol_sha256"] != content_id(_body(protocol)):
        raise ValueError("protocol SHA-256 does not match its body")
    if protocol["schema_version"] != SCHEMA or protocol["dataset_simulated"] is not True:
        raise ValueError("unsupported protocol version or unlabelled data")
    if protocol["purpose"] not in PURPOSES:
        raise ValueError("unknown protocol purpose")
    provider, dispatch_purposes, telemetry = PURPOSES[protocol["purpose"]]
    if protocol["provider"] != provider or PROVIDERS[provider] != protocol["model"]:
        raise ValueError("protocol provider/model is not pinned for this purpose")
    if protocol["telemetry_mode"] != telemetry:
        raise ValueError("protocol telemetry topology does not match its purpose")
    for key in ("approver", "statement_of_purpose", "uncertainty_handling"):
        if not isinstance(protocol[key], str) or not protocol[key].strip():
            raise ValueError("approver, purpose statement and uncertainty handling required")
    if not _strings(protocol["expected_outputs"]):
        raise ValueError("expected outputs must be listed")
    if not isinstance(protocol["versions"], dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v for k, v in protocol["versions"].items()
    ):
        raise ValueError("versions must be exact strings")
    _price(provider, protocol["model"], protocol["prices"])
    bounds = protocol["bounds"]
    if not isinstance(bounds, dict) or set(bounds) != BOUNDS:
        raise ValueError("exact token/attempt bounds are required")
    for key in BOUNDS - {"retry_assumption"}:
        if type(bounds[key]) is not int or bounds[key] <= 0:
            raise ValueError("bounds must be positive integers")
    if bounds["billing_token_bound"] < bounds["input_token_ceiling"]:
        raise ValueError("billing bound cannot be below the request ceiling")
    pins = protocol["evidence"]
    if not isinstance(pins, list) or (provider == "typesafe" and not pins):
        raise ValueError("scoring protocols must pin published evidence manifests")
    for pin in pins:
        if (
            not isinstance(pin, dict)
            or set(pin) != PIN
            or not all(HEX.match(str(pin[k])) for k in PIN - {"population", "mode", "case_count"})
            or pin["mode"] not in {"relational", "gds-augmented"}
        ):
            raise ValueError("invalid evidence manifest pin")
    runs = protocol["runs"]
    if not isinstance(runs, list) or not runs:
        raise ValueError("protocol must pin declared runs")
    run_keys = set()
    for run in runs:
        if (
            not isinstance(run, dict)
            or set(run) != RUN
            or not HEX.match(str(run["experiment_id"]))
            or not HEX.match(str(run["config_id"]))
            or run["telemetry_mode"] != telemetry
        ):
            raise ValueError("invalid run pin")
        run_keys.add((run["tenant_id"], run["run_id"]))
    if len(run_keys) != len(runs):
        raise ValueError("duplicate run pin")
    cases = protocol["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("protocol must list exact frozen cases")
    case_keys = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != CASE:
            raise ValueError("invalid case pin")
        if (case["tenant_id"], case["run_id"]) not in run_keys:
            raise ValueError("case belongs to an undeclared run")
        case_keys.add((case["tenant_id"], case["run_id"], case["task_id"], case["transaction_id"]))
    if len(case_keys) != len(cases) or len(
        {(c["tenant_id"], c["run_id"], c["task_id"]) for c in cases}
    ) != len(cases):
        raise ValueError("duplicate frozen case")
    covered, total, worst = set(), Decimal(0), Decimal(0)
    dispatch = protocol["dispatch"]
    if not isinstance(dispatch, list) or not dispatch:
        raise ValueError("protocol must contain dispatch envelopes")
    if len({d.get("protocol_id") for d in dispatch if isinstance(d, dict)}) != len(dispatch):
        raise ValueError("duplicate dispatch envelope")
    for item in dispatch:
        validate_dispatch(item)
        if item["provider"] != provider or item["model"] != protocol["model"]:
            raise ValueError("dispatch provider/model differs from the protocol")
        if item["purpose"] not in dispatch_purposes:
            raise ValueError("dispatch purpose differs from the protocol purpose")
        if (
            item["input_token_ceiling"] > bounds["input_token_ceiling"]
            or item["max_output_tokens"] > bounds["max_output_tokens"]
            or item["maximum_attempts"] > bounds["maximum_attempts"]
            or (
                provider == "typesafe"
                and (
                    item["maximum_attempts"] != bounds["maximum_attempts"]
                    or item["input_token_ceiling"] != bounds["input_token_ceiling"]
                )
            )
        ):
            raise ValueError("dispatch bounds or attempt count differ from the protocol")
        for task in item["tasks"]:
            key = (item["tenant_id"], item["run_id"], task["task_id"], task["transaction_id"])
            if key not in case_keys:
                raise ValueError("dispatch request is outside the frozen case set")
            covered.add(key)
        cost = dispatch_worst_case(provider, item, bounds)
        if money(item["usd_cap"]) < cost:
            raise ValueError("dispatch cap is below its worst-case cost")
        worst += cost
        total += money(item["usd_cap"])
    if covered != case_keys:
        raise ValueError("frozen case set differs from dispatch requests")
    if Decimal(str(protocol["worst_case_usd"])) != worst:
        raise ValueError("declared worst-case cost does not match bounds and prices")
    cap = money(protocol["usd_cap"])
    if total != cap or cap < worst:
        raise ValueError("USD cap does not cover worst case or match dispatch envelopes")
    if (
        not isinstance(ledger, dict)
        or ledger.get("schema_version") != "reckoner-ledger-snapshot-v1"
    ):
        raise ValueError("a reconciled ledger snapshot is required")
    pinned = protocol["ledger"]
    if not isinstance(pinned, dict) or set(pinned) != {"provider", "ledger_id"}:
        raise ValueError("protocol must pin a provider ledger identity")
    if ledger["provider"] != provider or pinned != {
        "provider": ledger["provider"],
        "ledger_id": ledger["ledger_id"],
    }:
        raise ValueError("wrong ledger for this protocol")
    if provider == "anthropic" and not ledger["legacy_verified"]:
        raise ValueError("original Anthropic ledger is not restored and verified")
    if ledger["unresolved"]:
        raise ValueError("an unresolved prior reservation blocks a new protocol")
    if Decimal(ledger["remaining_usd"]) < cap:
        raise ValueError("protocol cap exceeds the reconciled provider-wide remainder")
    return {
        "status": "valid",
        "protocol_sha256": protocol["protocol_sha256"],
        "provider": provider,
        "model": protocol["model"],
        "purpose": protocol["purpose"],
        "case_count": len(cases),
        "worst_case_usd": _decimal_text(worst),
        "usd_cap": protocol["usd_cap"],
        "remaining_usd": ledger["remaining_usd"],
        "ledger_id": ledger["ledger_id"],
        "execution_authorized": False,
    }


def approval_scope(protocol: dict) -> dict:
    return {
        "provider": protocol["provider"],
        "model": protocol["model"],
        "purpose": protocol["purpose"],
        "case_count": len(protocol["cases"]),
        "maximum_attempts": protocol["bounds"]["maximum_attempts"],
        "usd_cap": protocol["usd_cap"],
    }


def bind_approval(approval: dict, protocol: dict) -> dict:
    """An external record must restate this exact protocol's SHA, approver and scope."""
    approval = validate_approval_record(approval)
    if approval["protocol_sha256"] != protocol["protocol_sha256"]:
        raise ValueError("approval does not bind this protocol SHA-256")
    if approval["approver"] != protocol["approver"]:
        raise ValueError("approval is not from the protocol's named approver")
    if approval["scope"] != approval_scope(protocol):
        raise ValueError("approval scope differs from the exact protocol bounds")
    return approval


def _check_runs(cursor, protocol):
    """Declared runs, their tasks and pinned configurations must match exactly."""
    part = "scorer" if protocol["provider"] == "typesafe" else None
    for run in protocol["runs"]:
        row = cursor.execute(
            "SELECT r.document, r.telemetry_mode, c.document AS config "
            "FROM reckoner.v1_runs r JOIN reckoner.v1_configs c USING (tenant_id, config_id) "
            "WHERE r.tenant_id=%s AND r.run_id=%s",
            (run["tenant_id"], run["run_id"]),
        ).fetchone()
        if row is None:
            raise ValueError("declare the protocol's runs before reserving it")
        document, config = row["document"], row["config"]
        expected = {
            (c["task_id"], c["transaction_id"])
            for c in protocol["cases"]
            if (c["tenant_id"], c["run_id"]) == (run["tenant_id"], run["run_id"])
        }
        if (
            document["experiment_id"] != run["experiment_id"]
            or document["config_id"] != run["config_id"]
            or document["purpose"] != run["purpose"]
            or row["telemetry_mode"] != run["telemetry_mode"]
            or {(t["task_id"], t["transaction_id"]) for t in document["tasks"]} != expected
        ):
            raise ValueError("declared run differs from the protocol's frozen cases")
        models = [config[part]] if part else [config["note_model"], config["judge_model"]]
        bounds = protocol["bounds"]
        limits = config["limits"]
        if any(
            m["provider"] != protocol["provider"]
            or m["model"] != protocol["model"]
            or m["price_table"] != protocol["prices"]
            for m in models
        ) or any(
            bounds[key] > limits[key]
            for key in ("input_token_ceiling", "max_output_tokens", "maximum_attempts")
        ):
            raise ValueError("pinned run configuration differs from protocol model/prices/bounds")


def reserve_protocol(protocol: dict, ledger, *, approval: dict) -> str:
    """Owner action: validate, bind the external approval, reserve full envelopes.

    `ledger` is a ProviderBudget on the owner connection. Returns the protocol SHA-256
    used by `execute_protocol`. Nothing is dispatched here.
    """
    snapshot = ledger.snapshot(protocol.get("provider"))
    validate_protocol(protocol, snapshot)
    approval = bind_approval(approval, protocol)
    from psycopg.rows import dict_row

    _check_runs(ledger._connection.cursor(row_factory=dict_row), protocol)
    return ledger.authorize(protocol, protocol["dispatch"], approval=approval)


# --- measured inputs -------------------------------------------------------------


def load_evidence_manifest(path: Path) -> dict:
    """A Task 3b published manifest, verified by bytes and content identity."""
    payload = Path(path).read_bytes()
    manifest = json.loads(payload)
    body = {k: v for k, v in manifest.items() if k != "manifest_id"}
    if (
        manifest.get("schema_version") != "reckoner-evidence-manifest-v1"
        or manifest.get("dataset_simulated") is not True
        or manifest.get("manifest_id") != content_id(body)
        or manifest.get("mode") not in {"relational", "gds-augmented"}
        or manifest.get("case_count") != len(manifest.get("cases", []))
    ):
        raise ValueError("invalid or altered published evidence manifest")
    keys = [(c["tenant_id"], c["transaction_id"]) for c in manifest["cases"]]
    if len(set(keys)) != len(keys) or len({c["evidence_id"] for c in manifest["cases"]}) != len(
        keys
    ):
        raise ValueError("evidence manifest repeats a case")
    return {"manifest": manifest, "sha256": hashlib.sha256(payload).hexdigest()}


def request_measurements(entries) -> dict:
    """Exact request hashes and UTF-8 sizes from (task, evidence, request) entries."""
    cases = []
    for task, evidence, request in entries:
        size = len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode())
        cases.append(
            {
                "tenant_id": task["tenant_id"],
                "run_id": task["run_id"],
                "task_id": task["task_id"],
                "transaction_id": task["transaction_id"],
                "evidence_id": evidence["evidence_id"],
                "request_sha256": content_id(request),
                "utf8_bytes": size,
            }
        )
    cases.sort(key=lambda c: (c["tenant_id"], c["run_id"], c["task_id"]))
    sizes = sorted(c["utf8_bytes"] for c in cases)

    def rank(q):
        return sizes[max(0, math.ceil(q * len(sizes)) - 1)] if sizes else None

    body = {
        "schema_version": MEASUREMENTS,
        "dataset_simulated": True,
        "provider_calls": 0,
        "cases": cases,
        "distribution": {
            "count": len(sizes),
            "min": sizes[0] if sizes else None,
            "p50": rank(0.5),
            "p90": rank(0.9),
            "p99": rank(0.99),
            "max": sizes[-1] if sizes else None,
        },
    }
    return {**body, "measurement_id": content_id(body)}


def measure_scoring_requests(repo, protocol_cases) -> dict:
    """Build exact Jev requests from persisted evidence pinned by evidence_id."""
    from reckoner.v1.providers.jev import build_request

    entries = []
    for case in protocol_cases:
        task = repo.task(case["tenant_id"], case["run_id"], case["task_id"])
        if task["transaction_id"] != case["transaction_id"]:
            raise ValueError("declared task transaction differs from manifest case")
        evidence = repo.workflow_document(
            "v1_evidence", {"tenant_id": case["tenant_id"], "evidence_id": case["evidence_id"]}
        )
        if evidence is None or evidence["transaction_id"] != case["transaction_id"]:
            raise ValueError("pinned prepared evidence is missing")
        entries.append((task, evidence, build_request(task["transaction"], evidence)))
    return request_measurements(entries)


def _verified_measurements(document):
    body = {k: v for k, v in document.items() if k != "measurement_id"}
    if (
        document.get("schema_version") != MEASUREMENTS
        or document.get("measurement_id") != content_id(body)
        or not document.get("cases")
    ):
        raise ValueError("invalid request measurements")
    return document


def token_bounds(max_bytes: int, overhead: dict | None, *, ceiling_limit: int = 32000):
    """Deterministic, conservative bounds from measured evidence, fixed before asking.

    Without a measured token overhead, the bytes ceiling is the provider's state
    limit and billing allows 100% hidden overhead. With a pilot measurement, both are
    the measured worst case plus a 25% margin.
    """
    if overhead is None:
        return ceiling_limit, 2 * ceiling_limit
    body = {k: v for k, v in overhead.items() if k != "overhead_id"}
    if overhead.get("schema_version") != OVERHEAD or overhead.get("overhead_id") != content_id(
        body
    ):
        raise ValueError("invalid token overhead measurement")
    ratio = Decimal(overhead["max_tokens_per_byte"])
    extra = overhead["max_overhead_tokens"]
    if ratio <= 0 or type(extra) is not int or extra < 0:
        raise ValueError("invalid token overhead measurement")
    bound = math.ceil((Decimal(max_bytes) * ratio + extra) * Decimal("1.25"))
    bound = max(bound, math.ceil(max_bytes * Decimal("1.25")))
    if bound > ceiling_limit:
        raise ValueError("measured request bound exceeds the provider state limit")
    return bound, bound


def draft_scoring_protocol(
    *,
    purpose: str,
    approver: str | None,
    manifests: list[dict] | None,
    runs: list[dict] | None,
    measurements: dict | None,
    price_table: dict | None,
    ledger: dict | None,
    code_revision: str | None,
    token_overhead: dict | None = None,
) -> dict:
    """Draft (never execute) a Jev scoring protocol from measured inputs only."""
    if purpose not in PURPOSES or PURPOSES[purpose][0] != "typesafe":
        raise ValueError("not a scoring protocol purpose")
    required = {
        "approver": approver,
        "evidence_manifests": manifests,
        "declared_runs": runs,
        "request_measurements": measurements,
        "price_snapshot": price_table,
        "ledger_snapshot": ledger,
        "code_revision": code_revision,
    }
    if purpose != "pilot":
        required["token_overhead"] = token_overhead
    missing = {name for name, value in required.items() if not value}
    if missing:
        raise MissingInputs(missing)
    provider, _, telemetry = PURPOSES[purpose]
    model = PROVIDERS[provider]
    _price(provider, model, price_table)
    if ledger.get("provider") != provider:
        raise ValueError("ledger snapshot is for another provider")
    measurements = _verified_measurements(measurements)
    by_case, pins = {}, []
    for item in manifests:
        manifest = item["manifest"]
        pins.append(
            {
                "population": manifest["population"],
                "mode": manifest["mode"],
                "manifest_id": manifest["manifest_id"],
                "manifest_sha256": item["sha256"],
                "preparation_id": manifest["preparation_id"],
                "source_snapshot_id": manifest["source_snapshot_id"],
                "case_count": manifest["case_count"],
            }
        )
        for entry in manifest["cases"]:
            by_case.setdefault((entry["tenant_id"], entry["evidence_id"]), (manifest, entry))
    run_pins, declared = [], set()
    for run in runs:
        if run["purpose"] != purpose:
            raise ValueError("declared run purpose differs from the protocol")
        run_pins.append(
            {
                "tenant_id": run["tenant_id"],
                "run_id": run["run_id"],
                "experiment_id": run["experiment_id"],
                "config_id": run["config_id"],
                "purpose": run["purpose"],
                "telemetry_mode": telemetry,
            }
        )
        declared |= {
            (run["tenant_id"], run["run_id"], t["task_id"], t["transaction_id"])
            for t in run["tasks"]
        }
    cases, groups = [], {}
    for case in measurements["cases"]:
        key = (case["tenant_id"], case["run_id"], case["task_id"], case["transaction_id"])
        if key not in declared:
            raise ValueError("measured request is not a declared run task")
        found = by_case.get((case["tenant_id"], case["evidence_id"]))
        if found is None or found[1]["transaction_id"] != case["transaction_id"]:
            raise ValueError("measured evidence is not the published manifest document")
        cases.append(
            {
                "tenant_id": case["tenant_id"],
                "run_id": case["run_id"],
                "task_id": case["task_id"],
                "transaction_id": case["transaction_id"],
                "evidence_id": case["evidence_id"],
                "evidence_mode": found[0]["mode"],
            }
        )
        groups.setdefault((case["tenant_id"], case["run_id"]), []).append(case)
    if {(c["tenant_id"], c["run_id"], c["task_id"], c["transaction_id"]) for c in cases} != (
        declared
    ):
        raise ValueError("every declared run task needs a measured request")
    attempts = 3
    ceiling, billing = token_bounds(measurements["distribution"]["max"], token_overhead)
    bounds = {
        "input_token_ceiling": ceiling,
        "billing_token_bound": billing,
        "max_output_tokens": 1000,
        "maximum_attempts": attempts,
        "retry_assumption": RETRY.format(attempts=attempts),
    }
    dispatch, worst = [], Decimal(0)
    for (tenant, run_id), members in sorted(groups.items()):
        item = {
            "tenant_id": tenant,
            "run_id": run_id,
            "provider": provider,
            "purpose": purpose,
            "model": model,
            "input_token_ceiling": ceiling,
            "max_output_tokens": 1000,
            "maximum_attempts": attempts,
            "tasks": [
                {
                    "task_id": m["task_id"],
                    "transaction_id": m["transaction_id"],
                    "request_sha256": m["request_sha256"],
                }
                for m in sorted(members, key=lambda m: m["task_id"])
            ],
        }
        cost = dispatch_worst_case(provider, item, bounds)
        item["usd_cap"] = _decimal_text(cost)
        item["protocol_id"] = content_id(item)
        dispatch.append(item)
        worst += cost
    question = json.loads((PROMPTS / "jev-choice-v1.json").read_text())["question_version"]
    body = {
        "schema_version": SCHEMA,
        "dataset_simulated": True,
        "purpose": purpose,
        "provider": provider,
        "model": model,
        "telemetry_mode": telemetry,
        "approver": approver,
        "statement_of_purpose": (
            f"Jev {purpose} scoring of {len(cases)} frozen simulated cases with published "
            "prepared evidence; scores only, no routing or notes."
        ),
        "expected_outputs": [
            "persisted raw Jev distributions, confidence and usage per case",
            "request/response hashes and settled or uncertain cost per attempt",
            "score-only OTLP telemetry and a verification report",
        ],
        "uncertainty_handling": UNCERTAINTY,
        "evidence": pins,
        "runs": sorted(run_pins, key=lambda r: (r["tenant_id"], r["run_id"])),
        "cases": sorted(cases, key=lambda c: (c["tenant_id"], c["run_id"], c["task_id"])),
        "versions": {
            "code_revision": code_revision,
            "question_version": question,
            "request_measurement_id": measurements["measurement_id"],
            **(
                {"token_overhead_id": token_overhead["overhead_id"]}
                if token_overhead is not None
                else {}
            ),
        },
        "prices": price_table,
        "ledger": {"provider": provider, "ledger_id": ledger["ledger_id"]},
        "bounds": bounds,
        "worst_case_usd": _decimal_text(worst),
        "usd_cap": _decimal_text(worst),
        "dispatch": dispatch,
    }
    return identify(body)


def present_protocol(protocol: dict, ledger: dict) -> str:
    """Owner-facing approval request text. Presenting it authorizes nothing."""
    receipt_problems = []
    try:
        validate_protocol(protocol, ledger)
    except ValueError as error:
        receipt_problems.append(str(error))
    tenants = Counter(c["tenant_id"] for c in protocol["cases"])
    bounds = protocol["bounds"]
    lines = [
        f"# Paid-run approval request: {protocol['purpose']} ({protocol['provider']})",
        "",
        "Simulated data only. This draft is not execution and authorizes nothing. A "
        "provider's or user's displayed credit is not approval.",
        "",
        f"- Protocol SHA-256: `{protocol['protocol_sha256']}`",
        f"- Provider / model: {protocol['provider']} / `{protocol['model']}`",
        f"- Purpose: {protocol['statement_of_purpose']}",
        f"- Frozen cases: {len(protocol['cases'])} "
        f"({', '.join(f'{t}: {n}' for t, n in sorted(tenants.items()))}); "
        f"case list SHA-256 `{content_id(protocol['cases'])}` (full list in the protocol file)",
        "- Evidence manifests: "
        + (
            ", ".join(
                f"{p['population']}-{p['mode']} `{p['manifest_id']}`" for p in protocol["evidence"]
            )
            or "none (persisted decisions/notes)"
        ),
        f"- Bounds: request ceiling {bounds['input_token_ceiling']} tokens, billing bound "
        f"{bounds['billing_token_bound']} input tokens, {bounds['max_output_tokens']} output "
        f"tokens, {bounds['maximum_attempts']} attempts per case",
        f"- Retry assumptions: {bounds['retry_assumption']}",
        f"- Worst-case cost: USD {protocol['worst_case_usd']}; hard cap: USD {protocol['usd_cap']}",
        f"- Reconciled {ledger['provider']} ledger `{ledger['ledger_id']}`: liability USD "
        f"{ledger['liability_usd']}, remaining USD {ledger['remaining_usd']} of "
        f"{ledger['provider_cap_usd']}; unresolved: {len(ledger['unresolved'])}",
        f"- Nonrefundable uncertainty: {protocol['uncertainty_handling']}",
        "- Expected outputs: " + "; ".join(protocol["expected_outputs"]),
        f"- Approver: {protocol['approver']}",
        "",
        "To approve, the owner states consent to exactly this scope; the controller then "
        "records it as an external approval record quoting the owner's words:",
        "",
        "```json",
        json.dumps(
            {"protocol_sha256": protocol["protocol_sha256"], "scope": approval_scope(protocol)},
            indent=2,
            sort_keys=True,
        ),
        "```",
    ]
    if receipt_problems:
        lines += ["", "**Blocked:** " + "; ".join(receipt_problems)]
    return "\n".join(lines) + "\n"
