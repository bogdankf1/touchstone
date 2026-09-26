import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from touchstone_platform.extract import (
    RejectedEvent,
    ValidatedDeclaration,
    iter_declarations,
    iter_measurements,
)

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"
CUTOFF = datetime(2026, 9, 26, 12, tzinfo=UTC)


def _example(name):
    return json.loads((EXAMPLES / name).read_text())


def _row(
    event, *, received=CUTOFF, receipt_id="00000000-0000-0000-0000-000000000001", kind="measurement"
):
    attribute = "touchstone.measurement.json" if kind == "measurement" else "touchstone.run.json"
    event_name = (
        f"touchstone.measurement.{event['event_kind']}"
        if kind == "measurement"
        else "touchstone.run.declaration"
    )
    return (
        received,
        receipt_id,
        event["trace_id"] if kind == "measurement" else "trace-declaration",
        event["span_id"] if kind == "measurement" else "span-declaration",
        {
            "touchstone.tenant_id": event["tenant_id"],
            "touchstone.workflow_id": event["workflow_id"],
            "touchstone.workflow_version": event["workflow_version"],
            "touchstone.run_id": event["run_id"],
            "touchstone.task_id": event.get("task_id", ""),
            "touchstone.simulated": str(event.get("simulated", False)).lower(),
        },
        [event_name],
        [{attribute: json.dumps(event)}],
    )


class _Result:
    def __init__(self, rows):
        self.result_rows = rows


class _Client:
    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda row: row[:2])
        self.queries = []

    def query(self, sql, parameters):
        self.queries.append((sql, parameters))
        assert "LIMIT" in sql and "ReceivedAt" in sql and "ReceiptId" in sql
        after = (parameters["after_time"], parameters["after_id"])
        selected = [row for row in self.rows if after < row[:2] and row[0] <= parameters["through"]]
        return _Result(selected[: parameters["page_size"]])


def test_extract_keeps_timestamp_ties_late_occurrences_and_restart():
    first = _example("measurement-v1.json")
    second = copy.deepcopy(first)
    second["event_id"] = "second"
    second["occurred_at"] = "2019-01-01T00:00:00Z"
    third = copy.deepcopy(first)
    third["event_id"] = "after-cutoff"
    rows = [
        _row(first),
        _row(second, receipt_id="00000000-0000-0000-0000-000000000002"),
        _row(third, received=CUTOFF + timedelta(microseconds=1)),
    ]
    client = _Client(rows)
    one = list(iter_measurements(client, through=CUTOFF, page_size=1))
    two = list(iter_measurements(_Client(rows), through=CUTOFF, page_size=1))
    assert [event.identity.event_id for event in one] == ["measurement-fixture-1", "second"]
    assert [event.content_sha256 for event in one] == [event.content_sha256 for event in two]
    assert all(event.received_at.startswith("2026-09-26T12:00:00") for event in one)
    assert all(query[1]["page_size"] == 1 for query in client.queries)


def test_extract_rejects_mismatched_tenant_without_assigning_false_tenant():
    event = _example("measurement-v1.json")
    row = list(_row(event))
    row[4] = dict(row[4], **{"touchstone.tenant_id": "other"})
    result = list(iter_measurements(_Client([tuple(row)]), through=CUTOFF))
    assert len(result) == 1
    assert isinstance(result[0], RejectedEvent)
    assert result[0].tenant_id is None
    assert "tenant" in result[0].reason


def test_extract_rejects_empty_or_malformed_envelope():
    event = _example("measurement-v1.json")
    rows = []
    for index, payload in enumerate(["", "{bad"]):
        row = list(_row(event, receipt_id=f"00000000-0000-0000-0000-{index + 1:012d}"))
        row[6] = [{"touchstone.measurement.json": payload}]
        rows.append(tuple(row))
    found = list(iter_measurements(_Client(rows), through=CUTOFF))
    assert len(found) == 2
    assert all(isinstance(item, RejectedEvent) for item in found)


def test_extract_declaration_preserves_valid_identity_and_rejects_context_mismatch():
    declaration = _example("run-declaration-v1.json")
    good = _row(declaration, kind="declaration")
    bad = list(
        _row(declaration, receipt_id="00000000-0000-0000-0000-000000000002", kind="declaration")
    )
    bad[4] = dict(bad[4], **{"touchstone.run_id": "wrong"})
    found = list(iter_declarations(_Client([good, tuple(bad)]), through=CUTOFF, page_size=1))
    assert isinstance(found[0], ValidatedDeclaration)
    assert found[0].document["expected_task_count"] == 2
    assert isinstance(found[1], RejectedEvent)
    assert found[1].tenant_id == "tenant-fixture-a"
