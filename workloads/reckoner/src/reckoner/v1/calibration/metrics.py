"""Weighted diagnostics within a declared simulated population, never all payments."""

from bisect import bisect_right
from decimal import Decimal, InvalidOperation

import numpy as np

EDGES = (0, 0.005, 0.01, 0.02, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 1)
DECIMAL_EDGES = tuple(Decimal(str(edge)) for edge in EDGES)
SEED = 20260930
REPLICATES = 1000
METRICS = ("brier", "log_loss", "ece", "mean_probability", "observed_rate", "effective_n")


def probability(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid probability") from exc
    if not result.is_finite() or not 0 <= result <= 1:
        raise ValueError("probability must be finite and within [0,1]")
    return result


def _arrays(rows):
    seen = set()
    values = []
    for row in rows:
        try:
            if any(
                not isinstance(row[k], str) or not row[k]
                for k in ("tenant_id", "user_id", "case_id")
            ):
                raise ValueError("observation identities required")
            key = (row["tenant_id"], row["case_id"])
            if key in seen:
                raise ValueError("duplicate observation identity")
            seen.add(key)
            if type(row["label"]) is not int or row["label"] not in {0, 1}:
                raise ValueError("binary evaluator label required")
            weight = float(row["weight"])
            if not np.isfinite(weight) or weight <= 0:
                raise ValueError("positive finite inclusion weight required")
            values.append((float(probability(row["probability"])), row["label"], weight))
        except (KeyError, TypeError, OverflowError) as exc:
            raise ValueError("invalid evaluator observation") from exc
    array = np.array(values, dtype=float).reshape(-1, 3)
    return array[:, 0], array[:, 1], array[:, 2]


def _contributions(p, y, w, bins):
    """Additive sufficient statistics allow whole-user resampling without row copies."""
    result = np.zeros((len(p), 6 + 4 * (len(EDGES) - 1)))
    clipped = np.clip(p, 1e-6, 1 - 1e-6)
    result[:, :6] = np.column_stack(
        (
            w,
            w * p,
            w * y,
            w * w,
            w * (p - y) ** 2,
            -w * (y * np.log(clipped) + (1 - y) * np.log1p(-clipped)),
        )
    )
    for i in range(len(EDGES) - 1):
        result[bins == i, 6 + i * 4 : 10 + i * 4] = result[bins == i, :4]
    if not np.all(np.isfinite(result)) or not np.all(np.isfinite(result.sum(axis=0))):
        raise ValueError("inclusion weights overflow diagnostic calculations")
    return result, bins


def _summary(sums):
    total, predicted, observed, squares, brier, loss = sums[:6]
    if total == 0:
        return dict.fromkeys(METRICS), [dict.fromkeys(METRICS[3:]) for _ in EDGES[:-1]]
    buckets = []
    ece = 0.0
    for i in range(len(EDGES) - 1):
        weight, pred, obs, squared = sums[6 + i * 4 : 10 + i * 4]
        bucket = {
            "mean_probability": float(pred / weight) if weight else None,
            "observed_rate": float(obs / weight) if weight else None,
            "effective_n": float(weight * weight / squared) if squared else None,
        }
        if weight:
            ece += abs(pred - obs) / total
        buckets.append(bucket)
    return {
        "brier": float(brier / total),
        "log_loss": float(loss / total),
        "ece": float(ece),
        "mean_probability": float(predicted / total),
        "observed_rate": float(observed / total),
        "effective_n": float(total * total / squares),
    }, buckets


def _intervals(samples, names, reason):
    return {
        "status": "available" if samples else "unavailable",
        "reason": None if samples else reason,
        **{
            name: {
                "lower": float(np.percentile([s[name] for s in samples], 2.5)),
                "upper": float(np.percentile([s[name] for s in samples], 97.5)),
            }
            if samples
            else {"lower": None, "upper": None}
            for name in names
        },
    }


def _population(rows):
    p, y, w = _arrays(rows)
    bins = np.array(
        [
            min(bisect_right(DECIMAL_EDGES, probability(r["probability"])) - 1, len(EDGES) - 2)
            for r in rows
        ],
        dtype=int,
    )
    contributions, bins = _contributions(p, y, w, bins)
    sums = contributions.sum(axis=0)
    summary, bucket_values = _summary(sums)
    clusters = sorted({(r["tenant_id"], r["user_id"]) for r in rows})
    indexes = {key: i for i, key in enumerate(clusters)}
    cluster_sums = np.zeros((len(clusters), contributions.shape[1]))
    for row, values in zip(rows, contributions, strict=True):
        cluster_sums[indexes[(row["tenant_id"], row["user_id"])]] += values
    rng = np.random.default_rng(SEED)
    estimates, bucket_estimates = [], [[] for _ in EDGES[:-1]]
    empty, one_class = 0, 0
    bucket_empty, bucket_one_class = [0] * 10, [0] * 10
    for _ in range(REPLICATES):
        chosen = rng.integers(0, len(clusters), size=len(clusters)) if clusters else []
        replicate = cluster_sums[chosen].sum(axis=0) if clusters else np.zeros_like(sums)
        point, bucket_points = _summary(replicate)
        if replicate[0] == 0:
            empty += 1
        elif replicate[2] == 0 or replicate[2] == replicate[0]:
            one_class += 1
        elif len(clusters) >= 2:
            estimates.append(point)
        for i, bucket in enumerate(bucket_points):
            weight, _, obs, _ = replicate[6 + i * 4 : 10 + i * 4]
            if weight == 0:
                bucket_empty[i] += 1
            elif obs == 0 or obs == weight:
                bucket_one_class[i] += 1
            elif len(clusters) >= 2:
                bucket_estimates[i].append(bucket)
    buckets = []
    for i, values in enumerate(bucket_values):
        labels = y[bins == i]
        count = len(labels)
        buckets.append(
            {
                "lower": EDGES[i],
                "upper": EDGES[i + 1],
                "upper_inclusive": i == 9,
                "status": "available" if count else "unavailable",
                "counts": {
                    "cases": count,
                    "fraud": int(labels.sum()),
                    "legitimate": int(count - labels.sum()),
                },
                "weight_sum": float(sums[6 + i * 4]),
                **values,
                "sparse": count < 30 or labels.sum() < 5 or count - labels.sum() < 5,
                "intervals": _intervals(
                    bucket_estimates[i],
                    METRICS[3:],
                    "insufficient_user_clusters"
                    if len(clusters) < 2
                    else "empty_or_one_class_bucket",
                ),
                "bootstrap": {
                    "empty_replicates": bucket_empty[i],
                    "one_class_replicates": bucket_one_class[i],
                    "estimable_replicates": len(bucket_estimates[i]),
                },
            }
        )
    return {
        "status": "available" if rows else "unavailable",
        **summary,
        "counts": {
            "cases": len(rows),
            "fraud": int(y.sum()),
            "legitimate": int(len(rows) - y.sum()),
            "users": len(clusters),
        },
        "weight_sum": float(sums[0]),
        "buckets": buckets,
        "intervals": _intervals(
            estimates,
            METRICS,
            "insufficient_user_clusters" if len(clusters) < 2 else "empty_or_one_class_population",
        ),
        "bootstrap": {
            "method": "tenant-user-cluster-percentile",
            "seed": SEED,
            "replicates": REPLICATES,
            "percentiles": [2.5, 97.5],
            "empty_replicates": empty,
            "one_class_replicates": one_class,
            "estimable_replicates": len(estimates),
        },
    }


def calibration_metrics(rows: list[dict]) -> dict:
    """Return raw weighted metrics and seeded intervals, overall and per tenant."""
    overall = _population(rows)
    overall["per_tenant"] = {
        tenant: _population([r for r in rows if r["tenant_id"] == tenant])
        for tenant in sorted({r["tenant_id"] for r in rows})
    }
    return overall
