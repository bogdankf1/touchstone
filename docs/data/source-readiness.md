# Simulated CCTD source readiness

This report describes the local IBM Credit Card Transactions Dataset (CCTD) snapshot. All
transactions and people in these files are simulated. The files have not been modified, and the
generated aggregate inventory under `artifacts/` is excluded from git.

## Source inventory

| File | Bytes | Data rows | SHA-256 | Included in main transaction counts |
|---|---:|---:|---|---|
| `credit_card_transactions-ibm_v2.csv` | 2,350,744,057 | 24,386,900 | `b01fa323c98522f8c710c7f7242581860c97c50183f1c5fa9e772e5c674a7f15` | Yes |
| `User0_credit_card_transactions.csv` | 1,899,258 | 19,963 | `61c4ed49a98bf924c5df0a740e1c415b91624c83821f8415bd8db34c2d6a0d3c` | No |
| `sd254_cards.csv` | 487,120 | 6,146 | `85af46a5789cf5ac9674cee4184c828688fcf2c00566549f752393112ed14a7b` | No |
| `sd254_users.csv` | 224,394 | 2,000 | `99eff71679d5dcf3f49e537fd65ba848271faa4e8ba8e4d9ab368c26034a3d03` | No |

`User0_credit_card_transactions.csv` looks like an overlapping subset. It is inventoried
separately and is not concatenated with the main file because distinctness has not been proven.

## Main transaction observations

- Labels: 24,357,143 legitimate, 29,757 fraud, and 0 unknown.
- Naive source time range: `1991-01-02T07:10:00` through `2020-02-28T23:58:00`; 0 invalid
  timestamps. No timezone is assigned.
- Channels: 15,386,082 swipe, 6,287,598 chip, and 2,713,220 online transactions.
- Amount parsing: all 24,386,900 values have a leading dollar sign and all parse as whole minor
  units. There are 1,244,683 negative values and 20,213 zero values. The positive-value total is
  119,112,045,920 minor units. This is a format observation, not evidence of a currency.
- All transaction `(User, Card)` pairs match a `(User, CARD INDEX)` entry in the cards file.
  This establishes referential coverage for that join in this snapshot.
- Duplicate-looking records are retained as distinct source records. The profiler does not
  deduplicate rows.

The streaming run completed in 111.06 seconds with peak RSS of 23,822,336 bytes (about 22.7 MiB)
on the development host. It retained card keys and small per-user counters in memory, not the
transaction table.

## Documentation check and blockers

The [IBM TabFormer repository](https://github.com/IBM/TabFormer) identifies the released
credit-card data as synthetic and describes a 24-million-record dataset. The author's
[Synthesizing Credit Card Transactions paper](https://arxiv.org/abs/1910.03033) describes the
virtual-world generator and long simulated histories. Neither source establishes the timezone,
currency, or a row-position mapping between this users CSV and the transaction/card `User` key.

The following source facts remain unverified:

- The source timezone is unverified. Reckoner v0 explicitly assumes the wall-clock values are UTC
  for its controlled experiment and records `source_timezone_assumed=UTC` in provenance.
- The source currency is unverified. Reckoner v0 explicitly assumes USD for its controlled
  experiment and records `currency_assumed=USD` in provenance; the dollar sign is not presented as
  proof.
- `sd254_users.csv` contains names and attributes but no explicit user ID. Demographic enrichment
  must not join it by row position without authoritative evidence.
- Historical availability of current-age, FICO, and dark-web attributes is unverified. Those
  attributes must not enter time-correct features until their availability is established.

Transaction and card references are ready for canonical adaptation. The approved USD/UTC choices
are experimental normalization assumptions, not newly discovered source facts. The unresolved
user join still prevents demographic enrichment and any historical use of those attributes; it
does not invalidate the aggregate inventory or the checksum-pinned source backing.
