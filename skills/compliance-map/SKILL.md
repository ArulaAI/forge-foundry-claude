---
name: compliance-map
description: >
  Maps a feature spec (and optional code path) to BFSI / regulated-industry compliance controls
  across five framework catalogs — PCI-DSS 4.0, GDPR, SOX 404 ITGCs, NIST 800-53 rev 5, and
  EPA environmental reporting (Clean Water / Safe Drinking Water / Clean Air) — identifying
  which controls the spec touches, finding file:line evidence in the code, classifying each
  control as addressed / partial / unaddressed / not_applicable, and surfacing the coverage
  gaps with severity. Validates every control id against the shipped catalogs (no invented
  controls) and validates every evidence file against disk (no fabricated paths), then renders
  a Markdown, JSON, or SARIF report and gates on a max-severity threshold. Use when the user
  asks to "map to compliance controls", "check GDPR/PCI/SOX/NIST coverage", "find compliance
  gaps", "run a compliance audit", "verify PCI-DSS alignment", or before shipping a feature
  with regulatory exposure.
allowed-tools: Read, Grep, Glob, Bash
---

# /compliance-map

Map a feature specification to compliance controls, directly in this session: load the
five framework catalogs, identify which controls the spec touches, find file:line evidence in
the code, classify each control's coverage status, surface the gaps with severity, and render
the report. This is a review primitive — when `--code-path` is given it runs the charter §3.4
C10/C11 AI-quality sub-calls in-session and cross-maps their compliance-blindness (FM8)
findings back into PCI/GDPR gaps.

## When to invoke

- Before releasing a feature in a regulated industry (finance, healthcare, fintech) — verify control coverage.
- During spec review for BFSI features with regulatory exposure.
- When preparing for a compliance audit and you need an evidence-backed control matrix.
- When the user asks to "map to compliance controls", "check GDPR/PCI/SOX/NIST coverage", or "find compliance gaps".

## When invoked

S70 is a 2-round primitive. Round 1 identifies which controls the spec touches (per control
family); round 2 (when a `--code-path` is given) verifies evidence in the code.

1. **Select frameworks, load catalogs, and build control-touch prompt items (extract phase —
   Stage A).** Resolve the spec path the user gives (a missing spec is a hard error). Select the
   frameworks: explicit `--framework` flags take precedence, then `.foundry/compliance.yaml`, else
   all five. Load the five shipped catalogs (`references/catalogs/pci-dss-4.0.yaml`,
   `references/catalogs/gdpr.yaml`, `references/catalogs/sox-404-itgc.yaml`,
   `references/catalogs/nist-800-53-rev5.yaml`, `references/catalogs/epa-environmental.yaml`),
   group controls by family, and
   build one prompt item per (framework, family) group asking the model to identify touched
   controls (id prefix `controls:<framework>/<family>`).
2. **Judge each control-touch prompt item (round 1 — Stage B).** For each item in
   `round-1-prompts.json` (one item per framework/family group, id prefix
   `controls:<framework>/<family>`):
   - Read the `system` and `user` fields. They present the spec text and the controls in that
     family, asking which controls the spec touches.
   - Respond with a JSON array matching `response_format.schema` — one object per control with
     `control_id` (string), `touched` (boolean — `true` only if the spec covers the control,
     `false` to abstain), and `rationale` (string — one sentence). Each `control_id` MUST be a
     real catalog id from the loaded family (never invent a control id; if you cannot tie it to a
     catalog entry, set `touched=false`). Do **not** wrap the JSON in markdown fences.
   - Collect all responses into a `JudgmentEnvelope` and write it to
     `round-1-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "compliance-map",
       "round": 1,
       "items": [
         {"id": "controls:<framework>/<family>", "response": [{"control_id": "...", "touched": true, "rationale": "..."}, ...]},
         ...
       ]
     }
     ```
3. **Apply touched-control judgments and optionally build evidence-verification prompt items
   (continue after round 1).** Apply the touched-control judgments (anti-hallucination: invented
   ids dropped). If `--code-path` is given, run the Stage D.1 deterministic regex pre-scan
   (`code_signatures` regexes from the catalog, over the code path) to find hits, then produce
   one prompt item per regex hit (id prefix `evidence:<control-id>:<hit-index>`, where
   `<hit-index>` is the 0-based index of the hit within that control) asking the model to
   verify each hit as genuine evidence. If no `--code-path` is given, run Stage E (gap analysis)
   directly: `unaddressed` (no evidence), `partial` (single regex-only hit), `addressed`
   (two-or-more hits or one confirmed hit); then fire the C10/C11 §3.4 sub-calls in-session
   (unless `--skip-ai-quality` is set) and render the report.
4. **Judge each evidence-verification prompt item (round 2 — Stage D.2).** For each item in
   `round-2-prompts.json` (one item per regex hit, id prefix `evidence:<control-id>:<hit-index>`):
   - Read the `system` and `user` fields. They describe a single control, its `code_signatures`
     regexes, and the regex hits found in the code.
   - Respond with a JSON object matching `response_format.schema` — `is_evidence` (boolean —
     `true` means the regex hit is genuine evidence the control is implemented, `false` means a
     false positive), `file` (the exact path from the input — do not invent), `line` (the line
     number from the input), `snippet` (the relevant snippet), and optional `confidence` (one of
     `regex`, `llm`, `spec`). Do **not** wrap the JSON in markdown fences.
   - Collect all responses into a `JudgmentEnvelope` and write it to
     `round-2-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "compliance-map",
       "round": 2,
       "items": [
         {"id": "evidence:<control-id>:<hit-index>", "response": {"is_evidence": true, "file": "...", "line": 42, "snippet": "..."}},
         ...
       ]
     }
     ```
5. **Apply evidence judgments, fire §3.4 sub-calls, render and gate (continue after round 2).**
   Apply the evidence judgments (anti-hallucination: fabricated file paths dropped with a note).
   Run Stage E (gap analysis — `unaddressed`: no evidence; `partial`: single regex-only hit;
   `addressed`: two-or-more hits or one confirmed hit). Fire the C10/C11 §3.4 sub-calls
   in-session (unless `--skip-ai-quality` is set), with C11 FM8 cross-mapped to PCI/GDPR gaps
   (PAN-in-logger → PCI-DSS 3.5.1 critical, PAN-in-schema → PCI-DSS 3.3.1 critical,
   PII-without-retention → GDPR Art.5(1)(e) high). Render the report in the requested format —
   `markdown` (default), `json`, or `sarif` — to stdout or `--output`. Summarize the control
   matrix, gaps, and gate decision; stop the user on critical gaps and critical AI-quality
   findings.

## How to invoke

Invoke as `/compliance-map <spec_path>`. The user passes the path to the feature spec; options
refine the run: `--code-path PATH` (source directory to scan — enables Stage D evidence finding
and the §3.4 C10/C11 sub-calls),
`--framework pci-dss-4.0|gdpr|sox-404|nist-800-53-rev5|epa-environmental`
(one framework, repeatable; default the `.foundry/compliance.yaml` list or all five), `--git-diff
REF...REF` (PR-delta mode — narrow to changed files, e.g. `origin/main...HEAD`), `--format
markdown|json|sarif` (default `markdown`), `--output PATH` (write to a file; default stdout),
`--max-severity high|critical` (gate threshold), `--skip-ai-quality` (skip the C10/C11 §3.4
sub-calls), and `--include-vendored` (re-include vendored harness trees in the evidence rglob
scan; default off, dependency/build dirs stay excluded). You perform the framework selection, catalog load, touched-control identification,
evidence finding, gap analysis, and rendering yourself — there is no CLI to shell out to.

### Skill-first mode (in-session, default)

The body performs the extract→judge→continue loop directly in this session: loads the framework
catalogs and builds control-touch prompt items (extract), identifies touched controls and
verifies code evidence (judge — up to two rounds), then applies the judgments, fires the §3.4
C10/C11 sub-calls, and renders the report (continue).

### CI mode

In CI, the emitted workflow (from `ci.yaml`) invokes the agent CLI (Claude Code, Copilot, Codex,
or Antigravity), which loads this SKILL.md and runs the same extract → judge → continue protocol:
the deterministic `--extract-only` stage emits the control-touch and evidence-verification prompts,
the agent judges them, and `--continue --judgments` folds the verdicts back in. The agent *is* the
harness — there is no separate harness CLI flag.

## Scan exclusions

`_scan_signatures` uses `primitives._shared.scan_exclusions.is_excluded` to exclude `.venv`, `node_modules`, `__pycache__`, `.git`, build dirs, and the wholly-vendored harness dirs (`.foundry`, `.claude`) and the emitted-harness subpaths (`.github/skills`, `.agents/skills`, `.agent/skills`, `.cursor/rules`). The evidence scan **additionally** excludes the doc/CI/agent trees that produce false-positive control-signature hits — `docs/`, `.github/agents/`, `.github/workflows/`, and `TESTING.md` — by default (#941). Pass `--include-vendored` (threaded end-to-end to `include_vendored=True`) to re-include the vendored dirs, the emitted subpaths, **and** these doc/CI/agent trees when auditing emitted artifacts. See SPEC.md §Scan exclusions.

### Evidence matching & determinism (#889)

`_scan_signatures` matches each `code_signature` regex against **both** the file's relative path (a "line 0" path hit, at most one per file) **and** its content lines. The path-aware match makes present-but-textually-divergent evidence resolve (e.g. NIST AC-3 / IA-2 vs `api/middleware/auth.py`) instead of over-reporting a gap. The file walk is **sorted** (`p.as_posix()`), so the bounded `max_hits` truncation — and therefore whether a round-2 evidence prompt is emitted at all — is a pure function of the inputs, never of OS walk order or re-invocation. The same `(state + judgments) → round structure` is deterministic across repeated `--continue` calls: the round-1→round-2 transition re-runs the identical sorted scan and emits round 2 iff it yields ≥1 hit. The anti-hallucination gates (catalog-id validation + on-disk file-existence check + the in-session `is_evidence` judgment) are unchanged.

## Output

A compliance-mapping report — Markdown, JSON, or SARIF — with a per-control matrix (status
`addressed | partial | unaddressed | not_applicable` plus file:line evidence), a gaps section
with severity and remediation, and (when a code path is given) the aggregated C10/C11 AI-quality
findings. The JSON document carries a top-level `schema_version: "1.0"` and the SARIF document
carries it in its `properties` bag (distinct from the SARIF spec version) so a SOX/PCI audit
replay can pin the report schema shape. Consuming review primitives read this as the
`.foundry/context/compliance.yaml` compliance context: S11 security-review annotates its findings'
compliance refs against it, C4 code-review and S30 frontend-design swap their generic GDPR/PCI
placeholder for its framework list + control IDs. The authoritative output contract (the five
frameworks, the
status/severity vocabularies, the touched→evidence→gap mechanism, the catalog-validated and
disk-validated anti-hallucination guards, and the gate behavior) lives in `## Invariants` below.

## Invariants

- Five shipped catalogs: **PCI-DSS 4.0**, **GDPR**, **SOX 404 ITGCs**, **NIST 800-53 rev 5**, and
  **EPA environmental reporting** (`pci-dss-4.0`, `gdpr`, `sox-404`, `nist-800-53-rev5`,
  `epa-environmental`). The EPA catalog covers Clean Water Act discharge monitoring (NPDES/DMR),
  Safe Drinking Water Act (MCL/CCR/public notification), CROMERR electronic reporting,
  records retention, and Clean Air Act emissions — reusable for any regulated facility (e.g. a
  water utility), not hardcoded to one operator. Any other framework name is a hard
  error (zero-coverage guard — never a clean-but-empty report). DORA and FFIEC out of scope.
- Evidence matching is deterministic (#889): the signature scan walks files in sorted order and
  matches each regex against the file path **and** content, so present evidence resolves and the
  round-2 transition is reproducible across repeated `--continue` invocations (audit requirement).
- Control status vocabulary: **addressed**, **partial**, **unaddressed**, **not_applicable**.
  Conservative classification: regex-only hit is **partial**, not **addressed**.
- Every control: severity **low/medium/high/critical**. Every `unaddressed`/`partial` control
  yields a gap at the control's severity with remediation from `evidence_hints`.
- Anti-hallucination: invented control ids dropped; fabricated evidence file paths dropped with
  a note. Never invent control ids or file paths.
- Catalogs (`references/catalogs/pci-dss-4.0.yaml`, `references/catalogs/gdpr.yaml`, etc.) carry
  control ids, titles, `family`, severities, `code_signatures`, and `cwe_ids` — single source of
  truth; edit controls there, not inline.
- §3.4 sub-call: C10 (FM1–FM5) + C11 (FM6–FM10) fire when a code path is given; C11 FM8
  cross-maps to gaps:
  PAN-in-logger → PCI-DSS 3.5.1 (critical), PAN-in-schema → PCI-DSS 3.3.1 (critical),
  PII-without-retention → GDPR Art.5(1)(e) (high).
- `--max-severity high|critical` gate: gap at or above → exit-code-4; critical C10/C11 →
  exit-code-5 (CI/harness mode only); both stop the user.
- Exit codes: `0` success; `2` spec path not found or unsupported framework name; `4` gap at or
  above `--max-severity`; `5` critical C10/C11 findings (CI/harness mode only); `7`
  `--extract-only` succeeded — prompts written, body must judge and call `--continue`. Exits
  other than `0` and `7` are hard stops.
