# forge-foundry-claude

Forge Foundry engineering workbench, packaged as a **private** Claude Code plugin.
All 31 primitives (spec, review, security, modernization, SDET) ship as Claude Code
skills, backed by a bundled deterministic Python engine for the Python-load-bearing
primitives. It is built by vendoring
[`forge-foundry-primitives`](https://github.com/arula-ai/forge-foundry-primitives)
at a pinned tag — the source library remains the single source of truth.

## What you get

- **31 skills** — invoke them as slash commands (e.g. `/spec-write`, `/code-review`,
  `/security-review`, `/repo-digest`, the `mod-*` modernization workbench, the SDET
  scaffolders). Skill-first primitives run entirely in-session; Python-load-bearing (PLB)
  primitives delegate to the bundled `forge-validate` engine.
- **A self-contained engine** — no `uv` runtime dependency. The plugin bundles the
  deterministic Python engine and builds a one-time virtualenv on first use.

## Install

From within Claude Code:

```
/plugin marketplace add https://github.com/arula-ai/forge-foundry-claude
/plugin install forge-foundry-claude
```

The repository is **private** — `/plugin marketplace add` requires that your Git
credentials have read access to `arula-ai/forge-foundry-claude`. Distribution is
private-only; there is no public Claude marketplace listing.

## First-run bootstrap (one-time)

The plugin ships a `SessionStart` hook (`hooks/bootstrap.sh`) that builds a bundled
Python virtualenv under the plugin directory (`engine/.venv`) the first time a session
starts. **Requirements:** Python 3.11 or newer on `PATH`. The venv is built once and
reused on subsequent sessions (a vendor-SHA stamp skips the rebuild), with no `uv`
runtime dependency.

- **Skill-first primitives work immediately** — they run entirely in-session and need
  nothing from the venv.
- **The 23 PLB / hybrid primitives need the venv** — they resolve `forge-validate`
  through the `${CLAUDE_PLUGIN_ROOT}/bin/forge-validate` shim into the bundled venv, so
  they only work once bootstrap has completed.

## Troubleshooting

- **"engine not bootstrapped" / a PLB command exits `69`.** The bundled venv has not
  been built yet (or was removed). The `SessionStart` hook normally builds it on first
  activation; if you hit this, run the bootstrap manually:

  ```bash
  bash "${CLAUDE_PLUGIN_ROOT}/hooks/bootstrap.sh"
  ```

  Exit code `69` (`EX_UNAVAILABLE`) is the shim's dedicated "engine not bootstrapped"
  signal and never collides with the primitives' own `0–7` exit codes.

- **"python3.11+ not found."** The bootstrap requires Python 3.11 or newer on `PATH`.
  Install Python 3.11+ (e.g. via your OS package manager, `pyenv`, or python.org), then
  re-run the bootstrap command above. Skill-first primitives keep working without it.

- **Where run artifacts land.** PLB primitives write their output under
  `.forge-foundry/context/` in **your working project** (not the shared plugin cache),
  so artifacts are scoped to the repo you invoke them from.

- **Re-vendoring (maintainers).** To refresh the vendored trees from a new source tag:

  ```bash
  python3 scripts/vendor.py --source <path-to-forge-foundry-primitives> --ref <tag>
  ```

## How it's built

- Vendored from
  [`forge-foundry-primitives`](https://github.com/arula-ai/forge-foundry-primitives)
  **@ v5.0.2** (the pinned source ref recorded in
  [`engine/VENDOR_LOCK.json`](engine/VENDOR_LOCK.json)); deliberate divergences are
  logged in [`CHANGELOG-VENDOR.md`](CHANGELOG-VENDOR.md).
- **31 primitives** ship as skills, backed by the bundled deterministic Python engine.
- **Parity-tested** — the `tests/` suite verifies the vendored skills, engine, and
  manifests match the source (skill count, PLB rewrites, state-dir localization, version
  agreement, and a headless PLB smoke run).

## Re-vendoring from the source library

The `skills/`, `agents/`, and `engine/` trees are generated, not hand-edited. To
refresh them from a new tag of the source library:

```bash
python3 scripts/vendor.py --source /path/to/forge-foundry-primitives \
  --ref v5.0.2 [--version 0.1.0]
```

This locks the source (ref + SHA into `engine/VENDOR_LOCK.json`), copies the skills
and agents, copies the engine, rewrites PLB invocations to the bundled shim, and
patches the `version` field in both manifests. Deliberate divergences from the source
library are recorded in [`CHANGELOG-VENDOR.md`](CHANGELOG-VENDOR.md).
