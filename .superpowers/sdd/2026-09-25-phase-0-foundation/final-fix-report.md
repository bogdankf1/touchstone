# Phase 0 final review fix report

**Base:** `2fcfab7f5a6dfdb6d5b31e54a745a10da4ee1d06`
**Scope:** outcome-cost currency semantics, canonical IP validation, and an invalid-UTF-8 CLI regression test.

## Changes

- Added required `currency` to measurement outcome payloads. It must be a three-letter uppercase code when either `review_cost` or `error_cost` is present, including `"0.00"`; it must be `null` when both costs are unavailable (`null`).
- Replaced the unrecognized transaction `ip-address` format with nullable `ipv4`/`ipv6` alternatives.
- Added contract tests for known, unavailable, zero, missing-currency, invalid-currency, valid IPv4, valid IPv6, null IP, and invalid IP cases.
- Added a portable invalid-UTF-8 source fixture proving the profiler CLI returns exit code 2 and does not create its output. The existing `UnicodeError` handling already implemented the behavior, so no profiler source changed.

## TDD evidence

The first attempted red run could not initialize uv's default cache in the sandbox:

```text
$ uv run pytest contracts/tests/test_events.py workloads/reckoner/tests/test_profile.py -q
error: Failed to initialize cache at `/Users/bohdanburukhin/.cache/uv`
  Caused by: failed to open file `/Users/bohdanburukhin/.cache/uv/sdists-v9/.git`: Operation not permitted (os error 1)
```

The command was rerun with a writable temporary cache. Before the schema changes, the intended assertions failed:

```text
$ UV_CACHE_DIR=/private/tmp/touchstone-uv-cache uv run pytest contracts/tests/test_events.py workloads/reckoner/tests/test_profile.py -q
...........FFFFF......FF.........F...........                            [100%]
FAILED contracts/tests/test_events.py::test_pending_outcome_accepts_nullable_correctness
FAILED contracts/tests/test_events.py::test_observed_outcome_accepts_known_costs_with_currency
FAILED contracts/tests/test_events.py::test_outcome_accepts_unavailable_costs_with_null_currency
FAILED contracts/tests/test_events.py::test_outcome_zero_cost_still_requires_currency
FAILED contracts/tests/test_events.py::test_outcome_rejects_known_cost_without_currency
FAILED contracts/tests/test_events.py::test_measurement_accepts_zero_monetary_costs[review]
FAILED contracts/tests/test_events.py::test_measurement_accepts_zero_monetary_costs[error]
FAILED contracts/tests/test_events.py::test_transaction_rejects_invalid_ip_address
8 failed, 37 passed in 0.53s
```

The UTF-8 regression was already green during that red run because the CLI already catches `UnicodeError`. Its isolated result is:

```text
$ UV_CACHE_DIR=/private/tmp/touchstone-uv-cache uv run pytest workloads/reckoner/tests/test_profile.py::test_cli_returns_two_for_undecodable_existing_input -q
.                                                                        [100%]
1 passed in 0.01s
```

After the minimal schema changes, the focused suite was green. After tightening the zero-cost test to prove removal of currency is rejected, it remained green:

```text
$ UV_CACHE_DIR=/private/tmp/touchstone-uv-cache uv run pytest contracts/tests/test_events.py workloads/reckoner/tests/test_profile.py -q
.............................................                            [100%]
45 passed in 0.44s
```

## Final verification

```text
$ UV_CACHE_DIR=/private/tmp/touchstone-uv-cache uv run --all-packages pytest -q
..........................................................               [100%]
58 passed in 0.66s

$ UV_CACHE_DIR=/private/tmp/touchstone-uv-cache uv run ruff check .
All checks passed!

$ UV_CACHE_DIR=/private/tmp/touchstone-uv-cache uv run ruff format --check .
18 files already formatted

$ git diff --check
[no output; exit 0]
```

## Self-review

- The measurement condition checks the value type rather than truthiness, so the decimal string `"0.00"` requires currency exactly like any positive amount.
- Both unavailable costs remain explicitly representable as `null`, paired with `currency: null`; missing cost is not converted to zero.
- The existing strict outcome object now recognizes only the requested currency field; no unrelated payload fields were loosened.
- JSON Schema validation uses `FormatChecker`, and the standard `ipv4` and `ipv6` formats now accept both address families while rejecting garbage.
- The CLI test uses explicit invalid UTF-8 bytes and temporary files, so it does not depend on the host locale or modify source data.
- The diff is limited to the two schemas, their covering tests, the profiler regression test, and this report.

## Concerns

The outcome schema is intentionally stricter: every outcome producer must now include `currency`, using `null` only when both monetary amounts are unavailable. No outcome producer exists in Phase 0, so there is no in-repository runtime migration to make. Compose, kind, and the full source corpus were not rerun because these are schema/test-only fixes, as scoped by the controller.
