---
name: mod-app-analysis
description: >
  Analyze a source repository end-to-end — running /mod-playbook-discovery,
  /repo-digest, and (when a web tier is detected) /functional-map — to produce a
  compact app-digest.json (modules, imports, shared-libs, debt, migration-state,
  stack/flavor, tier) and a terse ANALYSIS.md with every claim cited path:line.
  The digest is consumable by the SP1 adapter (primitives._shared.modernization
  .adapters.app_digest) so downstream synthesizers can correlate the app layer
  with DB, API, and UI artifacts into a whole-stack system map. Use when you need
  the per-repo application analysis input for /mod-modernization-scope, or when
  you want a standalone app digest and analysis document for any repository.
---

# /mod-app-analysis

The per-repo **application analyzer**: an M-tier skill-first orchestrator that
runs the Foundry discovery stack over a source repository and assembles the
results into two durable artifacts:

- **`app-digest.json`** — a compact, schema-versioned description of the repo's
  modules, imports, shared libraries, tech debt, migration coexistence state,
  stack flavor, and tier hint. Shaped to round-trip through
  `primitives._shared.modernization.adapters.app_digest.to_digest` so the
  downstream `/mod-modernization-scope` synthesizer can join it with DB, API,
  and functional-map digests.
- **`ANALYSIS.md`** — a terse narrative analysis document that cites every
  claim as `path:line` and is validated by the roll-up aggregator's refcheck
  pass before writing.

`/mod-app-analysis` orchestrates existing Foundry primitives — it adds no
bespoke per-stack analysis:

1. `/mod-playbook-discovery` — 16-section `PLAYBOOK.md` with flavor-aware
   architecture, tech-debt inventory, and modernization analysis.
2. `/repo-digest` — repo structure, LOC breakdown, top-level module inventory,
   dependency graph.
3. `/functional-map` (web-tier repos only) — endpoint/navigation graph that
   the downstream `/mod-modernization-scope` adapter maps to HTTP interfaces.

The shared Python aggregator (`shared/app_digest_aggregator.py`) then assembles
those artifacts into the contracted `app-digest.json` shape and emits a
citation-validated `ANALYSIS.md`.

## When to invoke

- When you are running `/mod-modernization` (the full-stack orchestrator) and
  it needs the app-layer analysis input for Step 4.
- When you want a standalone per-repo application digest and analysis document
  without driving the full modernization chain.
- When you need to refresh the app-layer analysis for a repo whose playbook,
  dependencies, or tech-debt inventory has changed.
- NOT for SQL-database analysis → use `/mod-db-schema-analyze` (M1) +
  `/mod-stored-proc-analyze` (M2).
- NOT for the full modernization chain from one command → use `/mod-modernization`.

## When invoked

1. **Resolve the repo root and output directory.** The positional `<repo-root>`
   argument (default: current working directory) must exist and be a directory.
   Resolve it to an absolute path. The output directory is
   `.foundry/context/modernization/<repo-name>/` under the repo root where
   `<repo-name>` is the basename of the repo root. Verify it is writable;
   create it if absent. If the repo root does not exist, emit an error and stop.

2. **Run the discovery stack via dispatched subagents.** Run each analyzer with
   curated context — do not let any subagent inherit this session's history:

   a. **`/mod-playbook-discovery`** against the repo root. Pass
      `--no-synthesis` so the extract leg is deterministic (no model round-trip
      needed here — the synthesis narrative is not required by the digest).
      Expect `PLAYBOOK.md` and the structured section data under the repo's
      `.foundry/context/` tree. If this stage fails, emit an error naming the
      stage and stop.

   b. **`/repo-digest`** against the repo root. Expect the LOC breakdown,
      module list, and dependency summary in its output. Parse the LOC table and
      module/package inventory from its output. If this stage fails, emit an
      error naming the stage and stop.

   c. **(Web-tier only) `/functional-map`** — invoke only when `/repo-digest`
      or `/mod-playbook-discovery` signals a web-tier presence (e.g. detects
      route annotations, HTTP controller classes, JSP/template files, or a web
      framework dependency). Expect `functional-map.json` in
      `.foundry/context/functional-map/`. A failure here is non-fatal: record a
      warning in ANALYSIS.md and continue.

      **Thread the functional-map into the playbook (#1358).** When a
      `functional-map.json` is produced, the playbook's §8 Business Rules Catalog
      should carry the functional-map's **real** business rules instead of the
      per-class "candidate rule — pending verification" stubs. Re-render the
      playbook passing the functional-map's synthesized layer through the
      `functional_map` channel:

      ```python
      from primitives._shared.discovery.playbook import render_playbook
      from primitives._shared.modernization.adapters import functional_map_to_inventory
      import json

      fm = functional_map_to_inventory(json.load(open(".foundry/context/functional-map/functional-map.json")))
      playbook_md = render_playbook(analysis_digest, functional_map=fm)  # §8 uses fm.business_rules
      ```

      Without a functional-map (a non-web repo), the playbook renders exactly as
      before — the channel is additive and optional.

3. **Assemble and validate the app digest.** With all stage artifacts on disk,
   run the deterministic aggregator:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.app_analysis.shared.app_digest_aggregator \
       --repo-root <repo-root> \
       --context-dir .foundry/context/modernization/<repo-name>
   ```

   The aggregator:
   - Parses the `PLAYBOOK.md` (§2 architecture, §14 tech-debt, §15 modernization)
     and `/repo-digest` output to derive `modules`, `loc_by_language`, `imports`,
     `shared_libs`, `debt`, `migration_state`, `flavor`, and `tier`.
   - Validates every `path:line` citation in `ANALYSIS.md` via the
     `primitives._shared.modernization.refcheck` module. Any unresolvable
     citation is reported and removed before writing.
   - Writes `app-digest.json` and `ANALYSIS.md` to the output directory.

   If the aggregator exits non-zero, surface the error and stop.

4. **Surface the outputs.** Report the paths to `app-digest.json` and
   `ANALYSIS.md`, confirm that citations validated (or list any that were
   dropped), and note the discovered flavor and tier hint. If a web-tier was
   detected, confirm whether `/functional-map` ran successfully.

## How to invoke

Invoke as `/mod-app-analysis <repo-root>` where `<repo-root>` is the path to
the source repository to analyze (defaults to the current working directory).
Pass `--no-functional-map` to skip the `/functional-map` stage even when a
web tier is detected. Pass `--context-dir PATH` to override the default output
directory (`.foundry/context/modernization/<repo-name>/`).

YOU (the harness LLM) drive the discovery stack directly via dispatched
subagents — there is no Python control-plane to shell out to for the discovery
stages. The aggregator (`shared/app_digest_aggregator.py`) is the only Python
process and handles the post-discovery assembly.

## Output

All durable output lands under `.foundry/context/modernization/<repo-name>/`:

- **`app-digest.json`** — compact application digest in the contracted shape:
  ```json
  {
    "schema_version": "1.0",
    "flavor": "<stack-flavor>",
    "loc_by_language": {"Java": 12500, "XML": 800},
    "modules": [{"name": "...", "kind": "...", "weight": 1200}],
    "domains": ["owner", "vet", "visit"],
    "imports": [{"target": "...", "kind": "dependency"}],
    "shared_libs": ["spring-core", "jackson-databind"],
    "debt": ["No dependency injection — manual wiring", "..."],
    "migration_state": "Struts↔Spring coexistence",
    "tier": "strangler-fig"
  }
  ```
  Consumable by `primitives._shared.modernization.adapters.app_digest.to_digest`.

- **`ANALYSIS.md`** — terse narrative analysis: flavor/tier rationale, module
  inventory with weight, top tech-debt items, migration-state assessment, and
  modernization recommendation. Every claim cites a `path:line` in the source
  repo or in the upstream artifacts. Citations are validated; unresolvable ones
  are dropped before writing.

The upstream stage artifacts (`PLAYBOOK.md`, repo-digest output,
`functional-map.json` if produced) are left in their canonical locations and
referenced by relative path from `ANALYSIS.md`.

## Invariants

- **Skill-first orchestrator — §4.1 orchestrator-shape carve-out applies.** This
  primitive carries a minimal `shared/` (the deterministic aggregator only) and no
  `ci.yaml` and no registered CLI command. The discovery stages run in-session via
  dispatched subagents; the aggregator is the only subprocess invoked by the skill body.
- **No thin-air synthesis.** Every claim in `ANALYSIS.md` and every field in
  `app-digest.json` is derived from the on-disk artifacts produced by
  `/mod-playbook-discovery` and `/repo-digest`. No names are invented.
- **Citation validation is mandatory.** The aggregator runs refcheck over every
  `path:line` in `ANALYSIS.md` before writing. Unresolvable citations are
  dropped (not silently kept). The ANALYSIS.md written to disk has zero
  unresolved citations.
- **`app-digest.json` shape is contracted.** The JSON must round-trip through
  `primitives._shared.modernization.adapters.app_digest.to_digest` without
  error. A shape mismatch is a hard error; the aggregator validates the output
  against the adapter's expected shape before writing.
- **Output directory convention.** Durable output always lands under
  `.foundry/context/modernization/<repo-name>/`, mirroring the M0 convention
  of `.foundry/context/db/` for the SQL chain. The `<repo-name>` is the
  basename of the resolved repo root.
- **`mod-` prefix is mandatory.** The slash command is `/mod-app-analysis`, not
  `/app-analysis`. The M-tier `mod-` namespace prefix (charter §3.2.M)
  distinguishes modernization workbench skills from other S-tier skills.
- **No Bedrock backend and no Python control-plane on the discovery path.** The
  Python CLI never calls an LLM. The aggregator is pure assembly + validation.
- **Web-tier detection is advisory.** If `/functional-map` fails or is skipped,
  the digest is still valid — `functional-map.json` is recorded as absent, and
  the downstream `/mod-modernization-scope` will have no UI→API correlation
  edges for this repo (expected behavior for non-web repos).

## Target-stack judgment (SP6, additive — inert without a target)

**This block is inert when no `target-stack.json` is present.** When `/mod-modernization`
is running the full app-mod chain with a declared target (via `--target KEY=VALUE`), it
writes `target-stack.json` under `.foundry/context/modernization/.state/<run-id>/`. When
`/mod-app-analysis` is invoked as part of that chain, check for that artifact before
assembling the digest. If absent, skip this block entirely — output is byte-identical to
the no-target path.

**If a target stack is provided** (i.e. `target-stack.json` exists under the run-id state
dir):

1. **Flag source-stack-specific debt relative to the target.** When compiling the `debt`
   list for `app-digest.json` and the tech-debt narrative in `ANALYSIS.md`, annotate any
   debt item that is *specifically a consequence of the source stack being replaced*. For
   example:
   - "JSP view coupling — all user-facing HTML is rendered server-side via Spring MVC
     `ViewResolver`; migrating to Angular SPA requires extracting every view into a
     component tree" (when the target frontend is Angular or React).
   - "Struts `ActionForm` / `ActionForward` tangle — migrating to Spring Boot REST
     controllers requires decoupling the form-binding from the routing layer" (when
     the target backend is Spring Boot or Quarkus).
   - "JDBC template boilerplate without ORM mapping — migrating to JPA/Hibernate
     (the standard ORM in Spring Boot) requires entity model extraction from raw SQL"
     (when the source uses raw JDBC and the target stack is Spring Boot).
   Each such annotation is only added when there is source evidence for the pattern
   (a `path:line` citation from the playbook or repo-digest artifact); do NOT fabricate
   tech-debt entries.

2. **Surface migration-state coupling in `ANALYSIS.md`.** In the modernization-assessment
   section of `ANALYSIS.md`, include a paragraph that explicitly names the source stack's
   coupling points that the declared target requires dissolving. Frame it as:
   "Given the declared target stack (`<target dimensions>`), the highest-priority
   source-stack coupling points are: [list from step 1]." Cite every claim with a
   `path:line` reference. This paragraph is ONLY written when a target is present.

3. **Pass the target context to `/functional-map` when running it.** When a web tier is
   detected and you invoke `/functional-map` as a subagent (step 2c above), inform it
   that a `target-stack.json` is available under the run-id state dir so its own SP6
   block activates. The functional-map's target-aware synthesis will produce FR statements
   that name source-framework coupling explicitly, which the downstream scope synthesizer
   can act on.

**Backward-compatible guarantee:** when `target-stack.json` is absent (the default
standalone invocation), none of the above applies. The `app-digest.json` shape, the
`ANALYSIS.md` structure, and the aggregator's refcheck behavior are all unchanged.
