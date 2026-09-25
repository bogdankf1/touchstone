# Simulated evaluation cohort recipe

This recipe defines how Phase 1 will construct the frozen 1,000-transaction evaluation cohort
from simulated CCTD data. It does not claim that the cohort has been extracted. The committed
cohort manifest is a one-record fabricated contract fixture and is not measured evidence.

## Frozen construction procedure

1. Checksum the four unchanged source files before selection. Treat source amounts as USD and
   source wall-clock timestamps as UTC for this experiment. These are explicit assumptions, not
   verified properties of the generator. Record `currency_assumed=USD` and
   `source_timezone_assumed=UTC` in each canonical transaction and in the normalization artifact.
   Do not join the users file by row position or attach its unverified demographic attributes.
2. Use CSV data-record ordinals, excluding the header, with the transaction-file checksum to form
   stable transaction IDs. This preserves duplicate-looking source rows as distinct records and
   remains correct for quoted fields containing embedded newlines.
3. Freeze 2019 as the baseline holdout. Select exactly 100 fraud and 900 legitimate positive-value
   purchases using the lowest hashes of seed `20260925`, purpose, and transaction ID within each
   label stratum. Select a separate pre-2019 pilot of 2 fraud and 18 legitimate purchases by the
   same rule. The pilot and baseline are disjoint; 2020 and later remain reserved.
4. Assign complete users to the two synthetic tenants from pre-2019 activity only. Rank users by
   descending fraud fraction, break ties with a seeded user hash, and choose the prefix nearest
   30% of pre-holdout transaction volume for tenant B. Assign users without pre-holdout activity
   by a deterministic 70/30 hash bucket. Holdout and future labels never affect ownership.
5. Retain every user with any source fraud and every selected pilot or baseline user. Complete
   histories remain in the immutable original archive rather than being copied into the bundle.
   The history manifest pins that backing file by checksum, lists retained user identities and
   ownership, and counts complete records and entities. Every source fraud row, including
   nonpositive unsupported amounts, must be covered.
6. Emit separate runtime and oracle JSON Lines artifacts. Runtime documents contain only canonical
   `transaction-v1` fields; labels exist only in `oracle-v1` documents. Emit tenant assignment,
   history, normalization, and one tenant-scoped cohort manifest per purpose and tenant.
7. Hash every artifact into a strict `dataset-bundle-v1` index. Recheck source checksums, validate
   schemas and cross-artifact membership, then atomically rename a sibling staging directory into
   place. A failed preparation publishes no partial bundle; a changed source or artifact fails
   verification.
8. Report mutually exclusive eligible, unsupported, and invalid source counts alongside retained
   and selected counts. Rows without a valid user owner remain explicit unassigned source metadata;
   they never become a third operational tenant or an evaluation case. Graph coverage is explicitly
   `none` with zero included transactions in Reckoner v0. Later graph readers must apply
   per-transaction time cutoffs to the source-backed histories.

The enriched 100/900 class ratio describes this evaluation cohort only. Do not infer natural
population rates or population totals from it, and do not apply one sampling ratio across metrics
without a defined estimand and justified inclusion probabilities.

## Manifest finalization checks

For each tenant manifest, validate the JSON schema and semantic checks, then recompute
`manifest_id` from every field except `manifest_id`. Verify the cross-tenant union separately:
transaction IDs are unique, tenant counts sum to 1,000/100/900, and every tenant manifest carries
the same cohort ID. Freeze the manifests before model execution. Any later content change creates
a new identity.
