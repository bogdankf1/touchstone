# CLAUDE.md

**Read `PROJECT_BRIEF.md` in full before doing anything else.** It is the
canonical source of truth for this project. This file contains no independent
instructions.

## Hard stop

Do not write code, create files, scaffold a repository, install dependencies,
or run any state-changing command until the human has explicitly approved a
written specification.

Your first job:

1. Read `PROJECT_BRIEF.md` completely.
2. Ask the questions in §14 of that brief **in the terminal, one at a time**,
   waiting for each answer before asking the next.
3. Add your own questions wherever the brief is ambiguous.
4. Write a specification from the answers.
5. Present it and wait for explicit approval.

## Methodology

Superpowers (`obra/superpowers`) with spec-driven development:

```
brainstorm → spec → plan → worktree → TDD → subagent execution
→ code review → finish-branch
```

No shortcuts. If you find yourself rationalising a skipped step — "this is
simple", "the skill is overkill" — that is the moment the process exists for.

## Agent parity

This repo must work under both Claude Code and OpenAI Codex. `AGENTS.md`
mirrors this file. Keep configuration in plain files; avoid agent-exclusive
features.

## Layout

One repository. `platform/` (Touchstone), `workloads/reckoner/`, `web/`,
`contracts/`, `infra/`, `docs/`. See §4a of the brief for the three boundary
rules that keep Touchstone workflow-agnostic — they are non-negotiable.

## Honesty

The dataset is simulated. Never imply production deployment or real customer
data in any code, comment, commit message, or document.
