# S2 `test-maintainer` — CI overlay: enabling the PR-trigger closed loop

## What this is

The emitted `forge-test-maintainer.yml` workflow (produced by `forge-foundry-sync` from
`primitives/test_maintainer/ci.yaml`) is **dormant by construction**: it triggers only on
`workflow_dispatch`. This is the Foundry dormant-CI invariant — the Foundry workbench never
auto-runs in a consumer repo; the consumer opts in.

This overlay is the copy-paste recipe to **go live**: a `pull_request` trigger, a
`permissions` block, and a placeholder test-environment setup step. Paste the snippets into
your emitted workflow to turn on the S2 closed loop for every PR to `main`.

---

## Step 1 — replace the `on:` trigger

In your emitted `.github/workflows/forge-test-maintainer.yml`, replace:

```yaml
on:
  workflow_dispatch:
    inputs:
      # … (keep the inputs block as-is for manual runs)
```

with:

```yaml
on:
  # Keep workflow_dispatch for manual runs.
  workflow_dispatch:
    inputs:
      code_path:
        description: 'Path to the repository root'
        required: false
        default: '.'
        type: string
      diff_against:
        description: 'Git ref to diff against (e.g. HEAD~1)'
        required: false
        default: 'HEAD~1'
        type: string
      open_pr:
        description: 'Branch + raise PR over the verified-green edits'
        required: false
        default: 'false'
        type: boolean

  # ADD: run on every PR to main.
  pull_request:
    branches: [main]
```

---

## Step 2 — set permissions

The closed loop needs `contents: write` (to push the `forge/test-maintainer-<sha>` branch)
and `pull-requests: write` (for `gh pr create` and, in the zero-green case, `gh pr comment`).
These are already declared in the emitted workflow's top-level `permissions:` block — **verify
they are present and have not been overridden**.

```yaml
permissions:
  contents: write
  pull-requests: write
```

**Least-privilege note:** If you run the maintainer in dry-run mode only (no `--open-pr`), you
can scope down to `contents: read`. The emitter sets `contents: write` + `pull-requests: write`
because the full closed loop (TM-6) needs them; dry-run-only deployments can narrow them here.

---

## Step 3 — wire in your test environment

The S2 verify loop (TM-5) runs the impacted test subset via the Python / JS-TS / Java stack
adapter (`adapter.run_tests`). The agent CLI that drives the loop must be able to execute your
test suite. **Add a setup step before the `Run test-maintainer` step**:

```yaml
      # ── Install your test dependencies here ──────────────────────────────────
      # Reuse your existing test job's setup steps, or install directly.
      # Examples (uncomment the one that fits your stack):

      # Python (pytest + coverage.py):
      # - run: pip install -r requirements-dev.txt
      #   name: Install Python test dependencies

      # JavaScript / TypeScript (jest / vitest):
      # - uses: actions/setup-node@v4
      #   with:
      #     node-version: '20'
      #     cache: npm
      # - run: npm ci
      #   name: Install JS/TS test dependencies

      # Java (Maven + JaCoCo — coverage output to target/site/jacoco/):
      # - uses: actions/setup-java@v4
      #   with:
      #     java-version: '21'
      #     distribution: temurin
      # - run: mvn dependency:resolve --no-transfer-progress
      #   name: Resolve Maven dependencies
      # ── End test-dependency setup ─────────────────────────────────────────────
```

Place this step **after** `uv sync` (which installs Foundry's own Python dependencies) and
**before** the `Run test-maintainer` step.

---

## Step 4 — cache the coverage index (per-test coverage parity)

The impact engine is **coverage-primary**: for JS/TS and Java it consults a cached, incremental
**coverage index** (`.foundry/test-maintainer/coverage-index/<stack>.json`) that records, per
test file (JS/TS) / per test class (Java), the `(source, line)` pairs that test executed. That
index is what lets the maintainer select *indirectly-impacted* tests — a test that exercises the
changed code transitively, without importing its file — which import-reachability alone misses.
(Python is already `measured=True` from a single `--cov-context=test` run and does not need the
isolation-run index.)

Building that index from scratch (a cold cache) runs one scoped coverage invocation **per test
file / class**, which is expensive — slowest for Java (a scoped Maven/Gradle build per class).
Caching it across runs is what makes whole-suite coverage affordable. The model is:

- **`main`-side refresh (write):** when the workflow runs on `main`, the maintainer **refreshes**
  the index — re-measuring only the test files/classes whose own content **or** any covered
  source changed (a bounded staleness sweep), then persists the updated index.
- **PR-time consume (read-only):** when the workflow runs on a `pull_request`, the maintainer
  **restores the base index and consumes it read-only** — intersecting the cached base coverage
  with the diff's base-side line numbers to find impacted tests. **No coverage is re-measured on
  the PR.**

Add an `actions/cache` step **keyed by the dependency lockfile + the coverage-index schema
version**, so a lockfile change (new deps → potentially different coverage) or a schema bump
invalidates the cache, but routine PRs reuse it. Place it **after** the test-dependency install
and **before** `Run test-maintainer`:

```yaml
      # ── Restore / persist the per-test coverage index ────────────────────────
      # Keyed by the lockfile (deps change → re-measure) + the coverage-index
      # schema version (bump invalidates stale on-disk shapes). The base index
      # is refreshed on main; PRs restore-and-consume it read-only.
      - name: Cache coverage index
        uses: actions/cache@v4
        with:
          path: .foundry/test-maintainer/coverage-index
          # Pick the hashFiles glob that matches your stack's lockfile:
          #   JS/TS:  package-lock.json / pnpm-lock.yaml / yarn.lock
          #   Java:   pom.xml / **/gradle.lockfile
          key: forge-tm-covidx-v1-${{ runner.os }}-${{ hashFiles('**/package-lock.json', '**/pom.xml') }}
          # restore-keys lets a PR reuse the most recent base index even when
          # the lockfile drifted slightly (an over-cap / stale slice then
          # re-measures incrementally on the next main run).
          restore-keys: |
            forge-tm-covidx-v1-${{ runner.os }}-
      # ── End coverage-index cache ──────────────────────────────────────────────
```

The `-v1` segment of the cache key is the **coverage-index schema version** (the on-disk
`schema_version` field, currently `"1.0"`); bump it here whenever the index's JSON shape changes
incompatibly so old caches are not restored against a new reader.

**No new permissions.** The cache is gitignored consumer state restored from the Actions cache —
it needs neither `contents: write` nor any token beyond the default. The `actions/cache` action
uses the built-in cache service; the permissions in Step 2 are unchanged.

**Safety cap.** If a cold build (or a large staleness sweep) would exceed
`.foundry/sdet.yaml::s2.max_coverage_runs` (default 50), the maintainer **falls back to static
import-reachability selection** for that run (a logged warning, `measured=False`) rather than run
hundreds of isolation invocations or hang CI. A missing runner / unparseable coverage degrades
the same way — never fabricating per-test attribution. The static graph is always the floor; the
cache strictly *upgrades* recall when it is available.

---

## Step 5 — set `--open-pr` for the PR trigger path

When the workflow fires on `pull_request`, you usually want the full closed loop (propose →
verify → branch + PR). Set the `open_pr` input default to `true` **for the `pull_request`
trigger path**, or always pass `--open-pr` via the agent CLI invocation. The simplest approach
is to override the env default conditionally:

```yaml
      - name: Run test-maintainer
        env:
          CODE_PATH: ${{ inputs.code_path || '.' }}
          DIFF_AGAINST: ${{ inputs.diff_against || github.event.pull_request.base.sha }}
          # Enable --open-pr automatically on pull_request events.
          OPEN_PR: ${{ github.event_name == 'pull_request' && 'true' || inputs.open_pr || 'false' }}
        run: |
          set -euo pipefail
          # … (validation lines — keep as emitted) …
          PROMPT="Run /test-maintainer on ${CODE_PATH}."
          PROMPT="$PROMPT Diff against: ${DIFF_AGAINST}."
          if [ "$OPEN_PR" = "true" ]; then PROMPT="$PROMPT Pass --open-pr."; fi
          claude -p "$PROMPT" --allowedTools Bash,Read,Write --max-turns 50
```

---

## Step 6 — (optional) enable e2e maintenance (Playwright)

By default the maintainer's impact + verify engine covers **unit/integration** suites
(`pytest` / `jest`-`vitest` / `mvn`-`gradle`). It also understands **e2e/Playwright** suites,
but that path is **opt-in** because e2e is slow and flaky (#1029):

- **Impact association.** A change to a route/handler surfaces the e2e specs that exercise it via
  the e2e map (`primitives/test_maintainer/shared/e2e_map.py` + `e2e_impact.py`) — page-object /
  route references, not just unit tests that statically import the changed symbol.
- **Verify-by-running.** Pass **`--with-e2e`** to run the impacted Playwright subset through the
  green-gate. **Without `--with-e2e`** an accepted e2e edit is **not** applied as green — it is
  recorded as `unverified — needs human e2e run` so a human runs it (the run never silently
  applies an unverified e2e edit).

To turn e2e maintenance on in CI, install the Playwright runtime **after** your test-dependency
setup (Step 3) and add `--with-e2e` to the agent prompt:

```yaml
      # e2e (Playwright) — only if you want the e2e green-gate (slow):
      # - run: npx playwright install --with-deps chromium
      #   name: Install Playwright browsers
```

```yaml
      # In the "Run test-maintainer" step's prompt (Step 5), append --with-e2e:
          if [ "$OPEN_PR" = "true" ]; then PROMPT="$PROMPT Pass --open-pr."; fi
          PROMPT="$PROMPT Pass --with-e2e."   # opt into the e2e green-gate
```

Leave `--with-e2e` **off** for fast PR feedback; the impacted e2e specs are still *surfaced* in
the report (as `unverified`) so a reviewer knows which e2e tests to run by hand.

---

## Exit codes — expected, not failures

The CI step must **not** fail on exit 8 or 9. Add `continue-on-error` or an explicit check:

```yaml
      - name: Run test-maintainer
        id: tm
        # … env, run as above …
        continue-on-error: true

      - name: Check test-maintainer result
        run: |
          code=${{ steps.tm.outcome == 'success' && '0' || '1' }}
          # Exit 8: PR opened (success). Exit 9: report only, zero green edits (success).
          # The actual exit code from claude is captured via $? in the run: above;
          # map outcomes here if your harness surfaces exit codes differently.
          echo "Test-maintainer finished. See the workflow run log for the PR URL or report."
```

Full exit-code table (from `SPEC.md`):

| Code | Meaning |
|------|---------|
| 0 | Success — dry-run / apply, no PR requested. |
| 1 | Budget exceeded, or a git/gh step failed after green edits were produced. |
| 2 | Input error (bad git ref or SEC-8 validation failed). |
| 7 | Awaiting judgments (`--extract-only` wrote a prompts file — the agent re-invokes). |
| 8 | **PR opened** — `forge/test-maintainer-<sha>` branch committed, pushed, `gh pr create` ran. |
| 9 | **Report only** — `--open-pr` given but zero verified-green edits; no branch/PR created. |

Exits 8 and 9 are **normal operational outcomes**, not failures.

---

## Azure DevOps analogue

The emitted `azure-pipelines/forge-test-maintainer.yml` uses `trigger: none`. To enable on
PRs to `main`, replace it with a `pr:` block:

```yaml
# Replace:
trigger: none

# With:
trigger: none   # keep: opt out of CI pushes
pr:
  branches:
    include:
      - main
```

Permissions in Azure DevOps are granted at the pipeline level (service connection + repo
permission), not in the YAML. Grant the build service identity:
- **Contribute to pull requests** (so `gh pr create` / `gh pr comment` can run via the
  installed GitHub CLI with a `GITHUB_TOKEN` secret).
- **Push** on the target repository (so `git push` for the `forge/test-maintainer-<sha>`
  branch succeeds).

**Coverage-index cache analogue.** Use the `Cache@2` task keyed the same way (lockfile +
schema version) and pointed at `.foundry/test-maintainer/coverage-index`:

```yaml
- task: Cache@2
  inputs:
    key: 'forge-tm-covidx-v1 | "$(Agent.OS)" | package-lock.json | pom.xml'
    restoreKeys: 'forge-tm-covidx-v1 | "$(Agent.OS)"'
    path: '.foundry/test-maintainer/coverage-index'
  displayName: 'Cache coverage index'
```

No additional pipeline permission is needed — the cache is the agent's own scratch state.

---

## AWS CodeBuild analogue

The emitted `buildspec/forge-test-maintainer.yml` carries no trigger. Wire it to a CodeBuild
project with a **source webhook** filtered to `PULL_REQUEST_CREATED` and
`PULL_REQUEST_UPDATED` events. Grant the CodeBuild role:
- `codecommit:GitPush` / GitHub OAuth with push scope (for the branch push).
- GitHub App or PAT with `pull-requests: write` scope (for `gh pr create`).

Pass `OPEN_PR=true` as an environment variable override on the webhook-triggered builds.

**Coverage-index cache analogue.** CodeBuild caches via the project's `cache` config (S3 or
local), declared in the buildspec `cache.paths`:

```yaml
cache:
  paths:
    - '.foundry/test-maintainer/coverage-index/**/*'
```

For cross-run persistence configure an **S3 cache** on the CodeBuild project (a local cache only
survives warm-instance reuse). No extra IAM beyond the project's existing cache-bucket access is
required — the index is build scratch state, not a privileged artifact.

---

## Token-free clone

The emitted workflow uses `actions/checkout@v4` with `fetch-depth: 0` and no `token:` override.
The `GITHUB_TOKEN` is the default credential — never embed a PAT in the `url:` field (this
is guarded by `tests/test_ci_emitter.py::test_emitted_ci_never_embeds_token_in_url`).

`gh pr create` (called by the maintainer's `pr.py`) picks up `GITHUB_TOKEN` automatically from
the environment when running in GitHub Actions — no extra setup needed.

---

## Input validation

All string inputs (`code_path`, `diff_against`) are shell-validated against anchored regexes
by the emitted workflow before they reach the agent CLI. The `open_pr` bool is constrained to
`true|false` at the workflow layer (GH Actions `type: boolean`; Azure `type: boolean`;
CodeBuild validates it in-shell). Never bypass the validation step.

---

## Summary of what the overlay changes

| What | Dormant (emitted) | Overlay (consumer enables) |
|------|-------------------|---------------------------|
| Trigger | `workflow_dispatch` only | Add `pull_request: branches: [main]` |
| Permissions | `contents: write, pull-requests: write` | Same (already correct) — **the cache adds none** |
| Test env | Not set up (no test runner) | **Add your test-dep install step** |
| Coverage index | Not cached (cold every run) | **Add `actions/cache` keyed by lockfile + schema version** |
| `--open-pr` | `false` (default) | `true` on `pull_request` events |
| e2e (Playwright) | Off — e2e edits recorded `unverified` | Optional: install browsers + pass `--with-e2e` to green-gate them |
| Exit 8 / 9 | N/A (no PR path) | Expected success outcomes — don't fail CI |
