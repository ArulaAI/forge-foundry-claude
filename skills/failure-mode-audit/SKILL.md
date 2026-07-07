---
name: failure-mode-audit
description: >
  Rule-based audit of a directory or file for the five AI failure modes
  (FM1–FM5): FM1 Hallucinated Dependencies, FM2 Context-Window Blindness,
  FM3 Training-Data Bias, FM4 Overconfident Completions, and FM5 Security
  Vulnerability Propagation. Walks the target tree applying the deterministic
  detectors from the curated rule pack — import-vs-manifest checks, a Python
  cross-file size/distance heuristic, and the FM3/FM4/FM5 regex patterns —
  producing one fixed-severity finding per rule hit, then renders a Markdown,
  JSON, or SARIF report and gates on a max-severity threshold. This is the
  C10 §3.4 sub-call that every review primitive invokes in-session for the
  AI failure modes. Use when the user asks to "audit for failures", "check for
  hallucinated imports", "scan for AI failure modes", "run a failure-mode
  audit", or before committing AI-generated code.
allowed-tools: Read, Grep, Glob, Bash
---

# /failure-mode-audit

Rule-based audit for the five AI failure modes (FM1–FM5), directly in this session:
walk the target path, apply the deterministic detectors from the curated rule pack,
emit one fixed-severity finding per rule hit, then render the report and gate on the
max-severity threshold. Charter §3.4 mandates this as a sub-call of every review
primitive — when a review primitive needs the AI failure modes, it runs this body
in-session over the same target rather than shelling out.

## When to invoke

- Before committing AI-generated code — catch hallucinated imports and biased patterns.
- As the §3.4 C10 sub-call invoked by review primitives (C4 code-review, S11 security-review, and the others) for the FM1–FM5 AI failure modes.
- As part of any PR review (charter §3.4 mandates this AI-quality pass).
- When the user asks to "audit for failures", "scan for AI failure modes", or "check for hallucinated imports".

## When invoked

1. **Run the FM1–FM5 deterministic rule-pack scan (extract phase).** Resolve the target path the
   user gives (default: the current working directory; a missing path is a hard error). Run the
   FM1–FM5 deterministic scan in-session: load the rule pack from `references/failure_mode_rules.yaml`
   and the per-FM config from `.foundry/failure-modes.yaml` when present; apply FM1
   import-vs-manifest checks, FM2 cross-file size/distance heuristic, and FM3/FM4/FM5 regex
   patterns over the target path. When LLM-mode is configured for FM3/FM4 candidates, produce
   one prompt item per candidate (id prefix `judge:fm<N>:<index>` (N = the FM number, e.g. `fm3`/`fm4`; `<index>` is the 0-based candidate position)). When no LLM
   candidates exist (the default deterministic-only configuration), assemble the report from the
   deterministic findings and render directly.
2. **Judge each FM3/FM4 candidate prompt item (round 1 — when LLM mode is configured).** For
   each item in `round-1-prompts.json` (one item per FM3/FM4 candidate, id prefix
   `judge:fm<N>:<index>` (N = the FM number, e.g. `fm3`/`fm4`; `<index>` is the 0-based candidate position)):
   - Read the `system` and `user` fields. They describe a single candidate match (matched snippet
     and context) and ask the model to confirm whether it is a genuine FM3 idiom-mismatch or FM4
     overconfident-completion finding.
   - Respond with a JSON object matching `response_format.schema` — `verdict` (a string: FM3 uses
     `supported`/`refuted`; FM4 adds `new`) where `supported`/`new` confirm the candidate as a
     genuine finding and `refuted` dismisses it as a false positive, plus `reasoning` (a short
     string explaining the verdict). For an FM4 `new` verdict also include `claim_line` (integer)
     and `severity` (`low`/`medium`/`high`). Do **not** wrap the JSON in markdown fences.
   - Collect all responses into a `JudgmentEnvelope` and write it to
     `round-1-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "failure-mode-audit",
       "round": 1,
       "items": [
         {"id": "judge:fm3:0", "response": {"verdict": "supported", "reasoning": "..."}},
         ...
       ]
     }
     ```
3. **Apply judgments, render and gate (continue after round 1).** Merge the confirmed
   LLM-judged findings with the deterministic FM1/FM2/FM5 findings. Render the report in the
   requested format — `markdown` (default), `json`, or `sarif` — to stdout or `--output`. Apply
   the gate: make the user STOP on `critical` findings (e.g. hardcoded secrets, PAN literals)
   and address them before proceeding; surface `medium`/`low` items as improvement opportunities.
   Summarize the finding count by mode and severity.

## How to invoke

Invoke as `/failure-mode-audit <target>`. The user passes the path to audit (default: the
current working directory); options refine the run: `--format markdown|json|sarif` (default
`markdown`; SARIF requires an output file), `--output PATH` (write to a file; default stdout),
`--max-severity low|medium|high|critical` (gate threshold; default no gate), and
`--include-vendored` (re-scan the vendored `.foundry`/`.claude` harness trees; default off).
Per-FM enable flags and thresholds come from `.foundry/failure-modes.yaml` when present. You
perform the path resolution, rule-pack load, FM1–FM5 detection, optional LLM judgment of FM3/FM4
candidates, and rendering yourself — there is no CLI to shell out to.

**Scan scoping (#1192).** When you walk the target tree in-session, skip dependency dirs
(`.venv`, `venv`, `node_modules`, `__pycache__`, build/cache dirs) always, and skip the vendored
harness trees (`.foundry`, `.claude`) + emitted skill/rule subpaths unless `--include-vendored` is
set — otherwise a repo with a local virtualenv drowns the real findings in thousands of
third-party ones. For FM1, resolve the repo's own first-party top-level packages (flat-layout
`__init__.py` packages, `src/` layout roots, and the pyproject `[project].name`) and treat them as
declared, so the project's own imports are not flagged as hallucinated dependencies.

### Skill-first mode (in-session, default)

The body performs the extract→judge→continue loop directly in this session: runs the FM1–FM5
deterministic rule-pack scan (extract), optionally judges FM3/FM4 candidates when LLM mode is
configured (judge), then merges confirmed findings with the deterministic results and renders the
report (continue). When the default deterministic-only configuration is in effect (no LLM mode
configured), the extract phase produces findings directly and the judge round is skipped.

### CI mode

An independent CI path exists for automation: the foundry validator's `failure-mode-audit`
command runs the same contract through the `shared/` Python pipeline and the CI emitted from
`ci.yaml`. The FM1–FM5 detectors are deterministic rule-based and produce fixed-high-confidence
findings without model judgement; LLM-mode for FM3/FM4 candidate verification is opt-in via
`.foundry/failure-modes.yaml`. The extract→judge→continue protocol is the same in both
in-session and CI use.

## Output

A failure-mode-audit report — Markdown, JSON, or SARIF — listing each finding as `{mode,
severity, file, line, message, snippet}` (plus the CWE id on FM5 findings), ordered by the
severity ranking. The JSON document carries a top-level `schema_version: "1.0"` and the SARIF
document carries it in its `properties` bag (distinct from the SARIF spec version) so a SOX/PCI
audit replay can pin the findings schema shape. The authoritative output contract (the FM1–FM5
modes and their detection, the severities, the rule-pack source, the §3.4 sub-call-target
contract, and the gate behavior) lives in `## Invariants` below.

## Invariants

- Five failure modes: **FM1 Hallucinated Dependencies**, **FM2 Context-Window Blindness**,
  **FM3 Training-Data Bias**, **FM4 Overconfident Completions**, **FM5 Security Vulnerability
  Propagation**. Detectors are deterministic and rule-based (no model judgement) → C10 findings
  normalize to **fixed-high** confidence at the confidence-gate.
- Severity: **low/medium/high/critical**, ordered `low < medium < high < critical`. FM5 findings
  carry a CWE id. FM4 markers are **medium**; FM5 hardcoded secrets, provider tokens, JWTs,
  PAN literals are **critical**.
- Rule pack at `references/failure_mode_rules.yaml` (regexes, severities, file globs, CWE ids)
  is the single source of truth — edit rules there, not inline.
- C10 §3.4 sub-call contract: target-path-in, FM1–FM5-findings-out (mode, severity, file, line,
  message, CWE id).
- Max-severity gate: any finding at or above → CI exit-code-1; `critical` findings stop the user.
- Exit codes: `0` success; `1` gate failure (finding at or above `--max-severity`); `2` path not
  found; `7` `--extract-only` succeeded **with** LLM candidates — prompts written, body must judge
  and call `--continue`. When `--extract-only` finds **no** LLM candidates there is nothing to
  judge, so the deterministic FM1/FM2/FM5 findings are rendered directly (exit per the
  `--max-severity` gate, i.e. `0`/`1`) instead of a silent exit-0 that hides them (#1192). Exits
  other than `0` and `7` are hard stops.
