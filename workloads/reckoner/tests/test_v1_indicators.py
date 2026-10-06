from reckoner.v1.evidence.indicators import risk_indicators


def test_factors_use_supported_counts_and_deterministic_ranks():
    history = {
        "status": "available",
        "prior_30d_count": 5,
        "prior_30d_mean_usd": 10,
        "prior_24h_count": 5,
        "evidence_refs": ["past-card"],
    }
    neighbours = {
        "status": "available",
        "resolved_count": 20,
        "fraud_count": 1,
        "evidence_refs": ["past-merchant"],
    }
    result = risk_indicators({"amount_minor": 3000}, history, neighbours)
    assert [r["indicator_id"] for r in result] == [
        "amount-ratio",
        "card-burst",
        "merchant-exposure",
    ]
    assert [r["rank"] for r in result] == [1, 2, 3]
    assert all("risk-indicators-v1" in r["method"] for r in result)
    assert "5" in result[0]["description"] and "30-day" in result[0]["description"]
    assert result[2]["evidence_refs"] == ["past-merchant"]


def test_missing_or_insufficient_history_never_becomes_a_risk_factor():
    assert (
        risk_indicators(
            {"amount_minor": 999999},
            {"status": "unavailable"},
            {"status": "unavailable", "centrality": 999},
        )
        == []
    )
    assert (
        risk_indicators(
            {"amount_minor": 3000},
            {
                "status": "available",
                "prior_30d_count": 4,
                "prior_30d_mean_usd": 10,
                "prior_24h_count": 4,
            },
            {"status": "available", "resolved_count": 19, "fraud_count": 1},
        )
        == []
    )
