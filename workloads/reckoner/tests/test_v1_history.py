from copy import deepcopy

import pytest
from reckoner.data.adapter import adapt_row
from reckoner.v1.data.history import eligible_before, historical_resolution
from test_cohort import row


def transaction():
    source = row("user", 2017, "Yes", 1)
    source.update(Month="12", Day="25", Time="00:00")
    return adapt_row(source, source_sha256="a" * 64, source_record=1, tenant_id="tenant-a")[
        "transaction"
    ]


def test_seven_day_resolution_has_strict_availability_and_is_validated():
    tx = transaction()
    resolved = historical_resolution(tx, "fraud", "simulated-seven-days-v1")
    assert resolved["resolved_at"] == "2018-01-01T00:00:00Z"
    assert resolved["verdict"] == "decline"
    assert not eligible_before(tx["occurred_at"], resolved["resolved_at"], resolved["resolved_at"])
    assert eligible_before(tx["occurred_at"], resolved["resolved_at"], "2018-01-01T00:00:01Z")
    earlier = deepcopy(tx)
    earlier["occurred_at"] = "2017-12-24T23:59:59Z"
    resolution = historical_resolution(earlier, "legitimate", "simulated-seven-days-v1")
    assert eligible_before(
        earlier["occurred_at"], resolution["resolved_at"], resolved["resolved_at"]
    )


def test_equal_source_time_is_excluded_and_offsets_compare_as_instants():
    assert not eligible_before(
        "2018-01-01T00:00:00Z", "2017-12-01T00:00:00Z", "2018-01-01T00:00:00Z"
    )
    assert not eligible_before(
        "2017-12-31T22:00:00Z", "2018-01-01T02:00:00+02:00", "2018-01-01T00:00:00Z"
    )


@pytest.mark.parametrize("position", range(3))
def test_naive_inputs_are_rejected(position):
    times = ["2017-01-01T00:00:00Z", "2017-01-08T00:00:00Z", "2018-01-01T00:00:00Z"]
    times[position] = times[position].removesuffix("Z")
    with pytest.raises(ValueError, match="timezone"):
        eligible_before(*times)


def test_oracle_label_flip_cannot_change_canonical_transaction():
    source = row("user", 2017, "Yes", 1)
    first = adapt_row(source, source_sha256="a" * 64, source_record=1, tenant_id="tenant-a")
    source["Is Fraud?"] = "No"
    changed = adapt_row(source, source_sha256="a" * 64, source_record=1, tenant_id="tenant-a")
    assert first["transaction"] == changed["transaction"]
    assert first["oracle"]["label"] != changed["oracle"]["label"]
