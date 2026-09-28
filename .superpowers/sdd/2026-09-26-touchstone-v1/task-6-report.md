# Task 6 — independent fabricated synthetic workflow

The synthetic workload emits schema-valid OTLP traces without importing Reckoner or Touchstone implementation code. Its four declared tasks are split equally between two synthetic tenants. Three complete and one fails; two outcomes are correct. Fabricated provider usage totals USD 0.10, review cost USD 4, and error cost USD 2, yielding exact CPST USD 3.05 for the complete run. Each measurement is marked `simulated=true`; declarations identify `measurement_mode=fabricated` and `dataset_simulated=true`. The fixture includes `classify` and `case_note` nodes, pass/fail/error evaluations, and versioned judge/reference fields. The CLI emits a second run with a declared but absent `reference_faithfulness` check.

## RED → GREEN and checks

- The interrupted Task 6 worker reported a RED collection failure for the missing `touchstone_synthetic` package before implementation, followed by four focused tests passing. That RED was inherited from its handoff; this resumed agent did not rerun the pre-implementation state.
- Fresh focused command: `UV_PYTHON_INSTALL_DIR=/private/tmp/touchstone-uv-python UV_CACHE_DIR=/private/tmp/touchstone-uv-cache uv run --offline --all-packages pytest workloads/synthetic/tests platform/tests/test_workflow_independence.py -q` → **4 passed**.
- A packaging probe with default `uv build --offline --package touchstone-synthetic` failed when Hatchling built a wheel from an sdist: its forced shared-schema paths were unavailable inside the sdist. This is the same wheel-focused packaging pattern used by the platform package. The Task 6 deliverable builds a wheel directly: `uv build --wheel --offline --package touchstone-synthetic --out-dir /private/tmp/touchstone-task6-wheel` passed. A disposable Python 3.12 venv outside the checkout installed that wheel; its bundled measurement and run-declaration schemas matched the shared contract files byte for byte. The default sdist-to-wheel command remains unsupported and must not be used by the Task 7 image builder without a separate packaging change.
- Ruff check, Ruff format check, lockfile validation, and diff checks are recorded in the final receipt below.

## Real local collector → warehouse → API → UI acceptance

A dedicated disposable `touchstone-task6-95954` Compose project ran the real OTel collector and ClickHouse. `touchstone-synthetic emit --endpoint http://127.0.0.1:<published-port> --run-id task6-live` sent **44 OTLP requests** (two runs × two declarations plus 40 measurements). `touchstone refresh` accepted **4 declarations and 40 measurements**, with **0 rejected** events. The project and its volumes were removed after the check; the DuckDB local-preview snapshot is under `/private/tmp/touchstone-task6-95954-warehouse`.

The published FastAPI read routes returned workflow `synthetic-case-triage` with both tenant IDs and four tenant/run records. Aggregate `task6-live` returned 4 expected/received tasks, 3 completed, 1 failed, 2 correct, model cost `0.100000000000`, review cost `4.000000000000`, error cost `2.000000000000`, and CPST `3.050000000000`; `metrics_complete=true`, `measurement_mode=fabricated`, `dataset_simulated=true`, evaluation pass rate 0.5. Aggregate `task6-live-incomplete` retained the same costs and counts but reported 4 missing required checks, `metrics_complete=false`, and null CPST. No provider credentials or paid calls were used.

Against that published snapshot, the live Next.js page returned HTTP 200 for both run selections. The complete page rendered the exact CPST, fabricated-measurement label, and “Metrics complete”; the missing-check page rendered “Incomplete metric” and “Metrics incomplete.” This confirms the second workflow is selectable and visible through the real platform/UI path. Task 7 owns measured Reckoner replay and its side-by-side acceptance; this Task 6 check used the fabricated synthetic workflow only.

## Review

Only the synthetic package, its focused tests, the generic import-boundary test, root workspace metadata/lockfile, and this report belong to Task 6. Controller-owned edits in `docs/spec/003-touchstone-v1.md` and `docs/operations/user-dependencies.md` remain unstaged. No changes to platform aggregation or Reckoner were needed. The shared schema files are packaged into the wheel from `contracts/` rather than duplicated in source.

## Final receipt

The first broad `uv run --offline --all-packages pytest -m 'not integration' -q --tb=short` run returned **311 passed, 78 deselected in 215.35s**. It overlapped the packaging-path review, so it is not the final staged-tree receipt. The final, unchanged-tree run returned **310 passed, 78 deselected in 213.35s**. Fresh focused tests returned **4 passed in 0.13s**. `ruff check` returned `All checks passed!`; `ruff format --check` reported 5 files already formatted; `uv lock --check --offline` resolved 187 packages; `git diff --cached --check` exited 0. The focused commit follows this receipt.
