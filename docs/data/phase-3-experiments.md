# Frozen Phase 3 experiments

All CCTD transactions and label-derived resolutions are **simulated**. Currency
USD, source time zone UTC, and seven-day resolution availability are experimental
assumptions. This preparation makes no provider calls and demonstrates no real
reviewer accuracy, payment deployment, or customer behavior.

## Preparation and immutable inputs

```sh
uv run --frozen --all-packages reckoner v1 prepare \
  --source /path/to/immutable/archive \
  --baseline-bundle /path/to/frozen/phase1/data \
  --output /path/to/new/phase3/data
```

The output parent must exist; the output must be new. Preparation verifies all
original source and baseline-artifact checksums before scanning. It calls the
original strict CCTD adapter for every retained-user row. Canonical identities,
tenant assignments, complete retained histories, all source fraud, and the frozen
1,000-case 2019 baseline are preserved. There is no new CCTD business mapper.

The complete index contains source-record ordinals and byte offsets, canonical
entity references, source times, amounts, and channels. Canonical rows are read
through the original adapter from the checksum-pinned archive. The derived index
is not a replacement for that archive. `history.sqlite` has no labels or verdicts;
`resolutions.sqlite` is a separate privileged preparation artifact. Selected
runtime and oracle JSONL files are also separate. Files are owner-readable; the
published directory is mode 700. Runtime provider inputs must never include oracle
files, the source archive, or unrestricted resolution rows.

Preparation uses two 32 MiB SQLite page caches, batches of 10,000 history rows,
bounded class-selection heaps, and disk sorting for indexes. It checks that at
least 15 GiB remains free and derived preparation files stay below 20 GiB.
Complete publication uses a directory rename after all artifacts and hashes exist;
a publication lock serializes competing preparers. A crashed publication lock
requires inspection before removing that specific lock. No output path is reused.

## Cohorts and historical clocks

Seed: `20260930`. Development is 2017 and validation is 2018. Each selects the
lowest `sha256(seed:purpose:transaction_id)` values within fraud and legitimate
strata, with stable transaction-ID tie-breaking: 200 fraud and 1,800 legitimate.
The 20 original pilot IDs are excluded. A transaction must resolve strictly before
the next year starts; equality is excluded. Insufficient strata stop preparation
without publishing a reduced sample.

`resolved_at = occurred_at + 7 * 24 hours`, under policy
`simulated-seven-days-v1`. Historical transaction time and resolution time must
both precede query time strictly. Equal source timestamps provide no ordering.
Timezone-naive inputs are rejected. Prior-card state uses complete indexed
histories even when an evidence window changes or contains no transactions.

| Sample | Eligible fraud | Eligible legitimate | Selected fraud / legitimate | Unresolved exclusions | Original-pilot exclusions |
| --- | ---: | ---: | --- | ---: | ---: |
| Development 2017 | 212 | 1,506,955 | 200 / 1,800 | 29,781 | 0 |
| Validation 2018 | 2,367 | 1,503,196 | 200 / 1,800 | 29,856 | 4 |

Each manifest records `N_h`, `n_h`, and decimal `N_h/n_h`. These weights correct
class enrichment only within the retained-user, positive-purchase, time-valid
population; they do not undo entity-selection bias. Evaluators join each case to
its privileged oracle stratum to obtain its weight. Current/future labels never
enter runtime features. An oracle-label change can change sample membership and
oracle products; the canonical adapter does not use that label in runtime content.
Changing source bytes changes the source hash and therefore canonical identities.

The new pilot selects 2 fraud and 18 legitimate from development membership by a
separate `pilot` hash. Paid results can be reused only for identical frozen request
hashes, with reuse identified. These dataset manifests do not freeze subsequent
model/configuration artifacts or authorize paid execution. A strict executable
`experiment` record uses the sample ID as `cohort_version`, the preparation bundle
as `dataset_version`, and the selected IDs to create tenant-scoped tasks once its
run configuration is frozen.

Development and validation IDs are disjoint; neither intersects the frozen 2019
baseline. 2020 remains reserved. Canonical history from later years remains on disk
but cannot pass earlier evidence cutoffs.

## Verified local preparation

Published ignored directory:
`artifacts/phase3/data/frozen-v1` in the Phase 3 worktree.

- Source SHA-256: `b01fa323c98522f8c710c7f7242581860c97c50183f1c5fa9e772e5c674a7f15`.
- Original bundle: `2dcc4a9838db77552872ea489ff8a8e3b13a84b37a5806b2b02291c9f23848e0`.
- Original history: `9bba9993c2c20c21e9a4d989f5053c15c9db24837eb94b37e9840f03743d4890`.
- New bundle: `98c332ae28ed66c1f10a881d79c4f3eb9eafa269be9e42545327cecdacf790cd`.
- Development: `9e3fdd57d7cc90267879b73d9394f0c4dec19272993a56142fc85c05cb8b2560`.
- Validation: `1ae45495713bb5d54bac9e0359223d82e994e568680369922319670d96e7c0ce`.
- Pilot: `fb5e13e38c6606cc7ac9edc8e238b6f9ba7bd5add961eaad3a6195255623a02e`.

The strict scan verified 24,386,900 source records, 23,121,349 retained records,
21,910,205 retained positive purchases, 1,211,144 retained nonpositive records,
and all 29,757 source fraud records. Retained entities: 1,441 accounts, 5,055 cards,
124,930 merchants. Source history ranges from `1991-01-02T07:10:00Z` to
`2020-02-28T23:58:00Z`. The 2017/2018 strict strata exactly match the prior planning
scan; no discrepancy was accepted or repaired by weakening the adapter.

Preparation took 618.63 seconds and generated 6,066,606,861 bytes. Free disk was
60,275,363,840 bytes before and 54,780,391,424 bytes after. Peak resident memory
was unavailable: the timing wrapper's post-run `sysctl` was blocked by the sandbox.
The Python preparation completed successfully; the timing wrapper exited 1 after
printing its timing because of that denied system call. Receipt files remain in
ignored `artifacts/phase3/`. Repeated fabricated-source preparation verified
identical bundle identities and every file checksum; the full archive was scanned
once, and its published checksums and memberships were independently verified.

## Privileged operational import

```sh
uv run --frozen --all-packages reckoner v1 import \
  --bundle /path/to/phase3/data/frozen-v1 --env-file /path/to/owner-import.env
```

The owner-only environment contains `RECKONER_OWNER_DSN`,
`RECKONER_SOURCE_DIR`, and optionally `RECKONER_BASELINE_BUNDLE`. Include the
original baseline bundle to prepare working histories for its frozen 2019 queries.
A dedicated file is required; provider keys are not accepted or read. `--env-file -`
uses only those explicitly named process variables.

Apply migration 006 with the existing owner migration command first. Import verifies
checksums, then atomically loads canonical query rows, canonical historical rows,
and validated historical resolution documents. Its working set is the union of
97-day source windows before frozen queries (90-day resolved window plus seven-day
availability), together with previous-card records outside those windows. Import
receipts expose the query count, time intervals, previous-card coverage, and whether
2019 queries were included. It checks derived-bundle plus database size against
20 GiB and requires 15 GiB free disk before loading, during batches, and before
commit; complete source histories stay in the archive/index. The full
archive working-set import has not been benchmarked or published to measured stores.

`reckoner.v1_history_before(tenant_id, transaction_id)` returns the tenant's strict
30-day canonical transaction evidence. `reckoner.v1_resolved_before(...)` returns
strictly earlier, seven-day simulated resolutions in a 90-day resolved-case window.
`reckoner.v1_previous_card(...)` returns the strictly previous card transaction
without a window limit. Their cutoffs come from the canonical query transaction, never a caller-supplied
future time. Unknown or foreign-tenant query identities yield no rows. Runners can
execute these cutoff functions but cannot read their base history or resolution
tables. The API and evaluator have no unrestricted access to these new tables.
Records and indexed identities reject mutation, including owner updates. Import
retries compare existing canonical/resolution documents and reject conflicts.

The graph task must build its own frozen, bounded projection from these complete
source-backed histories and report graph coverage. This preparation does not claim
that a graph or a GDS projection has already been built.

## Offline calibration diagnostics (Task 5)

```sh
uv run --frozen --all-packages reckoner v1 calibrate \
  --development /path/to/development-predictions.json \
  --validation /path/to/validation-predictions.json \
  --output /path/to/new/calibration-report
```

This command accepts evaluator-only JSON lists. It reads no environment file,
provider key, archive, or database. A privileged evaluator must first join successful
strict scorer records to the frozen sample and oracle on tenant and transaction
identity. Copy the scorer's exact `raw_probability` decimal text into `probability`;
verify requested/reported model and question against the pinned configuration.
Failed, uncertain, or missing scores cannot become successful calibration rows.
Completeness is enforced by matching observed class counts to frozen `n_h`; do not
silently drop unsuccessful sample members. The 20 new pilot members are already
within the 2,000 development members and must not be excluded again.

Each observation contains:

```json
{
  "tenant_id": "synthetic-tenant-a",
  "user_id": "canonical-user-id",
  "case_id": "canonical-transaction-id",
  "probability": "0.2",
  "label": 0,
  "weight": "837.1972222222222222222222222",
  "purpose": "development",
  "year": 2017,
  "stratum": {"year": 2017, "N_h": 1506955, "n_h": 1800},
  "context": {
    "scorer": {
      "provider": "typesafe",
      "model": "jev-1.13.0",
      "question_version": "<frozen-question-version>"
    },
    "feature_version": "features-v1",
    "scaler_id": "<frozen-64-character-scaler-id>",
    "graph_version": "<pinned-graph-version>",
    "retrieval_version": "<pinned-retrieval-version>",
    "evidence_mode": "<explicit-pinned-evidence-arm>",
    "data_kind": "simulated-cctd"
  }
}
```

`label` is an evaluator-only integer (0 legitimate, 1 fraud). Stratum counts are
sample-wide class counts, not tenant counts; use the frozen year-specific counts
above and the preparation manifest's exact Decimal `N_h/n_h` text. Development
requires 2017 and validation requires 2018; missing counts, reused year provenance,
wrong weights, incomplete class samples, duplicate tenant/case identities, mixed
contexts, and overlapping development/validation cases are rejected. These checks
validate the supplied export; the exporter remains responsible for its source,
cohort, resolution cutoff, and exact scorer-record joins. Direct metric diagnostics
can operate on partial or empty rows, explicitly reporting availability.

The fit uses NumPy 2.5.3 and SciPy 1.18.1 on Python 3.12. The sole candidate is
`sigmoid(a * logit(clipped_p) + b)` with `a >= 0`, clip `[1e-6, 1-1e-6]`, initial
`(1,0)`, SciPy L-BFGS-B, analytic gradient, maximum 1,000 iterations, and weighted
mean log loss plus `.001 * ((a-1)^2 + b^2)`. Optimizer/library versions and actual
convergence are recorded. One-class development and failed/nonfinite optimization
remain unavailable for candidate application. Fitting reads development rows only.
Validation selects the candidate only if Brier strictly improves and log loss does
not increase. Raw scores otherwise remain exact; clipping applies only inside the
candidate map and log-loss calculation. Bucket assignment compares decimal inputs
to the exact specified edges, including the final endpoint 1.

The new output directory contains `report.json`, readable `report.md`, standalone
`reliability.svg` (Matplotlib 3.11.2 headless SVG backend), `candidate.json`, and `selected-artifact.json` (JSON null when raw
is retained). Candidate/report/selected artifact identities use `content_id` over
their complete bodies excluding their identity fields. Selection gives the frozen
candidate a new calibration identity containing its validation qualification;
use this selected identity in runtime configuration, rather than the candidate ID.
No existing output is overwritten. Reports distinguish raw and candidate metrics
for development/validation, overall and per tenant, including weighted Brier,
log loss, ECE, bucket counts, weighted rates, effective sample sizes and intervals.
Eligible 2017/2018 prevalence and its difference remain visible alongside sparse
warnings regardless of numerical selection.

Bootstrap draws whole `(tenant_id,user_id)` clusters with replacement 1,000 times,
seed `20260930`; intervals use 2.5/97.5 percentiles. Empty and one-class replicates
are counted and excluded from interval estimates. Fewer than two user clusters, or
zero estimable replicates, leaves bounds unavailable. Nonempty one-class populations
still have descriptive metrics; they do not acquire inferential confidence. Bucket
intervals likewise require two classes within the resampled bucket. Sparse marks
fewer than 30 cases or fewer than five of either class, a visible diagnostic heuristic
rather than a success gate. Small metric changes do not prove meaningful improvement;
weights do not repair entity-selection bias or establish real-payment calibration.

Runtime must call `validate_calibration_context(artifact, context)` before
`apply_calibration(raw_probability, artifact)`. Supply the independently pinned run
configuration's `scorer`, `feature_version`, `scaler_id`, `graph_version`, and
`retrieval_version`, plus explicit pinned `evidence_mode` and `data_kind`. The
validator requires a converged, content-hashed, validation-selected artifact with
valid numeric selection evidence and matching context. It does not derive the
requested context from the artifact or hardcode one model. Task 6 owns pinning the
explicit evidence mode in its runtime policy; it is not inferred from a graph
version. Pure numeric application separately accepts a converged candidate for
report evaluation; that does not qualify it for runtime. With no artifact,
`apply_calibration` returns the original Decimal unchanged, including 0 and 1.

Current Task 5 checks use **fabricated** labeled prediction fixtures only, with
`data_kind: fabricated`. They prove offline software behavior and cannot qualify a
run requesting `simulated-cctd`. No real Jev prediction export or calibration
acceptance has been produced; those remain bounded, separately approved paid-run
gates before the final 2019 experiment.

## Prepared evidence for the frozen populations (Task 3b)

Task 13 consumes persisted evidence; it never assembles evidence during a paid run.
`reckoner v1 evidence` (runbook: "Real-archive evidence preparation") prepares:

| Population | Cases | Modes |
| --- | ---: | --- |
| Development 2017 | 2,000 | relational |
| Pilot (development subset) | 20 | relational (development documents) |
| Validation 2018 | 2,000 | relational, gds-augmented |
| 2019 cohort (100 fraud / 900 legitimate) | 1,000 | relational, gds-augmented |

Development `gds-augmented` evidence is conditional (decision D4). It is prepared only if
the paired validation comparison selects the GDS arm, because calibration uses the
relational/vector arm and a changed input definition needs its own development and
validation pass.

Windows are unchanged: 30-day transaction evidence, a 90-day resolved-case window and
seven-day simulated resolution, all strictly before the query. The working set for query
day `D` is `[D - 97 days, D + 1 day)` plus previous-card rows. Graph projections are built
at `D 00:00Z`, so snapshot age is below 24 hours and is recorded in every document.
Comparable vectors exist for every eligible positive candidate, computed once from complete
source history; a query whose vector coverage is incomplete fails its day instead of being
persisted as partial.

`publish` writes immutable, content-addressed manifests `manifests/<population>-<mode>.json`.
Each lists every expected case exactly once, with `evidence_id`, coverage status, missing
reasons, `projection_id`, `page_rank_converged` and `snapshot_age_seconds`, and publish
refuses missing, extra or duplicate cases. Task 13 binds each run task to the `evidence_id`
in these manifests and computes its protocol request hashes from those exact documents.
Persisted counts, coverage and resource measurements are recorded when the stages run;
nothing here claims a stage has run.
