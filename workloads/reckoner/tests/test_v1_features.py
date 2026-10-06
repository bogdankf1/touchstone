import math
import sqlite3
from datetime import timedelta

import pytest
from reckoner.v1.data.history import SourceHistory, instant
from reckoner.v1.evidence.features import feature_vector, fit_scaler, history_features
from test_v1_history import transaction


def observation(amount, weight=1, purpose="development"):
    tx = transaction()
    tx["amount_minor"] = amount * 100
    return {
        "transaction": tx,
        "history": {"status": "available", "transactions": [], "previous": None},
        "weight": weight,
        "purpose": purpose,
    }


def test_weighted_development_scaler_and_eleven_coordinates():
    scaler = fit_scaler([observation(1), observation(9, 10), observation(99)])
    assert scaler["median"][0] == math.log1p(9)
    assert scaler["iqr"][0] == 1
    tx = transaction()
    tx["amount_minor"] = 900
    tx["payment_channel"] = "online"
    vector = feature_vector(tx, observation(1)["history"], scaler)
    assert len(vector) == 11
    assert vector[0] == 0
    assert vector[2:4] == [0, 0]
    assert vector[4:9] == [0, 0, 1, 0, 0]
    assert vector[-2:] == [1, 1]
    with pytest.raises(ValueError, match="development"):
        fit_scaler([observation(999, purpose="validation")])


def test_history_filters_strict_time_card_and_window_and_future_invariance():
    tx = transaction()
    tx["occurred_at"] = "2018-01-01T00:00:00.500000Z"

    def prior(identity, time, amount=1000, card=None):
        return {
            **tx,
            "transaction_id": identity,
            "occurred_at": time,
            "amount_minor": amount,
            "card_id": card or tx["card_id"],
        }

    past = prior("past", "2018-01-01T00:00:00Z")
    future = prior("future", "2018-01-01T00:00:00.600000Z")
    equal = prior("equal", tx["occurred_at"])
    lower = (instant(tx["occurred_at"]) - timedelta(days=30)).isoformat()
    boundary = prior("lower", lower, 2000)
    history = {"status": "available", "transactions": [boundary, past], "previous": past}
    values = history_features(tx, history)
    assert values["prior_24h_count"] == 1
    assert values["prior_30d_mean_usd"] == 15
    assert values["seconds_since_previous"] == 0.5
    scaler = fit_scaler([observation(1), observation(2)])
    assert feature_vector(tx, history, scaler) == feature_vector(
        tx,
        {
            **history,
            "transactions": [
                future,
                equal,
                prior("other", past["occurred_at"], card="other"),
                past,
                boundary,
            ],
        },
        scaler,
    )
    with pytest.raises(ValueError, match="unavailable"):
        feature_vector(tx, {"status": "unavailable"}, scaler)


def test_source_reader_fractional_cutoff_and_lower_bound_without_rewriting_bundle():
    reader = SourceHistory.__new__(SourceHistory)
    reader.connection = sqlite3.connect(":memory:")
    reader.connection.executescript(
        "CREATE TABLE entities(entity_key TEXT, identity TEXT,tenant_id TEXT,kind TEXT);"
        "CREATE TABLE history(source_record INTEGER,tenant_id TEXT,card_key TEXT,occurred_at TEXT);"
        "INSERT INTO entities VALUES('c','card','tenant','card');"
        "INSERT INTO history VALUES(9,'tenant','c','2018-01-01T00:00:00Z');"
        "INSERT INTO history VALUES(2,'tenant','c','2018-01-01T00:00:01Z');"
    )
    reader.transaction = lambda record: record
    assert list(reader.card_before("tenant", "card", "2018-01-01T00:00:00.5Z")) == [9]
    assert reader.previous_card("tenant", "card", "2018-01-01T00:00:00.5Z") == 9
    assert list(
        reader.card_before(
            "tenant", "card", "2018-01-01T00:00:01.5Z", since="2018-01-01T00:00:00.5Z"
        )
    ) == [2]


def test_wrong_scaler_and_negative_amount_are_explicitly_rejected():
    scaler = fit_scaler([observation(1), observation(2)])
    tx = transaction()
    broken = {**scaler, "median": [999, *scaler["median"][1:]]}
    with pytest.raises(ValueError, match="identity"):
        feature_vector(tx, observation(1)["history"], broken)
    tx["amount_minor"] = -200
    with pytest.raises(ValueError, match="negative"):
        feature_vector(tx, observation(1)["history"], scaler)


def test_scaler_unavailable_history_keeps_known_amount_coordinate():
    unknown = observation(99, weight=10)
    unknown["history"] = {"status": "unavailable"}
    scaler = fit_scaler([observation(1), observation(9), unknown])
    assert scaler["median"][0] == math.log1p(99)
    assert scaler["median"][1] == 0
    assert scaler["median"][2:4] == [0, 0]
