# Phase 1 completion execution plan

Spec: docs/spec/002-phase-1-completion.md, recording the human-approved bounded design.
Execution: fresh subagents, TDD, independent code review; existing isolated worktree on feat/phase-1-completion from merged Phase 1 916c1fe. User will push; no merge/push authorized here.

## Global Constraints

All source data is simulated. No provider calls before reviewed code and verified persistent ledger. Keep phase1-pilot-001 immutable, its USD 0.025043 charged against lifetime USD 10/global and USD 1/pilot. One newly approved phase1-pilot-002, then conditional phase1-baseline-001; no automatic further attempts. Frozen bundle/cohort/model/business metrics unchanged. Native JSON schema only, no tools/fallback/retries/prompt cache. Secrets stay in original protected files, never printed, copied to artifacts, or committed. Preserve measured volume and all original evidence. Role isolation and OTLP-only platform boundary remain binding.

## Task 1 — Versioned native JSON request and offline verification

Files: workloads/reckoner/src/reckoner/baseline/{prompt,provider,pricing,config}.py as necessary; workloads/reckoner/config/ (new versioned config while preserving original); contracts/schemas/run-config-v1.schema.json only if required for immutable format identity; relevant tests including test_prompt.py, test_provider.py, test_pricing.py, test_config.py, runner/CLI fixtures when genuinely affected. No unrelated refactor. Record any required interface expansion before implementation.

- [x] Inspect installed SDK/LiteLLM transformation. Write red HTTP transport tests demonstrating output_config.format schema passes through generation AND count_tokens, is included in canonical native request bytes/reservations, and no tools/cache/retries occur. Schema: object, properties outcome string enum [auto-approve,auto-decline,escalate], required [outcome], additionalProperties false. Preserve existing strict parser and failure tests.
- [x] Implement smallest versioned request change. Preserve old config/template files and support historical evidence reads; produce new config with content-addressed identity binding structured request semantics. Keep explicit dated Haiku, temperature0, max256, timeout60. Prefer native output_config passthrough rather than LiteLLM response_format paths that synthesize tools or deprecated output_format. Reject malformed or changed schema.
- [x] Verify preflight counts exact native schema; any count incompatibility fails closed, never silently omit it. Include provider-injected input overhead in existing conservative bound and preserve independent token-bound halt. Tests assert actual outbound JSON, not mocked completion arguments alone.
- [x] Focused red/green, then full suite once against disposable pinned Postgres (port55433, never55432), Ruff, formatting, diff checks. If missing cached build deps, populate only frozen/build-required cache safely before test; do not relax offline tests. No paid calls or root.env reads. Self-review, commit exact files, report commands/results. Controller dispatches task review and whole-code review before Task2 generation.

## Task 2 — Installed deployment and measured completion evidence

Files: aggregate docs/evidence/phase-1-completion.md, docs/operations/baseline-runbook.md and supporting aggregate receipts only. Controller owns plan status. Scripts/logs, local operation wrapper, generated evidence/backups stay ignored. No product behavior changes in this task; escalate any to controller for review before measurement.

- [x] Build installed image from reviewed committed code with unique tag/digest and provenance; run fabricated Compose smoke including read-only verifier and restart/resume, then separate kind smoke if changed package deployment warrants it. Keep measured services stopped during disposable tests, preserve historical image by immutable digest. Record memory/disk, dependencies, no extra stack.
- [x] Prepare safe operation commands using existing measured project touchstone-phase1-measured and absolute preserved paths under artifacts/phase1. New evidence under artifacts/phase1-completion. Restart pinned Postgres only, checksum migrations, assert original ledger20/0.025043 and no unknowns; verify frozen bundle/hash and runtime-only mounts. Reuse source preparation, never rerun 24M extraction. Verify original image/config artifacts remain retained. Recheck official model/prices. Root controller executes provider-facing commands with original user authorization context; worker prepares/monitors non-provider operations.
- [x] Preflight phase1-pilot-002 with new config and exact reviewed artifact; root executes one paid run after data/access/budget checks. Evaluate/export/report with isolated roles, strict validity20/20 and known costs/bounds. Record full-baseline projection and conservative reservation affordability. If any gate fails, preserve evidence and stop without another paid run.
- [x] Only on passedpilot root preflights phase1-baseline-001 and executes existing1000 cohort at identical settings. Monitor progress without raw prompts/labels/secret values. No repeat dispatched calls. Evaluate/export/report fixed denominators; verify manifests/hashes/OTLP semantic flags, per-tenant and aggregate reconciliation. If incomplete, report truthfully without restarting failed cases.
- [x] Back up database and artifacts excluding credentials, verify readability/checksums, stop measured API/Postgres preserving volume. Update truthful evidence/runbook and installed identities, limits and metric caveats. Preserve original pilot report unmodified. Run scoped docs checks/tracked audit, commit aggregate docs only. Controller final evidence review and finish-branch.

## Status

Completed. Reviewed code 8cf7d06 passed 312 tests, Ruff, Compose and kind validation. Pilot phase1-pilot-002 completed 20/20 valid outcomes for USD 0.009168; baseline phase1-baseline-001 completed 1,000/1,000 for USD 0.458940. Lifetime ledger includes the original failed pilot: 1,040 settled calls totaling USD 0.493151, no unknown charges or overages.

Baseline correctness is 892/1,000 under the approved scoring, with 94/100 fraud missed; CPST USD 8.208291412556053811659192825 and nearest-rank p99 1,315.160667 ms. The naive baseline is measured, not declared a good fraud detector. Both OTLP streams, response hashes and tenant/aggregate arithmetic verified. Database/artifact backups passed readability checks; measured services stopped while retaining their volume. See docs/evidence/phase-1-completion.md for full evidence and caveats.

Branch feat/phase-1-completion is retained for the human to push. No merge, push, or Phase 2 work performed. Final evidence review and documentation commit recorded in the branch history.
