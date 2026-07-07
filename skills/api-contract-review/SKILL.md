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
   this phase is one prompt item per contract file (id prefix `contract:<file>`).
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
     `round-1-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "api-contract-review",
       "round": 1,
       "items": [
         {"id": "contract:<file>", "response": [<findings array>]},
         ...
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
