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
- 2026-07-16T02:52:49Z v8.5.1@59daf6457ff4 — 38 primitives
- 2026-07-16T03:43:42Z v8.5.2@7b7b7717ca12 — 38 primitives
- 2026-07-16T13:40:56Z v8.5.3@a05b31c32840 — 38 primitives
- 2026-07-16T16:00:51Z v8.5.4@dc989a570e50 — 38 primitives
- 2026-07-16T18:12:37Z v8.6.0@a1be5ce26fe8 — 38 primitives
- 2026-07-20T13:13:53Z v8.7.0@6db94c1c4950 — 38 primitives
- 2026-07-20T14:20:13Z v8.7.1@b6a5269106d3 — 38 primitives
