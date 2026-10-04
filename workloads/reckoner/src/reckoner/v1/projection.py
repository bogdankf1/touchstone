"""Bounded provider-request projection of persisted evidence (`bounded-evidence-v1`).

Only what is sent to a provider is bounded; persisted evidence documents are never
changed, and note validation still checks claims against the full persisted evidence.
Every bound is visible in the request:

- A risk indicator keeps its rank, id, description (observed values) and method (formula
  and severity). It sends `evidence_ref_count` (the full count), `evidence_refs_sha256`
  (`content_id` of the full reference list in ascending lexicographic order, the SHA-256
  of its canonical JSON array), the first `REF_SAMPLE` references in that same ascending
  order as exemplars, and `evidence_refs_truncated`.
- Comparable cases are already a bounded display (top 5 by vector distance, one
  resolution reference each). The projection refuses anything larger rather than
  silently dropping entries.

`REF_SAMPLE` = 32. On 2026-10-04, across the 2,205 persisted documents (2017 development
and 2018 validation), indicators cite 5 to 18,551 references (p50 149). With three
truncated indicators, the worst-case Jev request is about 11 KB. That is a third of the
32,000 conservative bound, and below 25,600 (32,000 / 1.25) even at one token per byte.
"""

from copy import deepcopy

from reckoner.contracts import content_id

VERSION = "bounded-evidence-v1"
REF_SAMPLE = 32
COMPARABLE_LIMIT = 5
COMPARABLE_REFS = 1


def bounded_refs(refs: list[str]) -> dict:
    ordered = sorted(refs)
    return {
        "evidence_ref_count": len(ordered),
        "evidence_refs_sha256": content_id(ordered),
        "evidence_refs": ordered[:REF_SAMPLE],
        "evidence_refs_truncated": len(ordered) > REF_SAMPLE,
    }


def indicators(evidence: dict) -> list[dict]:
    return [
        {
            **{k: deepcopy(item[k]) for k in ("rank", "indicator_id", "description", "method")},
            **bounded_refs(item["evidence_refs"]),
        }
        for item in evidence["risk_indicators"]
    ]


def comparables(evidence: dict) -> list[dict]:
    cases = evidence["comparable_cases"]
    if len(cases) > COMPARABLE_LIMIT or any(
        len(case["evidence_refs"]) > COMPARABLE_REFS for case in cases
    ):
        raise ValueError("comparable cases exceed the bounded request projection")
    return deepcopy(cases)
