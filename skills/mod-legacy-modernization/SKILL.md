---
name: mod-legacy-modernization
description: >
  Drive the full SQL-database modernization chain end-to-end by composing the
  M-tier workbench skills in order — M1 schema-analyze → M2 stored-proc-analyze →
  M3 modernization-strategist (which may run an optional in-session C8 architecture-review
  pass per §3.4) → M4 sp-converter looped over the top-N highest-risk stored procedures — dispatching
  each stage as a subagent that invokes the stage's `/mod-*` skill, persisting
  per-stage pass/fail to a resumable state file, and rolling the stage outputs up
  into a single aggregated modernization report. Use when the user asks to
  "modernize a legacy database", "run the SQL modernization chain", "convert a
  stored-procedure monolith", or "build the M0 plan".
---

# /mod-legacy-modernization

The SQL-database modernization chain ORCHESTRATOR: an M-tier orchestrator skill that
composes the other modernization-workbench skills by reference. It takes a DDL-dump /
source input and drives the chain — M1 `/mod-db-schema-analyze` → M2
`/mod-stored-proc-analyze` → M3 `/mod-modernization-strategist` → M4
`/mod-sp-converter` — dispatching each stage as a subagent that invokes the stage's
downstream `/mod-*` skill, then aggregates the per-stage outputs into a single
`modernization-report.md`. M0 owns the per-SP fan-out over M4 (M4 itself is single-SP)
and the roll-up; it ships no bespoke per-stage logic — M1-M4 stay focused on their own
concerns. The run is stateful and resumable.

M0 exists because consumers should not have to learn the chain's stage list, the
M1→M2→M3→M4 ordering, or the convention that M4 needs the top-N highest-risk SPs from
M2's `RiskScore`. M0 owns the chain contract so the workbench skills can stay focused.

## When to invoke

- When the user wants to drive a legacy database all the way from a DDL dump to an
  aggregated modernization report in one workflow — not four hand-sequenced commands
  with manual top-N selection.
- When the user asks to "modernize a legacy database", "run the SQL modernization
  chain", "convert a stored-procedure monolith", or "build the M0 plan".
- To resume a partially-completed run (the previous run's state was persisted and can
  be picked up from the first incomplete stage).
- When a reviewer wants a single `modernization-report.md` to circulate before
  greenlighting the migration, rather than reading four separate artifacts.

## When invoked

1. **Load the chain definition + resume state.** Read the chain from
   `references/workflow.yaml` — the canonical M1→M2→M3→M4→rollup stage list, the stage
   order, and each stage's downstream `/mod-*` skill (`schema`→`/mod-db-schema-analyze`,
   `stored-procs`→`/mod-stored-proc-analyze`, `strategy`→`/mod-modernization-strategist`,
   `conversions`→`/mod-sp-converter`; the `rollup` stage is M0's own aggregator, no
   downstream skill). Stages `schema` and `rollup` are mandatory; `stored-procs`,
   `strategy`, and `conversions` are independently skippable via their `skip_when` flag.
   Determine the run id: if the user passed `--run-id`, use it; otherwise mint a fresh
   `run-<8hex>`. **Resume check:** if `.foundry/workflow-state/<run-id>/state.yaml`
   exists, load it, read `last_completed_stage`, and resume from the **stage immediately
   after** it (already-completed stages are not re-run; a `skipped` stage does not
   advance the cursor). Honor `--start-from STAGE` as an explicit override. If no state
   file exists, start fresh from `schema`. Verify the target DB-dump / source input
   exists (file-mode only — a missing input is a hard error).
2. **Run each chain stage as a dispatched subagent.** Walk the stages in
   `references/workflow.yaml` order. For each stage, dispatch a fresh subagent
   (curate exactly the context it needs; do
   not let it inherit this session's history) that invokes the stage's downstream
   `/mod-*` skill: M1 `/mod-db-schema-analyze` against the DDL input →
   M2 `/mod-stored-proc-analyze` against the same input + M1's `schema.json` →
   M3 `/mod-modernization-strategist` over the M1+M2 digests (the M3 stage may run an
   optional in-session C8 `/architecture-review` pass over its emitted `decomposition.json`
   per §3.4 — that pass happens *inside* the M3 stage, M0 does not invoke C8 directly) →
   M4 `/mod-sp-converter`
   **looped over the top-N highest-risk stored procedures** read from M2's
   `stored-procs.json`, sorted by `RiskScore.total` descending (ties break on name).
   **M0 owns the per-SP loop** — it dispatches one M4 subagent per top-N SP, because M4
   is single-SP; `--top-n` bounds the loop breadth (default 10; `0` disables the
   conversions fan-out). **Context passing:** pass each stage its required inputs
   explicitly — the DDL path for `schema`/`stored-procs`, the shared context-dir holding
   the prior stages' digests (default `.foundry/context/db/`) for `strategy`/`conversions`,
   and the run-id for state continuity; never let a subagent inherit this session's
   history. **Failure-modes opt-out:** honor per-failure-mode opt-outs declared in the
   consuming project's `.foundry/failure-modes.yaml` — a consumer-disabled failure mode
   is skipped while the chain still runs. **Persist:** after each stage, write its
   pass/fail status + artifact paths to `.foundry/workflow-state/<run-id>/state.yaml` so
   the run stays resumable. Halt the walk on a hard stage failure, recording which stage
   aborted.
3. **Aggregate + report.** Roll the per-stage outputs up into the aggregated
   `modernization-report.md` — executive summary, schema overview (M1), stored-procedure
   risk distribution (M2), proposed decomposition (M3), top-N conversion previews (M4),
   and — when the M3 stage ran its optional in-session C8 pass — an "Inherited sub-call
   findings" section that folds those `/architecture-review` findings in (the §3.4
   orchestrator carve-out — M0 declares no sub-calls of its own; it inherits whatever the
   delegated stages surfaced). Every
   name in the report traces back to a deterministic upstream artifact (the roll-up is
   rendered from on-disk M1-M4 outputs, not from a fresh LLM call). Surface the report
   plus the resumable-state location (`.foundry/workflow-state/<run-id>/`) and the list
   of skipped stages to the user.

## How to invoke

Invoke as `/mod-legacy-modernization` with the DDL-dump path (a directory of `.sql`
exports or a single `.sql` file). To resume, pass `--run-id ID` and optionally
`--start-from STAGE` to override the resume cursor; `--skip-stage NAME` drops an
optional stage (`stored-procs`, `strategy`, or `conversions`; `schema` and `rollup` are
mandatory); `--top-n N` bounds the M4 conversion loop breadth (default 10, `0` disables
the fan-out). The canonical stage list, order, and per-stage downstream-skill mapping
live in `references/workflow.yaml` — read it to drive the walk. YOU (the harness LLM)
drive the chain directly via dispatched subagents — there is no Python control-plane to
shell out to on the interactive path.

## Output

An aggregated `modernization-report.md` (under the context-dir, default
`.foundry/context/db/`) that rolls up every M1-M4 artifact — schema overview,
stored-procedure risk distribution, proposed decomposition, top-N conversion previews,
the inherited C8 architecture-review findings (only when the M3 stage ran its optional
in-session C8 pass), skipped stages, and a reproduction
recipe — alongside every upstream stage artifact (left in place, referenced by relative
path). Run state is persisted to `.foundry/workflow-state/<run-id>/state.yaml` (per-stage
pass/fail + artifact paths) so a partially-completed run can be resumed. The
authoritative stage order, the M4 top-N loop contract, and the resume + sub-call
inheritance contract live in `## Invariants` below.

## Invariants

- The chain order is exactly the one in `references/workflow.yaml` (M1 schema →
  M2 stored-procs → M3 strategy → M4 conversions → rollup) — drive the walk from that
  file by path; do not inline or reorder it. The chain is sequenced (not parallel)
  because each stage's input is the previous stage's output (M1 `schema.json` →
  M2 `stored-procs.json` → M3 `decomposition.json` → M4 `conversions/` → rollup).
- M0 owns the M4 top-N loop: M4 (`/mod-sp-converter`) is single-SP, so M0 dispatches one
  M4 subagent per stored procedure in M2's top-N highest-risk list (sorted by
  `RiskScore.total` descending, ties on name). `--top-n` bounds the breadth (default 10);
  `--top-n 0` disables the conversions fan-out while still running M1-M3 + rollup.
- Per-stage state is persisted to `.foundry/workflow-state/<run-id>/state.yaml` after
  every stage, recording its pass/fail + artifacts; a run is resumable by reading
  `last_completed_stage` and restarting at the next incomplete stage. A `skipped` stage
  does not advance the cursor.
- **§3.4 orchestrator carve-out applies.** M0 declares no sub-call rules of its own —
  it is a composition primitive, not a review-shaped one. It **inherits** the §3.4
  passes that the delegated stages may run: the M3 stage may run an optional in-session
  C8 `/architecture-review` pass over its `decomposition.json` (it is an in-session pass
  the agent runs, not a CLI sub-call — the Python CLI never invokes an LLM), and when M3
  ran it, those findings are folded into the report's "Inherited sub-call findings"
  section (M1/M2 are context-class exempt; M4 has no T1 sub-call). M0 never invokes C8
  directly.
- Per-failure-mode opt-outs declared in the consuming project's
  `.foundry/failure-modes.yaml` are honored: a consumer-disabled failure mode is skipped
  while the chain still runs.
- The slash command keeps the M-tier `mod-` prefix (`/mod-legacy-modernization`) so the
  modernization namespace stays distinct and cannot collide with the compliance-shaped
  S60 `/data-flow-map` (charter §3.2.M).
- This skill composes the existing modernization-workbench skills by reference (the
  `references/workflow.yaml` mapping); it defines no bespoke agents and contains no
  per-stage analysis logic of its own. The aggregated report is rendered from the
  on-disk M1-M4 artifacts (anti-hallucination: every name traces back to a deterministic
  upstream parse), not from a fresh LLM call.
- No Bedrock backend and no Python control-plane on the interactive path; the harness
  LLM drives the chain via subagents.
