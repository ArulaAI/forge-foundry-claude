# forge-foundry-claude — Claude Code Plugin Design

**Status:** Draft (brainstormed 2026-07-01)
**Target repo:** `arula-ai/forge-foundry-claude` (private)
**Source library:** `arula-ai/forge-foundry-primitives` (vendored at a pinned tag)

## Summary

`forge-foundry-claude` is a **self-contained Claude Code plugin** that repackages the
Forge Foundry engineering workbench — all 31 primitives — for Claude-Code-only use,
distributed through a **private marketplace** (git-URL install). It vendors the primitives'
skill bodies + agents and bundles the deterministic Python engine (`forge_validate` +
`primitives/_shared` + each hybrid/PLB primitive's `shared/`), so the 23 Python-load-bearing
and hybrid primitives keep their deterministic power (SQL parsing, extraction, refcheck) while
the 8 skill-first primitives run purely in-session.

The plugin is produced by a **re-runnable vendor script** (`scripts/vendor.py`) that copies
from a pinned checkout of the source library and mechanically rewrites the PLB skill bodies to
call the plugin's bundled engine. A **SessionStart hook** builds a Python venv on first
activation. A **parity-test suite** guards every re-vendor.

**First release ships the current 31 primitives.** S61 `functional-map` and S12
`red-team-review` (designed but not yet built in the source library) are picked up on a later
re-vendor.

## Motivation

The source library's distribution model is `forge-foundry-sync` — a CLI that copies primitives
+ emits harness adapters into a consuming repo, driven by `.foundry/manifest.yaml`. That works
for teams who adopt the sync workflow, but a **Claude Code plugin** is a lower-friction channel:
install once via the plugin marketplace, get all 31 primitives as skills, no per-repo manifest
or sync step. This design adds that channel **without forking behavior** — the engine logic is
vendored byte-for-byte; only packaging and the invocation path change.

### Charter alignment

The source charter states "No public marketplace publishing. Distribution is private-only via
`forge-foundry-sync`." This design honors that: the plugin is distributed through a **private**
marketplace repo (private git URL, `license: UNLICENSED`), never the public Claude marketplace.
It is a new *private* channel, orthogonal to `forge-foundry-sync`, not a public-publishing move.
The divergence (a second private distribution channel) is recorded here as explicit intent.

## Architecture

### Repo layout (self-contained, vendored)

```
forge-foundry-claude/
├── .claude-plugin/
│   ├── plugin.json              # hand-authored plugin manifest (+ SessionStart hook wiring)
│   └── marketplace.json         # hand-authored private single-plugin marketplace
├── skills/                      # 31 vendored skill dirs (skills/ at plugin root)
│   ├── code-review/{SKILL.md, references/, scripts/}
│   └── … (30 more)
├── agents/                      # vendored agent defs (e.g. playbook-discovery)
├── hooks/
│   └── bootstrap.sh             # SessionStart: idempotent venv build
├── bin/
│   └── forge-validate           # resolver shim → bundled venv, loud on not-bootstrapped
├── engine/                      # bundled Python workbench (vendored)
│   ├── forge_validate/
│   ├── primitives/
│   │   ├── _shared/**
│   │   └── <name>/shared/**     # hybrid + PLB only (skill/ lives under skills/)
│   ├── pyproject.toml           # what the bootstrap venv installs (editable)
│   └── VENDOR_LOCK.json         # {source_repo, ref, sha, vendored_at, primitive_count, version}
├── scripts/
│   └── vendor.py                # re-runnable vendor-copy + path-rewrite
├── tests/                       # parity tests (pure-Python, no network)
├── CHANGELOG-VENDOR.md          # divergence log from the source library
└── README.md
```

### Lifecycle / data flow

1. **Vendor time (maintainer):** `scripts/vendor.py --source <path> --ref <tag>` copies the 31
   `skill/` dirs → `skills/`, `agents/` → `agents/`, and the engine → `engine/`. It rewrites PLB
   skill bodies (see below) and writes `VENDOR_LOCK.json`. Parity tests verify the result.
2. **Install (user):** `/plugin marketplace add <private-git-url>` → `/plugin install
   forge-foundry-claude`.
3. **First activation:** the SessionStart hook (`hooks/bootstrap.sh`) builds `engine/.venv` and
   `pip install -e engine` if absent or stale (stamped by vendor SHA). Fails soft if python is
   missing.
4. **Use:** skill-first primitives run in-session; the 23 PLB/hybrid primitives Bash-invoke
   `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" <name> …` → bundled venv.

### Component boundaries

- **`skills/` + `agents/`** — pure content, vendored verbatim except two deliberate transforms
  (PLB invocation-prefix rewrite, state-dir default localization).
- **`engine/`** — the deterministic Python, unchanged logic; only packaged differently.
- **`hooks/bootstrap.sh` + `bin/forge-validate`** — the environment seam (build once; resolve +
  fail loud).
- **`scripts/vendor.py` + `tests/`** — the maintenance seam that keeps the plugin honest.

## The vendor script (`scripts/vendor.py`)

Deterministic copy-and-transform from a pinned source checkout. Five stages, each testable.

### Stage 1 — Resolve the source

`--source <path>` + `--ref <tag>`. Records the exact source SHA to `CHANGELOG-VENDOR.md` and
`engine/VENDOR_LOCK.json` (`{source_repo, ref, sha, vendored_at, primitive_count, version}`).
Refuses a dirty source tree unless `--allow-dirty`. Parity tests assert the lock matches
checked-in state.

### Stage 2 — Copy skills + agents

For each of the 31 `primitives/<name>/`:
- `skill/SKILL.md` → `skills/<slug>/SKILL.md` (slug = frontmatter `name`)
- `skill/references/**`, `skill/scripts/**` → under `skills/<slug>/`
- `agents/*.agent.yaml` → `agents/<name>.md` (reuse the source emitter's `emit_agent()` to
  convert canonical agent YAML → Claude agent format)

Symlinks skipped (SEC-10). The `/autowork` + `/review-pr` repo-dev commands are **excluded** by
an explicit denylist (they are maintainer tools, not primitives).

### Stage 3 — Copy the engine

- `forge_validate/**` → `engine/forge_validate/**`
- `primitives/_shared/**` → `engine/primitives/_shared/**`
- each hybrid/PLB `primitives/<name>/shared/**` → `engine/primitives/<name>/shared/**` (incl. M0
  `legacy_modernization/shared/report_aggregator.py` carve-out)
- generate `engine/primitives/<name>/__init__.py` where needed so imports resolve
- generate `engine/pyproject.toml`: `[project.scripts] forge-validate = "forge_validate.cli:main"`,
  dependencies pinned to the source's locked versions (read from source `uv.lock`)

### Stage 4 — PLB path-rewrite (the crux)

Source PLB/hybrid SKILL.md bodies invoke:

```bash
uv run --project "${foundry_project:-$(pwd)}" forge-validate data-flow-map \
  --state-dir "$state_dir" --continue --judgments round-1-judgments.json "$target"
```

The rewrite swaps only the command **prefix**, leaving all flags/args untouched:

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" data-flow-map \
  --state-dir "$state_dir" --continue --judgments round-1-judgments.json "$target"
```

Safety properties:
- **Precise:** regex anchored on `uv run --project <…> forge-validate`, applied only inside
  fenced ```bash blocks; can't touch prose.
- **Idempotent:** an already-rewritten body (detected by the `${CLAUDE_PLUGIN_ROOT}` marker) is
  skipped — re-vendoring a partial tree is safe.
- **Auditable:** emits a per-primitive rewritten-count; parity tests assert **zero** residual
  `uv run … forge-validate` in `skills/**` and **≥1** rewrite per PLB slug.
- **State-dir localization:** the default `.foundry/context/...` is rewritten to a project-local
  `.forge-foundry/context/...` so run artifacts land in the **user's working project**, not the
  shared (read-only-ish) plugin cache. Documented divergence.

### Stage 5 — Resolver shim

The rewrite points at a generated `bin/forge-validate` wrapper:

```bash
#!/usr/bin/env bash
VENV="${CLAUDE_PLUGIN_ROOT}/engine/.venv"
if [ ! -x "$VENV/bin/forge-validate" ]; then
  echo "forge-foundry-claude: engine not bootstrapped. The SessionStart hook builds it on first activation; if you see this, run: bash \"${CLAUDE_PLUGIN_ROOT}/hooks/bootstrap.sh\"" >&2
  exit 69   # EX_UNAVAILABLE — distinct from the primitives' 0-7 vocabulary
fi
exec "$VENV/bin/forge-validate" "$@"
```

Single point where "engine not ready" is detected with an actionable message. Exit `69` cannot
collide with the primitives' documented `0–7` exit codes.

## Bootstrap hook (`hooks/bootstrap.sh`)

Wired to `SessionStart` via `plugin.json`. Idempotent, one-time, fails soft:

- **Fast path:** a built venv stamped with the current vendor SHA (`$VENV/.forge-vendor-sha ==
  VENDOR_LOCK.sha`) exits in ms. A re-vendor changes the SHA → automatic rebuild next session, so
  users never run a stale engine against new skills.
- **Python discovery:** prefers `python3.11`, else `python3`; verifies `>= 3.11`. If missing/old,
  prints a warning and `exit 0` (never blocks the session).
- **Build:** `python -m venv engine/.venv` then `engine/.venv/bin/pip install -e engine`
  (editable → console scripts + clean imports). Stamps the SHA.
- The only hard error a user sees is when they **invoke a PLB skill** before the venv exists —
  surfaced by the shim (exit 69), not the hook.

## Manifests (hand-authored)

`.claude-plugin/plugin.json` — name, description, `version` (hand-bumped), author, `license:
UNLICENSED`, keywords, and the `SessionStart` hook block pointing at
`${CLAUDE_PLUGIN_ROOT}/hooks/bootstrap.sh`.

`.claude-plugin/marketplace.json` — private single-plugin marketplace (`name: forge-foundry`,
one plugin entry `source: ./`). Private repo + `UNLICENSED` enforces "no public marketplace."

`version` appears in three places (plugin.json, marketplace.json, VENDOR_LOCK.json) and a parity
test asserts they agree.

## Parity tests (`tests/`)

| Test | Asserts |
|------|---------|
| `test_all_skills_present` | exactly 31 `skills/<slug>/SKILL.md`; slugs match expected set; frontmatter `name` == dir name |
| `test_no_dev_commands_vendored` | `autowork`/`review-pr` absent from `skills/` |
| `test_plb_paths_rewritten` | zero `uv run … forge-validate` in `skills/**`; each PLB slug has ≥1 `${CLAUDE_PLUGIN_ROOT}/bin/forge-validate` |
| `test_state_dir_localized` | no `.foundry/context` defaults remain in vendored bodies |
| `test_engine_imports` | `import forge_validate.cli` + a sample `from primitives.data_flow_map.shared import …` succeed against `engine/` |
| `test_cli_registrations` | every PLB/hybrid slug is a registered `forge-validate` subcommand |
| `test_manifests_valid` | `plugin.json` + `marketplace.json` parse; versions agree with `VENDOR_LOCK.json` |
| `test_vendor_lock_matches` | `VENDOR_LOCK.json` SHA equals checked-in state |
| `test_shim_and_hook_executable` | `bin/forge-validate` + `hooks/bootstrap.sh` exist, `+x`; shim exits 69 when venv absent |

A green suite means: copy complete, path rewrites total, engine imports, manifests coherent.

## Error handling (end to end)

- **No/old python:** hook warns + exit 0; skill-first primitives work; PLB skills → shim exit 69
  with install/bootstrap guidance.
- **Bootstrap not yet run / mid-build:** shim exit 69, actionable message.
- **Re-vendor changed engine:** SHA-stamp mismatch → hook silently rebuilds next session.
- **Primitive exit codes 0–7:** pass through untouched; shim's 69 never collides.

## Verification strategy

Automated as far as possible; one manual gate:

- **Automated:** parity suite green; `engine/` imports; a scratch-dir venv bootstrap succeeds;
  shim exit-69 behavior; headless `forge-validate <name>` smoke runs on fixtures for 2–3 PLB
  primitives (e.g. `repo-digest`, `data-flow-map`, `code-review`).
- **Manual (user):** real `/plugin marketplace add <private-git-url>` → `/plugin install
  forge-foundry-claude` → invoke a skill and confirm it runs. (A plugin genuinely installing +
  activating in Claude Code can only be observed, not asserted headlessly.)

## Non-goals (YAGNI)

- **No public marketplace publishing** — private channel only.
- **No new emitter target** — the plugin is produced by a standalone vendor script, not by adding
  a `claude_plugin` target to `forge_sync` (chosen: hand-authored/thin + vendor-copy).
- **No behavior fork of the engine** — logic is vendored verbatim; only the invocation prefix and
  state-dir default diverge (both logged in CHANGELOG-VENDOR.md).
- **No `uv` runtime dependency** — bundled venv + `python -m` instead.
- **No `/autowork` or `/review-pr`** — maintainer tools, excluded.
- **S61 / S12 not in first release** — picked up on a later re-vendor once built upstream.

## Open questions for the plan

1. **Agent conversion reuse** — confirm the source `emit_agent()` can be imported and called
   standalone by `vendor.py` (it lives in `forge_sync/emitter/agents.py`), or whether the vendor
   script vendors that emitter helper too. (Lean: import the source emitter during vendoring — the
   vendor script runs with the source repo on `sys.path`.)
2. **Engine dependency pinning** — read exact pins from source `uv.lock` vs. loosen to ranges in
   `engine/pyproject.toml`. (Lean: pin to source-locked versions for reproducibility.)
3. **rules/** — no primitive currently ships `rules/`; the vendor script should handle them if
   present (future-proof) but not require them.
