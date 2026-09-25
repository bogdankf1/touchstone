# Simulated evaluation cohort recipe

This recipe defines how Phase 1 will construct the frozen 1,000-transaction evaluation cohort
from simulated CCTD data. It does not claim that the cohort has been extracted. The committed
cohort manifest is a one-record fabricated contract fixture and is not measured evidence.

## Ordered construction procedure

1. Resolve the currency, timezone, and card/user reference questions recorded in
   [`source-readiness.md`](source-readiness.md). The source currently establishes complete
   transaction-to-card references, but it does not establish a currency, timezone, or a
   row-position join to the users file. Record evidence for those decisions in the
   dataset-normalization version. Do not attach unverified user attributes or treat dollar-sign
   formatting as proof of currency.
2. Freeze a pre-holdout calibration interval and a later holdout interval. Determine the actual
   boundaries from the inventoried source coverage, document the rationale, and commit them before
   any scoring. Require `history_end < evaluation_start <= evaluation_end`.
3. Select the complete user and card histories required to retain every source fraud record. Add
   legitimate entities only within measured storage and graph limits. Preserve every retained
   history on disk; do not sample individual history rows.
4. Using only pre-holdout activity, assign each complete user history to one tenant. Aim for the
   agreed approximate 70/30 transaction split and different observed risk profiles, then report
   the achieved distributions. Do not invent proportions when the corpus cannot support them.
   Shared merchant identity does not transfer ownership of a transaction between tenants.
5. From eligible positive-value purchases in the holdout, stratify by the oracle label and use the
   declared seed plus stable source-record IDs to select 100 fraud and 900 legitimate transactions
   without replacement. Store labels only in the oracle artifact; runtime transaction artifacts
   must not contain them.
6. Emit one immutable manifest per tenant. The manifests share a cohort ID and record source
   checksums, seed, boundaries, inclusion method, counts, normalization version, selected stable
   transaction IDs, complete-history coverage, entity counts, graph scope, and limitations. Their
   union must contain exactly 1,000 unique IDs with class counts of 100 fraud and 900 legitimate.
7. Build graph and retrieval inputs with a cutoff before each transaction timestamp. A static graph
   containing a selected history's future edges, later fraud flags, or later resolved cases cannot
   serve an earlier decision.
8. Report source, retained, eligible, unsupported, and evaluation counts; selected user/card/
   merchant counts; whether histories are complete; and every graph subset or omission. If the
   complete required graph does not fit measured resources, stop for scope review rather than
   silently truncating it.

The enriched 100/900 class ratio describes this evaluation cohort only. Do not infer natural
population rates or population totals from it, and do not apply one sampling ratio across metrics
without a defined estimand and justified inclusion probabilities.

## Manifest finalization checks

For each tenant manifest, validate the JSON schema and semantic checks, then recompute
`manifest_id` from every field except `manifest_id`. Verify the cross-tenant union separately:
transaction IDs are unique, tenant counts sum to 1,000/100/900, and every tenant manifest carries
the same cohort ID. Freeze the manifests before model execution. Any later content change creates
a new identity.
