# PROJECT BRIEF — Reckoner & Touchstone

> **Read this file completely before doing anything.**
> This is the canonical source of truth. `CLAUDE.md` and `AGENTS.md` both
> point here and contain no independent instructions.

---

## 0. HARD STOP — READ FIRST

**You must not write any code, create any files, scaffold any repository,
install any dependency, or run any command that changes state until the human
has explicitly approved a written specification.**

Your first and only job right now is:

1. Read this entire brief.
2. Read the questions in §14.
3. Ask the human those questions **in the terminal, one at a time**, waiting
   for each answer before asking the next.
4. Add any further questions of your own wherever this brief is ambiguous.
5. Produce a written specification based on the answers.
6. Present it and **wait for explicit approval.**

Do not batch all questions into one wall of text — ask, wait, ask the next.
Do not assume a default for anything marked `[MUST ASK]`. Do not begin work
"to save time." If you are unsure whether something counts as work, it does —
ask first.

---

## 1. Methodology

This project uses the **Superpowers** skills framework (`obra/superpowers`)
with a spec-driven development approach.

**Required workflow for every unit of work, once approved:**

```
brainstorm → spec → plan → worktree → TDD (red/green/refactor)
→ subagent-driven execution → code review → finish-branch
```

Rules:

- Brainstorming precedes specification. Specification precedes planning.
  Planning precedes code. No shortcuts.
- Every feature is implemented in its own git worktree, on its own branch.
- Tests are written before implementation. Always.
- Each task is dispatched to a fresh subagent with a clean context window.
- Self-review happens before any branch is declared finished.
- If you catch yourself rationalising a skipped step — "this is simple", "the
  skill is overkill", "I need more context first" — that is precisely the
  moment the process exists for. Do not skip it.

**Agent compatibility:** this project must work under both Claude Code and
OpenAI Codex. Do not depend on features exclusive to either. Keep
configuration in plain files (`CLAUDE.md`, `AGENTS.md`, `docs/`, `.specify/`)
rather than agent-specific state.

---

## 2. What we are building, and why

Two deliverables — **Reckoner** and **Touchstone**. They are **not** peers.

### Reckoner — fraud & dispute triage (the workload)

A payments risk console. Transactions arrive; instead of a queue a human
works top to bottom, each transaction passes through a decision pipeline
that:

- auto-approves the clean majority,
- auto-declines the obvious fraud,
- escalates only genuinely ambiguous cases to a human reviewer, with the
  reasoning already assembled into a structured case note.

### Touchstone — the measurement platform (the headline)

The system that watches Reckoner and proves its claims. Every pipeline run emits
traces tagged by graph node, so cost, latency and outcome are attributable
step by step rather than as one blended number per request. Those land in an
event store, are modelled with tested metric definitions, and surface in a
dashboard where cost per correctly resolved dispute, eval pass rate, p99
latency and false-positive rate sit beside a deliberately naive baseline.

### Why the split matters

The purpose of this project is **not** to build a fraud detector. Plenty of
people have done that. The purpose is to be able to say:

> *This change cost this much and moved this number by this amount* —
> with the evidence in one link.

Touchstone is the differentiator. Reckoner exists so Touchstone has something
real to measure. **If time runs short, protect Touchstone.**

### Honesty requirements — non-negotiable

- The dataset is **simulated**. Never describe it, or let any documentation
  imply it is production data. State the limitation before anyone asks.
- Do not write any README, doc, commit message, or comment claiming this
  system ran in production, processed real customer transactions, or was
  deployed at a company.
- If a metric cannot be computed honestly, say so rather than estimating.

---

## 3. Dataset

**CCTD** — IBM Credit Card Transactions Dataset
(Kaggle: `ealtman2019/credit-card-transactions`).

- ~24.3M legitimate + ~29.7K fraudulent transactions (~0.12% fraud rate)
- Relational schema: users, cards, transactions; merchant, MCC, city fields
- Almost no obfuscation — human-readable features, which is why it was chosen

**Rejected alternatives and why** (do not revisit without reason):

| Dataset | Why rejected |
|---|---|
| ULB `creditcardfraud` | V1–V28 are PCA components. No entities, no graph, no readable case notes. Unusable here despite being the famous one. |
| Sparkov | Smaller (1.85M). Also contains merchant names literally containing "fraud", which biases LLMs. |
| Elliptic | Real graph, but Bitcoin-only — no merchant, device, or card entities. |

### Sampling

Full 24M rows will likely not fit the infrastructure budget. Sampling is
expected and acceptable, under these rules:

- **Keep all ~29.7K fraud rows.** Never downsample the positive class.
- Downsample legitimate transactions as needed.
- **Sample by entity, not by row.** Select a set of cards/users/merchants and
  keep *all* their transactions. Row-wise sampling destroys the edge density
  that graph community detection depends on — it would dissolve the exact
  structure we are trying to detect.
- **Record the sampling ratio** and apply it when reporting any absolute
  figure. Metrics must remain honest under sampling.
- Target whatever volume the infrastructure holds comfortably. A stratified
  2–5M rows is a reasonable starting assumption; verify against actual
  resources before committing.

---

## 4. Product & deployment model

- **SaaS.** The platform owns its data plane. Transactions live in our stores.
- **Rejected:** warehouse-native (Snowflake Native App), on-prem / embedded
  SDK, per-tenant mapping DSL. These exist to satisfy enterprise compliance
  departments and prove nothing here.
- **Multi-tenancy:** `tenant_id` on **every row, in every store, from the
  first migration.** Postgres column, ClickHouse column, Neo4j node property.
  Retrofitting this is painful; adding it now is free.
- **Two synthetic tenants**, created by splitting CCTD. This is a `WHERE`
  clause at load time, not an onboarding flow.
- **Shared graph.** One Neo4j graph with `tenant_id` as a property — *not* a
  database per tenant. This is deliberate: fraud rings that span tenants stay
  visible, which is the entire reason the graph exists. The privacy tension
  this creates is a product question to discuss in the PR/FAQ, not something
  to engineer away.
- **Do not build:** SSO, RBAC, audit logs, data residency, billing, customer
  onboarding flows. Enterprise theatre — expensive, proves nothing.
- **Do build:** tenant IDs, canonical event schema, cost attribution, eval
  gates. Cheap, and indistinguishable from production practice.

### Canonical transaction event

Define one canonical schema the pipeline understands. CCTD maps into it at
ingest via an Adapter. **CCTD column names must never leak into Cypher
queries, prompts, or business logic.**

---

## 4a. Repository layout

**One repository.** Two would cost coordination for no benefit at solo pace,
and the Next.js app is a single application serving both the reviewer console
and the metrics dashboard — splitting repos would split one app in half.

```
touchstone/
├── platform/           # Touchstone: OTel collector, ClickHouse, dbt, Dagster
├── workloads/
│   └── reckoner/       # pipeline, graph, decisions, case notes
├── web/                # Next.js — console, dashboard, admin
├── contracts/          # canonical event schema, OTel conventions
├── infra/              # docker-compose, k3s manifests
└── docs/               # PR/FAQ, Structurizr, ADRs
```

The `workloads/` directory is load-bearing. It states the architecture before
anyone reads code: Touchstone is a platform, Reckoner is its first tenant.
Adding a second workload later is a new folder, not a refactor.

### The three boundary rules — non-negotiable

Repository count does not enforce independence. These do:

| Rule | Why |
|---|---|
| Touchstone ingests **only via OTLP** — never by importing Reckoner code | A standard protocol is the contract |
| dbt models key on `workflow_id`, `node_name`, `tenant_id` — **never** on fraud domain terms | Reckoner vocabulary in the metrics layer is the coupling that kills the platform claim |
| Ship a **synthetic second workflow** that emits traces and appears in the dashboard | Demonstrates workflow-agnosticism instead of asserting it |

The third is cheap — a script emitting fake spans — and it is the difference
between claiming the platform is general and showing it.

### When to split

Extract Touchstone to its own repository if it gains a second real consumer,
or if it is published. Extracting later is easy; merging later is not. Until
then, one repository.

---

## 5. Decision pipeline (Reckoner)

### Component roles — strictly separated

| Component | Single responsibility |
|---|---|
| **Jev** (TypeSafe AI) | Scoring and routing. Typed, calibrated probability. Runs on every transaction. |
| **LangGraph** | Orchestration. Owns the graph, the state, the edges. |
| **Claude / OpenAI model** | Case-note synthesis only. Escalated cases only (~2–5% of volume). |
| **Neo4j + GDS** | Entity neighbourhood and fraud-ring detection. |

**Jev is not a replacement for LangGraph.** It is a decision function inside
the graph — a node, or an edge condition.

**Self-hosted open-weight models are OUT OF SCOPE.** Do not add vLLM, NIM,
Ollama, or GPU serving. Explicitly deferred.

### Three outcomes

`auto-approve` · `auto-decline` · `escalate`

This outcome space is hardcoded. It is the model, not a parameter.

### Success criterion

A decision is correct when it matches what a competent reviewer would decide
given the same case. The CCTD label is the oracle.

| Decision | Label = legit | Label = fraud |
|---|---|---|
| Auto-approve | correct | **fraud loss** |
| Auto-decline | **false positive** | correct |
| Escalate | correct-but-costly | correct-but-costly |

**Escalation counts as correct but carries the fixed review cost.** Scored as
wrong, the system learns to guess. Scored as free, it escalates everything.

### Primary metric

```
CPST = (model cost + review_cost × escalations + fraud losses + FP revenue loss)
       ──────────────────────────────────────────────────────────────────────────
                          count(correct decisions)
```

**The denominator is correct decisions, not attempted ones.** Dividing by
attempts makes a cheap model that fails often look good. That inversion is
the entire point of the metric.

The two error types are asymmetric: a missed fraud costs the transaction
amount; a false positive costs a legitimate sale plus customer trust. That
asymmetry is the business story — state the assumed numbers in the PR/FAQ and
defend them.

### Thresholds — amount-aware, admin-configurable

Defaults:

```
review_cost   = $4.00
margin_rate   = 0.30      # profit lost on a declined legitimate sale
t_low_floor   = 0.005     # 0.5%
t_low_ceiling = 0.05      # 5%
t_high        = 0.90
amount_aware  = true
```

Derived per transaction:

```
t_low  = clamp(review_cost / amount, t_low_floor, t_low_ceiling)
t_high = 0.90             # flat in v0
```

Behaviour: fraud probability below `t_low` → auto-approve; above `t_high` →
auto-decline; between → escalate.

Worked examples (CCTD average transaction ≈ $43, so most sit at the ceiling
and amount-awareness bites on the tail — which is where the money is):

| Amount | `t_low` | Meaning |
|---|---|---|
| $20 | 5.0% (ceiling) | Small txn — tolerate more risk |
| $400 | 1.0% | Review pays for itself sooner |
| $4,000 | 0.5% (floor) | Escalate on the faintest signal |

**Two requirements that matter more than the settings form:**

1. **Version the config.** Every decision record stores the ID of the
   threshold config that produced it. Otherwise changing a setting silently
   makes all historical metrics incomparable — the exact failure C exists to
   prevent.
2. **`amount_aware` is an A/B switch**, not a convenience toggle. Flat vs
   amount-aware thresholds on identical data produces a measured CPST delta.
   That is a result, not a design opinion.

### Calibration check — do this early

Jev returns calibrated probabilities. **Verify that on our data before
trusting any threshold.** Bucket predictions by score, compare predicted vs
actual fraud rate per bucket. If miscalibrated, every derived threshold is
meaningless. This takes an afternoon. Finding a calibration problem is a
better story than assuming there wasn't one.

### Case notes — escalated cases only

Structured, fixed schema. **Not free prose** — prose cannot be evaluated, and
anything unevaluable cannot be part of a cost-quality curve.

Required fields:

| Field | Source |
|---|---|
| Verdict recommendation | Jev score + graph signals |
| Confidence | Jev |
| Top 3 risk factors, ranked | Feature attribution |
| Entity neighbourhood summary | Neo4j — e.g. "this card shares a device with 4 accounts flagged in 30 days" |
| Comparable past cases | pgvector similarity over resolved cases |
| What would change the verdict | The reviewer's next action |

Secondary success criterion, for escalated cases: the case note is correct if
a reviewer reading it reaches the same verdict without opening the raw data.
That is an LLM-as-judge eval (DeepEval), with faithfulness checked against
retrieved graph facts (Ragas).

---

## 6. Graph design

- Neo4j + **Graph Data Science** library.
- **Hand-write Cypher.** Do not hide it behind an ORM or query builder.
  Learning Cypher directly is an explicit goal of this project.
- Entities: account/user → card → device → IP → merchant → transaction.
- **GDS community detection for fraud rings.** This is the capability that
  justifies a graph database at all — the signal lives in the shape of the
  connections, not in any single row. If this is not doing real work, the
  graph is not earning its place.
- Also use PageRank / centrality for entity risk propagation.
- **Postgres + pgvector is the baseline we benchmark Neo4j against**, not
  merely a second store. Produce a real comparison with numbers.

**Known risk:** aggressive sampling can destroy edge density and leave
community detection finding nothing. Mitigate via entity-based sampling (§3),
and verify empirically that rings remain detectable after sampling.

---

## 7. Storage — one job each

| Store | Single job |
|---|---|
| **Postgres + pgvector** | OLTP: transactions, decisions, cases, config, reviewer actions. Also the vector-search baseline. |
| **Neo4j** | Graph + GDS. |
| **ClickHouse** | Raw event log. Every agent turn, every decision, every token. High-cardinality, append-only, seconds-fresh. **Source of truth for Touchstone.** |
| **Snowflake** | Modelled marts. dbt reads ClickHouse, writes here. Governed metric layer; the dashboard queries this. |
| **DuckDB** | Local/CI dbt target. Same models, zero cost. |

The ClickHouse/Snowflake distinction is **raw events vs governed metrics**,
not OLAP vs OLTP (ClickHouse is also OLAP). Snowflake earns its place as the
layer where a metric has exactly one tested, versioned definition.

---

## 8. Measurement stack (Touchstone)

| Layer | Tool |
|---|---|
| Instrumentation | **OpenTelemetry GenAI semantic conventions** |
| Cost, budgets, alerts | **LiteLLM** |
| Event store | **ClickHouse** |
| Transformation | **dbt** |
| Semantic layer | **dbt MetricFlow** |
| Data quality | **dbt tests + Elementary** |
| Orchestration | **Dagster** |
| Evals — harness | **DeepEval** |
| Evals — RAG metrics | **Ragas** |
| Metric vocabulary | **DORA four keys**; adoption dimension from SPACE |

**Explicitly cut** (do not reintroduce without discussion): Great
Expectations, Soda, promptfoo, Evidence.dev, full SPACE, CrewAI, LlamaIndex,
Temporal, Backstage.

### Baseline

**Do not look for historical data.** Ship a deliberately naive v0 — single
model call, no routing, no caching, no graph — run it against N transactions,
record CPST, latency and eval pass rate. *That is the baseline.* It is real,
it is ours, and conditions are identical to the later measurement, which
makes it stronger than borrowed numbers.

Then ship v1 with Jev routing + graph + cascade and measure the delta. That
step produces the first genuine "problem → metric → decision → result" story.

### Required outputs of Touchstone

- Cost per graph node, per run, per task, **per tenant**
- Cost to serve per tenant → unit economics, not just aggregate spend
- Eval pass rate per run, trend over time, which cases regressed
- Coverage map: which flows have eval coverage and which do not
- Task lead time; adoption counts
- Baseline vs current, side by side
- Budgets with alerts *before* overrun
- **Agreement rate** — reviewer's actual decision vs model recommendation.
  This is the honest answer to "how do you know the LLM output was any good."
- **The threshold sweep chart** — CPST across a grid of `t_low`/`t_high`, with
  guardrail constraints (false-positive rate, escalation volume) marking
  rejected regions, and the chosen operating point annotated. Probably the
  single best artifact in the project.

---

## 9. Frontend

**One Next.js application, three surfaces.** Not multiple apps.

### Reviewer console
- Case queue
- Structured case note
- **Graph neighbourhood rendered inline** — the visual centrepiece
- Approve / decline actions; every click logged as ground truth

### Metrics dashboard
- Cost, quality, latency, agreement rate, threshold sweep
- Queries Snowflake

### Admin settings
- The six threshold parameters (§5), with live preview of resulting behaviour
- Config version history visible

### Design direction

Modern, professional, production-like. **Explicitly not "AI-ish".**

- Dense information; not large cards floating in whitespace
- **Keyboard-first**: `j`/`k` through the queue, `a`/`d` to decide. Reviewers
  process hundreds of cases a day.
- No chat interface. No sparkles. No purple gradients. No "✨ AI-powered"
  anything.
- Reference points: **Linear, Stripe Dashboard, Datadog**
- Accessible, responsive, fast

---

## 10. Engineering standards

### Design patterns — earned, not decorative

Use these where the domain genuinely calls for them:

| Pattern | Where it belongs here |
|---|---|
| **Strategy** | Risk scoring: Jev / heuristic / LLM / ensemble, swappable by config |
| **Chain of Responsibility** | The decision cascade — each handler resolves or passes on |
| **Adapter** | CCTD rows → canonical transaction event |
| **Repository** | One interface per store (Postgres / Neo4j / ClickHouse) |
| **Decorator** | Cost + latency instrumentation wrapping any node without touching node logic |
| **Observer** | Decision events → OTel → Touchstone |
| **Builder** | Assembling a case file from graph, scores, history |
| **Circuit Breaker** | Model provider failure → degrade to rules, never drop a transaction |
| **Specification** | Composable eligibility / routing rules |

**Do not use** Singleton, Visitor, Prototype, Flyweight — nothing here needs
them.

**Anti-goal:** a codebase full of `AbstractRiskStrategyFactoryProvider`.
Over-abstraction reads as junior. Every pattern must be justifiable by a
concrete requirement in this brief. If you cannot name the requirement, do not
use the pattern.

### Diagrams

| Layer | Tool |
|---|---|
| Architecture (C4: context → container → component) | **Structurizr DSL** — diagrams as code, versioned, one model many views |
| Sequence / state / flow | **Mermaid**, in-repo, renders in GitHub |
| Data lineage | **dbt docs** (generated) |
| Agent graph | **LangGraph's own generated diagram** — do not hand-draw it |
| Drift check | **archify**, at the end of each phase, to verify diagrams still match the code |

Diagrams are design artifacts, kept current — not decoration added at the end.

### Testing

- TDD per Superpowers: red → green → refactor.
- Unit tests for decision logic, threshold derivation, cost calculation.
- Integration tests for each store adapter.
- Eval suites (DeepEval + Ragas) gate case-note quality in CI.
- Metric definitions carry dbt tests.

---

## 11. Budget constraints — take these seriously

Approximately **$5–10 per platform**, demo purposes.

| Service | Constraint |
|---|---|
| Neo4j | **Aura free tier will not hold 24M rows.** Docker locally, or a cheap VM. |
| ClickHouse | Local Docker preferred; Cloud dev tier only if it fits. |
| Snowflake | Use the 30-day trial credits deliberately. Smallest warehouse, auto-suspend at 60s, dbt **on a schedule, never on every event**. |
| Kubernetes | Managed clusters exceed budget. Use **k3s on one cheap VM**, or `kind` locally. |
| LLM APIs | Fine at ~$10 *because* case notes run only on the ~2–5% escalated slice. Keep it that way. |
| Jev | Cheap by design — input ~$0.042/M tokens, output free. |

**If a design choice would blow the budget, flag it and propose the cheaper
equivalent. Never silently provision paid resources.**

---

## 12. Known risks — investigate early

1. **Jev API access.** Launched ~mid-September 2026. Whether a key is
   obtainable today, and what rate limits apply, is **unverified**. If gated,
   the cascade still works with a cheap classifier in that slot — but confirm
   in week one, not month two.
2. **GDS on sampled data.** Community detection needs edge density.
   Entity-based sampling mitigates; verify empirically.
3. **Volume vs budget.** 24M rows across Postgres + Neo4j + ClickHouse
   simultaneously on hobby infrastructure is not realistic. Expect to sample.
4. **Scope.** This is a large build. The failure mode is building Reckoner
   beautifully and never reaching Touchstone. Stand Touchstone up early,
   against Reckoner alone.

---

## 13. Suggested sequencing — for discussion, not yet approved

1. **PR/FAQ** — one page: what problem, for whom, what number proves it
   worked. **Written before any architecture.**
2. **Reckoner v0** — naive, single model call, no routing. Records the baseline.
3. **Touchstone v1** — OTel + ClickHouse + dbt + dashboard, measuring Reckoner only.
4. **Reckoner v1** — Jev gate, graph, cascade, case notes. Measure the delta in Touchstone.
5. **Touchstone v2** — threshold sweep, budgets, eval coverage map, per-tenant unit
   economics.

Step 4 produces the first real result.

---

## 14. Questions you must ask before proposing a specification

Ask these **in the terminal, one at a time**, waiting for each answer. Add
your own wherever this brief is ambiguous.

**Environment & access**

1. `[MUST ASK]` Which language/runtime for the backend — Python, TypeScript,
   or split (e.g. Python for the pipeline, TypeScript for the web app)?
2. `[MUST ASK]` Do you have a Jev API key yet? If not, should we build
   against an interface with a stub implementation and swap it in later?
3. `[MUST ASK]` Which LLM provider for case notes — Anthropic, OpenAI, or
   both behind LiteLLM?
4. `[MUST ASK]` Has the CCTD dataset been downloaded, and where does it live?
5. `[MUST ASK]` Local Docker Compose, a remote VM, or both? What hardware is
   available (RAM / disk)? This caps the sample size.
6. `[MUST ASK]` Is there an existing repo, or do we create one? The layout in
   §4a is decided — confirm the repo name (`touchstone`, or
   `reckoner-touchstone`).

**Scope & sequencing**

7. `[MUST ASK]` Confirm the sequencing in §13, or state a different order.
8. `[MUST ASK]` Kubernetes from the start, or added after services work under
   Docker Compose?
9. `[MUST ASK]` Will you write the PR/FAQ, or should the agent draft it for
   your review?

**Product detail**

10. `[MUST ASK]` What should the two synthetic tenants represent — e.g. two
    merchants of different size and risk profile? How should CCTD be split
    between them?
11. `[MUST ASK]` Target `N` for a baseline evaluation run — how many
    transactions constitute one measured run?
12. `[MUST ASK]` Should the reviewer console support a simulated reviewer
    (label-as-oracle) for automated runs, alongside real human clicks?

**Standards**

13. `[MUST ASK]` Preferred Python package manager and project layout (uv,
    poetry, pip-tools)?
14. `[MUST ASK]` Any existing lint / format / commit conventions to follow?
15. `[MUST ASK]` Should Superpowers be installed as a Claude Code plugin in
    this repo, and what is the equivalent expectation for Codex?

---

## 15. Reminder

**Nothing begins until the human approves a specification.**

Ask the questions. Write the spec. Present it. Wait.
