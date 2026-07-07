---
name: data-flow-map
description: >
  Map PII / sensitive-data flows in a codebase: inventory sources (DB columns, API
  params, env vars, file inputs), trace flows through the code, classify sinks, check
  retention markers (GDPR Art. 5(1)(e)), and LLM-review purpose-limitation alignment
  (GDPR Art. 5(1)(b)) — classifying everything as PII / PCI / PHI / internal / public.
  The detection, AST flow-tracing, sink classification, and retention check are
  deterministic Python (`primitives/data_flow_map/shared/`); this body delegates to
  `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" data-flow-map` rather than reproducing it. Use when the user asks to
  "map data flows", "trace PII", "audit for data privacy", "check retention markers",
  "generate a data-flow diagram", or when preparing for a GDPR / CCPA / PCI-DSS
  assessment.
allowed-tools: Read, Bash
---

# /data-flow-map

Generate a privacy data-flow map for a codebase — by delegating to the deterministic
`"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" data-flow-map` CLI. S60 detects PII / sensitive-data sources, traces flows
to sinks, classifies each datum (PII / PCI / PHI / internal / public), checks retention
markers, and LLM-reviews purpose-limitation alignment. The detection regex catalog, the
Python-AST flow tracer, the sink classifier, and the retention checker are Python
load-bearing logic in `primitives/data_flow_map/shared/`; this body resolves the target,
invokes the CLI, reads the report it wrote, and acts on the exit code. It does **not**
reproduce the analysis in-session.

## When to invoke

- Before a GDPR / CCPA privacy review — map all PII flows.
- When adding new data-collection features — verify purpose limitation.
- During a security review for data-exposure findings.
- When auditing a service for data-privacy compliance.
- Before a PCI-DSS assessment.
- When the user asks to "map data flows", "trace PII", "check retention markers", or
  "generate a data-flow diagram".

## When invoked

S60 is a 2-round primitive. Round 1 classifies ambiguous PII fields; round 2 (when a `--spec`
is given) reviews purpose limitation. Either round may be skipped if there is nothing to judge.

1. **Resolve the target and detect PII sources (extract phase — Stage A.1).** Take the code path
   the user gives (default: the current working directory; a missing path is a hard error). Load
   the classification catalog (from `--classification` if given, else the starter catalog). Run
   the regex-catalog PII source detection over the codebase, producing **definite sources** (the
   regex matched a closed-vocabulary pattern) and **ambiguous fields** (matched a heuristic that
   might be PII but needs confirmation). If no ambiguous fields exist and no `--spec` is given,
   run Stages B-D (flow-tracing, sink classification, retention check) directly and render the
   final report. If ambiguous fields exist, the result of this phase is one prompt item per
   ambiguous field (id prefix `pii:<field-name>`).
2. **Judge each PII prompt item (round 1 — ambiguity resolution).** For each item in
   `round-1-prompts.json` (one item per ambiguous field):
   - Read the `system` and `user` fields. They describe a single ambiguous field whose PII
     category could not be determined by the regex catalog.
   - Respond with a JSON object matching `response_format.schema` — `is_pii` (boolean),
     `classification` (one of the closed vocabulary values from the system prompt: `pii`,
     `pci`, `phi`, `internal`, `public`), `category` (e.g. `identity`, `contact`,
     `government_id`, `payment_account`, `payment_auth`, …), and `severity`
     (`low`/`medium`/`high`/`critical`). Set `is_pii=false` when the field is clearly not
     sensitive. Do **not** wrap the
     JSON in markdown fences and do **not** include any rationale outside the JSON object.
   - Collect all responses into a `JudgmentEnvelope` and write it to
     `round-1-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "data-flow-map",
       "round": 1,
       "items": [
         {"id": "pii:<field-name>", "response": {"is_pii": true, "classification": "<label>", "category": "<category>", "severity": "<level>"}},
         ...
       ]
     }
     ```
3. **Apply PII judgments and run Stages B-D (continue after round 1).** Apply the classification
   judgments to merge confirmed PII sources with the definite sources. Run Stages B-D
   deterministically: AST flow-tracing (Python/Java/TS), sink classification (7 sink kinds:
   `logger`, `http_client`, `db_write`, `response_body`, `file_write`, `queue`, `cache`),
   retention-marker check (status `explicit`/`missing`/`expired`, per GDPR Art. 5(1)(e)). If a
   `--spec` was given and purpose review is not skipped, produce one prompt item for the
   purpose-limitation review (id `review:purpose-limitation`) as round 2.
4. **Judge the purpose-limitation prompt item (round 2 — Stage E).** For the single item in
   `round-2-prompts.json` (id `review:purpose-limitation`):
   - Read the `system` and `user` fields. They describe the detected PII flows and the feature
     spec, asking for a purpose-limitation review per GDPR Art. 5(1)(b).
   - Respond with a JSON array matching `response_format.schema` — a `purpose_issues` array where
     each issue carries: `field` (a PIISource name from the detected source set — do not invent
     new names), `declared_purpose` (the purpose from the spec, or null), `observed_use` (how the
     field is actually used in code), and `severity`
     (`low`/`medium`/`high`/`critical`). Do **not** wrap the JSON in markdown fences.
   - Collect the response into a `JudgmentEnvelope` and write it to
     `round-2-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "data-flow-map",
       "round": 2,
       "items": [
         {"id": "review:purpose-limitation", "response": [<purpose_issues array>]}
       ]
     }
     ```
5. **Apply purpose-limitation judgments and render the final report (continue after round 2).**
   Apply the purpose-limitation judgments (anti-hallucination: a flagged `field` must appear
   in the detected source set or is dropped). Optionally fire the §3.4 C10/C11 sub-calls in-session
   (unless `--skip-ai-quality` is set). Render the report in the requested format — `markdown`
   (default), `json`, `sarif`, or `mermaid` — to stdout or `--output`. Summarize the sources,
   flows to sinks, retention-marker status, and any purpose-limitation gaps. Surface any finding
   at or above `--max-severity`. Exit `4` when a retention/purpose issue meets the gate, `5`
   when a critical C10/C11 finding survives FM8 folding.

## How to invoke

Invoke as `/data-flow-map`. The user passes the code path (default: the current working
directory); options map to CLI flags: `--classification PATH` (custom classification YAML,
merged with the starter catalog — custom wins on pattern conflict); `--spec PATH` (a feature
spec consumed by the Stage E purpose-limitation review); `--module-pattern GLOB` (scope the
scan, e.g. `**/*.py`); `--language python|java|ts|auto` (source language, default `auto`-detect);
`--format markdown|json|sarif|mermaid` (output format, default `markdown`); `--output FILE`
(write the report to a file instead of stdout); `--max-severity high|critical` (CI gate — exit
`4` if any retention / purpose issue is at or above this severity); `--skip-ai-quality` (skip
the charter §3.4 C10 + C11 sub-call); `--skip-purpose-review` (skip the round-2 purpose
review); `--include-vendored` (re-include vendored harness trees in the rglob walks; default off,
dependency/build dirs stay excluded). The source detection, flow-tracing, classification, retention check, and rendering are
Python load-bearing logic in `primitives/data_flow_map/shared/`; you orchestrate them by
delegating to `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" data-flow-map` rather than re-deriving the analysis in-session.

### Delegated extract → continue (in-session, default)

The body drives the deterministic CLI through the extract → judge → continue protocol:
`"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" data-flow-map --extract-only` runs the source detection and emits the
ambiguity-resolution (round 1) and purpose-review (round 2) prompts; you judge them in this
session and write the judgments envelope; `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" data-flow-map --continue --judgments`
folds the verdicts back in and renders the report. Only the round-1 (PII classification) and
round-2 (purpose limitation) judgments are model work — the CLI does everything else.

A **plain** invocation (`"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" data-flow-map <path>` with neither `--extract-only` nor
`--continue`) is a deterministic-only single-shot: it runs the full pipeline over the
confidently-detected sources and renders the map to stdout / `--output`, noting any ambiguous
fields it skipped. Use it for a quick deterministic map; use the extract → continue protocol when
you want in-session LLM classification of ambiguous fields and purpose review (#1195 4b).

### CI mode

In CI, the emitted workflow (from `ci.yaml`) invokes the agent CLI (Claude Code, Copilot, Codex,
or Antigravity), which loads this SKILL.md and runs the same extract → judge → continue protocol:
`"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" data-flow-map --extract-only` emits the ambiguity-resolution and purpose-review
prompts, the agent judges them, and `--continue --judgments` folds the verdicts back in. The agent
*is* the harness — there is no separate harness CLI flag.

## Scan exclusions

All four file-walking stages (A source detector, B flow tracer, C sink classifier, D retention checker) use `primitives._shared.scan_exclusions.is_excluded` to exclude `.venv`, `node_modules`, `__pycache__`, `.git`, build dirs, and the wholly-vendored harness dirs (`.foundry`, `.claude`) and the emitted-harness subpaths (`.github/skills`, `.agents/skills`, `.agent/skills`, `.cursor/rules`) — `.github/workflows/`, `.github/CODEOWNERS`, etc. are still scanned from every `rglob` walk. Symlinks are also skipped (SEC-10). Pass `--include-vendored` (threaded end-to-end to `include_vendored=True`) to re-include the vendored dirs + emitted subpaths when auditing emitted artifacts. See SPEC.md §Scan exclusions.

## Output

A privacy data-flow report (stdout by default, or the `--output` file) carrying the PII
sources, the flow traces to sinks, the retention-marker status, the purpose-limitation gaps,
and — for `--format mermaid` — a Mermaid sequence diagram (chunked for >30 nodes). The
authoritative output contract (the sources / flows / sinks / retention structure, the
PII/PCI/PHI classification scheme, the downstream consumers, and the exit-code scheme) lives
in `## Invariants` below.

## Invariants

- Five element families: **sources** (DB columns, API parameters, env vars, file inputs),
  **flows** (assignments, function calls, ORM/query objects), **sinks** (7 kinds: `logger`,
  `http_client`, `db_write`, `response_body`, `file_write`, `queue`, `cache`), **retention
  markers** (status `explicit`/`missing`/`expired`, per GDPR Art. 5(1)(e)), **purpose-limitation
  issues** (GDPR Art. 5(1)(b)).
- Source classification vocabulary: `pii`, `pci`, `phi`, `internal`, `public`; categories:
  identity, contact, government_id, payment_account, payment_auth, medical, financial_account,
  address, credential, biometric, behavioral, location (catalog: NIST SP 800-122, OWASP Testing
  Guide v4.2, GDPR Recitals 26/51, PCI-DSS v4.0). Org extension via `--classification`.
- Anti-hallucination: Stage A.2 `classification` must be a verbatim literal or dropped; Stage E
  flagged `field` must appear in detected source set or dropped with stderr warning.
- §3.4 sub-call invokes C10 + C11; C11 FM8 (`pii-without-retention`) folded into retention
  output and removed from `c11_findings` (no double-counting).
- Classification contract is load-bearing for **S11 security-review**, **C4 code-review**, and
  **S70 compliance-map** (GDPR Art. 30, PCI-DSS Req 1.2.4).
- Exit codes: `0` success; `1` generic error; `2` code path not found; `4` retention/purpose
  issues exceed `--max-severity`; `5` C10/C11 critical remaining after FM8 folding (CI/harness
  mode only); `7` `--extract-only` succeeded — prompts written, body must judge and call
  `--continue`. Exits other than `0` and `7` are hard stops.
