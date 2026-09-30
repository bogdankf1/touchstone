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
20 GiB while loading; complete source histories stay in the archive/index. The full
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
