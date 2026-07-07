---
name: spec-validate
description: >
  Round-trip spec-to-code traceability validation — the C5 completion gate.
  Parses the acceptance criteria from a C1-shaped spec's `Acceptance` section, then
  for each criterion locates the implementing code element(s) and classifies the match as
  satisfied, partial, unsatisfied, or not_applicable with a severity and a confidence,
  applies the three-way anti-hallucination guard (file-in-set drop, symbol-exists clear,
  kind-literal clear), optionally downgrades satisfied→partial when a linked test fails,
  detects scope drift (public-API features with no spec backing), fires the C10/C11 §3.4
  sub-calls in-session deduping overlaps against the canonical traces, then renders a
  Markdown, JSON, or SARIF report and acts on the gate. Use when the user asks to
  "validate an implementation against its spec", "check spec coverage", "verify acceptance
  criteria", "detect spec drift", or "run the completion gate" — or after C3 code-gen
  completes to confirm the implementation matches the spec before merge.
allowed-tools: Read, Grep, Glob, Bash
---

# /spec-validate

Round-trip spec-to-code traceability validation directly in this session — the completion
gate. Parse the acceptance criteria from the spec's `Acceptance` section, then for each
criterion locate the implementing code element(s) and classify the match (satisfied / partial
/ unsatisfied / not_applicable) with a severity and a confidence, apply the three-way
anti-hallucination guard, optionally downgrade on a linked test failure, detect scope drift,
fire the C10/C11 §3.4 sub-calls in-session and dedup overlaps against the canonical traces,
then render the report and act on the gate. This validates whether the implementation
*satisfies its spec* and blocks merge on drift — it is the gate, not a line-level code review
(that is C4 `/code-review`) or a structural review (that is C8 `/architecture-review`).

## When to invoke

- After implementing a feature or completing C3 `/code-gen` — confirm every acceptance
  criterion is satisfied before opening a pull request.
- Before merging — run the completion gate; block on unsatisfied or partial criteria at or
  above the severity threshold.
- When a spec changes — identify newly unsatisfied criteria and any scope drift the change
  introduced.
- When the user asks to "validate against the spec", "check spec coverage", "verify
  acceptance criteria", "detect spec drift", or "run the completion gate".

## When invoked

C5 is a 2-round primitive. Round 1 locates and classifies code elements per criterion; round 2
detects scope drift.

1. **Parse the acceptance criteria and build the codebase summary (extract phase — Stage A).**
   Resolve the spec path and the code path the user gives — both are required; a missing spec or
   code path is a hard error. Read the spec Markdown and parse its `Acceptance` section into
   criteria — each a list item (checkbox `- [ ]` / `- [x]`, plain bullet `-`/`*`/`+`, or
   numbered `1.`/`1)`), carrying a stable `id` slug and the criterion text — reuse the shared
   acceptance parser the C1 family uses; do not re-derive a parser. Then enumerate the reviewed files under the code path (skipping
   dotfiles/dot-dirs) and build a compact codebase summary listing each file and its top-level
   public symbols (functions, classes) so the per-criterion search has a grounded file/symbol set
   to reference. The result of this phase is one prompt item per acceptance criterion (id prefix
   `element:<criterion-id>`).
2. **Judge each criterion prompt item (round 1 — element search and classification).** For each
   item in `round-1-prompts.json` (one item per acceptance criterion):
   - Read the `system` and `user` fields. They describe a single acceptance criterion and the
     codebase summary.
   - Respond with a JSON object matching `response_format.schema` carrying: `status` (one of
     `satisfied`, `partial`, `unsatisfied`, `not_applicable`), `code_elements` (a list of
     `{file, line, name, kind}` refs where `kind` is one of `function/class/method/endpoint/
     test/constant/other` and `file` must be in the reviewed-files set — do not invent paths),
     `rationale` (a brief explanation), and `confidence` (`low`/`medium`/`high`). Apply the
     three-way anti-hallucination guard: only reference files in the reviewed-files set; only
     name symbols actually findable in those files; only use vocabulary `kind` values. Do
     **not** wrap the JSON in markdown fences.
   - Collect all responses into a `JudgmentEnvelope` and write it to
     `round-1-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "spec-validate",
       "round": 1,
       "items": [
         {"id": "element:<criterion-id>", "response": {"status": "...", "code_elements": [...], "rationale": "...", "confidence": "..."}},
         ...
       ]
     }
     ```
3. **Apply element judgments, optionally downgrade on test failure, then produce the scope-drift
   prompt (continue after round 1).** Apply the element judgments with the three-way
   anti-hallucination guard. Optionally apply Stage D: when `--test-results` is given, read the
   pytest-json-report file, map each `test_<slug>` node id to the criterion id, and downgrade any
   **satisfied** trace to **partial** (severity → `medium`) when its linked test failed or
   errored. Then produce one prompt item for scope-drift detection (id `drift:scope`).
4. **Judge the scope-drift prompt item (round 2 — Stage E).** For the single item in
   `round-2-prompts.json` (id `drift:scope`):
   - Read the `system` and `user` fields. They describe the spec and the implementation's
     top-level public symbols, asking for features whose purpose is not mentioned or implied
     in the spec.
   - Respond with a JSON array matching `response_format.schema` — a `drift_items` array where
     each item carries: `file` (must be in the reviewed-files set — do not invent paths), `line`
     (1-indexed), `name`, `kind` (one of the vocabulary values), and `description` (why this
     feature has no spec backing). Do **not** wrap the JSON in markdown fences.
   - Collect the response into a `JudgmentEnvelope` and write it to
     `round-2-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "spec-validate",
       "round": 2,
       "items": [
         {"id": "drift:scope", "response": [<drift_items array>]}
       ]
     }
     ```
5. **Apply scope-drift judgments, fire the §3.4 sub-calls, render and gate (continue after round
   2).** Apply the scope-drift judgments (file-validation guard: drop any item whose file is not
   in the reviewed-files set). Unless `--skip-ai-quality` is set, fire the C10/C11 §3.4 sub-calls
   in-session and dedup: any C10/C11 finding at the same (file, line) as a code element on a
   canonical trace is **MOVED** to `ai_quality.deduplicated_findings` — the trace stays canonical.
   Render the report in the requested format — `markdown` (default), `json`, or `sarif` — to
   stdout or `--output`. Summarize the per-status trace count, scope-drift items, and gate
   decision. The run **fails** (blocks merge) when any C10/C11 critical finding survives dedup,
   or when any unsatisfied/partial trace is at or above `--max-severity`.

## How to invoke

Invoke as `/spec-validate` — the user passes the spec path and the code path to validate against
(both required). Options refine the run: `--test-results PATH` (a pytest-json-report JSON for the
Stage D downgrade), `--format markdown|json|sarif` (default `markdown`), `--output PATH` (write to
a file; default stdout), `--max-severity high|critical` (the gate threshold for unsatisfied/partial
traces), and `--skip-ai-quality` (skip the C10/C11 §3.4 sub-calls). You perform the acceptance
parse, per-criterion search + classification, anti-hallucination guard, optional test-failure
downgrade, scope-drift detection, in-session C10/C11 aggregation + dedup, rendering, and gate
decision yourself — there is no CLI to shell out to.

### Skill-first mode (in-session, default)

The body performs the extract→judge→continue loop directly in this session across two rounds:
parses acceptance criteria and builds the codebase summary (extract), classifies each criterion's
implementation and detects scope drift (judge — two separate rounds), then assembles the report
and acts on the gate (continue).

### CI mode

CI runs the *same* skill-first loop: the emitted workflow (rendered from `ci.yaml`) invokes the
agent CLI, which loads this SKILL.md, runs the deterministic `--extract-only` stage, produces
the per-criterion classification and scope-drift judgments across the two rounds, and re-invokes
with `--continue --judgments <path>`. The agent *is* the harness — there is no separate harness
CLI flag.

## Output

A spec-validate report — Markdown, JSON, or SARIF — leading with a per-status summary count, then
the per-criterion acceptance traces (each with its status, confidence, severity, code-element
refs, and rationale), the scope-drift items, and the C10/C11 AI-quality summary with its
deduplicated entries. The authoritative output contract (the four match statuses, the severity
and confidence schemes, the three-way anti-hallucination guard, the Stage D downgrade, the §3.4
dedup discipline, the SARIF runs layout, and the gate / exit behavior) lives in `## Invariants`
below.

## Invariants

- Trace status vocabulary: **satisfied**, **partial**, **unsatisfied**, **not_applicable**.
  Severity from status: satisfied → `low`, partial → `medium`, unsatisfied → `high`,
  not_applicable → `low`. Confidence: **low/medium/high** (categorical). Code-element `kind`:
  **function/class/method/endpoint/test/constant/other**.
- Three-way anti-hallucination guard per code element: (1) **file validation** — not in
  reviewed-files set (basename-match fallback first) → dropped; (2) **symbol-exists** — symbol
  not found (AST, else regex) → `name` cleared; (3) **kind literal** — invalid `kind` → cleared
  to `other`. satisfied/partial trace with no valid elements → downgraded to unsatisfied.
  Scope-drift items get the same file-validation drop.
- Stage D: `test_<slug>` node ids mapped to criterion id; **satisfied** → **partial**
  (severity → `medium`) when linked test failed or errored; never upgrades; unreadable results
  file skipped with warning.
- Input: spec's `Acceptance` criteria (parsed by the shared C1 acceptance parser).
- §3.4 sub-call: C5 auto-invokes C10 (FM1–FM5) + C11 (FM6–FM10) unless AI-quality is skipped;
  same (file, line) as a canonical trace's code element → **MOVED** to
  `ai_quality.deduplicated_findings` and **removed** from the C10/C11 lists, trace canonical.
- SARIF: C5 traces + scope-drift → `runs[0]`, C10 → `runs[1]`, C11 → `runs[2]` (latter two when ran).
- Completion gate: **fails and blocks merge** if any C10/C11 critical finding survives dedup, or
  any unsatisfied/partial trace at or above `--max-severity`. Exit codes: `0` pass; `2` spec or
  code path not found; `4` unsatisfied/partial at or above threshold; `5` surviving critical
  C10/C11 (CI/harness mode only); `7` `--extract-only` succeeded — prompts written, body must
  judge and call `--continue`. Exits other than `0` and `7` are hard stops.
