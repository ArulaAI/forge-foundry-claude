---
name: test-from-spec
description: >
  Generate stack-native failing tests from a finished C1-shaped spec. Parses the spec's
  Acceptance checklist and Public API block, auto-detects the target stack (Python/pytest,
  JS/vitest, or Java/JUnit 5), generates one failing test file per acceptance criterion
  (happy-path plus at least one edge case, ≥2 non-trivial assertions, referencing only
  Public API symbols), then self-reviews for test theater and missing edge cases. Use when
  the user asks to "generate tests from spec", "write failing tests", "create test
  contracts", "run test-from-spec", or when a finished spec needs failing tests written
  before implementation.
allowed-tools: Read, Grep, Glob, Bash
---

# /test-from-spec

Generate stack-native failing tests from a C1-shaped spec document. This is a
**Python-load-bearing** primitive: the deterministic spec-parse and Stage-D gate live in
`shared/` and this body delegates to `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" test-from-spec`. It drives the
**extract → judge → continue** loop, producing one failing test file per acceptance
criterion before handing off to C3 `/code-gen`.

## When to invoke

- After C1 `/spec-write` produces a spec — generate the test contracts before any implementation.
- Before C3 `/code-gen` — in TDD flow the failing tests must exist first.
- When acceptance criteria change — regenerate the affected tests.
- When the user asks to "generate tests from spec", "write failing tests", or "create test contracts".

## When invoked

1. **Extract — parse the spec and emit prompts (exit 7).** Run:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" test-from-spec <SPEC> --extract-only --state-dir <state-dir>
   ```
   The CLI parses the spec's Acceptance checklist (every `- [ ]` item becomes one criterion with a
   slugified id), resolves the Public API symbol list from the Public API fenced block,
   auto-detects the stack from `--codebase-root` (or use `--stack` to override), and writes
   `round-1-prompts.json` under `<state-dir>`. Hard stops: spec not found (exit 2); no `- [ ]`
   criteria (exit 3). Exits 7 ("awaiting judgments").

2. **Judge — generate one failing test body per criterion.** Read `round-1-prompts.json`. Each
   prompt item carries the criterion text, the Public API list, and the resolved stack and
   framework. For each criterion write a stack-native test body seeded by the per-stack header at
   `references/stack_templates/pytest_header.py.tmpl` (Python),
   `references/stack_templates/vitest_header.ts.tmpl` (JS/TS), or
   `references/stack_templates/junit5_header.java.tmpl` (Java):
   - ≥2 non-trivial assertions covering the happy path AND at least one edge case.
   - Reference **only** symbols from the Public API list.
   - Written to **FAIL**: the implementation does not exist yet.
   - Never `assert True` / `assert False` as assertions.
   Collect all responses into `round-1-judgments.json` shaped per the prompts file's `_schema` hint.

3. **Continue — run the Stage-D gate and write the test files.** Re-invoke:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" test-from-spec <SPEC> --continue --judgments round-1-judgments.json
   ```
   The CLI runs the Stage-D theater and unknown-symbol check on each generated test body. Files
   that pass are written under `--output-dir` (default `tests/<spec_dir>/`). Theater findings
   (only `assert True`/`assert False` or no assertions) are rejected and reported;
   `unknown_symbol` findings are surfaced as warnings.

4. **Report.** Relay the CLI's summary: files written, any rejected criteria (with reason), and
   any warnings. Point the user at the generated tests as the failing contract ready for C3
   `/code-gen`.

## How to invoke

Invoke as `/test-from-spec` with the path to a C1-shaped spec (the output of `/spec-write`).
The body never generates tests in-session — it shells `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" test-from-spec` and loops
on exit 7 (one round). Useful flags: `--stack python|js|java|dotnet|react|angular|nodejs`
(override auto-detection), `--framework <name>` (framework override), `--codebase-root PATH`
(root for stack detection, default cwd), `--output-dir PATH` (where to write test files, default
`tests/<spec_dir>/` — one directory for the whole spec, using the importable `spec_dir` name
derived from the spec filename).

## Output

One failing test file per acceptance criterion, written under the output directory and named per
the stack convention (`test_<slug>.py`, `<slug>.test.ts`, or `<PascalSlug>Test.java`), plus a
Stage-D findings summary in the session. The authoritative output contract (per-test assertion
floor, happy + edge coverage, Public-API-only symbols, must-be-failing) lives in `## Invariants`
below.

## Invariants

- One test file per acceptance criterion parsed from the spec's `## Acceptance` checklist.
- Each test carries at least 2 non-trivial assertions (never a lone `assert True`/`assert False`)
  and covers a happy path plus at least 1 edge case.
- Each test references only symbols listed in the spec's `## Public API` block — no
  hallucinated APIs.
- Tests are written to FAIL: the implementation does not exist yet, so the test contract drives
  it. This is the authoritative output contract (the completeness gate keys on it).
- The per-stack test headers live under the `stack_templates` references directory (e.g.
  `references/stack_templates/pytest_header.py.tmpl`) — reference them by path; do not inline or
  duplicate their content.
- The spec must carry an Acceptance section (with `- [ ]` items); a missing Acceptance section is
  a hard stop. The Acceptance and Public API headings are matched at **any heading level**
  (`##`, `###`, …), so a spec that nests them under a parent section parses correctly (#1091). A
  missing or empty Public API section is **not** a hard stop — generation proceeds with no API
  symbols listed (the `unknown_symbol` check in Stage D has nothing to flag against).
