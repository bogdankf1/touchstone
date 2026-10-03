"""Real-archive evidence preparation: schedule, declaration, entities, policy, publication.

The source is the simulated CCTD archive. Preparation reads labels only through the
owner import path; nothing here calls a provider. Relational evidence identities are
reproducible because the working-set snapshot identity is constant for a source,
bundle, entity manifest and resolution policy, independent of populations or staging.
"""

import csv
import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckoner.contracts import content_id
from reckoner.data.artifacts import canonical_json, verify_bundle
from reckoner.v1.data.history import POLICY, instant

WORKING_SET_DAYS = 97
MODES = ("relational", "gds-augmented")
TENANTS = ("tenant-a", "tenant-b")
# Ruling R2: relational for every population; GDS-augmented for 2018 validation and the
# 2019 cohort. Development GDS evidence is a separate, conditional preparation (D4).
DEFAULT_POPULATIONS = {
    "development": ("relational",),
    "validation": ("relational", "gds-augmented"),
    "cohort-2019": ("relational", "gds-augmented"),
}
# A data-intrinsic reason is persisted and counted. Every other reason is attributable to
# preparation and fails the day before anything is persisted.
INTRINSIC = frozenset({"query card absent from GDS projection"})


class PreparationFault(ValueError):
    """A day cannot be persisted; nothing for that day was written."""

    def __init__(self, day: str, failures: list[dict]):
        self.day, self.failures = day, failures
        super().__init__(f"preparation fault on {day}: {json.dumps(failures, sort_keys=True)}")


def utc_day(value: str) -> str:
    return instant(value).date().isoformat()


def day_start(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def window(day: str) -> tuple[datetime, datetime]:
    """Working set for queries on `day`: [day - 97 days, day + 1 day)."""
    start = day_start(day)
    return start - timedelta(days=WORKING_SET_DAYS), start + timedelta(days=1)


def in_window(occurred_at: str, day: str) -> bool:
    start, end = window(day)
    return start <= instant(occurred_at) < end


# --- schedule -------------------------------------------------------------------


def schedule_cases(populations: dict, modes: dict, *, pilot: set) -> list[dict]:
    """One entry per UTC query day; each case appears once, in exactly one population."""
    owner = {}
    for name in sorted(modes):
        for tx in populations[name]:
            key = (tx["tenant_id"], tx["transaction_id"])
            if key in owner:
                raise ValueError(f"duplicate case {key} in {owner[key]} and {name}")
            owner[key] = name
    development = {k for k, name in owner.items() if name == "development"}
    if not set(pilot) <= development:
        raise ValueError("pilot cases must be a subset of the development population")
    days = defaultdict(list)
    for name in sorted(modes):
        for tx in populations[name]:
            key = (tx["tenant_id"], tx["transaction_id"])
            days[utc_day(tx["occurred_at"])].append(
                {
                    "tenant_id": key[0],
                    "transaction_id": key[1],
                    "population": name,
                    "modes": list(modes[name]),
                    "pilot": key in pilot,
                    "transaction": tx,
                }
            )
    return [
        {
            "day": day,
            "cases": sorted(days[day], key=lambda c: (c["tenant_id"], c["transaction_id"])),
        }
        for day in sorted(days)
    ]


def _runtime(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line]


def load_populations(bundle: Path, baseline_bundle: Path) -> tuple[dict, dict, dict]:
    """Frozen runtime documents, after verifying both bundle identities and checksums."""
    from reckoner.v1.data.prepare import verify_preparation

    bundle, baseline_bundle = Path(bundle), Path(baseline_bundle)
    index = verify_preparation(bundle)
    baseline = verify_bundle(baseline_bundle)
    if baseline["bundle_id"] != index["baseline_bundle_id"]:
        raise ValueError("baseline bundle identity mismatch")
    loaded = {}
    for purpose in ("development", "pilot", "validation"):
        loaded[purpose] = {
            "sample_id": index["samples"][purpose]["sample_id"],
            "transactions": _runtime(bundle / index["files"][f"runtime_{purpose}"]["path"]),
        }
    cohort = baseline["cohorts"]["baseline"]
    loaded["cohort-2019"] = {
        "sample_id": cohort["cohort_id"],
        "transactions": _runtime(
            baseline_bundle / baseline["files"][cohort["runtime_file"]]["path"]
        ),
    }
    return index, baseline, loaded


def query_schedule(bundle: Path, baseline_bundle: Path, populations: dict) -> list[dict]:
    _, _, loaded = load_populations(bundle, baseline_bundle)
    unknown = set(populations) - {"development", "validation", "cohort-2019"}
    if unknown or any(set(m) - set(MODES) or not m for m in populations.values()):
        raise ValueError(f"unsupported populations or modes: {populations}")
    development = {
        (tx["tenant_id"], tx["transaction_id"]): tx for tx in loaded["development"]["transactions"]
    }
    pilot = set()
    for tx in loaded["pilot"]["transactions"]:
        key = (tx["tenant_id"], tx["transaction_id"])
        if development.get(key) != tx:
            raise ValueError("pilot cases must be a subset of the development population")
        pilot.add(key)
    return schedule_cases(
        {name: loaded[name]["transactions"] for name in populations},
        populations,
        pilot=pilot if "development" in populations else set(),
    )


# --- declaration ----------------------------------------------------------------


def source_snapshot_id(bundle_id: str, source_sha256: str, entity_manifest_id: str, policy=POLICY):
    return content_id(
        {
            "working_set": "rolling-97d-v1",
            "bundle_id": bundle_id,
            "source_sha256": source_sha256,
            "entity_manifest_id": entity_manifest_id,
            "resolution_policy": policy,
        }
    )


def _cases_id(cases) -> str:
    return content_id(sorted([c["tenant_id"], c["transaction_id"]] for c in cases))


def declare_preparation(
    *, bundle, baseline_bundle, scaler, entity_manifest_id, schedule, populations
) -> dict:
    """Immutable declaration; its identity changes with any input, never with staging."""
    index, baseline, loaded = load_populations(bundle, baseline_bundle)
    if scaler["scaler_id"] != content_id({k: v for k, v in scaler.items() if k != "scaler_id"}):
        raise ValueError("scaler identity mismatch")
    cases = [c for entry in schedule for c in entry["cases"]]
    summary = {}
    for name in sorted(populations):
        members = [c for c in cases if c["population"] == name]
        summary[name] = {
            "sample_id": loaded[name]["sample_id"],
            "modes": list(populations[name]),
            "case_count": len(members),
            "cases_id": _cases_id(members),
        }
    if "development" in populations:
        members = [c for c in cases if c["pilot"]]
        summary["pilot"] = {
            "sample_id": loaded["pilot"]["sample_id"],
            "modes": list(populations["development"]),
            "case_count": len(members),
            "cases_id": _cases_id(members),
            "parent": "development",
        }
    body = {
        "schema_version": "reckoner-evidence-preparation-v1",
        "dataset_simulated": True,
        "provider_calls": 0,
        "bundle_id": index["bundle_id"],
        "baseline_bundle_id": baseline["bundle_id"],
        "source_sha256": index["source_sha256"],
        "scaler_id": scaler["scaler_id"],
        "feature_version": scaler["feature_version"],
        "entity_manifest_id": entity_manifest_id,
        "resolution_policy_version": POLICY,
        "windows": {
            "history_days": 30,
            "resolved_days": 90,
            "working_set_days": WORKING_SET_DAYS,
            "graph_window_days": 30,
        },
        "tenants": list(TENANTS),
        "populations": summary,
        "source_snapshot_id": source_snapshot_id(
            index["bundle_id"], index["source_sha256"], entity_manifest_id
        ),
    }
    return {**body, "preparation_id": content_id(body)}


# --- entity manifest ------------------------------------------------------------


def _first_observations(connection, column: str) -> dict:
    rows = connection.execute(
        f"SELECT {column}, min(occurred_at || '|' || printf('%012d', source_record)) "  # noqa: S608
        f"FROM history GROUP BY {column}"
    )
    return {key: (value[:-13], int(value[-12:])) for key, value in rows}


def entity_manifest(bundle: Path, source: Path) -> dict:
    """Every retained entity with its full-history first observation (privileged reader).

    Card ownership and the tenant-free merchant identity come from the first-observation
    source row, read at its recorded source offset; every derived identity is recomputed
    and must equal the indexed one.
    """
    from reckoner.v1.data.history import SourceHistory

    with SourceHistory(bundle, source) as history:
        connection = history.connection
        names = {
            key: (tenant, kind, identity)
            for key, tenant, kind, identity in connection.execute(
                "SELECT entity_key, tenant_id, kind, identity FROM entities"
            )
        }
        first = {}
        for column in ("account_key", "card_key", "merchant_key"):
            first.update(_first_observations(connection, column))
        if set(first) != set(names):
            raise ValueError("entity index and history disagree")
        offsets = dict(
            connection.execute(
                "SELECT source_record, source_offset FROM history WHERE source_record IN "
                "(SELECT value FROM json_each(?))",
                (json.dumps(sorted({record for _, record in first.values()})),),
            ).fetchall()
        )
        path = history.source / history.index["transaction_source"]
        entities = []
        with path.open("rb") as stream:
            headers = next(csv.reader([stream.readline().decode("utf-8-sig")]))
            for key in sorted(names, key=lambda k: names[k]):
                tenant, kind, identity = names[key]
                observed, record = first[key]
                stream.seek(offsets[record])
                raw = dict(
                    zip(
                        headers,
                        next(csv.reader(line.decode("utf-8") for line in stream)),
                        strict=True,
                    )
                )
                user, card = raw["User"].strip(), raw["Card"].strip()
                merchant = raw["Merchant Name"].strip()
                base = {"dataset": "cctd", "entity": kind, "tenant_id": tenant}
                derived = {
                    "account": {**base, "source_user": user},
                    "card": {**base, "source_user": user, "source_card": card},
                    "merchant": {**base, "source_merchant": merchant},
                }[kind]
                if content_id(derived) != identity:
                    raise ValueError(f"entity identity does not match its source row: {key}")
                entity = {
                    "tenant_id": tenant,
                    "kind": kind,
                    "identity": identity,
                    "key": content_id([tenant, kind, identity]),
                    "first_observed_at": observed,
                    "first_source_record": record,
                }
                if kind == "card":
                    entity["account_identity"] = content_id(
                        {
                            "dataset": "cctd",
                            "entity": "account",
                            "tenant_id": tenant,
                            "source_user": user,
                        }
                    )
                if kind == "merchant":
                    entity["shared_identity"] = content_id(
                        {"dataset": "cctd", "entity": "merchant", "source_merchant": merchant}
                    )
                entities.append(entity)
        body = {
            "schema_version": "reckoner-entity-manifest-v1",
            "dataset_simulated": True,
            "bundle_id": history.index["bundle_id"],
            "source_sha256": history.index["source_sha256"],
            "counts": dict(sorted(Counter(e["kind"] for e in entities).items())),
            "entities": entities,
        }
    return {**body, "entity_manifest_id": content_id(body)}


def reconcile_entity_manifest(ours: dict, prior: dict) -> dict:
    """Compare entity sets and first observations with an earlier (Task 3) manifest."""
    mine = {
        (e["tenant_id"], e["kind"], e["identity"]): (
            e["first_observed_at"],
            e["first_source_record"],
        )
        for e in ours["entities"]
    }
    theirs = {
        (e["tenant_id"], e["kind"], e["identity"]): (e["occurred_at"], e["source_record"])
        for e in prior["entities"]
    }
    shared = set(mine) & set(theirs)
    mismatched = sorted(k for k in shared if mine[k] != theirs[k])
    return {
        "ours": len(mine),
        "prior": len(theirs),
        "matching": len(shared) - len(mismatched),
        "first_observation_mismatches": len(mismatched),
        "mismatch_examples": [list(k) for k in mismatched[:20]],
        "only_in_ours": len(set(mine) - set(theirs)),
        "only_in_prior": len(set(theirs) - set(mine)),
    }


# --- missing-reason policy --------------------------------------------------------


def check_documents(day: str, items) -> dict:
    """Persist only when every reason is data-intrinsic; otherwise fail the whole day."""
    failures, intrinsic, statuses = [], Counter(), Counter()
    boundary = iso(day_start(day))
    for case, mode, document in items:
        coverage = document["coverage"]
        reasons = list(coverage["missing"])
        allowed = INTRINSIC if mode == "gds-augmented" else frozenset()
        faults = [reason for reason in reasons if reason not in allowed]
        if coverage["status"] == "unavailable" and not faults:
            faults.append("coverage unavailable")
        projection = document.get("graph_projection")
        if mode == "gds-augmented" and (projection is None or projection["cutoff"] != boundary):
            faults.append("graph projection cutoff is not the query day boundary")
        if mode == "relational" and projection is not None:
            faults.append("relational evidence carries a graph projection")
        if faults:
            failures.append(
                {
                    "tenant_id": case["tenant_id"],
                    "transaction_id": case["transaction_id"],
                    "mode": mode,
                    "reasons": sorted(set(faults)),
                }
            )
        else:
            intrinsic.update(reasons)
            statuses[coverage["status"]] += 1
    if failures:
        raise PreparationFault(day, failures)
    return {"intrinsic": dict(intrinsic), "statuses": dict(statuses)}


# --- resume -----------------------------------------------------------------


def _matches(fact: dict, mode: str, snapshot: str, day: str) -> bool:
    if fact["mode"] != mode or fact["snapshot"] != snapshot:
        return False
    if mode == "gds-augmented":
        return fact["graph_cutoff"] == iso(day_start(day))
    return fact["graph_cutoff"] is None


def _done(entry, facts, mode, snapshot):
    present = defaultdict(list)
    for fact in facts:
        if _matches(fact, mode, snapshot, entry["day"]):
            present[(fact["tenant_id"], fact["transaction_id"])].append(fact)
    expected = [(c["tenant_id"], c["transaction_id"]) for c in entry["cases"] if mode in c["modes"]]
    return all(present[key] for key in expected), present


def plan_run(schedule, facts, *, receipts, mode, through, snapshot) -> dict:
    """The persisted documents are the truth; receipts are reconstructed when missing."""
    recorded = {r["day"] for r in receipts}
    plan = {"complete": [], "reconstruct": [], "pending": []}
    for entry in schedule:
        if entry["day"] > through or not any(mode in c["modes"] for c in entry["cases"]):
            continue
        complete, _ = _done(entry, facts, mode, snapshot)
        if complete:
            if plan["pending"]:
                raise ValueError(f"{entry['day']} is complete after an incomplete earlier day")
            plan["complete"].append(entry["day"])
            if entry["day"] not in recorded:
                plan["reconstruct"].append(entry["day"])
        else:
            plan["pending"].append(entry["day"])
    return plan


def reconstruct_receipt(entry, facts, *, mode) -> dict:
    ids = sorted(
        f["evidence_id"]
        for f in facts
        if f["mode"] == mode
        and (f["tenant_id"], f["transaction_id"])
        in {(c["tenant_id"], c["transaction_id"]) for c in entry["cases"] if mode in c["modes"]}
    )
    return {
        "day": entry["day"],
        "pass": mode,
        "cases": sum(mode in c["modes"] for c in entry["cases"]),
        "evidence_ids": ids,
        "receipt_reconstructed": True,
    }


def projection_action(receipts, tenants, metric_count, *, referenced) -> str:
    """Per-tenant receipt creation is not atomic: rebuild a partial one only if unused."""
    if not receipts:
        return "build"
    ids = {r["projection_id"] for r in receipts}
    consistent = (
        sorted(r["tenant_id"] for r in receipts) == sorted(tenants)
        and len(ids) == 1
        and all(metric_count == r["node_count"] for r in receipts)
    )
    if consistent:
        return "reuse"
    return "refuse" if referenced else "rebuild"


# --- publication --------------------------------------------------------------


def document_mode(document: dict) -> str:
    return "gds-augmented" if document.get("graph_projection") is not None else "relational"


def _entry(document: dict) -> dict:
    projection = document.get("graph_projection") or {}
    return {
        "tenant_id": document["tenant_id"],
        "transaction_id": document["transaction_id"],
        "evidence_id": document["evidence_id"],
        "coverage_status": document["coverage"]["status"],
        "missing": document["coverage"]["missing"],
        "projection_id": document["source_snapshot_ids"]["graph"],
        "page_rank_converged": projection.get("page_rank_converged"),
        "snapshot_age_seconds": projection.get("snapshot_age_seconds"),
    }


def _write_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
        os.chmod(path, 0o644)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise FileExistsError(f"refusing to overwrite a different manifest: {path}") from None


def build_manifests(declaration, schedule, documents, *, mode) -> list[dict]:
    snapshot = declaration["source_snapshot_id"]
    cases = [c for entry in schedule for c in entry["cases"] if mode in c["modes"]]
    keyed = defaultdict(list)
    for document in documents:
        if document_mode(document) == mode and (
            document["source_snapshot_ids"]["postgres"] == snapshot
        ):
            keyed[(document["tenant_id"], document["transaction_id"])].append(document)
    expected = {(c["tenant_id"], c["transaction_id"]) for c in cases}
    missing = sorted(expected - set(keyed))
    extra = sorted(set(keyed) - expected)
    duplicate = sorted(k for k, v in keyed.items() if len({d["evidence_id"] for d in v}) > 1)
    for problem, keys in (("missing", missing), ("extra", extra), ("duplicate", duplicate)):
        if keys:
            raise ValueError(f"refusing to publish: {problem} cases {[list(k) for k in keys[:20]]}")
    manifests = []
    groups = defaultdict(list)
    for case in cases:
        groups[case["population"]].append(case)
        if case["pilot"]:
            groups["pilot"].append(case)
    for population in sorted(groups):
        members = sorted(groups[population], key=lambda c: (c["tenant_id"], c["transaction_id"]))
        declared = declaration["populations"][population]
        body = {
            "schema_version": "reckoner-evidence-manifest-v1",
            "dataset_simulated": True,
            "preparation_id": declaration["preparation_id"],
            "source_snapshot_id": snapshot,
            "population": population,
            "mode": mode,
            "sample_id": declared["sample_id"],
            "case_count": len(members),
            "cases": [_entry(keyed[(c["tenant_id"], c["transaction_id"])][0]) for c in members],
        }
        if "parent" in declared:
            body["parent_population"] = declared["parent"]
        manifests.append({**body, "manifest_id": content_id(body)})
    return manifests


def publish(declaration, schedule, documents, output_dir: Path, *, mode) -> list[dict]:
    """Content-addressed manifests; every expected case exactly once, never overwritten."""
    import hashlib

    written = []
    manifests = build_manifests(declaration, schedule, documents, mode=mode)
    for manifest in manifests:
        payload = canonical_json(manifest)
        path = Path(output_dir) / "manifests" / f"{manifest['population']}-{mode}.json"
        _write_once(path, payload)
        written.append(
            {
                "population": manifest["population"],
                "mode": mode,
                "manifest_id": manifest["manifest_id"],
                "sha256": hashlib.sha256(payload).hexdigest(),
                "cases": manifest["case_count"],
                "path": str(path.relative_to(output_dir)),
            }
        )
    return written
