"""Observed, deterministic risk indicators; never model attribution."""

VERSION = "risk-indicators-v1"


def risk_indicators(transaction: dict, history: dict, neighbours: dict) -> list[dict]:
    factors = []

    def add(identity, severity, description, formula, refs):
        factors.append(
            (
                severity,
                identity,
                {
                    "indicator_id": identity,
                    "description": description,
                    "method": f"{VERSION}: {formula}; severity={severity:.12g}",
                    "evidence_refs": refs,
                },
            )
        )

    if history["status"] == "available":
        count = history.get("prior_30d_count", 0)
        mean = history.get("prior_30d_mean_usd")
        ratio = transaction["amount_minor"] / 100 / mean if mean and mean > 0 else None
        if count >= 5 and ratio is not None and ratio >= 3:
            add(
                "amount-ratio",
                min(ratio / 10, 1),
                f"USD amount is {ratio:.12g} times the mean of {count} "
                "prior 30-day card purchases.",
                "count>=5 and amount/mean>=3; min(ratio/10,1)",
                history["evidence_refs"],
            )
        count = history.get("prior_24h_count", 0)
        if count >= 5:
            add(
                "card-burst",
                min(count / 20, 1),
                f"{count} prior 24-hour card transactions.",
                "count>=5; min(count/20,1)",
                history["evidence_refs"],
            )
    if neighbours["status"] == "available":
        count, fraud = neighbours.get("resolved_count", 0), neighbours.get("fraud_count", 0)
        if count >= 20 and fraud >= 1:
            add(
                "merchant-exposure",
                min(5 * fraud / count, 1),
                f"Shared merchant has {fraud} fraud among {count} eligible 90-day resolved cases.",
                "resolved>=20 and fraud>=1; min(5*fraud/resolved,1)",
                neighbours["evidence_refs"],
            )
    return [
        {**factor, "rank": rank}
        for rank, (_, _, factor) in enumerate(sorted(factors, key=lambda f: (-f[0], f[1]))[:3], 1)
    ]
