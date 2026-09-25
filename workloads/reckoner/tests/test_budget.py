from decimal import Decimal
from threading import Barrier, Thread

import pytest
from conftest import seed_run
from reckoner.storage.budget import BudgetExceeded, BudgetLedger
from reckoner.storage.postgres import PostgresRepository

pytestmark = pytest.mark.integration


def test_budget_is_shared_and_uncertain_cost_stays_reserved(pg):
    a = seed_run(pg, "r1", "tenant-a")
    b = seed_run(pg, "r2", "tenant-b")
    with PostgresRepository(pg.runner_dsn) as repo:
        ledger = BudgetLedger(repo)
        reserved = ledger.reserve(a, Decimal("6.00"))
        ledger.settle(reserved["call_id"], None, None)
        assert ledger.remaining() == Decimal("4.00")
        with pytest.raises(BudgetExceeded):
            ledger.reserve(b, Decimal("4.01"))


def test_two_six_dollar_reservations_race_and_only_one_succeeds(pg):
    tasks = [seed_run(pg, "race-a", "tenant-a"), seed_run(pg, "race-b", "tenant-b")]
    barrier = Barrier(2)
    outcomes = []

    def reserve(task):
        with PostgresRepository(pg.runner_dsn) as repo:
            barrier.wait()
            try:
                BudgetLedger(repo).reserve(task, Decimal("6.00"))
            except BudgetExceeded:
                outcomes.append("rejected")
            else:
                outcomes.append("reserved")

    threads = [Thread(target=reserve, args=(task,)) for task in tasks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["rejected", "reserved"]
    with PostgresRepository(pg.runner_dsn) as repo:
        assert BudgetLedger(repo).remaining() == Decimal("4.00")


def test_restart_preserves_spend_and_reservation_identity(pg):
    task = seed_run(pg, "restart", "tenant-a")
    with PostgresRepository(pg.runner_dsn) as repo:
        reservation = BudgetLedger(repo).reserve(task, Decimal("2.25"))

    with PostgresRepository(pg.runner_dsn) as restarted:
        ledger = BudgetLedger(restarted)
        assert ledger.remaining() == Decimal("7.75")
        assert (
            restarted.snapshot("restart", "tenant-a")["budget_entries"][0]["call_id"]
            == reservation["call_id"]
        )


def test_pilot_limit_is_shared_across_run_ids(pg):
    first = seed_run(pg, "pilot-one", "tenant-a", purpose="pilot")
    second = seed_run(pg, "pilot-two", "tenant-b", purpose="pilot")
    with PostgresRepository(pg.runner_dsn) as repo:
        ledger = BudgetLedger(repo)
        ledger.reserve(first, Decimal("0.70"))
        assert ledger.pilot_remaining() == Decimal("0.30")
        with pytest.raises(BudgetExceeded, match="pilot"):
            ledger.reserve(second, Decimal("0.31"))


def test_settlement_is_idempotent_but_conflicting_second_value_is_rejected(pg):
    task = seed_run(pg, "settle", "tenant-a")
    usage = {"input_tokens": 100, "output_tokens": 10}
    with PostgresRepository(pg.runner_dsn) as repo:
        ledger = BudgetLedger(repo)
        reservation = ledger.reserve(task, Decimal("1.00"))
        ledger.settle(reservation["call_id"], Decimal("0.25"), usage)
        ledger.settle(reservation["call_id"], Decimal("0.25"), usage)
        assert ledger.remaining() == Decimal("9.75")
        with pytest.raises(ValueError, match="conflicting"):
            ledger.settle(reservation["call_id"], Decimal("0.26"), usage)


def test_crash_after_reservation_leaves_uncertain_task_and_holds_funds(pg):
    task = seed_run(pg, "uncertain", "tenant-a")
    with PostgresRepository(pg.runner_dsn) as repo:
        reservation = BudgetLedger(repo).reserve(task, Decimal("1.50"))

    with PostgresRepository(pg.runner_dsn) as restarted:
        BudgetLedger(restarted).settle(reservation["call_id"], None, None)
        snapshot = restarted.snapshot("uncertain", "tenant-a")
        assert snapshot["tasks"][0]["status"] == "uncertain"
        assert snapshot["budget_entries"][0]["status"] == "uncertain"
        assert BudgetLedger(restarted).remaining() == Decimal("8.50")


def test_overage_is_persisted_and_blocks_future_dispatch(pg):
    first = seed_run(pg, "overage-a", "tenant-a")
    second = seed_run(pg, "overage-b", "tenant-b")
    with PostgresRepository(pg.runner_dsn) as repo:
        ledger = BudgetLedger(repo)
        reservation = ledger.reserve(first, Decimal("1.00"))
        ledger.settle(
            reservation["call_id"], Decimal("10.01"), {"input_tokens": 1, "output_tokens": 1}
        )
        assert ledger.remaining() == Decimal("0")
        with pytest.raises(BudgetExceeded):
            ledger.reserve(second, Decimal("0"))


def test_below_cap_overage_blocks_dispatch_after_repository_restart(pg):
    first = seed_run(pg, "below-cap-overage-a", "tenant-a")
    second = seed_run(pg, "below-cap-overage-b", "tenant-b")
    with PostgresRepository(pg.runner_dsn) as repo:
        ledger = BudgetLedger(repo)
        reservation = ledger.reserve(first, Decimal("1.00"))
        ledger.settle(
            reservation["call_id"],
            Decimal("1.01"),
            {"input_tokens": 1, "output_tokens": 1},
        )

    with PostgresRepository(pg.runner_dsn) as restarted:
        ledger = BudgetLedger(restarted)
        assert ledger.remaining() == Decimal("8.99")
        with pytest.raises(BudgetExceeded, match="overage"):
            ledger.reserve(second, Decimal("0.01"))
