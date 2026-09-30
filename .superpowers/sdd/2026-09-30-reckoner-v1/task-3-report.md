# Task 3 report

Approved base `b1a5d2cdf72679bb985a6e3b2c48e1ece3c71c68`. Temporal Postgres/pgvector
and Neo4j/GDS evidence implemented, with strict optional neighbourhood/projection
metadata, weighted 11-coordinate vectors and pinned risk indicators. All data is
simulated; provider calls zero. Source/frozen artifact identities unchanged.

## Verification

All commands ran from this Phase3 worktree with UV_PYTHON_INSTALL_DIR
`/private/tmp/touchstone-uv-python`, UV_CACHE_DIR `/private/tmp/touchstone-uv-cache`.
Dedicated integration endpoints: Postgres55433 and Bolt57687, fabricated test auth.

- Initial RED: missing evidence modules, then five failing feature/history/indicator
  tests. Whole-second history incorrectly excluded at +.5-second cutoff. GREEN five.
- Integration RED: invalid projection concurrency option, reserved driver parameter
  names. Corrected readConcurrency/$algorithm. Actual GDS integration passed.
- Observed RED/GREEN regressions also cover required complete candidate history,
  nonpositive vector input rejection, supported SQL/Cypher case population and
  incoming-only shared links (unrelated edge was rewritten before fix).
- Final scaler RED: `uv run --all-packages pytest workloads/reckoner/tests/test_v1_features.py -q`
  =>1 failed/4 passed: unavailable-history member rejected globally. Fixed fitting
  retains known amount, handles missing coordinates independently. Measured scaler
  (all2000 histories available) unchanged.
- `uv run --all-packages pytest workloads/reckoner/tests/test_v1_features.py workloads/reckoner/tests/test_v1_indicators.py -q`
  =>7 passed,0.03s.
- `uv run --all-packages pytest workloads/reckoner/tests/test_v1_retrieval.py -m integration -q`
  =>5 passed/3 deselected,1.51s. Actual pgvector exact top5/ties/runner grants.
- `uv run --all-packages pytest workloads/reckoner/tests/test_v1_graph.py -m neo4j_integration -q`
  =>4 passed,5.82s. Actual GDS, SQL/Cypher agreement, future/later snapshots and
  scoped shared-link regression. Unavailable service fails rather than skipping.
- Combined affected feature/indicator/history/storage/retrieval/graph tests =>31
  passed,20.07s before final added scaler regression. Store tests sequential.
- Scoped Ruff check and format check passed for14 touched Python files. Core Compose
  with tools and Task3 recipes render using generated fabricated envfiles; Kubernetes
  Service/StatefulSet parses and tested pgvector pin matches. Preparation bash syntax
  passes. No preserved core stack started.
- Direct wheel build `uv build --wheel --package reckoner` with explicit source
  revision succeeds; migration007/all4Cypher/extendedschema packaged. Default
  sdist-to-wheel path hit existing build-hook Git provenance issue; unchanged.
- No full archive preparation or broad baseline rerun.

## Compatibility and downstream interface

Exact arm64/index image pins, GPLv3 JAR checksum, actual GDS2026.09.0
`isLicensed=false`, pgvector0.8.6, Neo4jCommunity2026.09.0 and neo4j6.0.3 driver
validation are recorded in docs/operations/reckoner-v1-compatibility.md.
Actual tiny projection/Louvain/PageRank/drop calls succeeded before source load.
The pinned legacy projection procedure works but emits deprecation warning.
No Enterprise subscription or algorithm substitution.

Adapter task `{transaction: canonical transaction, query_time: occurrence instant}`
(query_time optional). Private config `{...validated persisted run-config, scaler:
detached scaler artifact}`; persist only strict run-config/scaler_id. Runtime checks
artifact hash and pinned ID. Scaler uses weighted inverse-CDF median/IQR, all2000
2017 development members (200fraud/1800legitimate), pilot subset retained.
Artifact `artifacts/phase3/task3/resource-scaler.json`, scaler_id
`a5fd8dbaa782b6c5cf66e947f6ba13faca07dea5be2fefc231da939949e10dbb`, file SHA256
`8bf9170296d8f34c6d8d747847e9027fd3fade78dd5853b4a46dfff59dcf863a`.
Development observation ID `a0661483ed419a7718a7df76f5c941fdcb3d14f932896dfd25b2b5b8b806c309`.

Schema adds optional strict neighbourhood and graph_projection; old fixtures remain
valid. Display100nodes/200edges retains totals/truncation and complete transaction
refs. Metadata pins actual cutoff/id/age/algorithm/parameters/counts/convergence.
Later projection cannot serve earlier query. Unknown coverage remains unavailable,
verified empty stays available. Centrality/community alone creates no factors.
Exact L2 similarity is1/(1+distance); ties transactionID. Same-tenant positive
supported purchases, occurrence/resolution strictly before query and resolution
>=query-90days define SQL/Cypher comparables. Candidate features precede candidate
instant; no clipping of nonpositive vector inputs. Preserve all histories/resolutions,
including nonpositive history counts/last-card evidence. Exclusion counts explicit.
Runner has cutoff functions/coverage view, no direct oracle/history/vector reads.
Migration007's pgvector image substitution in core Compose/k8s is early Task12
prerequisite, preserving major17/credentials/volumes/service settings.

## Bounded source receipt and exact reconciliation

Bundle `98c332ae28ed66c1f10a881d79c4f3eb9eafa269be9e42545327cecdacf790cd`.
Retained full manifest1441accounts/5055cards/124930merchant occurrences =131426entities;
all23121349histories/29757fraud retained. ManifestID
`022aeb4556401722cecbad0e1e1778523878670da479d1808b210ef2da08c07e`.
Import2018-02-24T00:00Z through2018-06-02T00:00Z: approved97day lookback plus query day,
required previous-card rows. Exact433596histories/433596resolutions/433596graphtransactions.
Full entity manifest retained; cutoff-eligible counts1434accounts/4595cards/117785
merchants =123814nodes. June1UTCday projection30day weights:177808directed edges
(both orientations), earlier ownership facts in bounded working set, explicit shared
identities. This is a bounded graph, not complete historical ownership import.

ProjectionID `2ca592c0133204694c1d6e9f5fc5b0b1cad3ba3bea58f2d597952b5ad2b080c8`.
Build/algorithms2.929582334s before metric persistence. Louvain80121communities
(including isolates, not fraud rings), default resolution,maxLevels10/maxIterations10,
tolerance.0001,concurrency1,no supported random seed. PageRankdamping.85/max20/
tolerance1e-7/concurrency1 **did not converge**,20iterations. False convergence stored
in2tenantreceipts and returned all7querymetadata. No parameter adjustment.
No remaining task-owned in-memory projection. All7frozen June1queries exact SQL/Cypher
neighbourhood agreement,0uncoveredgraphqueries. Totaltransactions in receipt order
3723/351/78/5690/2054/203/101. Nonpositive candidate exclusions in same order
5788/13829/13843/13828/5786/13838/13830 (overlapping populations, do not sum).
Actual real vectors0; SQLreceipts explicitly partial: eligible comparable vectors
unavailable. Fabricated integration verifies actual vector retrieval; full real
candidate preparation/retrieval belongs Task11.

Exploratory SQLadapter times5.103–29.706s/Cypher.511–2.419s are unequal work and
uncontrolled first-run/cache conditions, not fair benchmark or general graph-speed
claim. Task11 must investigate relational plans/indexes and equivalent work.

## Failure/recovery and resources

SQLite preparation EXPLAIN initially SCAN h (23121349history rows per lookup).
Equivalent entity tenant/kind predicates select existing unique entity index
(tenant,kind,identity) and history_card_time(card_key=?). Numeric instant comparisons
fix fraction/lower-bound edge without frozen-artifact rewrite. Exact cold scaler
speedup not quantified. Cold import20000/43.114s (~464rows/s),400000/884.688s
(~452rows/s),420000/931.205s. Concurrent fabricated mutation exposed global shared
links and deadlock near430k. Postgres atomic rollback, graph428k persisted, canonical
JSONL430k cached. Fixed incoming-only updates, deterministic row/pair order and
managed atomic conflict/idempotency writes. Failed log/samples preserved. Recovery
reuses manifest/scaler/JSONL/graph, appends missing rows; resumed import106.618940625s
is NOT cold full-load timing. No source re-extraction.

Failed raw samples304rows: Neo4jsampledmax2.350GiB/Postgres221.2MiB. Resumed70rows:
Neo4j2.954GiB/Postgres411.4MiB. Sampled maxima not exact peaks. Lifetime cgroup
memory.peak4,294,967,296Neo4j/2,149,351,424Postgres bytes; maxevents36/70021,
oom/oom_kill0,OOMKilledfalse/RestartCount0. Includes allTask3tests/import/cache;
projection cannot be isolated. Limits4GiB/2GiB within8GiBbudget; actual pressure
visible, no claimed unused headroom. Raw metadata in resource-reconciliation.json.

Bounded DB9287347→1941395123bytes,growth1932107776. Whole Postgresstore2905300992,
Neo4j3789230080. Bundle6066606861+artifacts840020943+whole stores=13601158876bytes
(~12.67GiB), under20GiB. Final hostfree40465248256bytes (~37.69GiB), above15GiB.
Runtime guard counts bundle/artifacts/DB; final reconciliation adds fullNeo4j/PGstores.
Receipt proves bounded snapshot only; full operational-import/all-query coverage
remain later delivery gates.

## Artifacts, preservation and handoff

All paths relative to this worktree:

- artifacts/phase3/task3/resource-receipt.json; verified contentID
  `fba778a565921920057a1b5cd091536ae5cc2aad45fdcaf6ce7e98f6332a35d1`, fileSHA256
  `957b8a1778e17eb71f90398f979b846ce1831f919224ce553d6ee6d9c5a0a85f`.
- artifacts/phase3/task3/resource-reconciliation.json; contentID
  `cd49610f52a28269d13411b90edf8b172e9625f63b586b6e168599ae55ab5283`.
- artifacts/phase3/task3/service-samples-failed.jsonl; SHA256
  `51220bdf8badad5906cf73883b37e73a6e00e90455d8b27c207e39ce25330955`.
- artifacts/phase3/task3/service-samples.jsonl; SHA256
  `513a9321d5fb881a1fb826c6492b7d090c21c0f0b4db6ff416a635e48b7382e7`.
- artifacts/phase3/task3/benchmark-failed.log and benchmark-resumed.log (distinct timing).
- artifacts/phase3/task3/entity-manifest.json, resource-scaler.json, source-records.jsonl.
  CanonicalJSONL741903980bytes/SHA256
  `96d70b4bd4e81c3dcde4b3360a1b9a340f665f4b1b7e36cc2e59f36a0a6f9aba`.
- artifacts/phase3/task3/plugins/neo4j-graph-data-science-2026.09.0.jar.

Large ignored artifacts preserved locally, not committed. Services stopped with
compose stop, preserving measured DBtask3_benchmark and graph. Project
`touchstone-phase3-task3`; containers `touchstone-phase3-task3-postgres-1` and
`touchstone-phase3-task3-neo4j-1`; named volumes
`touchstone-phase3-task3_task3-postgres` / `touchstone-phase3-task3_task3-neo4j`.
Image-created logsvolume `a792383fd4d708d312c6db8fed1a4d415455b94b48b1f779875e8afff50c6381`
also preserved; plugin bind retained. Restart from worktree:
`docker compose -f infra/compose.reckoner-v1.test.yaml up -d --wait`.
Do not rerun importer on existing measuredDB/deletevolumes/globalprune. Other stacks
untouched. Save artifacts/volumes before eventual worktree cleanup.

Changed scope: evidence package/Cypher/migration007,4testmodules/fixtures/benchmark,
SourceHistory/schema semantics, package/lock, CI/testrecipes/preparation, approved
pgvector coreimagepins and compatibility notes. Tasks4/6 use documented private
scaler input; Task11 retains full retrieval/performance gates; Task12deploymentgate.
Self-review concerns: PageRanknonconvergence, deprecated projection API, communityID
stability without randomseed, ownership facts limited to boundedimport, no full
operational/vector coverage claim. Controller independent review follows taskcommit.
