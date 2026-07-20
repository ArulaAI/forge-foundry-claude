# mod-runtime-explore — explore-plan.yaml examples

Copy-pasteable `explore-plan.yaml` files for `/mod-runtime-explore`, which drives
a **running** web app with local Playwright to map its reachable journeys. Grab
one, adapt the URLs/selectors/credential references, and pass it as the plan.

Validated against `../schemas/explore-plan.schema.json`. Required keys: `base_url`
+ `url_scope`.

| File | When to use |
|------|-------------|
| [`explore-plan.public-local.yaml`](explore-plan.public-local.yaml) | A public or local dev instance with no login. Just `base_url` + `url_scope`. |
| [`explore-plan.auth-gated.yaml`](explore-plan.auth-gated.yaml) | One login form, one credential set (single realm). |
| [`explore-plan.multi-role.yaml`](explore-plan.multi-role.yaml) | Several personas (admin/vet/user) — a full crawl per role, merged + role-tagged. |

## Two non-negotiable rules

1. **Secrets by reference ONLY.** Every `creds_ref` is an **env-var NAME**
   (`creds_from: env`) or a **gitignored file PATH** (`creds_from: file`). Never
   put a literal username/password/token in a plan file.
2. **`url_scope` is the containment guard.** `allow`/`deny` globs are checked
   before every navigation (and re-checked after redirects); **deny wins**. Always
   deny `**/logout**` and any destructive/irreversible action for the app.

See also: `../authoring-guide.md` (full field reference), `../multi-role-login-guide.md`
(roles/profiles + chained logins), `../legacy-web-playbook.md` (dirty legacy/SPA apps).
