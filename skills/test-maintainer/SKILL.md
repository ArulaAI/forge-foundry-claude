---
name: test-maintainer
description: >
  The closed-loop autonomous test maintainer. On a PR-to-main (or any git diff)
  it seeds the changed symbols, finds the empirically impacted tests via a
  coverage-primary engine (runtime coverage map ∩ changed lines, augmented by a
  static symbol/call graph for new symbols and coverage gaps), composes S1
  sdet-architect for the repo's testing conventions and best-practice gaps,
  proposes the minimal test edit per impacted test (update / add / delete /
  skip), runs the mandatory C10 (FM1-5) + C11 (FM6-10) self-review audit over
  every edit, then VERIFIES each edit green by actually running the impacted
  tests — refining on red up to a bounded budget and reverting if still red —
  and finally branches and opens a PR of the verified-green edits for human
  approval (never auto-merges, never touches production source). Use when the
  user asks to "maintain the tests on this PR", "update tests for code changes",
  "sync tests with production code", "fix the failing tests for this diff", or
  after refactoring production code that may have invalidated existing tests.
allowed-tools: Read, Grep, Glob, Bash
---

# /test-maintainer

The closed-loop autonomous test maintainer. Diff the working tree against a git ref → find the
**empirically impacted** tests (coverage-primary) → compose S1 for conventions + gaps → propose
the minimal edits → run the C10/C11 self-review gate → **verify each edit green by running the
impacted tests** (refine on red ≤N, revert if still red) → branch + open a PR of the
verified-green edits for human approval. It never modifies production source and never
auto-merges.

## When to invoke

- On a **PR to main** — keep the testing stack in sync with the change and open a follow-up PR.
- After refactoring production code — update the affected tests and prove them green.
- After a feature change that alters existing behavior — update + verify the impacted tests.
- During code review — close real coverage gaps in the diff's blast radius.
- When the user asks to "maintain the tests on this PR", "update tests for code changes", "sync
  tests with production code", or "fix the failing tests for this diff".

## When invoked

1. **Seed + impact (extract phase).** Resolve the target path (default: cwd; a missing path is a
   hard error). `--diff-against REF` is required. Run the deterministic stages in-session: diff
   against the ref → the changed **symbols** (funcs/methods/classes); enforce the max-files
   budget (default 20 — exceeding it is a hard stop). Build the **coverage-primary** impact set:
   run the suite once with per-test coverage (or consume an existing `coverage.xml` / `lcov.info`
   / `jacoco.xml`) and intersect the reverse index (test → executed line) with the diff's changed
   lines — these are the empirically impacted tests. All three stacks produce a `measured=True`
   per-test map: Python via one-shot `--cov-context=test`; **JS/TS + Java via a cached, incremental
   `CoverageIndex`** (`.foundry/test-maintainer/coverage-index/<stack>.json`, gitignored) — per
   test FILE (JS/TS) / CLASS (Java) isolation runs measured once on `main` and consumed read-only
   on PRs (base-side intersection). A **cold cache is expensive** (one isolation run per test
   file/class; slowest for Java) — the CI overlay's `actions/cache` step persists it across runs;
   `s2.max_coverage_runs` (default 50) caps a cold build / staleness sweep, and exceeding it (or a
   missing runner / unparseable output) **falls back to the static graph** (`measured=False`,
   logged) rather than hanging — never fabricating attribution. **Augment** with the static symbol /
   downstream-call graph for brand-new symbols (no coverage yet) and for **gap detection** (a
   changed symbol no test reaches → an `add` candidate). Union the two; coverage wins on conflict.
   Emit `impacted.json` + `coverage-map.json`. Then **compose S1 `sdet-architect`** scoped to the
   impacted area + a sample of the repo's tests → `s1-findings.json` (house-style conventions +
   blast-radius-scoped best-practice gaps). One prompt item per impacted (source, test) pair
   (id prefix `narrative:<test-file>`). No impacted tests and no gaps → nothing to judge, exit `0`.
2. **Propose the minimal edit per impacted test (round 1 — judge).** For each item in
   `round-1-prompts.json`:
   - Read the `system` / `user` fields — they carry the before/after source diff, the current
     test content, the impact class, and (when present) the S1 conventions + gap blocks.
   - Respond with JSON matching `response_format.schema`: `verdict` (`update` / `add` / `delete`
     / `skip`), `after_content` (full new test file content for `update` / `add`; empty for
     `delete`; absent for `skip`), `reasoning` (1-3 sentences). Style-match `update` edits to the
     S1 conventions; target `add` edits at the S1 gaps. Propose only the **minimal** edit — never
     change test intent or unrelated assertions. Stay within the per-run budget. No markdown
     fences.
   - Collect the responses into a `JudgmentEnvelope` and write `round-1-judgments.json` in the
     state dir:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "test-maintainer",
       "round": 1,
       "items": [
         {"id": "narrative:<test-file>", "response": {"verdict": "update", "after_content": "...", "reasoning": "..."}}
       ]
     }
     ```
3. **Self-review (C10/C11 gate), then VERIFY each edit green (continue after round 1).** Apply the
   verdict proposals and run the mandatory §3.4 **C10 (FM1-5) + C11 (FM6-10)** failure-mode audit
   over every edit's after-content: FM5/FM9 findings **drop** the proposal; FM1-FM4 surface as
   warnings. Then run the **strict green-gate** over each surviving edit:
   - Write the edit to its **test file** (never a production path — that is a hard error), then
     **run the impacted test subset** for that file via the stack runner (pytest / jest-vitest /
     maven-gradle). Run the whole impacted subset, not just the one edited test, so an edit that
     fixes its target but breaks a sibling is caught.
   - **Green** → keep the edit. **Red** → re-judge with the failure text as context (the refine
     round: re-propose a corrected edit and re-run), bounded by `s2.max_refine` (default 2). Still
     red after the budget → **revert** the edit to its pre-edit content and record
     `{test, reason, last_failure}` under **Needs-human**. Only verified-green edits survive.
   - Emit `verify-results.json` (per-edit `kept` / `reverted` dispositions).
4. **Branch + open a PR of the verified-green edits (after self-review).** If ≥1 verified-green
   edit exists: `git checkout -b forge/test-maintainer-<short-sha>`, commit **only** the green
   edits, `git push`, and `gh pr create` for human approval — **never auto-merge.** The PR body
   carries the summary (`N applied (verified green); A updates, B adds, C deletes`), a **Needs
   human attention** section (un-fixable tests + last failure; un-closed S1 gaps), and provenance
   (diff ref, impacted set, per-test coverage/static source). Zero green edits → **report only,
   no branch/PR** (in CI, optionally leave the report as a `gh pr comment`). Finish with a terse
   summary.

## How to invoke

Invoke as `/test-maintainer`. The user passes the path to maintain (default: cwd) and the ref to
diff against (required); options refine the run: `--diff-against REF` (the git ref, e.g. `HEAD~1`
or `origin/main` — required), `--open-pr` (branch + raise a PR over the verified-green edits;
default is a dry-run report), `--apply` (write the accepted edits without opening a PR),
`--max-files N` (cap on changed source files, default `20`), `--format markdown|json` (default
`markdown`), and `--output PATH` (report destination; default
`<path>/.foundry/proposed-test-edits.md`). You perform the diff, coverage-primary impact, S1
composition, verdict judgment, C10/C11 audit gate, **verify green-gate**, branch + PR, and
rendering yourself in-session — the agent *is* the harness.

> **Prerequisite — `s2.mode: llm` required for the closed loop.** The shipped default is
> `s2.mode: regex` in `.foundry/sdet.yaml`, which runs the deterministic pre-flight but
> **defers all proposing** (no LLM calls, no C10/C11 audit, no verify, no PR — the CLI prints
> a NOTICE and exits 0). Set `s2.mode: llm` before invoking the closed loop:
>
> ```yaml
> # .foundry/sdet.yaml
> s2:
>   mode: llm    # required; default "regex" defers proposing
> ```
>
> Also note: `--diff-against` uses a 3-dot (merge-base) diff (`git diff REF...HEAD`).
> **Uncommitted files and `git add -N` intent-to-add files are invisible to this form.** If the
> CLI warns "0 changed files", commit your work before invoking.

### Skill-first mode (in-session, default)

The body runs the extract→judge→continue loop directly in this session: seed + coverage-primary
impact + S1 composition (extract, exits awaiting judgments), per-test minimal edit proposal
(judge), then the C10/C11 audit gate + the **verify green-gate** + branch/PR (continue). The
**verify + refine loop** is the in-session model's job: it writes each edit, runs the impacted
subset, and on red re-judges (a fresh extract→judge→continue round) up to `s2.max_refine` before
reverting — the seam the CLI exposes as `repropose` (the CLI layer defaults it to "first-red
reverts"; the multi-round re-judge is driven here in-session).

### CI mode + the consumer overlay

An independent CI path runs the same contract through the `shared/` Python pipeline, emitted from
`ci.yaml` at sync time. The emitted CI is **dormant** (`on: workflow_dispatch`,
`permissions: contents: read`) per the dormant-CI invariant. To run the closed loop on
PR-to-main, the consumer pastes the documented overlay — see `references/ci-overlay.md` (the
`pull_request: [main]` trigger + `contents` / `pull-requests: write` permissions + a "reuse your
test-env setup" step). The coverage-primary impact, the audit gate, and the green-gate are
deterministic; the per-test verdict + refine are the in-session model judgments.

## Output

A maintainer run report — Markdown or JSON — opening with the summary (`N applied (verified
green); A updates, B adds, C deletes; D dropped`), then a per-proposal section (test file,
verdict, impact classes, 1-3 sentence reasoning, unified diff), a **Needs human attention**
section (un-fixable tests + last failure; un-closed S1 gaps), and a dropped section (each
FM5/FM9-dropped proposal + warning). In `--open-pr` mode the verified-green edits land on
`forge/test-maintainer-<sha>` and a PR is opened for human approval (exit 8); zero green → report
only (exit 9). The authoritative output contract lives in `## Invariants` below.

## Invariants

- Input: **source files only** — test files excluded from the diff seed. Unsupported extensions
  silently skipped.
- Impact is **coverage-primary**: the runtime coverage map (test → executed line) ∩ the diff's
  changed lines is the source of truth; the static symbol / downstream-call graph augments it for
  new symbols + gap detection. Impact classes (**signature** / **behavior** / **coverage**) are
  retained as proposer context. Anti-hallucination: affected-symbol list narrowed to names the
  test actually references via AST name scan.
- Verdict per impacted test: **update**, **add**, **delete**, or **skip** (skip → no proposal).
  Proposals are the *minimal* edit; intent and unrelated assertions are not changed. `add` edits
  close a real, in-style S1 gap; `update` edits style-match the S1 conventions.
- C10 (FM1-5) + C11 (FM6-10) §3.4 audit runs over **every** proposed edit before verify (never
  bypassed). **FM5/FM9** → proposal **dropped**; **FM1-FM4** → warning only.
- **Strict green-gate:** an edit lands in the PR only if its impacted tests go green and C10/C11
  pass. Red → refine ≤`s2.max_refine` (default 2) → still red → **revert** + report under
  Needs-human. Un-fixable edits are never committed. Zero green → no empty PR.
- Hard rules: never modifies production source; never touches tests outside the impacted set;
  never writes a non-test path (hard-errors on one); **never auto-merges** — always opens a PR
  for human approval.
- Exit codes: `0` success (or no impacted tests/gaps); `1` over max-files/budget, or a git/gh
  step failed after producing green edits; `2` bad ref or missing path or SEC-8 argv rejection;
  `7` `--extract-only` succeeded — prompts written, body must judge and `--continue`; `8` PR
  opened over the verified-green edits; `9` `--open-pr` but zero green → report only. Exits other
  than `0`/`7`/`8`/`9` are hard stops.
