# Vendor Changelog — forge-foundry-claude

Divergences from `forge-foundry-primitives` (source of truth). Every re-vendor updates
`engine/VENDOR_LOCK.json` with the source ref + SHA.

## Deliberate divergences

1. **PLB invocation prefix.** Source skill bodies invoke `forge-validate <slug>` bare
   (only `work_orchestrator` wraps it as `uv run forge-validate <slug>`), and some
   references invoke `python -m primitives.<…>`. The vendor rewrite targets both the bare
   and the occasional `uv run` `forge-validate <slug>` shapes plus the `python -m
   primitives.<…>` shapes, rewriting them to `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate"
   <slug>` (resolver shim → bundled venv) and the bundled engine-venv python
   respectively. Rationale: no `uv` runtime dependency; self-contained.
2. **Excluded commands.** `/autowork` and `/review-pr` (maintainer tools) are not vendored.

> **Considered and rejected:** localizing the state dir from `.foundry/context/…` to
> `.forge-foundry/context/…`. The engine's Python defaults and every cross-primitive read
> are already CWD-relative and consistent on `.foundry/context`, so run artifacts already
> land in the user's working project. Rewriting only the docs created a path mismatch that
> broke the M-tier chain; the rewrite was reverted and the vendored skills keep
> `.foundry/context`, matching the engine.

## Vendor history

<!-- vendor.py appends one line per re-vendor: <date> <ref>@<sha12> — <primitive_count> primitives -->
- 2026-07-07T12:22:05Z v5.0.2@0f5f563588b2 — 31 primitives
