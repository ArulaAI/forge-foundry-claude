---
name: production-failure-audit
description: >
  Rule-based audit of a directory or file for the five dangerous production
  failure modes (FM6–FM10): FM6 Stale-pattern Hallucination, FM7
  Overgeneralization, FM8 Compliance Blindness, FM9 Test-coverage Theater, and
  FM10 Architectural Decay. Walks the target tree applying the deterministic
  detectors from the curated rule pack — the FM6/FM7/FM8 framework-idiom,
  domain-misfit, and regulatory regexes, the FM9 tautology regexes plus an AST
  zero-assert-test pass, and the FM10 architecture.yaml layer-violation and
  import-cycle checks — producing one fixed-severity finding per rule hit, then
  rendering a Markdown, JSON, or SARIF report and gating on a max-severity
  threshold. Counterpart to C10 failure-mode-audit (FM1–FM5); this is the C11
  §3.4 sub-call that every review primitive invokes in-session for the
  production failure modes. Use when the user asks to "audit for production
  failures", "check for stale patterns", "scan for compliance blindness",
  "check for test theater", or before promoting AI-generated code to production.
allowed-tools: Read, Grep, Glob, Bash
---

# /production-failure-audit

Rule-based audit for the five dangerous production failure modes (FM6–FM10),
directly in this session: walk the target path, apply the deterministic detectors
from the curated rule pack, emit one fixed-severity finding per rule hit, then
render the report and gate on the max-severity threshold. Counterpart to C10
`/failure-mode-audit` (FM1–FM5, the common AI failure modes). Charter §3.4 mandates
this as a sub-call of every review primitive — when a review primitive needs the
production failure modes, it runs this body in-session over the same target rather
than shelling out.

## When to invoke

- Before promoting AI-generated code to production — catch stale framework idioms,
  domain-misfit crypto, regulatory blind spots, hollow tests, and architectural decay.
- On any PR touching BFSI-relevant code (finance, banking, insurance, securities).
- As the §3.4 C11 sub-call invoked by review primitives (C4 code-review, S11 security-review, C8 architecture-review, and the others) for the FM6–FM10 production failure modes.
- When the user asks to "audit for production failures", "check for stale patterns", "scan for compliance blindness", or "check for test theater".

## When invoked

1. **Run the FM6–FM10 deterministic rule-pack scan (extract phase).** Resolve the target path the
   user gives (default: the current working directory; a missing path is a hard error). Run the
   FM6–FM10 deterministic scan in-session: load the rule pack from `references/production_rules.yaml`
   and the `production:` namespace in `.foundry/failure-modes.yaml` when present; apply
   FM6/FM7/FM8 framework-idiom, domain-misfit, and regulatory regexes; FM9 tautology regexes +
   AST zero-assert-test pass; and FM10 architecture.yaml layer-violation and import-cycle checks
   over the target path. When LLM-mode is configured for FM6/FM7/FM10 candidates, produce one
   prompt item per candidate (id prefix `judge:fm<N>:<index>` (N = the FM number, e.g. `fm6`/`fm7`/`fm10`; `<index>` is the 0-based candidate position)). When no LLM candidates
   exist (the default deterministic-only configuration), assemble the report from the deterministic
   findings and render directly.
2. **Judge each FM6/FM7/FM10 candidate prompt item (round 1 — when LLM mode is configured).** For
   each item in `round-1-prompts.json` (one item per LLM-mode candidate, id prefix
   `judge:fm<N>:<index>` (N = the FM number, e.g. `fm6`/`fm7`/`fm10`; `<index>` is the 0-based candidate position)):
   - Read the `system` and `user` fields. They describe a single candidate match (matched snippet
     and context) and ask the model to confirm whether it is a genuine FM6 stale-pattern,
     FM7 domain-misfit, or FM10 architectural-decay finding.
   - Respond with a JSON object matching `response_format.schema` — `verdict` (a string, one of
     `supported`/`refuted`) where `supported` confirms the candidate as a genuine finding and
     `refuted` dismisses it as a false positive, plus `reasoning` (a short string explaining the
     verdict). Do **not** wrap the JSON in markdown fences.
   - Collect all responses into a `JudgmentEnvelope` and write it to
     `round-1-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "production-failure-audit",
       "round": 1,
       "items": [
         {"id": "judge:fm6:0", "response": {"verdict": "supported", "reasoning": "..."}},
         ...
       ]
     }
     ```
3. **Apply judgments, render and gate (continue after round 1).** Merge the confirmed
   LLM-judged findings with the deterministic FM8/FM9 findings. Render the report in the
   requested format — `markdown` (default), `json`, or `sarif` — to stdout or `--output`. Apply
   the gate: make the user STOP on `critical` findings (e.g. a PAN column or a PAN logged in
   plain text) and address them before proceeding; surface `high`/`medium`/`low` items as
   improvement opportunities. Summarize the finding count by mode and severity.

## How to invoke

Invoke as `/production-failure-audit <target>`. The user passes the path to audit (default: the
current working directory); options refine the run: `--format markdown|json|sarif` (default
`markdown`; SARIF requires an output file), `--output PATH` (write to a file; default stdout), and
`--max-severity low|medium|high|critical` (gate threshold; default no gate), and
`--include-vendored` (re-scan the vendored `.foundry`/`.claude` harness trees; default off).
Per-FM enable flags, FM8's `warn_only`, and FM10's `architecture_yaml` path come from the
`production:` namespace in `.foundry/failure-modes.yaml` when present. You perform the path
resolution, rule-pack load, FM6–FM10 detection, optional LLM judgment of FM6/FM7/FM10 candidates,
and rendering yourself — there is no CLI to shell out to.

**Scan scoping (#1192).** When you walk the target tree in-session, skip dependency dirs (`.venv`,
`venv`, `node_modules`, `__pycache__`, build/cache dirs) always, and skip the vendored harness
trees (`.foundry`, `.claude`) + emitted skill/rule subpaths unless `--include-vendored` is set —
otherwise a repo with a local virtualenv drowns the real findings in thousands of third-party ones.

### Skill-first mode (in-session, default)

The body performs the extract→judge→continue loop directly in this session: runs the FM6–FM10
deterministic rule-pack scan (extract), optionally judges FM6/FM7/FM10 candidates when LLM mode
is configured (judge), then merges confirmed findings with the deterministic results and renders
the report (continue). When the default deterministic-only configuration is in effect (no LLM
mode configured), the extract phase produces findings directly and the judge round is skipped.

### CI mode

An independent CI path exists for automation: the foundry validator's `production-failure-audit`
command runs the same contract through the `shared/` Python pipeline and the CI emitted from
`ci.yaml`. The FM6–FM10 detectors are deterministic rule-based and produce fixed-high-confidence
findings without model judgement; LLM-mode for FM6/FM7/FM10 candidate verification is opt-in via
the `production:` namespace in `.foundry/failure-modes.yaml`. The extract→judge→continue protocol
is the same in both in-session and CI use.

## Output

A production-failure-audit report — Markdown, JSON, or SARIF — listing each finding as `{mode,
severity, file, line, message, snippet}` plus the CWE id, ordered by the severity ranking. The JSON
document carries a top-level `schema_version: "1.0"` and the SARIF document carries it in its
`properties` bag (shared C10 writer; distinct from the SARIF spec version) so a SOX/PCI audit replay
can pin the findings schema shape. The authoritative output contract (the FM6–FM10 modes and their
detection, the severities, the rule-pack source, the §3.4 sub-call-target contract, and the gate
behavior) lives in `## Invariants` below.

## Invariants

- Five production failure modes (FM6–FM10; counterpart to C10's FM1–FM5): **FM6 Stale-pattern
  Hallucination**, **FM7 Overgeneralization**, **FM8 Compliance Blindness**, **FM9 Test-coverage
  Theater**, **FM10 Architectural Decay**. FM10 uses `architecture.yaml` for layer-violation and
  import-cycle checks. Detectors are deterministic and rule-based (no model judgement) → C11
  findings normalize to **fixed-high** confidence at the confidence-gate.
- Severity: **low/medium/high/critical**, ordered `low < medium < high < critical`, plus CWE id
  per finding. FM8 PAN-in-schema and PAN-logged → **critical** (CWE-311/CWE-359); FM7
  Fernet-for-PAN and `verify = False` → **high** (CWE-327/CWE-295); FM10 layer violations →
  **high** (CWE-1061), cycles → **medium**. FM8 findings downgraded to **low** while `warn_only`
  holds (default until S70 `/compliance-map` ships).
- Rule pack at `references/production_rules.yaml` (regexes, severities, file globs, CWE ids)
  is the single source of truth — edit rules there, not inline.
- C11 §3.4 sub-call contract: target-path-in, FM6–FM10-findings-out (mode, severity, file, line,
  message, CWE id).
- Max-severity gate: any finding at or above → CI exit-code-1; `critical` findings stop the user.
- Exit codes: `0` success; `1` gate failure (finding at or above `--max-severity`); `2` path not
  found; `7` `--extract-only` succeeded **with** LLM candidates — prompts written, body must judge
  and call `--continue`. When `--extract-only` finds **no** LLM candidates the deterministic
  FM6–FM10 findings are rendered directly (exit per the `--max-severity` gate, i.e. `0`/`1`)
  instead of a silent exit-0 that hides them (#1192). Exits other than `0` and `7` are hard stops.
