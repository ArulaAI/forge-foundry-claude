---
name: mod-test-inventory
description: >
  Inventory a source repository's existing tests as parity oracles. Walks the
  repo for test files (JUnit/TestNG, pytest/unittest, Jest/Vitest/Mocha) via the
  shared discovery walk — honoring the #1359 build-output exclusions so compiled
  or vendored tests under target/, build/, node_modules/ are never ingested —
  and emits test-inventory.json: per test its name, the unit/endpoint it
  exercises, and its assertion intent. The scope stage slices these by bounded
  context (endpoint→domain) and the /mod-modernization-scope synthesizer carries
  them into the Test Spec (§11) as parity oracles (is_parity_oracle=True), so the
  migration spec grounds acceptance against actual legacy behavior. Use when you
  need the legacy-test grounding input for /mod-modernization-scope, or a
  standalone catalog of a repo's tests for any flow (e.g. S1 sdet-architect).
---

# /mod-test-inventory

The **legacy-test / parity-oracle analyzer**: an M-tier skill-first orchestrator
that walks a source repository for its existing tests and assembles them into one
durable artifact:

- **`test-inventory.json`** — a compact, schema-versioned catalog of every test
  the walk can identify: per test its `name`, the `unit`/`endpoint` it exercises
  (best-effort from the target class/module/route), its `assertion_intent` (what
  it checks), and the `file:line` where it is declared.

This is the one input no other analyzer produces. The benchmark's key move —
carrying existing tests forward as **parity oracles** so the migration spec's
acceptance is grounded in *actual legacy behavior* — has zero source without it.
`/mod-test-inventory` is its own primitive so other flows (e.g. S1
`sdet-architect`) can reuse the inventory.

The deterministic walk + parse is the aggregator's job; deeper interpretive work
(nuanced assertion intent, endpoint→unit binding the regexes cannot infer) is the
skill body's in-session job — skill-first.

## When to invoke

- When you are running `/mod-modernization-scope` (the app-tier synthesizer) and
  it needs the legacy-test grounding input so §11 renders **parity oracles**
  instead of synthesized-only T- scenarios.
- When you want a standalone catalog of a repository's existing tests — what is
  tested, at what unit/endpoint, with what assertion intent — for migration
  planning or for S1 `sdet-architect`.
- When you need to refresh the test inventory for a repo whose test suite changed.
- NOT for authoring *new* tests → use S3 `test-from-spec` / S4 `test-scaffold`.
- NOT for SQL-database analysis → use `/mod-db-schema-analyze` (M1).

## When invoked

1. **Resolve the repo root and output directory.** The positional `<repo-root>`
   argument (default: current working directory) must exist and be a directory.
   Resolve it to an absolute path. The output directory is
   `.foundry/context/modernization/<repo-name>/` under the invoking repo where
   `<repo-name>` is the basename of the repo root (mirroring `/mod-app-analysis`).
   Verify it is writable; create it if absent. If the repo root does not exist,
   emit an error and stop.

2. **Walk and parse the tests via the deterministic aggregator.** Run:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.test_inventory.shared.test_inventory_aggregator \
       --repo-root <repo-root> \
       --context-dir .foundry/context/modernization/<repo-name>
   ```

   The aggregator walks the repo with
   `primitives._shared.scan_exclusions.walk_source_files` (so the #1359
   build-output exclusions apply — `target/`, `build/`, `node_modules/`, `dist/`,
   `.venv/`, and the vendored `.foundry/` / `.claude/` trees are skipped),
   classifies each test file by framework, and extracts every test case: its
   `name`, the `unit`/`endpoint` it exercises, its `assertion_intent`, and the
   `file:line` of the declaration. It writes `test-inventory.json`. If the
   aggregator exits non-zero, surface the error and stop.

3. **(Optional) Enrich in-session.** Read `test-inventory.json`. For entries the
   deterministic pass left with an empty `assertion_intent` or `null` `endpoint`,
   open the cited `file:line` and author a concise `assertion_intent` / bind the
   `endpoint` from the test body — carry-forward, never invent. This is the
   skill-first interpretive step; it enriches, it does not fabricate. Do NOT add a
   test entry that has no `file:line` in the source.

4. **Surface the outputs.** Report the path to `test-inventory.json`, the test
   count, the test-file count, and the frameworks detected. Note that the
   inventory is now available to `/mod-modernization-scope`, which will slice it
   by bounded context and mark carried-forward tests as parity oracles in §11.

## How to invoke

Invoke as `/mod-test-inventory <repo-root>` where `<repo-root>` is the path to
the source repository to inventory (defaults to the current working directory).
Pass `--context-dir PATH` to override the default output directory
(`.foundry/context/modernization/<repo-name>/`).

YOU (the harness LLM) resolve the repo root and (optionally) enrich the emitted
inventory in-session; the aggregator
(`shared/test_inventory_aggregator.py`) is the only Python process and handles
the deterministic walk + parse. There is no CLI subcommand and no exit-7 loop —
the aggregator is invoked directly as a module.

## §11 parity-oracle integration (how the inventory reaches the Test Spec)

`test-inventory.json` is consumed **additively** by `/mod-modernization-scope`:

1. `/mod-modernization-scope` discovers `test-inventory.json` alongside the
   app-digest / functional-map / schema / api artifacts (same
   `.foundry/context/modernization/<repo>/` convention).
2. Its pipeline slices the inventory by bounded context using the **shared
   `context_resolver`** (the SAME endpoint→domain join SP-A/SP-C/SP-D use — no new
   slicer) and threads the per-context tests into each spec prompt as
   `parity_oracles`.
3. The harness carries each parity oracle into that spec's `test_scenarios` with
   `is_parity_oracle=True`, so §11 renders real oracle grounding rather than
   synthesized-only T- scenarios.

**The integration is additive and optional.** Absent `test-inventory.json`, §11
renders synthesized T- scenarios exactly as before (honest, but not parity
oracles) — no regression to the no-inventory path.

## Output

All durable output lands under `.foundry/context/modernization/<repo-name>/`:

- **`test-inventory.json`** — legacy-test catalog in the contracted shape:
  ```json
  {
    "schema_version": "1.0",
    "repo": "spring-mvc-jsp-petclinic",
    "test_file_count": 12,
    "test_count": 47,
    "frameworks": ["junit"],
    "tests": [
      {
        "name": "testShowOwner",
        "unit": "OwnerController",
        "endpoint": "/owners/{id}",
        "assertion_intent": "asserts status().isOk()",
        "file": "src/test/java/org/example/OwnerControllerTest.java",
        "line": 42,
        "framework": "junit"
      }
    ]
  }
  ```

Every entry is grounded in a real `file:line` declaration; no test is invented.

## Invariants

- **Skill-first orchestrator — §4.1 orchestrator-shape carve-out applies.** This
  primitive carries a minimal `shared/` (the deterministic aggregator only) and no
  `ci.yaml` and no registered CLI subcommand. The walk + parse runs in
  the aggregator subprocess; any interpretive enrichment runs in-session.
- **No thin-air synthesis.** Every entry in `test-inventory.json` is grounded in a
  real `file:line` test declaration parsed from the source repo. No test is
  invented; the in-session enrichment step only refines `assertion_intent` /
  `endpoint` for entries that already exist.
- **Build-output exclusions are honored (#1359).** The walk uses
  `walk_source_files`, so compiled / vendored tests under `target/`, `build/`,
  `node_modules/`, `dist/`, `.venv/` and the vendored `.foundry/` / `.claude/`
  trees are never ingested.
- **`mod-` prefix is mandatory.** The slash command is `/mod-test-inventory`, not
  `/test-inventory`. The M-tier `mod-` namespace prefix (charter §3.2.M)
  distinguishes modernization-workbench skills from other S-tier skills.
- **Output directory convention.** Durable output always lands under
  `.foundry/context/modernization/<repo-name>/`, mirroring `/mod-app-analysis` so
  `/mod-modernization-scope` discovers it in one place.
- **Additive §11 integration.** The scope synthesizer consumes the inventory when
  present and marks carried-forward tests as parity oracles; absent the inventory,
  §11 renders synthesized scenarios with no regression.
- **The Python CLI never calls an LLM.** The aggregator is a pure walk + parse +
  roll-up. The interpretive enrichment is the harness's in-session job.
