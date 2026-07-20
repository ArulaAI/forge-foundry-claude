---
name: mod-runtime-explore
description: >
  Drive a RUNNING web application with local Playwright to discover reachable user
  journeys, screens, and features — the runtime complement to /functional-map. A
  bundled crawler does deterministic breadth; the body drives auth + deep paths via a
  step-CLI; the aggregator emits runtime-journeys.json (+ reconciliation vs the static
  functional-map). Fully local, no MCP. Playwright is an optional `[browser]` extra.
allowed-tools: Read, Grep, Glob, Bash
---

# mod-runtime-explore — runtime feature discovery

The runtime complement to the static `/functional-map`: drive a live instance of the
app and map the journeys, screens, and features that are actually reachable at runtime
(auth-gated flows, SPA client-routes, multi-step wizards). It runs in three phases —
a deterministic breadth **crawl** (Phase A, Python), an agent-driven **deep-drive** of
the blocked frontiers (Phase B, this body), and a deterministic **synthesis +
reconciliation** (Phase C, Python). Fully local; no MCP.

## When to invoke

Invoke when the operator wants the app's real user journeys discovered by exploring a
**running** instance rather than from source code — e.g. to cross-validate a static
`/functional-map`, surface auth-only or SPA-only flows, or inventory the reachable
screens. Requires:

- a running target and an `.foundry/explore-plan.yaml` (base URL, url_scope allow/deny,
  budget, auth reference — secrets by reference, never inline);
- a local Playwright install (the optional `[browser]` extra) **and**
  `playwright install chromium`, using the form that matches how the CLI was installed.
  For a git-based `uv tool` install (the pinned-tag default):
  `uv tool install --force 'forge-foundry-primitives[browser] @ git+https://github.com/arula-ai/forge-foundry-primitives@<tag>'`
  then `uv tool run --from 'forge-foundry-primitives[browser]' playwright install chromium`.
  For a project checkout: `uv sync --extra browser && uv run playwright install chromium`.
  A plain `pip install ...[browser]` targets the wrong interpreter for a `uv tool` install.
  If it is missing, every phase exits **6** with the exact install commands — install then re-run.

## When invoked

1. **Read + validate the plan.** Read `.foundry/explore-plan.yaml`. Confirm the
   `url_scope` allowlist bounds the target domain and the `deny` list covers
   destructive/irreversible paths (logout, delete, purchase-confirm). The credentials
   come from the referenced env var / gitignored file — never inline.

2. **Phase A — breadth crawl.** Run the deterministic crawler; it walks the reachable
   in-scope screens and stops at blocked frontiers (login, wizards, forms needing
   input), writing the graph + the frontiers and exiting 7:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.crawler --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state --out .foundry/context/runtime
   ```

3. **Phase B — deep-drive the frontiers.** Read `.state/crawl-graph.json` +
   `.state/explore_prompts.json`. Start a session, log in using the plan's credentials
   reference, and drive each blocked frontier (wizards, realistic form fills using the
   seed data) one action at a time, observing each result and `snapshot`-ing the screens
   you reach. Each `snapshot` also captures a **route-named screenshot** into
   `.foundry/context/runtime/screenshots/` (parity with Phase A) — take one on every
   distinct screen you reach so the visual inventory is complete.

   **Read `references/legacy-web-playbook.md` first** — it is the field guide for driving
   dirty legacy / SPA apps (Angular hash-routing, autocomplete, NgRx, lazy routes). Three
   golden rules govern every action: **(1) never hard-sleep** — wait for a specific
   UI/network condition with `wait-for` (a selector, a `--url` glob, or `--networkidle`);
   **(2) prefer stable selectors** in this order — `data-testid` > ARIA role+name > label >
   text > placeholder > CSS; **(3) on SPAs, wait for the client route + network to settle**
   (set `budget.wait_until: networkidle`, and `wait-for --url`/`--networkidle` after actions).

   ### Per-screen control sweep (#1600)

   On **every screen you reach** (every URL where you call `snapshot`), immediately after
   snapshotting, perform a full control sweep using the following sequence. This produces
   the `controls[]` evidence that populates `Screen.controls` in `runtime-journeys.json`
   and feeds the Functional Controls section of `RUNTIME-DISCOVERY.md`.

   **Invariant: every control detected on a reached screen must end up as a `Control`
   record — exercised (with a screenshot + `data_state_change`) or `detected_only` (if
   it could not be safely exercised). No control is silently omitted.**

   ```mermaid
   flowchart TD
       A["snapshot the screen"] --> B["controls detect"]
       B --> C{kind?}
       C -->|sort / dropdown| D["controls auto-exercise"]
       D --> E["Control record: exercised_by=auto\ndata_state_change captured"]
       C -->|filter / checkbox / radio /\nedit / modal / paginate| F["Exercise manually:\nread region before\nfill / select / click\nread region after\nsnapshot --label"]
       F --> G["Control record: exercised_by=agent\ndata_state_change = before→after delta"]
       C -->|button / other / not safe to exercise| H["Control record: exercised_by=detected_only"]
       E --> I["Merge all Control records onto the screen"]
       G --> I
       H --> I
   ```

   **Step 1 — inventory the controls.** Run `controls detect` immediately after each
   `snapshot`. It returns a JSON array of `Control` records (id, kind, selector, label)
   with `exercised_by="detected_only"`, **and persists them onto the screen's
   `controls[]` in `crawl-graph.json`** (keyed by the current URL, #1623) — so the Phase-C
   aggregator reads them without a manual merge. Capture the JSON too — it is your
   authoritative inventory for authoring the JUDGMENT records in step 3.

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli controls detect --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   # → JSON array: [{"id": "CTL-001", "kind": "sort", "selector": "th.sortable", "label": "Last Name", ...}, ...]
   ```

   **Step 2 — auto-exercise trivial controls.** Run `controls auto-exercise` once per
   screen. It mechanically exercises every `sort` and `dropdown` control: for each it
   reads the affected region text before, clicks/selects, reads after, captures a labeled
   screenshot (`<route>__<label-slug>__<id>.png`), and emits a JSON array of `Control`
   records with `exercised_by="auto"` and `data_state_change` set to the before→after
   delta. Any navigation that would leave `url_scope` is refused (`data_state_change =
   "SCOPE_REFUSED"`) and the page is navigated back automatically. Like `detect`, it
   **persists the full detected set onto the screen's `controls[]`** (#1623) — exercised
   records supersede their `detected_only` counterparts by selector.

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli controls auto-exercise --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   # → JSON array of auto-exercised Control records (exercised_by="auto", data_state_change filled)
   ```

   **Step 3 — exercise JUDGMENT controls manually.** For every control that `controls
   detect` returned with kind `filter`, `checkbox`, `radio`, `edit`, `modal`, or
   `paginate`, exercise it yourself using `fill`/`select`/`click` with a realistic value
   (from the plan's `seed_data_ref` — **never inline secrets or synthetic values not in
   the seed**). Capture a labeled screenshot and read the affected region before and after
   to author the `data_state_change`. Use this pattern for each JUDGMENT control:

   ```bash
   # --- Before reading (capture the "before" state of the affected region) ---
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli read "#results-table" --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   # → prints the region's visible text (the "before" snapshot)

   # --- Exercise the control (realistic value from seed_data_ref): ---
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli fill "#search-input" --from-seed last_name --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli wait-for "#results-table" --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state

   # --- After reading (capture the "after" state) ---
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli read "#results-table" --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   # → diff against the "before" text to populate data_state_change

   # --- Label the screenshot with what changed ---
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli snapshot --label "filter-by-last-name" --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   # → prints the labeled screenshot ref: "screenshots/<route>__filter-by-last-name__<id>.png"
   ```

   **Step 4 — record the JUDGMENT controls.** `controls detect` and `controls
   auto-exercise` already persist their records onto the screen's `controls[]` in
   `crawl-graph.json` (#1623), so you only need to write back the controls YOU exercised
   manually in step 3:
   - Auto-exercised (`sort`/`dropdown`) and detected controls are already on the graph — no
     manual step.
   - Manually exercised controls: update the corresponding records on the screen with
     `exercised_by="agent"`, the labeled screenshot ref, and the before→after
     `data_state_change` you observed.
   - Controls not exercised (button / other / any that could not be safely driven):
     stay `exercised_by="detected_only"` with `data_state_change=""` (already recorded).

   Every control from `controls detect` must appear on the screen — no control may be
   silently dropped.

   **Safe-exercise judgment.** Do not exercise a control if doing so would:
   - Submit a destructive or irreversible action (anything on the plan's `deny` list —
     logout, delete, purchase-confirm).
   - Require inline secrets (use `seed_data_ref`; if the field needs a credential that
     is not in seed data, record it as `detected_only`).
   - Navigate outside `url_scope` (the step-CLI will refuse it at exit 3 anyway, but
     do not attempt it).
   In these cases record the control as `exercised_by="detected_only"` with a note in
   `data_state_change` such as `"not exercised: destructive action"` or
   `"not exercised: off-scope navigation"`.

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli session start --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   # LOG IN with the single `login` verb — it navigates, fills BOTH fields, and submits
   # within ONE browser/page lifetime, so a CSRF-protected multi-field form (Spring
   # Security / Rails / Django / most server-rendered stacks) completes: the hidden CSRF
   # token and both field values survive to submission (#1622). Separate fill→fill→submit
   # invocations each re-`goto` the page, wiping filled fields + rotating the token, so they
   # CANNOT complete such a form — prefer `login`. Credentials resolve from the plan's
   # auth.creds_ref (env var / gitignored file) — never inline. The submit is form-scoped
   # (routes to the form holding the password field, not a header search form — #1624a).
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli login "$("${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli creds user --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state)" "$("${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli creds pass --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state)" --username-selector "#username" --password-selector "#password" --submit-selector "#login-form button" --login-url https://staging.example.com/login --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   # MULTI-REALM login: an app may declare several `auth.profiles`, each a named realm on
   # the same login URL but with its OWN selectors. Pass --profile to resolve that realm's
   # selectors + login_url automatically (and --role to resolve a role's own creds):
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli login "$("${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli creds user --profile realm_a --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state)" "$("${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli creds pass --profile realm_a --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state)" --profile realm_a --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   # Realistic wizard/form fills pull from the plan's seed_data_ref by field name:
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli fill "#first_name" --from-seed first_name --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   # Prefer STABLE selectors via --by (data-testid > role > label > text > placeholder > css):
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli fill "Full name" --from-seed first_name --by label --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli click "save-btn" --by testid --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   # Pick a dropdown option (by value/label/index) and wait for a condition — NO hard sleeps:
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli select "#country" --label "United States" --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli wait-for "#results" --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli wait-for --networkidle --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli snapshot --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli session end --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state
   ```

   **No hard sleeps** — instead of guessing a delay, use `wait-for <selector>` (waits until
   it is visible; `--state hidden` for a spinner to vanish), `wait-for --url <glob>` (client
   route/hash change, scope-checked), or `wait-for --networkidle` (SPA data loaded). Pick
   dropdown options with `select … --value|--label|--index`. **Target with `--by`** on
   `fill`/`click`/`wait-for` — `testid|role|label|text|placeholder|css` (default `css`),
   most-stable first — and `--nth <n>` for the nth match. See
   `references/legacy-web-playbook.md` for the full field guide.

   Credentials come from the plan's `auth.creds_ref` (an env var when `creds_from: env`,
   a gitignored file when `creds_from: file`) via the `creds <field>` subcommand — the
   env-var name lives in the plan, not hardcoded here. **Multi-realm apps** declare
   several named `auth.profiles` (each a realm with its own selectors + credential
   reference, sharing one login URL); pick one with `--profile <name>`, read its selectors
   with `login-selectors --profile <name>`, and resolve its creds with `creds <field>
   --profile <name>`. (A real-world case: a portal whose LAN / partner (e.g. "FBSI") /
   internal ("XTRAC") realms all live on one login page but fill different fields — the
   profile names + selectors are yours to declare, nothing is assumed.) Realistic form
   values come from
   `seed_data_ref` via `fill --from-seed <field>`. Never `goto` a URL outside the plan's
   `url_scope` — the step-CLI refuses it (exit 3); respect the deny list and do not submit
   destructive actions. **Run the step commands sequentially, one at a time** — each
   depends on the session state (`storageState` + marker + `crawl-graph.json`) the
   previous one wrote; running them in parallel would lose updates (the writes are atomic
   but a read-modify-write across concurrent processes is not serialized).

   ### Multi-role exploration (a `roles` block is present)

   When the plan declares `roles` (named user perspectives — e.g. `admin` / `teller` /
   `auditor`; names are the operator's, nothing app-specific), do a **FULL SEPARATE CRAWL
   PER ROLE** (clean isolation, not session-switching). **For each role, in turn:**

   1. Run **Phase A** (the crawler) into that role's OWN per-role state slice + out dir —
      `--state-dir .foundry/context/runtime/.state/roles/<role>` and
      `--out .foundry/context/runtime/roles/<role>`:

      ```bash
      "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.crawler --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state/roles/admin --out .foundry/context/runtime/roles/admin
      ```

   2. Run **Phase B** (deep-drive) authenticating **as that role**, into the SAME per-role
      state slice. Resolve the ROLE's credentials with `creds <field> --role <name>` (a role
      owns its OWN `creds_ref`; `--role` takes precedence over `--profile`). Fill the login
      fields using the role's `login_profiles` selectors — for a **chained login** (ordered
      `login_profiles`, e.g. `["lan", "xtrac"]`), authenticate through each realm IN ORDER:
      read realm 1's selectors via `login-selectors --profile lan`, fill+submit them with the
      role's creds, then realm 2 (`login-selectors --profile xtrac`), and so on:

      ```bash
      "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli session start --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state/roles/admin
      # Realm 1 of the chained login (login_profiles[0]) — one `login` verb per realm resolves
      # the realm's selectors + login_url (--profile) and the role's creds (--role), filling +
      # submitting in one lifetime so a CSRF-protected form completes (#1622):
      "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli login "$("${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli creds user --role admin --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state/roles/admin)" "$("${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli creds pass --role admin --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state/roles/admin)" --profile lan --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state/roles/admin
      # Realm 2 of the chained login (login_profiles[1]) — repeat the `login` verb for `xtrac`.
      # …then deep-drive the frontiers + snapshot exactly as the single-perspective Phase B above, all under this role's --state-dir.
      "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.step_cli session end --plan .foundry/explore-plan.yaml --state-dir .foundry/context/runtime/.state/roles/admin
      ```

   3. Author that role's `journeys.json` under its slice (step 4 below), then move to the
      next role. Each role's crawl-graph.json lives at
      `.foundry/context/runtime/.state/roles/<role>/crawl-graph.json`.

   Then in **Phase C** run the aggregator in **role-merge mode** — pass one `--role-graph
   <role>=<path>` per role (instead of `--crawl-graph`). It unions the per-role graphs,
   tags each screen with the role(s) that reached it, and emits a unified
   `runtime-journeys.json` + a `RUNTIME-DISCOVERY.md` with the **Role Coverage** section.
   The merge automatically **rewrites each role's screenshot refs to `roles/<role>/…`**
   (#1624b) so the top-level `RUNTIME-DISCOVERY.md`'s screenshot links resolve to the
   role-sliced PNGs — no manual copy of screenshots to a top-level dir is needed:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.journey_aggregator --role-graph admin=.foundry/context/runtime/.state/roles/admin/crawl-graph.json --role-graph teller=.foundry/context/runtime/.state/roles/teller/crawl-graph.json --journeys .foundry/context/runtime/.state/journeys.json --base-url https://staging.example.com --out .foundry/context/runtime
   ```

   A plan with **no `roles`** ⇒ the single-perspective flow above, unchanged.

4. **Author `journeys.json`.** From the merged graph, write
   `.foundry/context/runtime/.state/journeys.json` naming each journey (`RJRN-NNN`) as a
   persona/goal arc with an ordered `path` of screen ids you actually reached — no
   fabrication, only real screens.

5. **Phase C — synthesize + reconcile.** Render the final artifacts (pass
   `--functional-map` when a static map exists so the reconciliation runs):

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.journey_aggregator --crawl-graph .foundry/context/runtime/.state/crawl-graph.json --journeys .foundry/context/runtime/.state/journeys.json --base-url https://staging.example.com --out .foundry/context/runtime --functional-map .foundry/context/functional-map/functional-map.json
   ```

## How to invoke

The body drives the three `"${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" -m primitives.runtime_explore.shared.*` CLIs above
(crawler / step_cli / journey_aggregator) via the `Bash` tool — there is no CLI
delegation layer (this is a skill-first orchestrator; the phases run in-session). See
`references/authoring-guide.md` for the plan format, url_scope + deny guidance, seed
data, and the secrets discipline.

## Output

- `runtime-journeys.json` — the machine artifact (screens, nav edges, journeys,
  external edges, budget) under `.foundry/context/runtime/`.
- `screenshots/<route-slug>__<screen-id>.png` — a route-named screenshot per screen
  (both phases), under `.foundry/context/runtime/screenshots/`.
- `RUNTIME-DISCOVERY.md` — the human report (screen inventory with a screenshot link per
  row, Mermaid nav map, reachability summary, and — when a static `functional-map.json`
  is present — the reconciliation: confirmed / runtime-only / static-only).
