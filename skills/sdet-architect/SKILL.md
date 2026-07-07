---
name: sdet-architect
description: >
  Design a layered test architecture for a project, stack, and cloud target. Emits a structured
  JSON payload (tooling picks + ADR rationale per section); a bundled Python script detects
  stacks, scaffolds idempotent test configs, and renders the 8-section test-architecture.md.
  Use when the user asks to "design test architecture", "set up a testing strategy",
  "create a test plan", "establish coverage targets", or when a new project or stack needs a
  documented test layer architecture.
---

# /sdet-architect

Design a layered test architecture for a project, stack, and cloud target, directly in this
session: emit a structured tooling-pick + ADR JSON payload; then invoke the bundled Python script
to detect stacks, scaffold idempotent configs, and render the 8-section `test-architecture.md`.

## When to invoke

- Before sprint 0 or project kickoff — define the test cone strategy up front.
- When onboarding a new stack (Python, JS/TS, Java) and establishing test-framework conventions.
- When a project lacks a documented test layer architecture, or you are migrating test runners
  and need a coverage plan.
- Before CI configuration, to align coverage targets with per-layer expectations.
- When the user asks to "design test architecture", "set up a testing strategy", or
  "establish coverage targets".

## When invoked

1. **Emit the structured tooling-pick + ADR payload.** Resolve the target project root (default:
   the current working directory). Run the stack-detection script (`--detect-only`) to discover
   which stacks are present — Python (`pyproject.toml` / `requirements.txt` / a `tests/` scan),
   JS/TS (`package.json` deps + scripts), and Java (`pom.xml` / `build.gradle`). Detection
   descends into bounded, non-excluded subdirectories (depth ≤ 2), so **monorepo** sub-stacks
   (e.g. a JS/TS `portal/` beside a Python repo root) are found, not only the manifests at the
   repo root. Each detected stack carries a `root` field (POSIX relative path; `"."` = repo root)
   in the `--detect-only` JSON — use it to place each stack's config scaffolding and output
   document under the right sub-project. A polyglot project yields one document per detected stack
   (one per `(language, root)` pair). For each detected language, reason about the
   per-layer tooling picks: **unit** (framework + mock pattern + test-data approach), **contract**
   (Pact vs JSON-schema choice based on service topology), **integration** (Testcontainers vs
   WireMock depending on cloud target and available infra), **e2e** (Playwright vs k6 based on
   UI vs load goals), plus synthetic-data libraries and PII-isolation rule for the test-data
   strategy. Assemble the 8-section ADR rationale prose (one paragraph per section). Write this
   as a structured JSON payload (schema described in the Payload schema section below).
   The `--detect-only` JSON also reports `data_backends` — the data backends detected from the
   dependency manifests (postgres / mysql / mssql / oracle / dynamodb / mongodb), or the
   `--data-backend` override. **When `data_backends` is non-empty, also design the data-testing
   strategy** (testing the data + data-systems themselves, distinct from the fixtures-focused
   "Test data strategy" section): for each backend, reason about the per-category tooling picks
   across the nine data sections — data stack profile, data-test cone target, schema & migration,
   referential integrity & constraints, data quality assertions, ETL / pipeline correctness,
   deterministic test data, migration data parity, CI integration. Defaults to reason from:
   migrations Alembic / Flyway / Liquibase / EF; referential-integrity SQL probes; data quality
   great_expectations / pandera / soda / dbt-tests; ETL source↔target reconciliation; deterministic
   seeded generators; migration row/aggregate parity. A category that does not apply to a backend
   (e.g. relational schema/migration on DynamoDB) gets an empty rationale → `_Not applicable for
   this backend._`. Emit these under the `data_architectures` payload key (schema below).
2. **Run the scaffold + render script.** With the payload saved to a temp file (or piped via
   stdin), invoke:
   ```
   python3 scripts/sdet_scaffold.py --payload <payload.json> --project-root <root>
   ```
   The script: detects stacks, writes idempotent config files (`pytest.ini` for Python — skipped
   if `pytest.ini` exists or `pyproject.toml` already has `[tool.pytest.ini_options]`;
   `vitest.config.ts` for JS/TS when no vitest/jest config exists; patches `package.json`
   `scripts.test` when absent — gated independently from the vitest config; injects
   Surefire into `pom.xml` or appends `useJUnitPlatform()` to `build.gradle` for Java), then
   renders the 8-section `test-architecture.md`. No installed foundry CLI is required — the
   script runs with a bare `python3` (stdlib only).
3. **Report the result.** Show the user which files were written or skipped (the script prints a
   JSON summary), display or summarize the rendered `test-architecture.md`, and note any
   stack-detection gaps (e.g. unrecognized monorepo layout). If no stacks are detected, report
   that and stop.

## How to invoke

Invoke as `/sdet-architect`. The user passes the project root (default: current working
directory); options refine the run: `--cloud-target aws|azure|gcp|onprem|none` (default none),
`--coverage-target N` (integer 0-100, default 80), `--existing-tests respect|replace|augment`
(default respect), `--output FILE` (override the default
`<project_root>/test-architecture-<name>.md`), and `--data-backend name[,name]` (override
data-backend detection; closed set postgres/mysql/mssql/oracle/dynamodb/mongodb). Configuration
also reads from `.foundry/sdet.yaml`. There is no installed foundry CLI — you emit the JSON
payload, then invoke the bundled script directly.

## Payload schema

The JSON payload you emit must be an object with:

```jsonc
{
  "project_name": "string — project name",
  "coverage_target": 80,           // integer, 0–100
  "cloud_target": "aws",           // "aws" | "azure" | "gcp" | "onprem" | "none"
  "sections": {
    "stack_profile":     { "body": "...", "rationale": "ADR prose..." },
    "test_cone_target":  { "body": "...", "rationale": "ADR prose..." },
    "layer_unit":        { "body": "...", "rationale": "ADR prose..." },
    "layer_contract":    { "body": "...", "rationale": "ADR prose..." },
    "layer_integration": { "body": "...", "rationale": "ADR prose..." },
    "layer_e2e":         { "body": "...", "rationale": "ADR prose..." },
    "test_data_strategy":{ "body": "...", "rationale": "ADR prose..." },
    "ci_integration":    { "body": "...", "rationale": "ADR prose..." }
  }
}
```

Each `"body"` field carries the section content (tooling choices, tables, lists); each
`"rationale"` field carries the ADR prose (one to three sentences explaining *why* the picks
are right for this stack and context). The script assembles these into the 8 canonical sections
in their fixed order, each ending with a `### Rationale` block.

### Data-testing payload (`data_architectures`)

When `data_backends` is non-empty, add a `data_architectures` key — a map of **backend name →
nine data sections** (same `{body, rationale}` shape). The script renders one
`data-test-architecture-<backend>.md` per backend whose entry you supply:

```jsonc
{
  // ... project_name / coverage_target / cloud_target / sections (above) ...
  "data_architectures": {
    "postgres": {
      "data_stack_profile":     { "body": "...", "rationale": "ADR..." },
      "data_cone_target":       { "body": "...", "rationale": "ADR..." },
      "schema_migration":       { "body": "...", "rationale": "ADR..." },
      "referential_integrity":  { "body": "...", "rationale": "ADR..." },
      "data_quality":           { "body": "...", "rationale": "ADR..." },
      "etl_pipeline":           { "body": "...", "rationale": "ADR..." },
      "deterministic_data":     { "body": "...", "rationale": "ADR..." },
      "migration_parity":       { "body": "...", "rationale": "ADR..." },
      "ci_integration":         { "body": "...", "rationale": "ADR..." }
    }
    // ... one entry per data backend ...
  }
}
```

Omit a section (or give it an empty `rationale`) to render `_Not applicable for this backend._`.

## Output

A `test-architecture-<name>.md` per detected stack, each written **under that stack's own root**
(`test-architecture-<name>.md` at the repo root for the root stack; `portal/test-architecture-<name>.md`
for a `portal/` sub-stack) — a polyglot project gets multiple docs, one per `(language, root)` pair.
Each carries the 8 canonical sections — Stack profile, Test cone target, Layer 1 — Unit,
Layer 2 — Contract, Layer 3 — Integration, Layer 4 — E2E, Test data strategy, CI integration —
each with an ADR rationale block, plus any idempotently-scaffolded test-config files written
alongside. The script's JSON output (written to stdout) lists every file written or skipped;
`test_architecture_md` is the **list** of doc paths written. `--output` overrides the path only for
a single-stack project (one path cannot name N docs); with multiple stacks the per-root default is used.
The authoritative output contract (the 8 sections in order, idempotent scaffolding) lives in
`## Invariants` below.

When `data_backends` are present (and you supplied `data_architectures` entries), the script also
writes one `data-test-architecture-<backend>.md` per backend (the nine data sections), idempotent
and containment-guarded; the JSON summary lists them under `data_test_architecture_md`.

In scoped-analysis mode the output is `s1-findings.json` written to `--run-dir`, conforming
to `references/schemas/s1-findings.schema.json`. No `test-architecture.md` and no
scaffolding are produced in scoped-analysis mode.

## Scoped-analysis mode (additive — for S2 composition)

When invoked by S2 `test_maintainer` as part of the TM-4 composition step, `/sdet-architect`
operates in **scoped-analysis mode** rather than the whole-suite strategy mode above. The inputs
are a bounded "impacted area" (the impacted symbols/tests identified by S2's impact engine) plus
a sample of the repo's existing test files. The output is `s1-findings.json` — a structured
artifact with:

- **Conventions** — inferred framework, layout, naming, fixture/mock idioms, assertion style from
  the sampled tests (so S2's edits match the repo's house style, not a generic template).
- **Best-practice gaps (blast-radius-scoped)** — missing negative/edge-case tests, untested new
  symbols, test smells, scoped to the impacted area.

**Composition seam:** S2 invokes S1 over the impacted set; S1 emits `s1-findings.json` (validated
and written by the bundled script); S2 consumes it in its proposer phase. S1 does **not**
re-implement S2's impact engine; S2 does **not** re-implement S1's convention inference.

### Scoped-analysis flow

1. **Receive the impacted area from S2.** The user (or S2's CLI) passes:
   - A list of impacted symbols (function/method/class names that changed).
   - A list of impacted test IDs (test files or test IDs from the coverage/static map).
   - A sample of existing test file contents (up to ~10 files; prioritize test files nearest the
     impacted area).
2. **Infer conventions in-session.** Read the sampled test files. Identify: primary framework
   (`pytest`/`jest`/`junit5`/`vitest`/…), directory layout convention, function/class naming
   pattern, fixture and mock idioms, assertion style. Write a concise inference for each.
3. **Identify gaps scoped to the blast radius.** For each impacted symbol, check whether:
   - There is a negative test (error/exception path).
   - There are edge-case tests (boundary inputs, empty inputs, overflow, None/null).
   - The symbol itself has *any* test reaching it (if not → `untested_symbol` gap).
   - Existing tests in the impacted area exhibit test smells (magic literals, no assertions,
     copy-paste across cases).
   Only report gaps within the blast radius (impacted symbols and their covering tests); do **not**
   audit the whole suite.
4. **Emit the structured findings payload.** Construct the `s1-findings.json` object conforming
   to `references/schemas/s1-findings.schema.json` (schema_version "1.0", conventions
   object, gaps array). Run the bundled script to validate and write the artifact:
   ```
   python3 scripts/sdet_scaffold.py --scoped-findings <findings.json> --run-dir <run-dir>
   ```
   The script validates structural integrity (required keys, types, enum values) and writes
   `s1-findings.json` under `--run-dir`. Report any validation errors to the user.
5. **Return.** The artifact is ready for S2 to consume. Do not produce `test-architecture.md`
   or scaffold configs in scoped-analysis mode.

**S1's existing whole-suite/strategy contract is fully preserved.** Scoped-analysis is an
**additive** mode: it does not alter or replace the 8-section architecture flow. A `/sdet-architect`
call without the scoped-analysis inputs follows the whole-suite flow exactly as documented above.

## s1-findings.json schema

The findings payload emitted in scoped-analysis mode must be an object with:

```jsonc
{
  "schema_version": "1.0",
  "conventions": {
    "framework": "pytest",                                // primary test framework
    "layout": "tests/ mirroring src/",                   // directory layout convention
    "naming": "test_<thing>_<condition>",                 // function/class naming pattern
    "fixture_mock_idioms": "@pytest.fixture in conftest.py",
    "assertion_style": "assert <expr>",
    "extra": { "optional_key": "optional value" }        // optional additional observations
  },
  "gaps": [
    {
      "symbol": "compute_impacted",                       // changed/new symbol
      "gap_type": "missing_negative_test",               // see enum below
      "description": "No test verifies behaviour when the coverage map is empty.",
      "suggested_test_name": "test_compute_impacted_empty_coverage_returns_static_fallback"
    }
  ]
}
```

`gap_type` enum: `missing_negative_test`, `missing_edge_case`, `untested_symbol`,
`test_smell`, `missing_contract_test`, `other`.

The full machine-readable schema lives in `references/schemas/s1-findings.schema.json`.

## Invariants

- `test-architecture.md` carries exactly these 8 sections, in this order: **Stack profile**,
  **Test cone target**, **Layer 1 — Unit**, **Layer 2 — Contract**, **Layer 3 — Integration**,
  **Layer 4 — E2E**, **Test data strategy**, **CI integration**. Each section ends with a
  `### Rationale` ADR block (or `_Not applicable for this stack._` when there is none).
- One document per detected stack — a polyglot project gets multiple documents; no detected
  stack means no document. Detection descends into bounded, non-excluded subdirectories
  (depth ≤ 2, skipping VCS/dependency/build/hidden dirs and symlinks), so monorepo sub-stacks
  are surfaced and deduplicated by `(language, root)`; each stack's config scaffolding and output
  document are placed under its own `root` (e.g. `portal/vitest.config.ts`).
- Config scaffolding is idempotent: a config file is written only when the relevant block is
  absent (no `pytest.ini` / `[tool.pytest.ini_options]`, no vitest/jest config, no
  `maven-surefire-plugin` / `useJUnitPlatform()`), and re-running never overwrites existing
  configuration. The architecture is designed once per project; subsequent runs are safe no-ops
  for already-configured stacks.
- The bundled script (`scripts/sdet_scaffold.py`) is stdlib-only — no installed foundry
  CLI and no additional pip packages are required. The model does all judgment (tooling picks,
  ADR rationale; convention inference and gap identification in scoped-analysis mode); the script
  handles all deterministic work (stack detection, config writes, section assembly, s1-findings
  structural validation and write).
- Scoped-analysis mode: `s1-findings.json` conforms to `references/schemas/s1-findings.schema.json`
  (`schema_version` "1.0"; `conventions` with five required string fields; `gaps` array with
  `symbol`, `gap_type` (closed enum), `description`). The bundled script rejects structurally
  invalid payloads at write time so S2 never receives a corrupt artifact.
- **Scoped-analysis is additive and non-breaking.** The whole-suite/strategy contract (8 sections,
  idempotent scaffolding, `--payload` / `--detect-only` / `--scaffold-only`) is unchanged.
