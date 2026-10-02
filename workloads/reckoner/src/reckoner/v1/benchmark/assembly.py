"""Offline evaluator assembly: label join, relevance proxy, replacement and report body."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from reckoner.contracts import content_id
from reckoner.v1.benchmark.protocol import REPLACEMENT_SCHEMA, check_protocol, check_report_id
from reckoner.v1.benchmark.queries import distribution
from reckoner.v1.benchmark.report import MEASUREMENT_MODES, RETRIEVAL_SCHEMA
from reckoner.v1.benchmark.resources import sample_maxima

ANNOTATION_SCHEMA = "retrieval-report-annotations-v1"
ANNOTATION_KEYS = {
    "schema_version",
    "limitations",
    "relevance_interpretation",
    "plan_observation",
    "resource_accounting_stage",
    "first_pass_protocol",
}


def _check_annotations(annotations: dict) -> dict:
    if annotations.get("schema_version") != ANNOTATION_SCHEMA:
        raise ValueError("annotations require schema_version " + ANNOTATION_SCHEMA)
    if set(annotations) - ANNOTATION_KEYS:
        unknown = sorted(set(annotations) - ANNOTATION_KEYS)
        raise ValueError("unknown annotation fields: " + ", ".join(unknown))
    limitations = annotations.get("limitations")
    if not limitations or not all(isinstance(x, str) and x for x in limitations):
        raise ValueError("annotations require nonempty limitations")
    if not isinstance(annotations.get("relevance_interpretation"), str):
        raise ValueError("annotations require relevance_interpretation")
    return annotations


def _annotation(annotations, key):
    if not isinstance(annotations.get(key), str) or not annotations[key]:
        raise ValueError(f"annotations require {key}")
    return annotations[key]


def _agreement(result, label):
    return [(c["verdict"] == "decline") == (label == "fraud") for c in result["top_five"]]


def join_labels(vector_results: list[dict], labels: dict) -> list[dict]:
    """Evaluator join after retrieval; labels never enter a runtime query."""
    joined = []
    for result in vector_results:
        label = labels.get(result["query_id"])
        if label is None:
            raise ValueError("missing evaluator label for a measured query")
        if result.get("query_label", label) != label:
            raise ValueError("observation label disagrees with evaluator labels")
        matches = _agreement(result, label) if result["top_five"] else None
        joined.append(
            {
                **result,
                "query_label": label,
                "label_agreement": sum(matches) / len(matches) if matches else None,
            }
        )
    return joined


def relevance_proxy(vector_results: list[dict], interpretation: str) -> dict:
    returned = [r for r in vector_results if r["top_five"]]

    def counts(rows):
        pairs = sum(len(r["top_five"]) for r in rows)
        return pairs, sum(sum(_agreement(r, r["query_label"])) for r in rows)

    pairs, matches = counts(returned)
    fraud = counts([r for r in returned if r["query_label"] == "fraud"])
    legitimate = counts([r for r in returned if r["query_label"] == "legitimate"])
    return {
        "empty_queries": sum(r["top_five"] == [] for r in vector_results),
        "unavailable_queries": sum(r["top_five"] is None for r in vector_results),
        "returned_pairs": pairs,
        "matching_pairs": matches,
        "fraction": matches / pairs if pairs else None,
        "fraud_query_returned": fraud[0],
        "fraud_query_matches": fraud[1],
        "legitimate_query_returned": legitimate[0],
        "legitimate_query_matches": legitimate[1],
        "interpretation": interpretation,
    }


def apply_replacement(body: dict, receipt: dict, first_pass_protocol: str) -> None:
    if receipt.get("schema_version") != REPLACEMENT_SCHEMA or receipt.get(
        "receipt_id"
    ) != content_id({k: v for k, v in receipt.items() if k != "receipt_id"}):
        raise ValueError("replacement receipt content does not match receipt_id")
    if receipt["protocol_id"] != body["protocol_id"]:
        raise ValueError("replacement belongs to a different protocol")
    rows = receipt["queries"]
    if [r["transaction_id"] for r in rows] != [q["transaction_id"] for q in body["queries"]]:
        raise ValueError("replacement query order differs from observation")
    for row, query in zip(rows, body["queries"], strict=True):
        for name in ("sql", "cypher"):
            if row[name + "_members_hash"] != query[name + "_members_hash"]:
                raise ValueError("replacement membership differs from measured observation")
    body["replacement_receipt_id"] = receipt["receipt_id"]
    body["cache_protocol"]["original_first_pass_valid"] = False
    body["cache_protocol"]["first_pass"] = first_pass_protocol
    body["invalid_original_first_pass_ms"] = {
        name: body["latency_ms"][name]["first_pass"] for name in ("sql", "cypher")
    }
    for name in ("sql", "cypher"):
        body["latency_ms"][name]["first_pass"] = distribution([r[name + "_ms"] for r in rows])
        for row, query in zip(rows, body["queries"], strict=True):
            query["timings"][name + "_ms"][0] = row[name + "_ms"]


def artifact_record(path: Path) -> dict:
    digest, size, newlines, last = sha256(), 0, 0, b""
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
            newlines += chunk.count(b"\n")
            last = chunk[-1:]
    record = {"sha256": digest.hexdigest(), "bytes": size}
    if Path(path).suffix == ".jsonl":
        record["rows"] = newlines + (1 if size and last != b"\n" else 0)
    return record


def assemble_report(
    observation,
    protocol,
    *,
    labels,
    annotations,
    artifacts,
    replacement=None,
    resources=None,
    cgroup=None,
    samples=None,
):
    """Evaluator assembly of stored observations; annotations are copied verbatim."""
    check_protocol(protocol)
    _check_annotations(annotations)
    body = deepcopy(check_report_id(observation))
    if (
        body.get("schema_version") != RETRIEVAL_SCHEMA
        or body.get("measurement_mode") not in MEASUREMENT_MODES
    ):
        raise ValueError("observation needs a labelled retrieval benchmark measurement_mode")
    if body.get("protocol_id") != protocol["protocol_id"] or body.get("query_ids") != [
        q["transaction_id"] for q in protocol["queries"]
    ]:
        raise ValueError("observation does not belong to the frozen protocol")
    if "report_id" in observation:
        body["source_report_id"] = observation["report_id"]
    body["vector_results"] = join_labels(body.get("vector_results", []), labels)
    body["vector_relevance_proxy"] = relevance_proxy(
        body["vector_results"], annotations["relevance_interpretation"]
    )
    body["limitations"] = list(annotations["limitations"])
    if "plan_observation" in annotations:
        body["plan_observation"] = _annotation(annotations, "plan_observation")
    if replacement is not None:
        apply_replacement(body, replacement, _annotation(annotations, "first_pass_protocol"))
    if resources is not None:
        body["resources"] = {
            **resources,
            "accounting_stage": _annotation(annotations, "resource_accounting_stage"),
        }
    if cgroup is not None:
        body["cgroup_current_lifetime"] = cgroup
    if samples:
        body["service_sample_maximum_bytes"] = {
            name: sample_maxima(rows) for name, rows in samples.items()
        }
    body["artifacts"] = dict(sorted(artifacts.items()))
    return body
