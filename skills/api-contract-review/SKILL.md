---
name: api-contract-review
description: >
  API-contract review of OpenAPI 3.0/3.1, AsyncAPI 2.x/3.x, and GraphQL SDL across seven
  lenses — versioning, error_shapes, auth_spec, idempotency, backward_compatibility,
  pagination, rate_limiting — with confidence-based filtering, RFC/OWASP grounding, and an
  optional baseline diff. Detects the contract format, parses its endpoints, optionally diffs
  against a baseline git ref for breaking changes, reviews each lens producing a severity +
  confidence per finding, drops findings below the confidence threshold, fires the C10/C11
  §3.4 sub-calls in-session and dedups C11 overlaps against the S40 findings, then renders a
  Markdown, JSON, or SARIF report. Use when the user asks to "review an API contract",
  "validate an OpenAPI spec", "check an AsyncAPI schema", "review a GraphQL SDL", or when
  enforcing backward compatibility on an API change.
allowed-tools: Read, Grep, Glob, Bash
---

# /api-contract-review

API-contract review with confidence-based filtering, directly in this session: detect the
contract format, parse its endpoints, optionally diff against a baseline git ref, review each
across the seven lenses producing a severity and a confidence per finding, filter out findings
below the confidence threshold, fire the C10/C11 §3.4 AI-quality sub-calls in-session and dedup
C11 overlaps against the S40 findings, then render the report.

## When to invoke

- Before publishing or versioning an API — catch contract issues early.
- When reviewing a PR that changes an OpenAPI, AsyncAPI, or GraphQL schema.
- During API design review — validate schema completeness and backward compatibility.
- When enforcing backward compatibility against a prior release (give a `--baseline` ref).
- When the user asks to "review an API contract", "validate an OpenAPI spec", or "check an AsyncAPI schema".

> **Target is a contract document, not a source directory.** The `contract_path`
> argument must be an OpenAPI 3.0/3.1 YAML/JSON file, an AsyncAPI 2.x/3.x YAML
> file, or a GraphQL SDL (`.graphql`/`.gql`). If the user points at `api/routes/`
> or a similar source directory, ask them to export the spec first, e.g.:
> ```bash
> python -c "import app; import json; print(json.dumps(app.app.openapi()))" > openapi.json
> ```

## When invoked

1. **Load rules, detect the format, and parse the contract (extract phase).** Resolve the target
   path the user gives (default: the current working directory; a missing path is a hard error).
   Read any `.foundry/api-rules.yaml` beside the path (or at `--rules-yaml`) — it may set
   `disabled_lenses`, `severity_overrides`, `custom_rfc_refs`, `pagination.max_page_size`, and
   `rate_limiting.require_retry_after`; absent, defaults apply. Then sniff the format from
   extension + content — **openapi-3.0**, **openapi-3.1**, **asyncapi-2**, **asyncapi-3**, or
   **graphql**; if the format cannot be resolved that is a hard error unless `--format-hint` was
   given. Parse the contract into endpoints (REST path+method, AsyncAPI channel+operation, GraphQL
   type+field). When `--baseline REF` is set, read the prior contract at that ref via git and
   compute the backward-compat diff — **removed** endpoints, **type_narrowed** fields, and **new
   required fields** — feeding that summary into the `backward_compatibility` lens. The result of
   this phase is one prompt item per contract file (id `contract:<basename>` — the file's
   name only, e.g. `contract:openapi.yaml`, not its full path).
2. **Judge each prompt item (per-contract lens review).** For each item in
   `round-1-prompts.json` (one item per contract file):
   - Read the `system` and `user` fields. They describe a single contract file, the parsed
     endpoints, the rules config, and the baseline diff when one was computed.
   - Respond with a JSON array matching `response_format.schema` — a `findings` array where each
     finding carries: `lens` (one of the seven lens names), `file` (the contract file under review
     — do not invent a different path), `line` (1-indexed), optional `endpoint`, `severity`
     (`low`/`medium`/`high`/`critical`), `confidence_score` (a float in `[0,1]`; the
     pipeline maps it to the categorical `confidence`), `title`,
     `description`, optional `suggestion`, optional `rfc_ref`, and `breaking` (boolean). Walk all
     selected lenses: **versioning** (semver path vs header, deprecation/sunset), **error_shapes**
     (RFC 7807 problem+json), **auth_spec** (declared security schemes, OAuth flows),
     **idempotency** (POST with Idempotency-Key per RFC 9110), **backward_compatibility** (removed
     paths, narrowed types, new required fields), **pagination** (cursor/offset consistency, max
     page size), and **rate_limiting** (rate-limit + retry-after headers per RFC 6585). Apply the
     three anti-hallucination guards: **DROP** any finding whose `file` is not the contract under
     review; **CLEAR** the `endpoint` (keep the finding) if it is not one of the parsed endpoints;
     **CLEAR** the `rfc_ref` (keep the finding) unless it matches `^RFC\s?\d{3,5}$` or
     `^OWASP\s.*$`. Do **not** wrap the JSON in markdown fences.
   - Collect all responses into a `JudgmentEnvelope` and write it to
     `round-1-judgments.json` in the same state directory. **Each item's `id` MUST be
     copied verbatim from the matching item in `round-1-prompts.json`** — it is
     `contract:<basename>` (the file's name only, e.g. `contract:openapi.yaml`, **not**
     the full path). An item whose id does not match a prompt id is silently skipped,
     yielding an empty report. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "api-contract-review",
       "round": 1,
       "items": [
         {"id": "contract:openapi.yaml", "response": [<findings array>]}
       ]
     }
     ```
3. **Apply judgments and render (continue phase).** Apply the three anti-hallucination guards to
   the collected findings. Apply the confidence filter — `> 0.7` high, `[0.3, 0.7]` inclusive
   medium, `< 0.3` low; drop every finding below `--confidence-threshold` (default **medium**).
   Fire the C10/C11 §3.4 sub-calls in-session and dedup (unless `--skip-ai-quality` is set),
   scoped to C11 **FM6** and **FM7**: for each such C11 finding at the same (file, line) as an
   S40 finding, **MOVE** it to `ai_quality.deduplicated_findings` — the S40 finding stays
   canonical; C10 findings pass through unchanged. Render the report in the requested format —
   `markdown` (default), `json`, or `sarif` — to stdout or `--output`. Summarize the findings,
   surface any `breaking` findings and any finding at or above `--max-severity`, and stop the
   user on critical findings. In SARIF the S40 findings occupy `runs[0]`, C10 `runs[1]`, C11
   `runs[2]` (latter two when sub-calls ran).

## How to invoke

Invoke as `/api-contract-review`. The user passes the path to the contract (default: the current
working directory); options refine the run: `--baseline REF` (git ref of a baseline contract for
the backward-compat diff, e.g. `HEAD~1` or `main`), `--rules-yaml PATH` (explicit
`.foundry/api-rules.yaml`; default auto-detect), `--format-hint
openapi-3.0|openapi-3.1|asyncapi-2|asyncapi-3|graphql` (override format detection), `--lens
versioning|error_shapes|auth_spec|idempotency|backward_compatibility|pagination|rate_limiting`
(one lens, repeatable; default all seven), `--confidence-threshold low|medium|high` (default
`medium`), `--max-severity high|critical` (the gate threshold), `--skip-ai-quality` (skip the
C10/C11 sub-calls), `--format markdown|json|sarif` (default `markdown`), and `--output PATH` (write
to a file; default stdout). **Do not confuse `--format-hint` with `--format`:** `--format-hint`
overrides detection of the *input* contract type (OpenAPI/AsyncAPI/GraphQL), while `--format`
selects the *output* rendering (markdown/json/sarif). You perform the format detection, contract
parse, baseline diff, per-lens review, confidence filter, in-session C10/C11 aggregation + dedup,
and rendering yourself — there is no CLI to shell out to.

### Skill-first mode (in-session, default)

The body performs the extract→judge→continue loop directly in this session: detects the contract
format, parses endpoints, optionally computes the baseline diff (extract), reviews each lens
producing findings in the `round-1-prompts.json` prompt format (judge), then applies the
anti-hallucination guards, confidence filter, and dedup discipline and renders the report
(continue).

### CI mode

In CI, the emitted workflow (from `ci.yaml`) invokes the agent CLI (Claude Code, Copilot, Codex,
or Antigravity), which loads this SKILL.md and runs the same extract → judge → continue protocol:
the deterministic `--extract-only` stage emits the per-contract review prompts, the agent judges
them, and `--continue --judgments` folds the verdicts back in. The command still regex-validates
`--baseline REF` as a subprocess-argv boundary (SEC-8). The agent *is* the harness — there is no
separate harness CLI flag.

## Output

An api-contract-review report — Markdown, JSON, or SARIF — listing each finding by lens with its
severity, confidence level + score, optional endpoint, RFC/OWASP ref, and `breaking` flag, plus the
baseline diff summary when a baseline was given, followed by the aggregated C10/C11 AI-quality
findings and their deduplicated entries. The authoritative output contract (the seven lenses, the
severity levels, the confidence filter, the three anti-hallucination guards, the baseline-diff
behavior, the §3.4 dedup discipline, the SARIF runs layout, and the gate behavior) lives in
`## Invariants` below.

## Invariants

- Seven lenses — **versioning**, **error_shapes**, **auth_spec**, **idempotency**,
  **backward_compatibility**, **pagination**, **rate_limiting** — all by default; `--lens` selects
  a subset; `.foundry/api-rules.yaml::disabled_lenses` skips listed lenses.
- Supported contract formats: `openapi-3.0`, `openapi-3.1`, `asyncapi-2`, `asyncapi-3`,
  `graphql`; override with `--format-hint`; unresolvable format is a hard error.
- Every finding: severity **low/medium/high/critical**, confidence score `[0,1]`, optional
  `endpoint`, optional `rfc_ref`, `breaking` flag. Three anti-hallucination guards: wrong `file`
  → **dropped**; unknown `endpoint` → **cleared** (finding retained); `rfc_ref` not matching
  `^RFC\s?\d{3,5}$` or `^OWASP\s.*$` → **cleared** (finding retained).
- Confidence thresholds: `> 0.7` high, `[0.3, 0.7]` inclusive medium, `< 0.3` low; findings
  below `--confidence-threshold` (default `medium`) are dropped.
- `--baseline REF`: diff produces **removed**, **type_narrowed**, and **new required fields**
  lists feeding the `backward_compatibility` lens; diff failure degrades to a note.
- C10 (FM1–FM5) + C11 (FM6–FM10) aggregated unless `--skip-ai-quality`; dedup scoped to C11
  **FM6**/**FM7** — same (file, line) as an S40 finding → **MOVED** to
  `ai_quality.deduplicated_findings`, S40 canonical; C10 not deduped.
- SARIF: S40 → `runs[0]`, C10 → `runs[1]`, C11 → `runs[2]` (latter two when sub-calls ran).
- `--max-severity` gate: critical S40 findings, and critical C10/C11 surviving dedup, stop the user.
- Exit codes: `0` success; `2` contract path not found; `3` format unresolvable; `4` findings
  exceed `--max-severity`; `5` critical C10/C11 findings surviving dedup (CI/harness mode only);
  `7` `--extract-only` succeeded — prompts written, body must judge and call `--continue`. Exits
  other than `0` and `7` are hard stops.

## Target-stack judgment (SP6, additive — inert without a target)

**This block is inert when no `target-stack.json` is present.** When `/mod-modernization`
is running the full app-mod chain with a declared target (via `--target KEY=VALUE`), it
writes `target-stack.json` under `.foundry/context/modernization/.state/<run-id>/`. When
`/api-contract-review` is invoked as part of that chain (e.g. by the orchestrator to review
a contract that exists alongside the source), check for that artifact. If absent, skip this
block entirely — all behavior and all output are byte-identical to the no-target path.

**If a target stack is provided** (i.e. `target-stack.json` exists under the run-id state
dir):

1. **Frame the extraction as the CURRENT contract to modernize toward the target.** When
   writing findings and the report summary, make explicit that this contract represents the
   *existing* (source-stack) API surface — the starting point for the migration, not the
   target design. In the opening summary of the Markdown report, include a sentence such as:
   "This review documents the **current API contract** of the source application. Given the
   declared target stack (`<backend dimension label>`), the downstream `/mod-modernization-scope`
   will synthesize a target REST API design in §8b, tracing each new resource endpoint to its
   originating controller mapping identified here."

2. **Identify modernization-relevant findings.** During per-lens review, note which findings
   represent patterns that the target stack migration would naturally address (e.g. a missing
   versioning scheme that the target Spring Boot service will adopt, an auth scheme that the
   target stack replaces with OAuth2/JWT, a REST-anti-pattern that the new resource-per-aggregate
   design dissolves). Add a `migration_note` comment in the report text (NOT as an extra JSON
   field — the schema is `extra="forbid"`) by appending `[migration: <note>]` to the finding's
   `description` string where relevant. This annotation is advisory; it does not change the
   finding's severity, confidence, or `breaking` status.

3. **Propagate the current contract to the scope stage.** The deterministic extraction
   (`extract_operations`) already writes structured `ApiExtraction` output. When a target is
   present, confirm in the run summary that the `functional-map.json` controller inventory
   (if available under `.foundry/context/functional-map/`) will supplement the S40 extraction
   in §8 Current API Surface — so the scope synthesizer has the fullest possible picture of
   the current API before designing §8b. No additional writes are needed; this is a reminder
   to the operator that the two sources are complementary.

**Backward-compatible guarantee:** when `target-stack.json` is absent (the default standalone
invocation), none of the above applies. The review lenses, confidence filter, anti-hallucination
guards, §3.4 sub-calls, dedup discipline, and all exit codes are unchanged. The report format
and content are byte-identical to the pre-SP6 behavior.
