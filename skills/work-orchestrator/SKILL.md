---
name: work-orchestrator
description: >
  Drive a GitHub issue all the way to a reviewable PR through a recursive,
  accuracy-driven workflow: draft a spec (C1), review the spec before building,
  build tests (S3), implement — recursively decomposing the work into sub-units
  whenever the planner (C2) judges a node to be more than one independent unit
  (no fixed depth) — review the diff in fresh-context subagents (C4 + S11 + a
  clean-room intent-review), validate (C5), and gate (C6) at two tiers; a single
  PR is opened only if the entire node tree is gate-clean. The recursion,
  node-tree bookkeeping, stage sequencing, gating, and roll-up all run in Python
  at ~0 model tokens via `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" work-orchestrator`; this body only
  drives the outer extract→judge→continue loop and produces one compact
  structured artifact per round. Use when the user asks to "orchestrate", "run
  the full workflow", "drive the pipeline", "spec to ship", or "automate a
  feature from issue to PR".
allowed-tools: Read, Bash, Task
delegates-to-cli: true
---

# /work-orchestrator

A **recursive, accuracy-driven, Python-load-bearing** workflow engine (C7). It drives a GitHub
issue to a reviewable PR by composing the foundry skills over a **recursive node tree**: the root
node is the issue; the planner (C2) splits any node it judges to be more than one independent unit,
and each leaf runs the full build+review+gate lifecycle. All bookkeeping lives in
`"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" work-orchestrator` (the control plane); **this body drives only the outer
extract→judge→continue loop** — re-invoking the CLI until it exits non-7 — and produces exactly one
compact structured artifact per round.

## When to invoke

- When the user wants to drive a feature from a GitHub issue all the way to a reviewable PR.
- When the user asks to "orchestrate", "run the full workflow", "drive the pipeline", or "spec to ship".
- To resume a partially-completed run (state is persisted under `.foundry/workflow-state/<run-id>/`).

## When invoked

1. **Start the run (extract leg).** Invoke the control plane:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" work-orchestrator <issue-ref> --extract-only
   ```
   It walks the node tree to the next frontier node + stage needing model work, writes
   `round-N-prompts.json` under `.foundry/workflow-state/<run-id>/`, and **exits 7** ("awaiting
   judgments"). Exit 0/4/5 instead means the tree is already complete (see the Output section).
2. **Judge the one round (fresh-context subagent).** Read the single prompt item in
   `round-N-prompts.json`. It carries curated inputs only (the node's spec path, the diff, the
   upstream reports — never this session's history) and a `response_format`. Dispatch a
   **fresh-context subagent** to perform that one stage (a Mode-2 structured judgment, or a Mode-3
   delegated sub-pipeline that runs the whole downstream skill and returns its structured report).
   Write its result to `round-N-judgments.json` shaped per the prompts file's `_schema` hint —
   one `items[]` entry keyed by the prompt item's `id`.
3. **Continue the loop (continue leg).** Feed the judgment back:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" work-orchestrator <issue-ref> --continue --judgments round-N-judgments.json
   ```
   The control plane validates + applies the judgment, advances node/stage state (including the
   split-vs-build decision and the spec-review one-retry bound), and either emits the next round
   (exit 7 — go to step 2) or renders the final report (exit 0/4/5 — go to step 4).
4. **Surface the outcome.** When the CLI exits non-7, relay its rendered WorkflowReport: the node
   tree, each leaf's node-gate verdict, the opt-in root-review summary, the overall tree-gate, and
   — when not fully clean — the resumable-state location. A PR is opened **only if the tree-gate is
   `pass`** (every leaf clean *and* every enabled root review clean); otherwise no PR is opened.
   Merge is advisory — **never auto-merge**.

## How to invoke

Invoke as `/work-orchestrator` with the issue reference (a GitHub URL, `owner/repo#N`, or a bare
`#N`). The body never orchestrates in-session — it shells `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" work-orchestrator` and
loops on exit 7. Useful flags (full surface in `references/stages/pipeline.md`):
`--run-id` (resume a specific run), `--start-from NODE`, `--no-branch`, `--no-pr`,
`--force-pr` (open a draft even when not clean), `--push` (push the feature branch
`foundry/<slug>` before `gh pr create` — off by default; without it the exact push + create
commands are surfaced when the branch isn't on the remote, #1107), and the opt-in root reviews `--with-compliance`,
`--with-threat-model`, `--with-data-flow`, `--with-arch-review` (off by default).

A bare (non-`--continue`) re-invocation of an existing run **resumes** it — it never restarts from
the `spec` stage. On a run that has already **settled**, that means it re-runs the finalize + PR
stage honoring the current flags, so to (re)open, force, or push the PR on a finished run, re-invoke
**without** `--continue` (optionally with `--force-pr` / `--push`); do NOT feed a judgment to a
settled tree via `--continue` (#1194). Note: `work-orchestrator` does **not** auto-consume M-tier
`.forge-foundry/context/db/` artifacts — reference M-tier findings in the issue body instead (#1194 3c). The canonical
per-node lifecycle, the three execution modes, the recursion, the two gate tiers, and the
root-level opt-in reviews are documented in `references/stages/pipeline.md` — read it to understand
what each round is doing.

## Output

A WorkflowReport (node tree + per-leaf node-gate verdicts + opt-in root-review summary + overall
tree-gate recommendation) rendered by the control plane and relayed to the user, plus the
resumable-state location under `.foundry/workflow-state/<run-id>/`. The CLI exit code is the
authoritative outcome: `0` tree complete + clean (PR opened, or prepared with `--no-pr`); `4`
tree-gate not clean (no PR — report surfaced); `5` a hard stage failure halted a node; `2` issue
ref unresolvable; `3` malformed workflow/judgments; `7` awaiting judgments. The full contract lives
in `## Invariants` below.

## Invariants

- The recursion, node-tree bookkeeping, stage sequencing, structured-data marshalling, gating, and
  roll-up rendering all run in **Python at ~0 model tokens** (`"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" work-orchestrator`).
  This body drives only the outer extract→continue loop and never orchestrates in-session.
- Each model turn is **exactly one compact structured artifact** — the control plane never narrates,
  the model never orchestrates. Prompts carry curated inputs only, never this session's history.
- A node **splits** whenever C2 judges it to be more than one independent unit (accuracy-driven, no
  fixed depth); a **leaf** is any node C2 calls `atomic`. All leaves build onto one branch
  `foundry/<root-issue-slug>`, sequentially.
- Two gate tiers: a **node-gate** per leaf (C6 over its review chain + intent-review + spec-validate)
  and a **tree-gate** at the root (C6 over all node verdicts + the opt-in root reviews). A PR is
  opened **only if the tree-gate is `pass`**; `--force-pr` opens a draft instead. Merge is advisory —
  **never auto-merge**; C7 does **not** push by default and **never pushes/merges the base
  branch** — `--push` pushes the feature branch only so the PR can be raised via `gh pr create` (#1107).
- System-shaped reviews (S70/S60/S10/C8) are **root-level and opt-in** (off by default); they run
  once at the root against the assembled tree and feed the tree-gate, never per-leaf.
- §3.4 AI-quality auditing is composed **inside** each review skill (C4/S11/… fire their own
  C10/C11); C7 does not invoke them separately.
