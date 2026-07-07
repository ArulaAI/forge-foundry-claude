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
/plugin marketplace add <private-git-url>
/plugin install forge-foundry-claude
```

(Replace `<private-git-url>` with the URL of this repository — distribution is
private-only, there is no public marketplace listing.)

## Bootstrap (one-time)

The plugin ships a `SessionStart` hook (`hooks/bootstrap.sh`) that builds a bundled
virtualenv for the engine the first time a session starts. **Requirements:** Python
3.11 or newer on `PATH`. The venv is built once under the plugin root and reused on
subsequent sessions; PLB skills resolve `forge-validate` through the
`${CLAUDE_PLUGIN_ROOT}/bin/forge-validate` shim into that venv.

## Re-vendoring from the source library

The `skills/`, `agents/`, and `engine/` trees are generated, not hand-edited. To
refresh them from a new tag of the source library:

```bash
python scripts/vendor.py --source /path/to/forge-foundry-primitives \
  --ref v5.0.0 [--version 0.1.0]
```

This locks the source (ref + SHA into `engine/VENDOR_LOCK.json`), copies the skills
and agents, copies the engine, rewrites PLB invocations to the bundled shim, and
patches the `version` field in both manifests. Deliberate divergences from the source
library are recorded in [`CHANGELOG-VENDOR.md`](CHANGELOG-VENDOR.md).
