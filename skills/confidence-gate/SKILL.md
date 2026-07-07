---
name: confidence-gate
description: >
  The C6 pre-merge confidence gate — a risk-modeled aggregator over already-produced
  upstream review reports. Sniffs each JSON report to one of twelve upstream primitive
  types (C4 code-review, C5 spec-validate, C8 architecture-review, S10 threat-model,
  S11 security-review, S20 cloud-arch-review, S30 frontend-design, S40 api-contract-review,
  S60 data-flow-map, S70 compliance-map, C10 failure-mode-audit, C11 production-failure-audit),
  normalizes its findings to a severity + confidence, runs the deterministic blast-radius
  risk math (severity_weight × confidence_weight × primitive_weight × count) against the
  policy thresholds to land a pass / review / fail verdict, then drafts a short rationale and
  selects the top findings under the anti-hallucination guard. Use when the user asks to "run
  the pre-merge confidence gate", "aggregate review findings into a merge decision", "get a
  go/no-go verdict", "model the blast radius", "check if we're ready to merge", or when several
  upstream review reports need a single recommendation before opening a pull request.
allowed-tools: Read, Grep, Glob, Bash
---

# /confidence-gate

The pre-merge confidence gate, run directly in this session: ingest the already-produced JSON
reports from upstream review primitives, sniff each to its source type and normalize its
findings to a severity + confidence, run the deterministic blast-radius risk math against the
policy thresholds to land a pass / review / fail verdict, then draft a short rationale and
select the top findings under the anti-hallucination guard. This is a risk-modeled aggregator —
it consumes findings other primitives already produced and synthesizes one merge recommendation;
it does not re-run those reviews and it has no §3.4 sub-call of its own.

## When to invoke

- As the final gate before opening a pull request — aggregate every upstream review report into
  a single go/no-go signal.
- After running the review suite (C4 / C5 / C8 / S-series / C10 / C11) — fold their reports into
  one verdict.
- When the user asks to "run the confidence gate", "model the blast radius", "get a go/no-go
  verdict", "aggregate review findings into a merge decision", or "check if we're ready to merge".

## When invoked

1. **Extract phase — ingest and score the reports (Stages A + B).** Resolve every report path the
   user gives (at least one is required; a missing path is a hard error). Read each JSON file and
   sniff its source type by matching its top-level keys against `references/sniffer_keys.yaml` —
   each of the twelve upstream types lists the `positive` keys it must carry and the `negative`
   keys it must not. A report matching no type — or more than one — is a malformed-report error.
   The expected input is each upstream review's **report JSON** (the output of `<review> …
   --format json`), **not** a skill-protocol `round-N-judgments.json` / `round-N-prompts.json`
   envelope — those carry `schema_version`/`primitive`/`round`/`items[]` and are rejected with a
   clear "looks like a judgments envelope, not a report JSON" message (see
   `docs/skill-first-protocol.md`, #885).
   Normalize every report's findings into a common record carrying its source type, a severity
   (low / medium / high / critical), a confidence level (low / medium / high), and an optional
   file + line + title + description. Group normalized findings by source type. Then load the gate
   policy (`--policy PATH`, else `.foundry/gate-policy.yaml`, else built-in defaults) and compute
   the blast-radius score: sum over each finding `severity_weight × confidence_weight ×
   primitive_weight`, per source then aggregate. Map the aggregate score to a verdict: score <
   pass threshold → **pass**; pass ≤ score < review threshold → **review**; score ≥ review
   threshold → **fail**. The verdict is fully deterministic — no LLM involved.
2. **Judgment phase — synthesize the rationale and select top findings (Stage C, the one model
   step).** Using the per-source risk summaries, the deterministic verdict, and the highest-severity
   candidate findings, produce the following rationale object — it becomes the `response` of a
   single judgment item whose id is `narrative:confidence-gate`:
   ```json
   {
     "rationale": "<2–3 sentence explanation of why the gate landed pass/review/fail>",
     "top_findings": [
       {
         "report_type": "<one of the twelve valid type keys>",
         "severity": "<low|medium|high|critical>",
         "title": "<finding title verbatim from the ingested input>",
         "description": "<optional brief description>"
       }
     ]
   }
   ```
   Do **not** wrap the JSON in markdown fences. Wrap this object as the `response` of a single
   judgment item with id `narrative:confidence-gate`, collect it into a `JudgmentEnvelope`, and write it to
   `round-1-judgments.json` in the state directory — the `--continue` path looks up exactly
   this id:
   ```json
   {
     "schema_version": "1.0",
     "primitive": "confidence-gate",
     "round": 1,
     "items": [
       {"id": "narrative:confidence-gate", "response": {"rationale": "<…>", "top_findings": [<…>]}}
     ]
   }
   ```
   Do **not** invent findings. Every `top_findings` entry must reference a (report_type, title)
   pair actually present in the ingested reports — drop any that do not match and note the drop.
   Select up to three highest-risk findings. The rationale is explanatory only and never changes
   the deterministic verdict from the extract phase.
3. **Continue phase — render and act on the gate (Stage D).** Apply the anti-hallucination guard
   to the selected top findings (drop fabricated entries). Render the gate report in the requested
   format — `markdown` (default), `json`, or `sarif` — to stdout or the `--output` destination.
   The report leads with the verdict, aggregate score, and pass + review thresholds; then the
   rationale; then the top findings; then the per-primitive risk summary table (findings +
   per-severity histogram + contributed score per source); then the input report list. Then act
   on the gate: when `--max-recommendation` is set and the verdict exceeds it (ordering pass <
   review < fail), the run fails (the exit-equivalent gate); otherwise it passes. Stop the user on
   a fail verdict; surface a review verdict as findings to address before merge.

## How to invoke

Invoke as `/confidence-gate` — the user passes one or more upstream review report paths (at least
one required). Options refine the run: `--policy PATH` (a `gate-policy.yaml` of weight + threshold
overrides; defaults to `.foundry/gate-policy.yaml` then the built-in defaults), `--format
markdown|json|sarif` (default `markdown`), `--output PATH` (write to a file; default stdout), and
`--max-recommendation pass|review|fail` (fail the run if the verdict exceeds this level). You
perform the report ingest + sniff, finding normalization, deterministic blast-radius risk math,
verdict computation, rationale + top-findings synthesis under the anti-hallucination guard,
rendering, and gate decision yourself — there is no CLI to shell out to.

### Skill-first mode (in-session, default)

Work through the three phases sequentially in-session: extract (Stages A + B), judge (Stage C),
continue (Stage D). The CI path runs the same contract through the `shared/` Python pipeline —
that path is for pipelines, not for this in-session body, which does the work itself.

### CI mode

CI runs the *same* skill-first loop: the emitted workflow (rendered from `ci.yaml`) invokes the
agent CLI, which loads this SKILL.md, runs the deterministic `--extract-only` stage for
Stages A + B, produces the Stage C judgments, and re-invokes with `--continue --judgments <path>`
for Stage D. The agent *is* the harness — there is no separate harness CLI flag.

## Output

A confidence-gate report — Markdown, JSON, or SARIF — leading with the verdict (pass / review /
fail), the aggregate risk score and the pass + review thresholds, then the rationale, the top
findings, the per-primitive risk summary table (findings + per-severity histogram + contributed
score per source), and the input report list. The authoritative output contract (the twelve
ingested report types + their sniffers, the blast-radius risk math, the verdict scheme +
thresholds + exit-equivalent gate, the per-source normalization, the anti-hallucination guard,
and the SARIF runs layout) lives in `## Invariants` below.

## Invariants

- Ingests reports from exactly twelve primitive types: **c4_code_review**, **c5_spec_validate**,
  **c8_architecture_review**, **s10_threat_model**, **s11_security_review**,
  **s20_cloud_arch_review**, **s30_frontend_design**, **s40_api_contract_review**,
  **s60_data_flow_map**, **s70_compliance_map**, **c10_failure_mode_audit**,
  **c11_production_failure_audit**; report matching no sniffer is a malformed-report error.
- **No §3.4 sub-call** — pure aggregator; never re-runs C10/C11 or any review.
- Normalization: severity **low/medium/high/critical**; confidence **low/medium/high** (categorical,
  not numeric). S10: severity from mean DREAD score (≥ 8 → critical, ≥ 6 → high, ≥ 4 → medium,
  else low) at default **medium** confidence. S-series: copy finding's own severity + confidence,
  drop out-of-vocabulary values. C10/C11: copy severity at fixed **high** confidence.
- Score formula: `severity_weight × confidence_weight × primitive_weight` per finding, summed per
  source, then summed to aggregate. Default weights — severity: low=1, medium=4, high=10,
  critical=25; confidence: low=0.3, medium=0.7, high=1.0; primitive: 1.0 review-class, 1.5
  C10/C11. `gate-policy.yaml` overrides; malformed policy is a hard error.
- Verdict — **pass** / **review** / **fail** — deterministic from score vs. thresholds (default
  pass=20, review=50): score < pass → pass; pass ≤ score < review → review; score ≥ review →
  fail. Never an LLM judgment.
- Exactly one model call (rationale + up to three top findings); explanatory only, never alters verdict.
- Anti-hallucination on selected top findings: drop if source type not in the twelve, severity
  out of vocabulary, or (source type, title) not in ingested input; each drop noted.
- `--max-recommendation` gate (ordering pass < review < fail): verdict exceeds it → run fails.
  Exit codes: `0` pass/within-threshold; `2` path not found or malformed JSON; `3` malformed
  policy; `4` verdict exceeds `--max-recommendation`; `7` `--extract-only` succeeded — prompts
  written, body must judge and call `--continue`. Exits other than `0` and `7` are hard stops.
- SARIF: one `runs[]` per ingested source primitive, followed by a synthetic
  `forge-foundry/confidence-gate` run carrying verdict + score.
