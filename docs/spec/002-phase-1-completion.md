# Phase 1 completion — native structured output

Status: approved by the human in conversation on 2026-09-26, including one revised pilot and the conditional baseline. This records that bounded design; it does not reopen the foundation decisions.

## Scope and success

Complete the frozen 1,000-case simulated CCTD baseline using dated Haiku 4.5. Enable Anthropic native JSON-schema output for exactly one outcome: auto-approve, auto-decline, or escalate. Preserve strict local validation, single calls, no retries/fallback/tools/prompt caching, temperature 0, 256 output tokens, and current price/budget checks. No decision-quality tuning, cohort changes, or Phase 2 work.

Version the full request construction/schema and run configuration. The schema is part of the immutable request, provider wire payload, token-counting input, and reservation calculation. Preserve historical config/prompt artifacts and all failed pilot results; do not reclassify fenced/prose output as valid. Refusal, truncation, invalid schema/usage, or uncertain charges remain failures. Provider grammar compilation caching is an unavoidable native structured-output implementation detail; it is distinct from prompt/token caching, which remains disabled and checked.

Keep the original phase1-pilot-001 and its 20 settled charges totaling USD 0.025043. Reuse the exact persistent measured database/volume and frozen bundle. One new run phase1-pilot-002 uses the existing 20 pre-2019 cases. Only after 20 valid outcomes, known bounded usage, no uncertainty/overages, verified exports, and an affordable full reservation may phase1-baseline-001 run the original 100 fraud/900 legitimate 2019 cases. No automatic third pilot or baseline retry. All pilot costs remain inside the cumulative USD 1 sublimit and all provider costs inside USD 10 lifetime total.

## Implementation and validation

Use the existing LiteLLM provider boundary with the native output_config.format JSON schema; prove actual HTTP generation/count requests contain the same schema and no synthetic tool fallback. Retain the current parser and request allowlist. Configuration identity must change when request semantics change, while old stored evidence remains readable. Preserve the existing pricing formula unless concrete testing shows a required correction; schema and added provider input overhead must be covered by preflight bounds.

TDD covers exact wire payloads, invalid/refused/truncated responses, hash changes, frozen history, no retry/tool/cache fallback, and token reservation including schema. Run focused tests, full offline/integration checks, installed-image smoke, and independent review before paid calls. Record installed image/code/config/prompt identities. Existing architecture boundaries and metrics are unchanged.

Report exact aggregate/per-tenant counts, costs, CPST components, quality/validity rates, and nearest-rank p99 with population counts. Retain known failures in fixed denominators. Completion requires all 1,000 expected outcomes and known costs; no guaranteed success is claimed before measuring. Back up state, verify backup readability, stop measured services preserving volume, and hand off the branch without merging or pushing.

Reference checked 2026-09-26: https://platform.claude.com/docs/en/build-with-claude/structured-outputs (Haiku 4.5 support, output_config.format, extra input tokens, token counting, refusal/truncation caveats).
