"""Bounded hash selection of simulated, time-valid class strata."""

import hashlib
import heapq
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from reckoner.v1.data.history import instant

SEED = 20260930
LIMITS = {"fraud": 200, "legitimate": 1800}


def selection_key(purpose: str, transaction_id: str, seed: int = SEED):
    return hashlib.sha256(f"{seed}:{purpose}:{transaction_id}".encode()).hexdigest(), transaction_id


class Candidate:
    def __init__(self, key, record):
        self.key, self.record = key, record

    def __lt__(self, other):
        return self.key > other.key


class Selection:
    def __init__(self, year: int, seed: int, excluded_ids: set[str]):
        if year not in {2017, 2018}:
            raise ValueError("experiment year must be 2017 or 2018")
        self.year, self.seed, self.excluded_ids = year, seed, excluded_ids
        self.purpose = "development" if year == 2017 else "validation"
        self.counts, self.exclusions = Counter(), Counter()
        self.heaps = {label: [] for label in LIMITS}
        self.cutoff = datetime(year + 1, 1, 1, tzinfo=UTC)

    def add(self, record: dict):
        tx, oracle = record["transaction"], record["oracle"]
        occurred = instant(tx["occurred_at"])
        if occurred.year != self.year:
            return
        if tx["transaction_id"] in self.excluded_ids:
            self.exclusions["prior_pilot"] += 1
            return
        if occurred + timedelta(days=7) >= self.cutoff:
            self.exclusions["unresolved"] += 1
            return
        label = oracle["label"]
        if label not in LIMITS:
            raise ValueError("invalid oracle label")
        self.counts[label] += 1
        heap = self.heaps[label]
        heapq.heappush(
            heap, Candidate(selection_key(self.purpose, tx["transaction_id"], self.seed), record)
        )
        if len(heap) > LIMITS[label]:
            heapq.heappop(heap)

    def finish(self):
        for label, limit in LIMITS.items():
            if self.counts[label] < limit:
                raise ValueError(f"insufficient {label} for {self.purpose}: {self.counts[label]}")
        selected = sorted(
            (candidate for heap in self.heaps.values() for candidate in heap),
            key=lambda candidate: candidate.key,
        )
        return {
            "purpose": self.purpose,
            "year": self.year,
            "seed": self.seed,
            "selected": [candidate.record for candidate in selected],
            "strata": {
                label: {
                    "N_h": self.counts[label],
                    "n_h": limit,
                    "weight": str(Decimal(self.counts[label]) / Decimal(limit)),
                }
                for label, limit in LIMITS.items()
            },
            "exclusions": dict(sorted(self.exclusions.items())),
        }


def select_sample(records: Iterable[dict], *, year: int, seed: int, excluded_ids: set[str]) -> dict:
    selection = Selection(year, seed, excluded_ids)
    for record in records:
        selection.add(record)
    return selection.finish()
