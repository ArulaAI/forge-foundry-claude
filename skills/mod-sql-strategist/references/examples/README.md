# mod-sql-strategist (M3) — config examples

Config for `/mod-sql-strategist` (the SQL-database modernization strategist that
turns M1 schema + M2 stored-proc digests into a decomposition plan).

| File | When to use |
|------|-------------|
| [`constraints.yaml`](constraints.yaml) | Optional `--constraints` — team size, latency budget, data classification, forbidden tech. Biases the sequencer's `deferred` list + the narrative. |

## `--target-paradigm`

Also shapes the plan (not a file — a flag): `--target-paradigm {microservices,modular-monolith,batch-platform}` (default `microservices`). It steers the Stage C sequencer heuristics and the Stage F diagrams.

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-sql-strategist \
  --context-dir .foundry/context/db \
  --target-paradigm modular-monolith \
  --constraints ./constraints.yaml \
  --extract-only --state-dir .state
```

> **Compatibility note.** `--constraints` and its constraint families
> (team-size / latency-budget / data-classification / forbidden-tech) are defined
> by the strategist SPEC/USAGE. Confirm the flag against your installed CLI
> version (`"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-sql-strategist --help`) before relying on it, and
> treat the exact key names here as the SPEC's documented shape.
