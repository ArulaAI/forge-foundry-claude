---
name: functional-map
description: >
  Reverse-engineer a functional-requirements map from a web-application source tree using the
  PLB extract→synthesize→continue protocol: Phase A detects the stack flavor and walks source
  files; Phase B extracts a FunctionalDigest (endpoints, guards, roles, capabilities, nav edges,
  form fields, validation rules, business contexts, i18n entries, and Web Flow definitions);
  exit 7 with synthesis_prompts.json; the body authors synthesis.json (user flows, journeys,
  role reconciliation, FR statements, acceptance criteria); Phase C renders the 13-section
  Markdown map + JSON FR-inventory sidecar; Phase D audits citation accuracy. Use when the user
  asks to "map the functional requirements", "generate a functional map", or "reverse-engineer
  the feature set" of a web app.
allowed-tools: Read, Grep, Glob, Bash
---

# /functional-map

Reverse-engineer a **functional-requirements map** from a web-application source tree. This is a
**Python-load-bearing** primitive: `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" functional-map` runs the deterministic
pipeline (flavor detection, extraction, rendering, audit) and this body authors the synthesis
JSON (user flows, journeys, role reconciliation, FR statements, acceptance criteria). The body
never writes source files directly.

## How to invoke

```
/functional-map
```

## When to invoke

- When the user asks to "map the functional requirements", "generate a functional map", "reverse-
  engineer the feature set", or "document the routes and access control" of a web app.
- After `/repo-digest` or `/data-flow-map` when a functional view is needed for design review,
  compliance mapping, or onboarding.

## When invoked

1. **Extract — detect flavor, walk sources, emit synthesis prompts (exit 7).** Run:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" functional-map --code-root <ROOT> --extract-only --state-dir <state-dir>
   ```
   The CLI runs Phase A (flavor detection via web_* flavor packs or generic fallback) and Phase B
   (24 sub-extractors accumulating endpoints, guards, roles, capabilities, nav edges, form fields,
   validation rules, business contexts, i18n entries, and Web Flow definitions into a
   `FunctionalDigest`). Writes `analysis.json` and `synthesis_prompts.json` (schema_version 2.0,
   SP1–SP13) under `<state-dir>/<run-id>/`. Exits 7 ("awaiting judgments"). Hard stops: code
   root not found (exit 2).

2. **Synthesize — author `synthesis.json`.** Read `synthesis_prompts.json` from the state dir.
   It is a **tiered context**: the `context.*_index` keys give you the COMPLETE inventory
   compactly (every endpoint, form field, business context, i18n key — nothing omitted), while the
   rich detail excerpts carry the top-N entities in full — `context.endpoints` at the context level,
   and the `form_fields` / `business_contexts` / `i18n_entries` / `i18n_keys` / `webflows` arrays
   on the individual SP items (`i18n_keys` on the SP5 validation_requirements item; `webflows` on
   the SP10 webflow_requirements item).
   - **The index is your complete map.** `context.endpoints_index` is a list of dense strings,
     one per endpoint, `"{id}|{method}|{path}|{handler}|{file}:{line}"` (e.g.
     `"EP-001|POST|/transfer/submit|TransferController.submit|Transfer.java:42"`). Every
     endpoint's `file:line` is present here, so you can **cite ANY endpoint from the index alone**
     — you never need `analysis.json` just to satisfy a citation. `context.form_fields_index`
     (`"{endpoint_id}|{name}|{field_type}|{required}"`), `context.business_contexts_index`
     (one line per endpoint with its `sec=/cond=/async=/pag=/upload=/notif=/sess=/err=` flags),
     and `context.i18n_index` (`"{key}|{locale}"`) are likewise complete.
   - **Rich detail is top-N.** `context.truncated` marks ONLY the detail tier as partial
     (e.g. `context.truncated.endpoints_detail = {shown: 200, total: 500}`); the indexes are
     complete and are never listed under `truncated`. `context.index_semantics` states this
     explicitly.
   - **On-demand analysis.json.** When you need field-level detail for an endpoint that is NOT in
     the detail tier (i.e. beyond the top-N, flagged by `context.truncated.endpoints_detail`),
     Read `analysis.json` from the state dir — it is a compact single-object digest written
     minified (not line-oriented), so parse it and select the `EP-NNN` records you need. This
     fallback is rarely needed and never required for citations, which the endpoints_index already
     satisfies.
   **Shared excerpts (`endpoints`, `nav_edges`, `roles`, `validations`) live under the top-level
   `context` key; each item's `uses` list names which context slices apply to it.**
   **Match each prompt item by its `kind` field, NOT its `id` (SPn).** The
   prompts file contains only a SUBSET of these items — items with no data are omitted, so the
   `SPn` numbers are NOT contiguous at runtime and position is unreliable. Each item carries a
   `kind` and an `instruction`; produce the corresponding `synthesis.json` fields below. Author a
   `synthesis.json` conforming to the synthesis.json v2.0 schema:

   - `schema_version`: `"2.0"`

   **Item `kind` → output field mapping** (an item is present only if it has data):

   - **`kind: flows`** → author `flows[]` (UserFlow objects): `id` (`FLOW-NNN`), `name`, `steps`,
     `variant` (`happy`|`error`|`role_scoped`), `variant_label`. Each step: `order`, `endpoint_id`,
     `description`, `file`, `line`, `data_flow`. Assemble named flows from the endpoints + nav
     edges in the item; include happy-path flows, error/exception-path flows (variant `error`),
     and role-scoped variants (variant `role_scoped`). §5 of the map groups flows by variant.
   - **`kind: journeys`** → author `journeys[]` (UserJourney objects): `id` (`JRN-NNN`), `persona`,
     `goal`, `flow_ids`, `narrative`. Return 2–6 goal-oriented journeys per persona.
   - **`kind: roles`** → author `reconciliation`:
     `{ reconciled: [{name, source}], code_only: [], brd_only: [] }`, where each `source ∈
     {code, brd, both}`. Flag `code_only`/`brd_only` mismatches when a BRD role list is present.
   - **FR-statement kinds** → append to `fr_statements[]` (FRStatement objects): `id` (`FR-NNN`),
     `statement`, `priority` (`must`|`should`|`may`), `fr_type` (from the §4.1 vocabulary),
     `source_evidence` (array of `EP-NNN` endpoint ids — **NOT** file:line pairs). Use the
     `fr_type` that matches each `kind`:
     | item `kind` | `fr_type`(s) to emit |
     |-------------|----------------------|
     | `form_field_requirements` | `validation`, `display_rule` |
     | `validation_requirements` | `validation` |
     | `access_control_requirements` | `access_control` |
     | `session_requirements` | `session` |
     | `navigation_requirements` | `functional`, `display_rule` |
     | `notification_requirements` | `notification`, `async` (for async / background processing) |
     | `webflow_requirements` | `session`, `functional` (wizard / multi-step navigation) |
     | `data_display_requirements` | `pagination`, `file_upload` |
     | `i18n_requirements` | `functional` (locale-specific display/validation behavior) |
   - **`kind: business_rules`** → author `business_rules[]` (BusinessRule objects): `step_ref`
     (an `endpoint_id` or field name — **NOT** `FLOW-NNN.N`), `rule`, `source` (from
     `{validation, guard, jpa, session, conditional, error_flow, inferred}`).
   - **`acceptance_criteria` (no prompt item — DERIVE these).** There is NO `kind` for acceptance
     criteria. After authoring the `fr_statements` above, DERIVE `acceptance_criteria[]` from the
     FRs you wrote. Each AcceptanceCriterion is `fr_id` (references an FR `id` you authored),
     `id` = `{fr_id}-AC{N}` (e.g. `FR-001-AC1`), and `given`/`when`/`then` Gherkin clauses. Put
     the clause text only in each value — do NOT prefix it with the `Given`/`When`/`Then` keyword.

     **Coverage is mandatory — every FR MUST get at least one AC.** Emit ≥1 AcceptanceCriterion
     for EVERY `fr_statement` you authored, regardless of priority (`must`, `should`, AND `may`).
     Do not self-select a subset; a `may`-priority display or i18n FR still gets its one
     positive AC. Phase D reports any FR left with no AC as a coverage gap (§10 of the map).

     **Cover negative / error paths where the evidence supports them.** A single happy-path AC is
     not enough for a requirement whose whole point is rejecting bad input or handling failure.
     For any FR **backed by evidence of a failure mode** — i.e. its `source_evidence` endpoints
     appear in a `validation` rule (`context["validations"]`), a guard / access-control constraint
     (`business_contexts[].security_roles` or `.conditional`), or an `error_flow`
     (`business_contexts[].error_flows`) — ALSO emit a **negative/error** AcceptanceCriterion
     whose `when` supplies the invalid/failing input and whose `then` asserts the rejection,
     validation message (use the matching i18n error key when one exists, e.g. `nonNumeric`,
     `duplicate`), or error page. Examples: a numeric-telephone validation FR → a positive AC
     (valid phone saved) AND a negative AC (non-numeric phone → `nonNumeric` error redisplayed);
     a duplicate-pet-name FR → the duplicate is rejected with the `duplicate` message; an
     unhandled-exception FR → the error page is rendered. **Do NOT invent error paths with no
     evidence** — if the digest shows no validation/guard/error_flow for an FR (e.g. a pure
     read/display FR), one positive AC is sufficient.

   **Lean output:** Produce the minimum valid `synthesis.json`. Omit optional array fields when
   empty (do not emit `"business_rules": []`). No prose commentary outside string-typed fields.
   Keep `statement` values concise (one sentence). The `source_evidence` array contains `EP-NNN`
   ids only — not file:line pairs.

   **Key invariants:**
   - **Copy step `file`/`line` VERBATIM** from the matching endpoint record — take it from
     `context["endpoints"]` when the endpoint is in the detail tier, otherwise from its
     `context["endpoints_index"]` line (the `{file}:{line}` tail, present for ALL endpoints).
     Do NOT infer, compute, or guess a code location. Phase D matches each step's `(file, line)`
     against the endpoint's declaration by `endpoint_id`; citation accuracy < 0.60 → exit 6.
   - `source_evidence` in FRStatements uses `EP-NNN` endpoint ids — NOT file:line pairs. Phase D
     validates these with warning-only (no exit-6 for FR evidence).
   - `fr_type` MUST be from the controlled vocabulary
     (validation|display_rule|access_control|audit|notification|pagination|file_upload|concurrency|session|async|functional).
     Invalid value → exit 3.
   - `id` format for AC: `{fr_id}-AC{N}` (e.g. `FR-001-AC1`).
   - **The synthesis schema forbids unknown keys (`extra="forbid"` / `additionalProperties: false`).**
     Do not add explanatory or extra fields to any object — any stray key → exit 3.

   Write `synthesis.json` to `<state-dir>/<run-id>/synthesis.json`.

3. **Continue — render and audit.** Re-invoke:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" functional-map --code-root <ROOT> --continue --judgments <state-dir>/<run-id>/synthesis.json --state-dir <state-dir>
   ```
   The CLI runs Phase C (renders the 13-section Markdown map + JSON FR-inventory sidecar) and
   Phase D (audits citation accuracy against the digest). Exits 0 on success, 6 if the citation
   accuracy floor (0.60) is breached.

## Deterministic-only path (--no-synthesis)

Pass `--no-synthesis` to skip the synthesis round entirely:
```bash
"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" functional-map --code-root <ROOT> --no-synthesis
```
Phases A + B run; the map renders with `⚠️ UNVERIFIED` stubs for flows, journeys, FR statements,
and AC sections. Exits 0. Use for quick endpoint inventories or when synthesis is deferred.

## Per-bounded-context slicing (--system-map / --contexts)

A **two-mode contract** (SP-A, #1381). *Standalone* — no context input — is the default and
emits exactly one whole-app `FUNCTIONAL-MAP.md` + `functional-map.json` (byte-identical to the
pre-slicing behavior; slicing is dormant). *Sliced* mode activates only when a context input is
supplied, and **additionally** emits one full functional-map per bounded context:

```bash
# From an app-mod system-map (bounded contexts = node domains):
"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" functional-map --code-root <ROOT> --system-map .foundry/context/modernization/system-map.json

# Or an explicit {context: [endpoint-key]} map (endpoint id, http_iface, or path):
"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" functional-map --code-root <ROOT> --contexts contexts.json
```

The whole-app doc is still written as the index/overview; each context also lands under
`functional-map/<slice>/FUNCTIONAL-MAP.md` + `functional-map/<slice>/functional-map.json`. Every
functional-map fact tied to an endpoint (business context, validation rules, form fields, guards,
nav edges, FR statements, acceptance criteria, business rules, flows, journeys) is partitioned into
that endpoint's context slice — each per-slice JSON carries the **full** functional-map for its
endpoints, not the thin FR-inventory. The endpoint→context join is the same deterministic resolver
`distribute_frs` uses (`primitives/_shared/modernization/context_resolver.py`). With no external
contexts a standalone-but-sliced request falls back to clustering endpoints by their own path-prefix
signals. Slicing is deterministic — no extra LLM round-trips.

## Output

A Markdown document (`FUNCTIONAL-MAP.md`) with thirteen sections:

1. **Executive Summary** — synthesized narrative + primitive count, role count, capability groups.
2. **Endpoint Inventory** — table of all discovered endpoints (ID, path, method, handler, file:line).
3. **Guard & Access-Control Map** — per-endpoint guard kinds and roles.
4. **Role × Capability Matrix** — cell values: `allow` / `deny` / `public`; gaps flagged.
5. **User Flows** — step-by-step sequences grouped by variant (happy / error / role_scoped).
6. **User Journeys** — persona-goal-narrative arcs referencing flows.
7. **Role Reconciliation** — code-only vs BRD-only vs reconciled roles (if `--spec` provided).
8. **Navigation Graph** — Mermaid `flowchart LR` of nav edges (ajax edges as dashed `-.->` links).
9. **FR Statements** — functional requirements table grouped by fr_type; priority column.
10. **Acceptance Criteria** — Gherkin Given/When/Then table per FR.
11. **Business Rules** — per-step rule/source table.
12. **i18n Coverage** — key inventory: locale, value, linked endpoint.
13. **Form Field Inventory** — per-endpoint fields: name, type, required, constraints, label.

Plus a JSON FR-inventory sidecar (`functional-map.json`) with the structured endpoint + FR list.

**Durable output.** Unless `--output <path>` is given, both artifacts are written to durable
named files under `--state-dir` (default `.foundry/context/functional-map/`) — `FUNCTIONAL-MAP.md`
and `functional-map.json` — and the written paths are printed, matching the M-tier
`.foundry/context/` convention. Pass `--output <path>` to write the `--format`-selected payload to
a specific file instead.

## Invariants

- The body's ONLY filesystem effect is authoring `synthesis.json` to the state dir — Python does
  all extraction, rendering, and audit operations.
- Every `file`/`line` in a flow step MUST reference the exact values from the digest's endpoint
  record. The CLI rejects step citations that don't match (accuracy < floor → exit 6).
- The Python CLI never calls an LLM. Flavor detection and extraction are deterministic; only the
  synthesis round uses the in-session body.
- `--no-synthesis` is a supported deterministic-only path; stubs are clearly marked `⚠️ UNVERIFIED`.

## Target-stack judgment (SP6, additive — inert without a target)

**This block is inert when no `target-stack.json` is present.** When `/mod-modernization` is
running the full app-mod chain and a target stack has been declared (via `--target KEY=VALUE`),
it writes `target-stack.json` under `.foundry/context/modernization/.state/<run-id>/`. When
`/functional-map` is invoked as part of that chain, check for that artifact before the synthesis
step. If the file is absent, skip this block entirely — output is byte-identical to the no-target
path.

**If a target stack is provided** (i.e. `target-stack.json` exists under the run-id state dir):

1. **Flag UI-framework-bound FRs.** During synthesis, for each FR statement you author, note
   whether it is bound to the *source* UI framework (e.g. a JSP form-rendering FR, a Thymeleaf
   template constraint, a Spring MVC view-resolver dependency). Mark these FRs as
   `"target_relevant": true` in your internal reasoning — they are *frontend-target relevant*
   and the downstream scope synthesizer (SP3 §8b + SP5) will prioritize them when designing the
   target UI. Do NOT add any extra keys to the emitted `synthesis.json` (its schema is
   `extra="forbid"`); instead, ensure these FRs appear with `fr_type: "display_rule"` or
   `"functional"` and that their `statement` language names the framework coupling explicitly
   (e.g. "The owner-creation form is rendered as a JSP view via Spring MVC's `ViewResolver`
   — the Angular SPA migration must re-implement this as a reactive form component.").

2. **Expose the controller endpoint inventory for scope §8.** The deterministic Phase B
   extraction already writes the full endpoint inventory to `analysis.json` and to the
   `endpoints_index` in `synthesis_prompts.json`. **No extra work is needed here** — the
   downstream `/mod-modernization-scope` adapter (`primitives/_shared/modernization/adapters/`)
   reads `functional-map.json` and maps its endpoint records into the §8 Current API Surface.
   Ensure the `synthesis.json` flow steps contain accurate `file`/`line` citations for every
   controller endpoint — these are the source of the controller endpoint inventory that §8
   renders as the implicit current API contract (SP3 #1404 relief).

3. **Note the target frontend dimension.** Read the `frontend` dimension from `target-stack.json`
   (e.g. `"angular"`, `"react"`). In the §1 Executive Summary and in relevant FR statements,
   acknowledge the target UI framework where it shapes the functional requirements — for example,
   "the login-page navigation flow will be re-implemented as an Angular route guard" or "the
   form-validation feedback (currently Spring MVC `BindingResult`) is a candidate for Angular
   reactive-form validators." This annotation is advisory; it informs the scope synthesizer but
   does not alter the extracted facts.

**Backward-compatible guarantee:** when `target-stack.json` is absent (the default standalone
invocation), none of the above applies. The `synthesis.json` schema is unchanged, no extra fields
are emitted, and the functional map output is byte-identical to the pre-SP6 behavior.
