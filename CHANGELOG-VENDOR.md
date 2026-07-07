# Vendor Changelog — forge-foundry-claude

Divergences from `forge-foundry-primitives` (source of truth). Every re-vendor updates
`engine/VENDOR_LOCK.json` with the source ref + SHA.

## Deliberate divergences

1. **PLB invocation prefix.** Source skill bodies invoke `uv run --project "<foundry>"
   forge-validate <name>`. Vendored bodies invoke `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate"
   <name>` (resolver shim → bundled venv). Rationale: no `uv` runtime dependency; self-contained.
2. **State-dir default.** `.foundry/context/…` → `.forge-foundry/context/…` so run artifacts land
   in the user's working project, not the shared plugin cache.
3. **Excluded commands.** `/autowork` and `/review-pr` (maintainer tools) are not vendored.

## Vendor history

<!-- vendor.py appends one line per re-vendor: <date> <ref>@<sha12> <primitive_count> primitives -->
