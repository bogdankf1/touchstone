import random

import pytest
from reckoner.v1.data.sampling import select_sample


def records(year=2017, fraud=205, legitimate=1805):
    for label, count in [("fraud", fraud), ("legitimate", legitimate)]:
        for n in range(count):
            yield {
                "transaction": {
                    "tenant_id": "tenant-a",
                    "transaction_id": f"{year}-{label}-{n}",
                    "occurred_at": f"{year}-01-01T00:00:00Z",
                },
                "oracle": {"label": label},
            }


def test_selection_is_order_independent_weighted_and_excludes_prior_pilots():
    data = list(records())
    excluded = {"2017-fraud-0", "2017-legitimate-0"}
    first = select_sample(data, year=2017, seed=20260930, excluded_ids=excluded)
    random.Random(12).shuffle(data)
    second = select_sample(data, year=2017, seed=20260930, excluded_ids=excluded)
    assert first == second
    assert len(first["selected"]) == 2000
    assert first["strata"]["fraud"] == {"N_h": 204, "n_h": 200, "weight": "1.02"}
    assert {x["transaction"]["transaction_id"] for x in first["selected"]}.isdisjoint(excluded)


def test_unresolved_late_year_and_other_years_are_excluded():
    data = list(records()) + list(records(2018))
    late = {
        "transaction": {
            "tenant_id": "tenant-a",
            "transaction_id": "late",
            "occurred_at": "2017-12-25T00:00:00Z",
        },
        "oracle": {"label": "fraud"},
    }
    first = select_sample(data + [late], year=2017, seed=20260930, excluded_ids=set())
    second = select_sample(data, year=2018, seed=20260930, excluded_ids=set())
    assert first["exclusions"]["unresolved"] == 1
    assert {x["transaction"]["transaction_id"] for x in first["selected"]}.isdisjoint(
        x["transaction"]["transaction_id"] for x in second["selected"]
    )


def test_insufficient_fraud_fails():
    with pytest.raises(ValueError, match="insufficient fraud"):
        select_sample(records(fraud=199), year=2017, seed=20260930, excluded_ids=set())
