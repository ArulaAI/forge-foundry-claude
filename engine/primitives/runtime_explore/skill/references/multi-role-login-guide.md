# Multi-role login for `runtime_explore`

A practical guide to configuring `/mod-runtime-explore` to explore a web app **from multiple user
roles**, where each role logs in with its own credentials — including apps whose login is a
**multi-step (chained)** process across several realms.

Worked end-to-end against **PetClinic** with three roles: **admin**, **vet**, and **user**.

---

## The two-layer mental model

There are two distinct things in the explore-plan, and keeping them straight is the whole trick:

| Layer | What it is | Owns |
|-------|-----------|------|
| **`auth.profiles`** | Named *login realms* — the **mechanics** of a login form | the **selectors** (`username_selector`, `password_selector`, `submit_selector`) + an optional per-realm `login_url` + a **default** credential reference (`creds_from`/`creds_ref`) |
| **`roles`** | Named *personas to explore as* (admin / vet / user) | each role's **own credentials** (`creds_ref`) + an **ordered list of profile keys** (`login_profiles`) |

**The key rule:** the **profile owns which fields to type into** (the selectors); a **role owns the
actual username/password** for that persona. When you explore *as a role*, the agent fills the
profile's selectors with the **role's** credentials.

> **Precedence:** a profile *also* carries a `creds_from`/`creds_ref` (its own default, required by
> the schema). When you name a `--role`, the **role's `creds_ref` wins** and the profile's is
> ignored — the role supplies the credentials, the profile supplies only the selectors. The
> profile's own creds are used only when you drive it *without* a role (`creds --profile <name>`).
> In a multi-role plan, give each profile a placeholder/service credential reference; the real
> per-persona secrets live on the roles.

So "a set of logins for admin, vet, and user" = **three roles**. Whether each role's login is
*single-step* or *multi-step (chained)* decides how many profiles each role lists in
`login_profiles`.

---

## Case A — one login form, three personas (the common case)

PetClinic has a single login page; admin, vet, and user simply have different credentials. You need
**one profile** (the login form's selectors) and **three roles** (the three credential sets).

```yaml
# .foundry/explore-plan.yaml
base_url: "https://staging.petclinic.example/"

url_scope:
  allow: ["https://staging.petclinic.example/**"]
  deny:  ["**/logout/**", "**/logout"]        # never let the crawler log itself out

budget: { max_pages: 150, max_depth: 6, wait_until: networkidle }   # networkidle suits SPA / Thymeleaf

auth:
  mode: agent_login
  login_url: "/login"                          # shared by every realm (relative to base_url)
  profiles:
    petclinic_form:                            # the login form's mechanics (one form, reused by all roles)
      username_selector: 'input[name="username"]'
      password_selector: 'input[name="password"]'
      submit_selector:   'button[type="submit"]'
      creds_from: env                          # profile default (required by schema); IGNORED when a --role is used
      creds_ref: PETCLINIC_SERVICE_CREDS       #   → the real per-persona secrets live on the roles below

roles:                                         # the three personas — each with its OWN credentials
  admin:
    login_profiles: [petclinic_form]           # single entry = single-step login
    creds_from: env
    creds_ref: PETCLINIC_ADMIN_CREDS           # env var holding {"username": "...", "password": "..."}
  vet:
    login_profiles: [petclinic_form]
    creds_from: env
    creds_ref: PETCLINIC_VET_CREDS
  user:
    login_profiles: [petclinic_form]
    creds_from: file
    creds_ref: .secrets/petclinic-user.yaml    # gitignored file: {username: ..., password: ...}
```

### Supplying the credentials (by reference — never inline)

`creds_ref` is a *reference*, resolved at runtime into a `{field: value}` map. The login step
retrieves fields by name (`creds username`, `creds password`), so **name the fields `username` and
`password`** (matching what the fill commands ask for). Two forms:

**`creds_from: env`** — the env var's value is a **JSON object** (or a bare string, which binds to the
field `value`). The Case-A plan above references three env vars plus the profile's service default:

```bash
# One JSON object per persona. Keys must be the field names the login step retrieves.
export PETCLINIC_ADMIN_CREDS='{"username":"admin",  "password":"s3cret-admin"}'
export PETCLINIC_VET_CREDS='{"username":"vet01",  "password":"s3cret-vet"}'
# The profile's default (required by the schema, but IGNORED when exploring as a --role):
export PETCLINIC_SERVICE_CREDS='{"username":"svc-explore","password":"unused-at-role-time"}'
```

Prefer a file you `source` rather than shell history — the values still land in the process
environment the CLI reads:

```bash
# .secrets/petclinic.env   (add .secrets/ to .gitignore)
export PETCLINIC_ADMIN_CREDS='{"username":"admin","password":"s3cret-admin"}'
export PETCLINIC_VET_CREDS='{"username":"vet01","password":"s3cret-vet"}'
export PETCLINIC_SERVICE_CREDS='{"username":"svc-explore","password":"x"}'
```
```bash
set -a; source .secrets/petclinic.env; set +a     # load, then run /mod-runtime-explore
```

**`creds_from: file`** — a path to a **gitignored** YAML (or JSON) `field: value` map. The Case-A
`user` role uses this:

```yaml
# .secrets/petclinic-user.yaml   (add .secrets/ to .gitignore)
username: jean
password: "s3cret-user"
```

Add the secrets directory to your ignore file so nothing is ever committed:

```gitignore
# .gitignore
.secrets/
```

### What running it does

Because `roles` is non-empty, the skill runs a **full separate crawl + deep-drive pass per role**
(admin, then vet, then user) — each into its own state slice
(`.foundry/context/runtime/.state/roles/<role>/`), authenticating with that role's credentials. The
aggregator then merges the per-role graphs and emits a **Role Coverage** section in
`RUNTIME-DISCOVERY.md`: which screens each role reaches, per-role counts, and — most usefully — the
**role-gated delta** (e.g. "these 8 admin screens are admin-only").

---

## Case B — the "multi-logging process": chained (multi-step) login

If your login is a **sequence of screens** — e.g. an outer SSO / portal realm, *then* the PetClinic
app realm — that's a **chained login**: the role's `login_profiles` lists **more than one profile,
in order**. The role authenticates through each realm in sequence.

```yaml
# .foundry/explore-plan.yaml  (auth + roles shown; base_url / url_scope / budget as above)
auth:
  mode: agent_login
  login_url: "/sso/login"
  profiles:
    sso:                                       # realm 1 — the outer portal
      username_selector: 'input[name="corpId"]'
      password_selector: 'input[name="corpPass"]'
      submit_selector:   '#sso-submit'
      login_url: "/sso/login"                  # per-realm override of the shared login_url
      creds_from: env                          # profile default (required); IGNORED when a --role is used
      creds_ref: PETCLINIC_SERVICE_CREDS
    petclinic_app:                             # realm 2 — the inner app login
      username_selector: 'input[name="username"]'
      password_selector: 'input[name="password"]'
      submit_selector:   'button[type="submit"]'
      login_url: "/app/login"
      creds_from: env
      creds_ref: PETCLINIC_SERVICE_CREDS

roles:
  admin:
    login_profiles: [sso, petclinic_app]       # CHAINED: authenticate through sso → then petclinic_app
    creds_from: env
    creds_ref: PETCLINIC_ADMIN_CREDS
  vet:
    login_profiles: [sso, petclinic_app]
    creds_from: env
    creds_ref: PETCLINIC_VET_CREDS
  user:
    login_profiles: [sso, petclinic_app]
    creds_from: file
    creds_ref: .secrets/petclinic-user.yaml
```

Each role walks **realm 1 → realm 2**, filling each realm's selectors with the *same role's*
credentials.

### Credentials for the chained case

**Same credentials at both realms** (SSO and app accept the same username/password): identical to
Case A — each role's `creds_ref` holds one `username`/`password`, reused at both realms.

```bash
# .secrets/petclinic.env  (source before running; add .secrets/ to .gitignore)
export PETCLINIC_ADMIN_CREDS='{"username":"admin","password":"s3cret-admin"}'
export PETCLINIC_VET_CREDS='{"username":"vet01","password":"s3cret-vet"}'
export PETCLINIC_SERVICE_CREDS='{"username":"svc-explore","password":"x"}'   # profile default (unused at role time)
```
```yaml
# .secrets/petclinic-user.yaml   (the file-based `user` role)
username: jean
password: "s3cret-user"
```

**Different credentials per realm** (e.g. a shared SSO login, then a *per-persona* app login): put
**every field** in the role's creds map under distinct names, and retrieve each by name at fill time
(`creds sso_user --role admin`, `creds app_user --role admin`, …). The map can carry as many fields
as your login chain needs:

```bash
# env form — the role's creds map carries all four fields
export PETCLINIC_ADMIN_CREDS='{"sso_user":"corp-sso","sso_pass":"sso-shared-pw","app_user":"admin","app_pass":"s3cret-admin"}'
```
```yaml
# …or the file form for the `user` role — .secrets/petclinic-user.yaml
sso_user:  corp-sso
sso_pass:  sso-shared-pw
app_user:  jean
app_pass:  "s3cret-user"
```

You can **mix** the two shapes in one plan: a single-step role uses `login_profiles: [petclinic_form]`
while a chained role uses `login_profiles: [sso, petclinic_app]`.

---

## How the agent drives it (per-role runbook)

You normally don't run these by hand — you author the plan, export the creds, and invoke
`/mod-runtime-explore` (or `/mod-modernization --runtime-explore`); the skill body drives the
per-role loop. This is what it does under the hood for one role, so you can see the
credential/selector split in action:

```bash
STATE=.foundry/context/runtime/.state/roles/admin
OUT=.foundry/context/runtime/roles/admin
PLAN=.foundry/explore-plan.yaml

# Phase A — breadth crawl as admin (its own state slice)
python -m primitives.runtime_explore.shared.crawler --plan $PLAN --state-dir $STATE --out $OUT

# Phase B — log in as admin. For a CHAINED login, walk each profile IN ORDER.
python -m primitives.runtime_explore.shared.step_cli session start --plan $PLAN --state-dir $STATE

# realm 1 (sso): read its selectors, fill with ADMIN's creds (--role admin), submit
python -m primitives.runtime_explore.shared.step_cli login-selectors --profile sso --plan $PLAN --state-dir $STATE
python -m primitives.runtime_explore.shared.step_cli fill '<sso username_selector>' \
  "$(python -m primitives.runtime_explore.shared.step_cli creds username --role admin --plan $PLAN --state-dir $STATE)" --plan $PLAN --state-dir $STATE
python -m primitives.runtime_explore.shared.step_cli fill '<sso password_selector>' \
  "$(python -m primitives.runtime_explore.shared.step_cli creds password --role admin --plan $PLAN --state-dir $STATE)" --plan $PLAN --state-dir $STATE
python -m primitives.runtime_explore.shared.step_cli submit '<sso submit_selector>' --plan $PLAN --state-dir $STATE

# realm 2 (petclinic_app): repeat with --profile petclinic_app, same --role admin
# … then the same sequence for vet and user, each into roles/vet and roles/user.
```

The two step-CLI verbs that make the split explicit:

- **`creds <field> --role admin`** → resolves `roles.admin.creds_ref` (the persona's credentials).
  `--role` takes precedence over `--profile`.
- **`login-selectors --profile sso`** → prints that realm's
  `username_selector` / `password_selector` / `submit_selector` / `login_url` as JSON. Selectors are
  **not** secrets, so this is safe to print and pipe.

---

## Validation rules (they fail loudly, so you catch mistakes early)

- **Exactly one auth shape.** A plan uses *either* `auth.profiles` *or* the flat single-realm
  `auth.creds_from` / `auth.creds_ref` — never both, and never neither.
- **Every `login_profiles` entry must name a declared profile.** `login_profiles: [petclinic_form]`
  requires `auth.profiles.petclinic_form` to exist, or you get a `PlanError` that lists the valid
  profile names.
- **Roles require an `auth` block.** No auth → roles are rejected.
- **No inline secrets anywhere.** A literal `password:` / `token:` / `secret:` value in the plan is
  rejected (exit 3). Everything is by reference (`creds_from` + `creds_ref`).

---

## Prerequisites

`runtime_explore` drives a real browser via Playwright, which is an **optional extra**. Install it to
match how the CLI was installed — e.g. for a git-based `uv tool` install:

```bash
uv tool install --force 'forge-foundry-primitives[browser] @ git+https://github.com/arula-ai/forge-foundry-primitives@<tag>'
uv tool run --from 'forge-foundry-primitives[browser]' playwright install chromium
```

If the browser extra is missing, the skill exits **6** with the exact install commands.

---

## Field reference

**`auth.profiles.<name>`** (an `AuthProfile`):

| Field | Required | Notes |
|-------|----------|-------|
| `username_selector` | yes | CSS selector for the username/id field. |
| `password_selector` | yes | CSS selector for the password field. |
| `submit_selector` | no | CSS selector for the submit button (falls back to a generic submit). |
| `login_url` | no | Per-realm override of the top-level `auth.login_url`. |
| `creds_from` | yes | `env` or `file`. |
| `creds_ref` | yes | The env var name or file path (never a secret value). |

**`roles.<name>`** (a `RoleRef`):

| Field | Required | Notes |
|-------|----------|-------|
| `login_profiles` | yes¹ | Ordered list of `auth.profiles` keys. One = single-step; more = chained. |
| `creds_from` | yes | `env` or `file` — this persona's credentials. |
| `creds_ref` | yes | Env var / file path for this persona's `{username, password}`. |

¹ `login_profiles` may be empty **only** when `auth` is a flat single-realm login (no
`auth.profiles`); the role then uses that flat login with its own `creds_ref`.

---

*Part of the `runtime_explore` (`/mod-runtime-explore`) skill. See also
[`authoring-guide.md`](authoring-guide.md) and [`legacy-web-playbook.md`](legacy-web-playbook.md),
and the [Skills Usage Guide](../../../../docs/skills-usage-guide.md) `#mod-runtime-explore` entry.*
