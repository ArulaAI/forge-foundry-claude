---
name: mod-modernization
description: >
  Drive the full app-modernization chain end-to-end by composing five analyzer
  skills in order — S61 /functional-map → M1 /mod-db-schema-analyze → S40
  /api-contract-review → SP3 /mod-app-analysis → SP2 /mod-modernization-scope —
  dispatching each stage as a subagent that invokes the stage's slash command,
  persisting each analyzer's durable artifact to the conventional
  .foundry/context/ discovery path the scope synthesizer reads from, tracking
  per-stage pass/fail/skip in a resumable state file, and rolling a top-level
  MODERNIZATION-SUMMARY.md. Use when the user asks to "modernize this app",
  "run the app-modernization chain", "produce modernization docs (specs, ADRs,
  roadmap) for a repo", or "build the SP4 modernization plan".
---

# /mod-modernization

The app-modernization chain ORCHESTRATOR (app-mod SP4): an orchestrator skill that
composes the five analyzer/synthesizer skills by reference and drives them in order
over one source repository:

1. **S61 `/functional-map`** → `functional-map/functional-map.json` (+ `FUNCTIONAL-MAP.md`)
2. **M1 `/mod-db-schema-analyze`** → `db/schema.json`
3. **S40 `/api-contract-review`** → `api/api-contract.json`
4. **SP3 `/mod-app-analysis`** → `modernization/<repo>/app-digest.json` (+ `ANALYSIS.md`)
5. **SP2 `/mod-modernization-scope`** → reads stages 1–4 and emits the **four
   deliverables**: `MODERNIZATION.md` + `system-map.json`, `scope/<domain>.spec.md`,
   `adr/ADR-NNNN-*.md`, and `ROADMAP.md`.

`/mod-modernization` exists so consumers do not have to learn the chain's stage list,
the ordering, the discovery-path convention each analyzer must write to, or the
`data:<table>` join-key contract that threads the UI→API→DB system map. It owns the
chain contract so the analyzer skills stay focused on their own concerns; it adds no
bespoke per-stage analysis. The run is **stateful and resumable** and each stage is
**gracefully skippable** when it does not apply.

## When to invoke

- When the user wants to drive a repository all the way from source to a full set of
  modernization documents (deconstruction, specs, ADRs, roadmap) in one workflow —
  not five hand-sequenced commands with manual artifact-path plumbing.
- When the user asks to "modernize this app", "run the app-modernization chain",
  "produce modernization docs for a repo", or "build the SP4 modernization plan".
- To **resume** a partially-completed run — the previous run's per-stage state was
  persisted and the chain picks up from the first incomplete or failed stage.
- NOT for the **SQL-database** modernization chain (DDL dump → stored-proc conversions)
  → use M0 `/mod-sql-modernization`.
- NOT for a single analyzer's output — invoke that analyzer directly.

## When invoked

1. **Resolve the repo and the run state.** Resolve the positional `<repo>` argument
   (default: current working directory) to an absolute directory; a missing repo is a
   hard error. Let `<repo-name>` be its basename. The context root is
   `.foundry/context/` under the repo (override with `--context-root`). Determine the
   run id: if the user passed `--run-id`, use it; otherwise mint a fresh `run-<8hex>`.
   The resumable state file is
   `<context-root>/modernization/.state/<run-id>/state.json`.
   **Resume check:** if that file exists, load it; every stage already recorded
   `complete` or `skipped` is NOT re-run — resume from the first stage that is `pending`
   or `failed`. If no state file exists, start fresh at stage 1 with all five stages
   `pending`. Honor `--start-from STAGE` as an explicit resume-cursor override.

   **Detect the multi-repo repo-set (MR7, #1556).** Enterprise systems split their UI /
   API / DB / IaC tiers across **separate repositories**. Before resolving a single
   positional `<repo>`, check for a repo-set: either a **`.foundry/repos.yaml`** manifest
   under the working dir, OR one or more repeatable **`--repo <role>=<path>`** overrides on
   the command line (`role ∈ {ui, api, db, iac}`). Load it with the shared reader (mirrors
   the `parse_target` snippet above):

   ```bash
   uv tool run --from forge-foundry-primitives python3 - "$@" <<'PY'
   import json, sys
   from pathlib import Path
   from primitives._shared.modernization.repos import build_repo_set
   # argv is the list of --repo overrides ("ui=../web-ui" …); [] when none passed.
   repo_set = build_repo_set(Path.cwd(), tuple(sys.argv[1:]))
   print(json.dumps([{"role": e.role, "path": e.path, "name": Path(e.path).name}
                     for e in repo_set.entries], indent=2))
   PY
   ```
   > Use `uv tool run --from forge-foundry-primitives python3 …` (not a bare
   > `PYTHONPATH=.foundry python3 …`) so the interpreter has the primitives' deps
   > (`pydantic`/`pyyaml`). A bare ambient `python3` (stock macOS) fails with
   > `ModuleNotFoundError: No module named 'pydantic'`/`'yaml'`.

   - **NEITHER a `repos.yaml` NOR any `--repo` → the single-`<repo>` flow below, UNCHANGED**
     (the `build_repo_set` call returns an empty set; every downstream behavior is
     byte-identical — the #1408 additive invariant).
   - **A non-empty repo-set → repo-set mode:** the per-repo analyzers (step 2) run once per
     repo into that repo's `modernization/<repo-name>/` slot, and the scope stage (step 4)
     correlates all repos into ONE system map. `<repo-name>` is the **basename of the repo's
     path**. The repo-set is threaded into the scope stage (step 4) and recorded in
     `state.json` under a `repos:` list so the roll-up renders a per-repo section.

   **Resolve and persist the optional target stack (SP2, #1410).** `/mod-modernization`
   accepts a **repeatable `--target KEY=VALUE`** option declaring the multi-dimensional
   stack to modernize *toward* (e.g. `--target frontend=angular --target
   backend=spring-boot@3.3 --target database=oracle --target cloud=aws`). Each pair is
   `KEY=VALUE[@version]`; an omitted `@version` resolves to the bundled registry's
   latest-known version (offline — never a live lookup). Parse + resolve them **once**,
   surfacing any warnings (an unknown key/value is carried verbatim with a warning, never
   a hard error) to the operator:

   ```bash
   uv tool run --from forge-foundry-primitives python3 - "$@" <<'PY'
   import json, sys
   from primitives._shared.modernization.target_stack import parse_target
   ts, warns = parse_target(sys.argv[1:])   # each argv is one KEY=VALUE[@version]
   for w in warns:
       print(f"warning: {w}", file=sys.stderr)
   print(json.dumps(ts.model_dump(), indent=2))
   PY
   ```
   > As above, `uv tool run --from forge-foundry-primitives python3 …` (the interpreter
   > with `pydantic`/`pyyaml`), not a bare `PYTHONPATH=.foundry python3 …`.

   When **≥1 `--target` pair** was supplied, write the serialized `TargetStack` to
   `<context-root>/modernization/.state/<run-id>/target-stack.json` — the exact path the
   scope stage auto-discovers under its run-id state dir. The persisted shape is a
   `TargetStack` (list of resolved dimensions), for example:

   ```json
   {
     "dimensions": [
       {
         "key": "backend",
         "value": "spring-boot",
         "version": "3.3",
         "version_source": "explicit",
         "registry_matched": true,
         "label": "Spring Boot 3.3"
       }
     ],
     "notes": ""
   }
   ```

   **No `--target` supplied → do NOT write `target-stack.json`** — the whole chain stays
   byte-identical to its descriptive behavior (fully backward-compatible). The target,
   once persisted, is available to every stage under the run-id state dir; each stage
   SKILL that is target-aware reads it from there.

   **Ingest the optional stated High-Level Architecture (Track A, #1428).** Discover
   HLA documents in **`.foundry/hla/`** under the repo (override the directory with an
   optional `--hla-dir <path>`). Collect every `*.md` and `*.docx` in that folder
   (sorted, deterministic):
   - **`.md` → read the text directly** (stdlib file read — deterministic, offline).
   - **`.docx` → extract the text IN-SESSION, in the SKILL body — NEVER a Python
     dependency.** A `.docx` is a zip of XML parts; read the archive's
     `word/document.xml` and strip the tags to recover the paragraph text (you, the
     harness, do this in-session — do NOT add or invoke `python-docx` or any binary
     parser; the chain stays air-gapped and dependency-free). This honors the offline
     invariant (Global Constraints).

   For each collected document, summarize from its prose the stated architecture
   elements — `stated_components`, `boundaries`, `integrations`, `constraints`,
   `nfrs` (free-form human prose → structured lists; leave a list empty when the HLA
   states nothing for it — no fabrication). **Only when ≥1 HLA document exists**, write
   the serialized `HlaSource` to
   `<context-root>/modernization/.state/<run-id>/hla-source.json` — the exact path the
   scope stage auto-discovers under its run-id state dir. The persisted shape is an
   `HlaSource` (a list of documents), for example:

   ```json
   {
     "documents": [
       {
         "source_path": ".foundry/hla/architecture.md",
         "text": "The system is a three-tier web application. The Owner Service owns owner and pet records; the Vet Service owns veterinarian and specialty data. Both persist to PostgreSQL.",
         "stated_components": ["Owner Service", "Vet Service"],
         "boundaries": ["owner", "vet"],
         "integrations": ["PostgreSQL"],
         "constraints": ["must remain on-premises"],
         "nfrs": ["99.9% availability"]
       }
     ]
   }
   ```

   **No `.foundry/hla/` docs → do NOT write `hla-source.json`** — the chain stays
   byte-identical to its descriptive behavior (fully backward-compatible; the scope
   stage renders no Architecture Conformance section). When `hla-source.json` was
   written, pass `--hla <context-root>/modernization/.state/<run-id>/hla-source.json`
   to the scope stage (step 4) so it grounds the specs/ADRs against the stated
   architecture and emits the **Architecture Conformance** section.

   **Optionally run the runtime explorer FIRST (`--runtime-explore`).** When the user
   passes **`--runtime-explore`**, drive the running application with local Playwright
   BEFORE the source-analysis stages, so its runtime discovery is on disk when the scope
   stage (stage 5) reads it. This is an **optional pre-stage**, not one of the five source
   analyzers — driving a live, authenticated, deployed target is not a source-analysis
   step, so it is opt-in and gated on an explore-plan:
   - **Require the plan.** Read **`.foundry/explore-plan.yaml`** under the repo (override
     with `--explore-plan <path>`). If `--runtime-explore` was passed but no plan exists,
     record the `runtime-explore` pre-stage `skipped` (note: "no explore-plan.yaml; create
     one — see /mod-runtime-explore") and continue with the source chain (never hard-fail
     the modernization run over a missing optional plan). If `--runtime-explore` was NOT
     passed, skip this pre-stage entirely (no runtime discovery; the chain is unchanged).
   - **Dispatch `/mod-runtime-explore`** as a fresh subagent against the repo. It reads
     `.foundry/explore-plan.yaml`, crawls from the plan's `base_url` inside its
     `url_scope`, logs in via the plan's `auth` reference when required (Phase B), and
     writes **`<context-root>/runtime/runtime-journeys.json`** + `RUNTIME-DISCOVERY.md` +
     `screenshots/` — the exact discovery path the scope stage auto-consumes. Requires the
     optional `[browser]` extra locally (install it with the form that matches how the CLI
     was installed — for a git-based `uv tool` install, the pinned-tag default, use
     `uv tool install --force 'forge-foundry-primitives[browser] @ git+…@<tag>'` then
     `uv tool run --from 'forge-foundry-primitives[browser]' playwright install chromium`;
     see `/mod-runtime-explore`'s USAGE for the pip / project-checkout forms). If Playwright /
     a browser is missing, `/mod-runtime-explore` exits 6 — record the pre-stage `skipped`
     (note the exit-6 install hint) and continue (the source chain still runs).
   - **Persist** the pre-stage outcome to `state.json` under `stages.runtime-explore` as
     `{status, note, artifact}` (`complete` when `runtime-journeys.json` was written,
     `skipped` when opt-out / no plan / no browser, `failed` when it ran and errored),
     exactly like the five stages. It is **resumable** and **gracefully skippable** — a
     failed or skipped runtime-explore never blocks the source-analysis chain.
   - **Additive, no wiring needed downstream.** The scope stage (stage 5) auto-discovers
     `runtime/runtime-journeys.json` at its conventional path and threads the runtime
     journeys/screens into the per-context specs (`runtime_observed`) + the BRD (business
     narrative + embedded screenshots) — you pass NO extra flag to stage 5 for this. When
     the pre-stage was skipped, the artifact is simply absent and the scope output is
     byte-identical to a run without it (the #1408 additive invariant).

   **Optionally modernize ONE feature slice at a time (`--feature` / `--context`, #1542).**
   When the user passes **`--feature "<name>"`** (repeatable, e.g. `--feature "Account
   Summary: Contribution History"`) or the low-level **`--context <domain>`**, deconstruct
   just that slice + its dependency closure — its dependencies come along as **Boundary
   Contracts**, not re-specced. Re-run with the same `--run-id` and the next `--feature`
   to advance a piece at a time. **The analyzers ALWAYS run whole-app** — global discovery
   is required to compute what the slice depends on; only the SP2 scope stage (stage 5) is
   scoped.

   **The feature name is preserved as the spec's human title (#1609 F4).** Once the
   plain-English name (e.g. `"Vet Search"`) is resolved to a bounded-context domain slug
   (e.g. `"vets"`), pass BOTH to the scope CLI continue leg:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-modernization-scope --code-root <ROOT> \
     --run-id <RUN-ID> --continue --judgments <state-dir>/<run-id>/synthesis.json \
     --context vets --feature-label "Vet Search"
   ```

   `--context` selects the slice; the slug stays the stable `spec_id` and filename
   (`scope/spec-vets.spec.md`). `--feature-label` titles every in-scope spec with the
   plain-English name — the rendered heading becomes **"Vet Search — Modernization
   Specification"** instead of the raw slug. Absent `--feature-label`, the title is the
   slug-derived default (byte-identical to a plain `--context` run).

   Concrete end-to-end example — `/mod-modernization . --feature "Vet Search"`:

   ```mermaid
   flowchart TD
     A["--feature 'Vet Search'"] --> B["stages 1–4 run WHOLE-APP<br/>(reuse cache on a --run-id re-run)"]
     B --> C["SP2 extract leg whole-app<br/>system-map.json + functional-map.json on disk"]
     C --> D["rank_feature_candidates('Vet Search', functional_map, system_map, app_digest)<br/>→ candidates: vets/4, owners/1; suggestion: ['vets']"]
     D --> E["JUDGE in-session: 'Vet Search' → context 'vets'<br/>(evidence: /vets path, VetController handler,<br/>display_name 'Vet List' on /vets endpoint)"]
     E --> F["validate_context_selection(['vets'], system_map) → valid"]
     F --> G["SP2 continue leg<br/>--context vets --feature-label 'Vet Search'"]
     G --> H["scope/spec-vets.spec.md titled 'Vet Search — Modernization Specification'<br/>+ BRD + ADRs + scoped ROADMAP + Boundary Contracts"]
   ```

   - **Resolve `--feature` → context(s) with judgment.** After the SP2 extract leg has
     written the whole-app `system-map.json` + `functional-map.json`, read
     `primitives/_shared/modernization/feature_resolver.py` and call
     `rank_feature_candidates(feature, functional_map=<fm dict>, system_map=<map>,
     app_digest=<digest dict>)` for each `--feature`. It returns ranked `.candidates`
     (context + score + match evidence) + a deterministic `.suggestion` + `.low_confidence`.
     The resolver scores each context by how many of the feature's tokens hit that context's
     **domain label, endpoint paths + handlers, FR statements, flow ids/view-state names,
     and app-digest entity names**. Each `Endpoint` in the functional-map also carries an
     optional `display_name` (F1, #1606 — the human page/view/route name captured by
     `/functional-map`); read the candidate evidence — it will surface those display names —
     to confirm or override the suggestion, because the business feature name rarely matches
     the code's identifiers. (Threading `display_name` as an active scoring signal is planned
     for F2/F3; today it is visible in the evidence list but does not change the numeric score.)
     A `--context <domain>` skips this (it IS the resolved context).
   - **Validate the pick.** Call `validate_context_selection(picked, system_map)` → it
     returns `(valid, unknown)`. If `unknown` is non-empty, **STOP** and report the ranked
     candidate contexts (the operator named a feature you can't confidently place) rather
     than silently modernizing the wrong slice or nothing.
   - **Run the scope stage scoped — pass the feature label.** Pass `--context <domain>`
     (once per resolved context) AND `--feature-label "<name>"` (the original plain-English
     string from the operator) to the SP2 continue-leg invocation. `--feature-label` is what
     gives the emitted spec its human title; omitting it leaves the title as the slug-derived
     default. Only that slice's specs/BRDs/ADRs + a scoped `ROADMAP` are written; its
     dependencies land in the **Boundary Contracts** section on `MODERNIZATION.md`. Record
     the scoped feature(s)/context(s) in `state.json`.
   - **Absent `--feature`/`--context`** → the whole-app run, **byte-identical** to before
     (the #1408 additive invariant; the golden fixtures are unchanged).

2. **Run each chain stage as a dispatched subagent, persisting to the discovery path.**
   Walk the five stages IN ORDER. For each not-yet-complete stage, dispatch a fresh
   subagent (curate exactly the context it needs — the repo path and, for the scope
   stage, the context root; do NOT let it inherit this session's history) that invokes
   the stage's slash command. **Each analyzer's durable artifact MUST land at the exact
   discovery path the scope synthesizer (stage 5) reads from** — this is the load-bearing
   contract (a mismatch silently produces nothing):

   | Stage | Slash command | Durable artifact (relative to `<context-root>`) |
   |---|---|---|
   | 1 functional-map | `/functional-map` | `functional-map/functional-map.json` |
   | 2 db-schema | `/mod-db-schema-analyze` | `db/schema.json` |
   | 3 api-contract | `/api-contract-review` | `api/api-contract.json` |
   | 4 app-analysis | `/mod-app-analysis` | `modernization/<repo-name>/app-digest.json` |
   | 5 scope | `/mod-modernization-scope` | `modernization/MODERNIZATION.md` (+ system-map.json, scope/, adr/, ROADMAP.md) |

   **Full-synthesis pass (`--full-synthesis`).** Every stage that supports a synthesis
   round runs it by default (functional-map, db-schema, api-contract, and the scope
   stage) — EXCEPT stage 4's `/mod-app-analysis`, which runs its own
   `/mod-playbook-discovery` sub-call token-cheaply (`--no-synthesis`) since the
   app-digest doesn't need the playbook narrative. When the user passes
   **`--full-synthesis`**, thread **`--with-playbook-synthesis`** into the stage-4
   `/mod-app-analysis <repo> --with-playbook-synthesis` invocation so the playbook's
   narrative sections are synthesized too — a genuinely end-to-end fully-synthesized
   pass. Absent `--full-synthesis`, the chain is byte-identical to prior behavior.

   After each stage runs, **persist** its outcome to `state.json` under
   `stages.<stage-id>` as `{status, note, artifact}` where `status` is one of
   `complete` (artifact written), `failed` (ran but errored), or `skipped` (not
   applicable). This keeps the run resumable across a mid-chain failure.

   **Repo-set mode — per-repo analyzer passes (MR7, #1556).** When step 1 detected a
   non-empty repo-set, the single-`<repo>` stage table above is REPLACED by a per-repo
   loop: **for EACH repo in the set**, run only that role's analyzers against THAT repo's
   path, writing to **`modernization/<repo-name>/`** (`<repo-name>` = basename of the
   repo's path). These per-repo output locations are EXACTLY what the scope stage's
   multi-repo discovery (MR2) reads — it tries `modernization/<repo-name>/<artifact>.json`
   first, then falls back to the conventional single location — so **align the paths
   precisely**:

   | Repo role | Analyzers run for that repo | Per-repo durable artifact(s) (relative to `<context-root>`) |
   |---|---|---|
   | **every repo** | `/mod-app-analysis <repo-path>` | `modernization/<repo-name>/app-digest.json` |
   | `ui` | + `/functional-map` (+ optionally `/mod-runtime-explore`, `/mod-test-inventory`) | `modernization/<repo-name>/functional-map.json` (or conventional `functional-map/functional-map.json`) |
   | `api` | + `/api-contract-review` | `modernization/<repo-name>/api-contract.json` (or conventional `api/api-contract.json`) |
   | `db` | + `/mod-db-schema-analyze` | `modernization/<repo-name>/schema.json` (or conventional `db/schema.json`) |
   | `iac` | `/mod-app-analysis` only — its IaC node is built by the scope stage (MR6) walking the repo tree | `modernization/<repo-name>/app-digest.json` |

   Dispatch each per-repo analyzer as a fresh subagent scoped to that repo's path. After
   each repo's analyzers run, **persist** its per-role outcomes to `state.json` under a
   **`repos:` list** — one entry per repo, `{role, name, stages: {<stage-id>: {status,
   note, artifact}}}` — so the roll-up renders a per-repo section (see the roll-up step).
   The top-level `stages.scope` entry still tracks the SHARED scope synthesis. A repo
   whose role-specific analyzer legitimately has nothing to do marks that stage `skipped`
   in its `stages` map and the chain continues (same graceful-skip rule as single-repo).

   **Graceful skip (a stage that does not apply must NOT hard-fail the chain).** If an
   analyzer legitimately has nothing to do, mark it `skipped` with a `note` explaining
   why and **continue** — do not halt. Concretely:
   - **No DB → stage 2 (db-schema).** If the repo has no DDL / SQL schema, record
     `db-schema` as `skipped` (note: "no schema in repo") and continue. Do NOT write a
     `db/schema.json`.
   - **No web/API tier → stages 1 and 3.** If there is no HTTP/route surface, mark
     `functional-map` and/or `api-contract` `skipped` and continue.
   Because the artifacts are *legitimately absent* (skipped), stage 5 must treat their
   absence as "skipped, not an error" — pass the scope stage the `--functional-map`,
   `--schema`, `--api`, or `--app-digest` **override flags ONLY for stages that ran**,
   and instruct it (via `--no-<artifact>` semantics documented in its own SKILL, or by
   simply not producing the file) to skip the corresponding correlation layer. A stage
   that was *expected and ran but produced no artifact* is a `failed` stage, not a
   skipped one — the distinction is the operator's cue in the roll-up.

   **Halt-on-failure.** If a stage that was expected to run errors, record it `failed`,
   write the state, and STOP the walk (do not run downstream stages). A later re-invoke
   with the same `--run-id` resumes from that failed stage.

3. **Enforce the `data:<table>` UI→API→DB join contract at stage 4.** The system map
   stage 5 fuses only threads if the app-digest emits, for every backend-read table, an
   import whose `target == "data:<table>"` (lowercased). That coordinate joins the DB
   schema's exposed `data:<table>` interfaces (from stage 2) — it is the load-bearing
   API→DB half of the UI→API→DB thread (the SP1/SP2/SP3 contract). When dispatching
   stage 4 (`/mod-app-analysis`), ensure this convention holds; `/mod-app-analysis`'s
   `shared/app_digest_aggregator.py::_parse_consumed_tables` already emits the `data:`
   prefix, so the requirement is that the app-analysis stage actually ran and captured
   the repo's data-access section. If stage 2 was skipped (no DB), there is simply no
   `data:` counterpart to join and the thread is (correctly) absent.

4. **Run the synthesizer (stage 5) and then roll up.** Dispatch
   `/mod-modernization-scope` against the same `<context-root>`, **passing `--run-id
   <run-id>` (the same id minted in step 1)**. Passing the run-id ensures the scope stage
   writes its state files (`system-map.json`, `synthesis_prompts.json`, `synthesis.json`)
   under `<state-dir>/<run-id>/` — the same directory the orchestrator uses — rather than
   the default `<state-dir>/run/`, which would create an ambiguous parallel state dir. The
   scope stage auto-discovers the four upstream artifacts at their discovery paths, builds
   the fused system map, and writes the four deliverables under `<context-root>/modernization/`.
   Pass the per-stage override flags only for stages that ran. **If step 1 persisted a
   `target-stack.json`** (i.e. `--target` was supplied), also pass
   `--target <context-root>/modernization/.state/<run-id>/target-stack.json` so the scope
   stage synthesizes toward the target; when no target was supplied, omit the flag (the
   scope stage auto-discovers under the run-id state dir and finds none — descriptive
   output, unchanged). **If step 1 persisted an `hla-source.json`** (i.e. `.foundry/hla/`
   docs existed), also pass
   `--hla <context-root>/modernization/.state/<run-id>/hla-source.json` so the scope
   stage grounds the synthesis against the stated architecture and emits the
   **Architecture Conformance** section; when no HLA was supplied, omit the flag (the
   scope stage auto-discovers, finds none, and omits the section — descriptive output,
   unchanged).

   **Repo-set mode — thread the repo-set + `--modernized` into the scope stage (MR7,
   #1556).** When step 1 detected a non-empty repo-set, pass it through to
   `/mod-modernization-scope`:
   - **`--repo <role>=<path>`** once per repo. (Belt-and-suspenders: SP2 itself calls
     `build_repo_set(Path.cwd(), repo_overrides)`, so a present `.foundry/repos.yaml` is
     picked up even without the flags — but pass them explicitly so a `--repo` override on
     the orchestrator command line reaches SP2.) With a repo-set, SP2's multi-repo
     discovery reads each repo's `modernization/<repo-name>/` artifacts (written in step 2)
     and correlates them into ONE system map.
   - **`--modernized <token>`** once per orchestrator `--modernized` passthrough (a new
     repeatable orchestrator option). Each token forces the matched unit(s) to `modern` —
     it matches a bounded-context/domain label, else a repo/role/node-id substring (e.g.
     `--modernized api`). Use it to hand-declare an already-migrated tier so SP2's
     what's-left filter (MR5) demotes it to a **Boundary Contract** instead of re-planning it.
   - Continue to thread the existing **`--feature` / `--context`** scope and, when the user
     asked for it, **`--no-migration-synthesis`** (skips SP2's L3 migration handshake — every
     band stays as the deterministic MR3 scorer decided).

   **Repo-set mode — author the migration-state judgments (MR4, #1556).** On a multi-repo
   run, the scope stage's extract leg emits **migration-judgment prompts** for the units it
   can't confidently band (in `<state-dir>/<run-id>/migration_prompts.json`), alongside the
   synthesis prompts, and exits 7 — the same extract→judge→continue handshake the scope
   stage already drives for synthesis. Extend that handling: read
   `migration_prompts.json`, and for each uncertain unit read its cited `signals` +
   `ambiguous_files` **in-session** and decide whether it is already **modern** (migration
   complete), still **legacy** (not started), or **partial** (mid-migration / strangler-fig).
   Author one verdict object per uncertain unit into a **`migration_states` section of the
   SAME `synthesis.json`** you feed to `--continue` (one file, one continue leg) — each
   verdict `{code_root, band, confidence, rationale}` where `code_root` names a real unit
   from `migration-states.json` and the `rationale` cites `file:line`:

   ```json
   {
     "schema_version": "2.0",
     "specs": [ ... ],
     "adrs": [ ... ],
     "migration_states": [
       {"code_root": "../orders-api#app", "band": "modern", "confidence": "high",
        "rationale": "Struts config is dead; all live routes are @RestController (src/App.java:12)"}
     ]
   }
   ```

   The continue leg overrides that unit's deterministic band with your verdict
   (`source: "harness"`), and any uncertain unit you did NOT judge stays `unverified` — it
   is never dropped (SP2's honesty rule); it is scoped IN with a ⚠️ marker. On a
   **single-repo** run there is no migration scoring, no `migration_prompts.json`, and this
   whole step is absent (byte-identical, #1408).

   Record stage 5 as `complete` / `failed`. Then run the deterministic
   roll-up renderer to produce the top-level summary:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" modernization-rollup \
       --state <context-root>/modernization/.state/<run-id>/state.json \
       --output <context-root>/MODERNIZATION-SUMMARY.md
   ```

   > **Note — direct module invocation (fallback).** If `forge-validate` is not on
   > `PATH` (e.g. the consumer has not installed the CLI or is running from the synced
   > `.foundry/primitives/` tree directly), invoke the aggregator with an interpreter
   > that **has the primitives' deps** (pyyaml, pydantic, …) — use `uv tool run`, which
   > resolves the installed tool's environment:
   > ```bash
   > uv tool run --from forge-foundry-primitives \
   >     python3 -m primitives.modernization.shared.rollup_aggregator \
   >     --state <context-root>/modernization/.state/<run-id>/state.json \
   >     --output <context-root>/MODERNIZATION-SUMMARY.md
   > ```
   > **Do NOT use a bare `PYTHONPATH=.foundry python3 -m …`** — the ambient `python3`
   > (stock macOS in particular) lacks `pyyaml`/`pydantic` and the module import fails
   > with `ModuleNotFoundError: No module named 'yaml'`. If `uv` is unavailable, invoke
   > the tool venv interpreter directly (it carries the deps), e.g.
   > `~/.local/share/uv/tools/forge-foundry-primitives/bin/python3 -m …`.

   **§SP-A.2 — functional-map runs TWICE (whole-app pass, then sliced pass).** Bounded
   contexts come from the system-map, which is built *inside* the scope stage (stage 5) —
   after the analyzers. So stage 1's `/functional-map` runs **whole-app** (no
   `--system-map`): its `functional-map.json` feeds discovery and the system-map build,
   byte-identically to today. Once stage 5 has written
   `<context-root>/modernization/system-map.json`, dispatch a **second, sliced**
   `/functional-map` pass that reads it:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" functional-map \
       --code-root <repo> \
       --state-dir <context-root>/functional-map \
       --system-map <context-root>/modernization/system-map.json
   ```

   Because stage 1's whole-app pass already ran its synthesis round into the SAME
   `<context-root>/functional-map/run/` state, this bare `--system-map` re-invocation
   **auto-resumes** from the existing `run/synthesis.json` (it does NOT re-enter the
   extract leg) and emits the slices directly (exit 0). If — and only if — that
   whole-app synthesis is genuinely absent, the CLI **WARNs** that no slices were
   emitted and prints the explicit continue form to run once synthesis exists:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" functional-map --code-root <repo> --continue \
       --judgments <context-root>/functional-map/run/synthesis.json \
       --state-dir <context-root>/functional-map \
       --system-map <context-root>/modernization/system-map.json
   ```

   This additionally emits `<context-root>/functional-map/functional-map/<slice>/` per
   bounded context (the whole-app doc stays as the index). The scope stage then reads the
   slice matching each spec's context (SP-B+ consume these). **Do NOT** move slicing into
   the scope extract-leg — the design explicitly rejects that (it would hide the slice
   artifacts and duplicate functional-map's grouping logic). Record the sliced pass as
   `complete` / `failed` under `stages.functional-map-sliced`.

   > **Slicing granularity — expect path-prefix buckets on an app-mod system-map.** The
   > app-mod system-map's nodes are coarse architectural components (`.#ui` / `.#app` /
   > `.#api` / `.#db`) and carry no domain labels, so the endpoint→context resolver has
   > nothing finer to partition on: every application endpoint collapses to the single
   > `.#app` component. Slicing therefore **deliberately falls back to path-prefix
   > clustering** (`owners`, `vets`, …), and the CLI surfaces this — a `NOTE` on stderr
   > plus a `functional-map/SLICING-NOTE.md` beside the slices. Do NOT expect
   > domain-driven bounded-context slices from a coarse app-mod system-map; the buckets
   > are resource-prefix groupings. (Supply an explicit `--contexts {domain: [ep-key]}`
   > map if you need a different partition — that overrides the fallback.)

5. **Surface the outcome.** Report the top-level `MODERNIZATION-SUMMARY.md`, the four
   deliverables (or which were skipped), the resumable-state location, and — if the
   chain is `BLOCKED` — which stage failed and how to resume.

## How to invoke

Invoke as `/mod-modernization <repo>` where `<repo>` is the source repository
(default: current working directory). To resume, pass `--run-id ID` (and optionally
`--start-from STAGE` to override the resume cursor). Pass `--context-root PATH` to
override the default `.foundry/context/`. Pass repeatable `--target KEY=VALUE[@version]`
(e.g. `--target frontend=angular --target backend=spring-boot@3.3`) to declare the
modernization target stack — the chain persists it as `target-stack.json` and threads it
into the scope stage; omit it entirely for byte-identical descriptive output. Drop stated
High-Level Architecture docs in `.foundry/hla/` (`*.md` / `*.docx`; override the folder
with `--hla-dir PATH`) to ground the modernization against them — the chain ingests them
into `hla-source.json` and the scope stage emits an Architecture Conformance section;
omit the folder entirely for byte-identical descriptive output.

**Multi-repo (MR7).** When the app is split across repos, name them in
`.foundry/repos.yaml` (roles `ui`/`api`/`db`/`iac`) or pass repeatable
`--repo <role>=<path>` overrides; the orchestrator runs each repo's role-appropriate
analyzers into `modernization/<repo-name>/` and the scope stage correlates all repos into
ONE system map. Pass repeatable `--modernized <role|context>` to hand-declare an
already-migrated tier (SP2 demotes it to a Boundary Contract). Absent both a `repos.yaml`
and any `--repo`, the run is single-repo and byte-identical to before. A generic example:

```text
# .foundry/repos.yaml
repos:
  ui:  { path: ../web-ui,     role: ui }
  api: { path: ../orders-api, role: api }
  db:  { path: ../db-schema,  role: db }
  iac: { path: ../terraform,  role: iac }

/mod-modernization --run-id run-1 --modernized api
```

YOU (the harness LLM) drive the chain
directly via dispatched subagents that invoke each stage's slash command — there is no
Python control-plane to shell out to on the interactive path; the only Python process
is the deterministic roll-up renderer (`shared/rollup_aggregator.py`), invoked once at
the end.

## Output

All durable output lands under `<context-root>` (default `.foundry/context/`):

- **The four SP2 deliverables** under `<context-root>/modernization/`:
  `MODERNIZATION.md` + `system-map.json` (deconstruction / fused system map),
  `scope/<domain>.spec.md` (role-complete specs), `adr/ADR-NNNN-*.md` (ADRs), and
  `ROADMAP.md` (wave-by-wave strangler-fig extraction order).
- **The upstream analyzer artifacts**, each at its discovery path:
  `functional-map/functional-map.json` (+ `FUNCTIONAL-MAP.md`), `db/schema.json`,
  `api/api-contract.json`, `modernization/<repo-name>/app-digest.json` (+ `ANALYSIS.md`).
- **`MODERNIZATION-SUMMARY.md`** at the context root — the deterministic top-level
  roll-up: overall status (`COMPLETE` / `IN PROGRESS` / `BLOCKED`), a per-stage table
  (status + note + artifact path), a resume hint when blocked, and the skipped-stage
  callout. Rendered by `shared/rollup_aggregator.py` from the on-disk state file.
- **The resumable state** at `<context-root>/modernization/.state/<run-id>/state.json`
  (per-stage `status`/`note`/`artifact`), so a partially-completed run can be resumed.
- **`target-stack.json`** at `<context-root>/modernization/.state/<run-id>/target-stack.json`
  — the serialized `TargetStack`, written ONLY when `--target` was supplied (SP2, #1410).
  Absent it, the chain is fully backward-compatible.
- **`hla-source.json`** at `<context-root>/modernization/.state/<run-id>/hla-source.json`
  — the serialized `HlaSource`, written ONLY when `.foundry/hla/` docs existed (Track A,
  #1428). Absent it, the chain is fully backward-compatible (no Architecture Conformance
  section).

## Invariants

- **Skill-first orchestrator — §4.1 orchestrator-shape carve-out applies.** This
  primitive carries a minimal `shared/` (the deterministic roll-up renderer
  `rollup_aggregator.py` only) and no `ci.yaml` and no registered CLI command. The five
  analyzer stages run in-session via dispatched subagents; the roll-up renderer is the
  only subprocess the body invokes.
- **Discovery-path contract (guards the #1291-class silent gap).** Each analyzer's
  durable artifact MUST be persisted at the exact path the SP2
  `/mod-modernization-scope` synthesizer auto-discovers under its `--context-root`:
  `functional-map/functional-map.json`, `db/schema.json`, `api/api-contract.json`, and
  `modernization/<repo>/app-digest.json`. These paths are recorded in
  `shared/rollup_aggregator.py::ARTIFACT_DISCOVERY_PATHS`, which
  `tests/test_modernization_rollup.py` asserts stays byte-identical to the scope
  synthesizer's `_DISCOVERY` table. A drift silently produces an empty system map.
- **`data:<table>` UI→API→DB join contract.** Stage 4's `app-digest.json` must emit
  `imports[].target == "data:<table>"` for every backend-read table so the fused system
  map threads API→DB (joining stage 2's exposed `data:<table>` interfaces). This is the
  load-bearing SP1/SP2/SP3 contract.
- **Resumable.** Per-stage `status`/`note`/`artifact` is persisted to
  `<context-root>/modernization/.state/<run-id>/state.json` after every stage. A run is
  resumable by re-invoking with the same `--run-id`: stages already `complete` or
  `skipped` are not re-run; the walk restarts at the first `pending`/`failed` stage.
- **Graceful skip vs. failure.** A stage that legitimately does not apply (e.g. no DB
  for stage 2) is marked `skipped` and the chain CONTINUES — it is NOT a hard failure.
  Stage 5's missing-artifact handling distinguishes "legitimately absent (skipped)"
  (correlation layer omitted) from "expected but missing (failed)" (chain halts). Only
  a `failed` stage halts the walk.
- **§3.4 orchestrator carve-out applies.** `/mod-modernization` declares no sub-call
  rules of its own — it is a composition primitive. It inherits whatever §3.4 sub-calls
  the delegated analyzer/synthesizer stages run in-session.
- **`mod-` prefix is mandatory.** The slash command is `/mod-modernization` (charter
  §3.2.M) so the modernization namespace stays distinct.
- **Target stack is opt-in and backward-compatible (SP2, #1410).** `--target KEY=VALUE`
  is parsed + registry-resolved once and persisted as `target-stack.json` under the
  run-id state dir, then passed to the scope stage via `--target`. With NO `--target`,
  no `target-stack.json` is written and every stage renders its byte-identical descriptive
  output — the target only shapes interpretation + synthesis, never what the extractors
  report the app currently IS.
- **HLA grounding is opt-in and backward-compatible (Track A, #1428).** `.foundry/hla/`
  docs (`*.md` read directly; `*.docx` text extracted **in-session by the SKILL body —
  never a Python dependency**, honoring the offline invariant) are summarized into an
  `HlaSource`, persisted as `hla-source.json` under the run-id state dir ONLY when docs
  exist, then passed to the scope stage via `--hla`. With NO HLA docs, no `hla-source.json`
  is written and the scope stage emits no Architecture Conformance section — byte-identical
  descriptive output. The HLA only grounds interpretation + synthesis (specs align to it;
  ADRs justify deviations); it never fabricates a conformance verdict.
- **Multi-repo is opt-in and backward-compatible (MR7, #1556).** A `.foundry/repos.yaml`
  or any `--repo <role>=<path>` override switches the orchestrator into repo-set mode: each
  repo's role-appropriate analyzers run into `modernization/<repo-name>/` (the exact paths
  SP2's MR2 discovery reads), the scope stage correlates all repos into ONE system map
  (threaded via `--repo`), `--modernized <role|context>` demotes already-migrated tiers to
  Boundary Contracts, and the multi-repo migration handshake authors the `migration_states`
  section of the `--continue` judgments. The roll-up records a `repos:` list and renders a
  per-repo section. With NEITHER a `repos.yaml` NOR any `--repo`, the run is single-repo and
  byte-identical to before — `build_repo_set` returns an empty set and no `repos:` key is
  written.
- **No thin-air synthesis.** The roll-up summary is rendered from the on-disk state
  file; every stage name, status, and artifact path traces back to a recorded stage
  outcome. No Bedrock backend and no Python control-plane on the interactive path.
