# Authoring an `explore-plan.yaml`

The plan is the only input `mod-runtime-explore` needs beyond a running target. Save it
at `.foundry/explore-plan.yaml`.

> **Driving the app itself?** For how to reliably drive a dirty legacy / SPA app in Phase B
> — selector-stability order, no-hard-sleep waiting, Angular/NgRx/lazy-route patterns — read
> the companion field guide **[`legacy-web-playbook.md`](legacy-web-playbook.md)**.

```yaml
base_url: "https://staging.example.com"
url_scope:
  allow: ["https://staging.example.com/**", "https://staging.example.com"]
  deny:  ["**/logout**", "**/admin/delete/**", "**/checkout/confirm**"]
budget:
  max_pages: 200
  max_depth: 6
  page_timeout_ms: 15000
  total_timeout_s: 900
  wait_until: domcontentloaded   # OPTIONAL nav strategy: domcontentloaded (default) |
                                 # networkidle (use for Angular/SPA — waits for the network
                                 # to settle after the SPA fetches its data) | load
auth:                        # OPTIONAL — omit entirely for a no-auth / public target
  mode: agent_login
  login_url: "/login"
  creds_from: env            # env | file — a REFERENCE, not the secret
  creds_ref: FORGE_EXPLORE_USER
seed_data_ref: "./.foundry/explore-seed.yaml"   # gitignored realistic form values
```

**No-auth target?** Omit the `auth` block entirely. Phase A (the crawl) never uses
`auth`; it's only exercised in Phase B when the agent crosses a login frontier. A minimal
plan for a public / local dev instance is just `base_url` + `url_scope`:

```yaml
base_url: "http://localhost:8080/petclinic/"
url_scope:
  allow: ["http://localhost:8080/petclinic/**"]
  deny:  []
```

## Multiple login profiles (multi-realm apps)

Some legacy apps expose several auth **realms** on ONE login URL, distinguished only by
different input selectors (each realm's username/password fields have distinct
`name`/`id` attributes). Declare each realm under `auth.profiles` — an **arbitrary**
profile name mapped to its selectors + its own credential reference:

```yaml
auth:
  mode: agent_login
  login_url: "/login"          # the shared default; a profile may override it
  profiles:
    realm_a:                   # names are yours — nothing is assumed
      username_selector: "#user_a"
      password_selector: "#pass_a"
      submit_selector: "#submit_a"     # OPTIONAL — falls back to a generic submit
      creds_from: env
      creds_ref: REALM_A_CREDS
    realm_b:
      username_selector: "input[name='UserB']"
      password_selector: "input[name='PassB']"
      login_url: "/login/b"            # OPTIONAL per-profile override
      creds_from: env
      creds_ref: REALM_B_CREDS
```

- The `auth` block is **EXACTLY ONE** shape: **flat** (`creds_from` + `creds_ref`, no
  `profiles` — the single-realm shape above) OR **profiles** (non-empty `profiles`, no
  top-level `creds_from`/`creds_ref`). Mixing both, or neither, → exit 3. `login_url`
  stays required at the top level.
- The SKILL body reads a realm's selectors with `step_cli login-selectors --profile <name>`
  (prints them as JSON — selectors are not secrets), fills those fields, and resolves that
  realm's creds with `step_cli creds <field> --profile <name>`. With exactly one profile,
  `--profile` is optional (it defaults to that one).
- Everything else is generic: names, selectors, and credential refs are entirely
  operator-supplied. A real-world example is a portal with LAN / partner / internal realms
  (e.g. all filling `input[name="UserID"]` on `replogin.<env>.example.com`) — but those are
  *your app's* values to fill in, never baked into the schema.

## Multi-role exploration (`roles`)

Explore the app from several named user **perspectives** (e.g. `admin` / `teller` /
`auditor` — names are yours, nothing app-specific is assumed). Each role gets a **FULL
SEPARATE CRAWL + deep-drive** into its own per-role state slice
(`.foundry/context/runtime/.state/roles/<role>/`), then the aggregator merges the per-role
graphs and tags each screen with the role(s) that reached it — surfacing the role-gated
delta (screens only one role can reach). N roles ⇒ N× runtime (accepted, for clean
isolation — NOT session-switching).

```yaml
roles:
  admin:
    login_profiles: ["lan", "xtrac"]   # ORDERED ⇒ CHAINED login: log in through lan, then xtrac
    creds_from: env
    creds_ref: ADMIN_CREDS             # the ROLE's own credential reference
  teller:
    login_profiles: ["lan"]            # a single realm
    creds_from: env
    creds_ref: TELLER_CREDS
```

- A non-empty `roles` block **requires** an `auth` block (roles authenticate via login).
- `login_profiles` names `auth.profiles` keys, **in order**. More than one ⇒ a **chained
  login** (authenticate through each realm in sequence — the outer SSO realm, then the inner
  app realm, etc.). Each key must exist under `auth.profiles`, else exit 3.
- The **profile supplies the selectors**; the **role supplies the credentials** (its own
  `creds_from`/`creds_ref`). The SKILL body reads a realm's selectors with
  `login-selectors --profile <name>` and resolves the role's creds with `creds <field>
  --role <name>` (which takes precedence over `--profile`).
- When `auth` is a **flat single-realm** block (no `auth.profiles`), leave `login_profiles`
  empty — the role uses the flat login with its own creds. (A flat plan with a role that
  lists profiles → exit 3; a profiles plan with an empty-`login_profiles` role → exit 3.)
- Per-role inline secrets are caught by the same recursive secret guard (exit 3).
- **No `roles` ⇒ today's single-perspective run, byte-identical** (the `roles`/
  `role_coverage` fields are omitted from the artifact when empty).

## url_scope — the containment guard

- Every navigation (crawl + agent) is checked against `url_scope` **before** the browser
  goes there, and the FINAL landed URL is re-checked after redirects. An off-allowlist
  URL is recorded as an external edge and never followed.
- `allow`/`deny` are `fnmatch` globs over the **full URL** (`*`/`**` match any chars
  incl `/`). `https://app.example/**` matches every path under the host but NOT a
  lookalike host (`app.example.attacker.com` is rejected). Include the bare base URL
  too (`https://app.example`) if you want it crawlable without a trailing slash.
- **`deny` globs are case-sensitive** — spell destructive paths in the case the app
  serves (`**/admin/**` will not block `/ADMIN/x`). Always deny logout, delete,
  purchase/confirm, send-email, and any other irreversible action.

## Secrets — by reference only

- The plan holds only the **name** of an env var (`creds_from: env`) or the **path** to
  a gitignored file (`creds_from: file`). An inline `password`/`token`/`secret`/… key
  anywhere in the plan is rejected (exit 3).
- The step-CLI resolves the reference for you: `step_cli creds <field> --plan … --state-dir …`
  reads the env var (`creds_from: env` — a JSON object → a field map, or a bare string →
  the `value` field) or the gitignored file (`creds_from: file` — a YAML/JSON
  `{field: value}` map) named in the plan, and prints the value (no trailing newline) so
  it pipes straight into a `fill`. A missing env var / file → exit 2; an unreadable one →
  exit 3. Secrets never enter `crawl-graph.json`, `runtime-journeys.json`, or any prompt
  file — the plan carries only the *name*, and the value is fetched at runtime.

## seed_data — realistic form values

`seed_data_ref` points at a gitignored file mapping field names to realistic values the
agent uses when filling wizards/forms it must cross. Keep it out of git. The step-CLI
looks values up by field name: `step_cli fill "#first_name" --from-seed first_name …`
fills the selector with `seed["first_name"]`. An absent `seed_data_ref` → an empty map
(use literal `fill <selector> <value>` instead); a named-but-missing file → exit 2.
The fill value is resolved and validated **before** the browser launches: `fill`
with neither a literal `<value>` nor `--from-seed` → exit 3, and `--from-seed <field>`
naming a field absent from the seed → exit 2, both without starting Chromium.
**`--from-seed` takes precedence** — if you pass both a literal `<value>` and
`--from-seed <field>`, the seed value wins.

## Dropdowns + waiting — `select` and `wait-for`

- **`select <selector> --value|--label|--index <x>`** picks a `<select>` option. Exactly
  one of the three is required — none / multiple → exit 3 (validated before launch).
- **`wait-for`** is the **no-hard-sleep** primitive — never guess a delay:
  - `wait-for <selector>` waits until the selector is **visible** (`--state hidden|attached|detached` for the opposite — e.g. a loading spinner to vanish);
  - `wait-for --url <glob>` waits until the browser URL matches (Angular hash routing) — the glob is scope-checked (exit 3 if outside `url_scope`);
  - `wait-for --networkidle` waits until the network settles (SPA data loaded);
  - `--timeout-ms` overrides `budget.page_timeout_ms`; a timeout → exit 1 (clean, not a traceback).

  Exactly one target (selector / `--url` / `--networkidle`) is required → exit 3 otherwise.

## Stable selectors — `--by` (on `fill` / `click` / `wait-for`)

Target with the most-stable handle available, not a brittle CSS path. `--by` chooses the
strategy (default `css` — the raw selector), and `--nth <n>` picks the nth match:

| `--by` | resolves via | selector form |
| --- | --- | --- |
| `testid` | `get_by_test_id` | the `data-testid` value |
| `role` | `get_by_role` | `"<role>=<name>"` (e.g. `button=Save`) or a bare `<role>` |
| `label` | `get_by_label` | the input's label text |
| `text` | `get_by_text` | visible text |
| `placeholder` | `get_by_placeholder` | the input's placeholder |
| `css` (default) | `page.locator` | any CSS selector — last resort |

E.g. `click "save-btn" --by testid`, `fill "Full name" --from-seed first_name --by label`,
`wait-for "button=Save" --by role`. Prefer them in the order above.

See `references/legacy-web-playbook.md` for the full selector/wait discipline.

Example `seed_data_ref` file (gitignored):

```yaml
first_name: Ada
last_name: Lovelace
email: ada@example.test
city: London
```

## Screenshots

Both phases capture a PNG per discovered screen into
`.foundry/context/runtime/screenshots/`, **route-named** as `<route-slug>__<screen-id>.png`
(URL path slugified, concrete id segments → `{}`, `/` → `home`) — e.g.
`owners-find__SCR-001.png`, `owners-{}__SCR-002.png`. The screen-id suffix keeps names
unique and joined to `runtime-journeys.json`. Phase B's `snapshot` writes to
`--out/screenshots` (default: the `--state-dir`'s parent, so the standard SKILL layout
needs no extra flag). `RUNTIME-DISCOVERY.md`'s screen-inventory table links each shot.

## Installing Playwright (optional `[browser]` extra)

Use the form that matches how the workbench CLI was installed, and run
`playwright install chromium` with the **same interpreter** the CLI runs from:

```bash
# uv tool from git — the DEFAULT when the CLI was installed from a pinned tag.
uv tool install --force \
  'forge-foundry-primitives[browser] @ git+https://github.com/arula-ai/forge-foundry-primitives@v8.2.0'
uv tool run --from 'forge-foundry-primitives[browser]' playwright install chromium

# OR project checkout:  uv sync --extra browser && uv run playwright install chromium
# OR registry pip:      pip install 'forge-foundry-primitives[browser]' && playwright install chromium
```

A plain `pip install ...[browser]` targets whatever `pip` is on PATH, **not** the
uv-tool venv the CLI runs from, so runtime_explore still won't see Playwright. If
Playwright or the browser binary is missing, every phase exits **6** and prints these
commands — install, then re-run.
