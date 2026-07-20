# Twin-repo delta — a legacy monolith AND an in-progress rewrite of the SAME app

Use this when a rewrite is already underway: you have the original legacy repo
and a newer target repo that has rebuilt *some* of the app. Point the chain at
**both** and it auto-derives what's-left-to-modernize at endpoint/entity
granularity — no manual `--modernized` list.

```bash
# --code-root points at the LEGACY source (that's what gets deconstructed).
"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-modernization-scope \
  --code-root ../legacy/src \
  --legacy ../legacy \
  --twin-target ../rewrite \
  --extract-only
```

Or via the orchestrator (drives analyzers → scope end-to-end, same flags):

```bash
/mod-modernization . --run-id run-1 --legacy ../legacy --twin-target ../rewrite
```

Rules:
- **Both-or-neither:** `--legacy` and `--twin-target` must be supplied together.
  Absent both, the run is byte-identical to a normal single-repo run.
- Both paths are SEC-7 validated (no leading `-`; a `../sibling` is allowed when
  it resolves under the trusted siblings root).

What it produces (in addition to the usual package):
- **`migration-delta.json`** — the auditable record: each legacy bounded context
  paired to its rewrite match, and every legacy endpoint/entity classified
  `present-complete` (rebuilt) or `absent` (not migrated yet). Every legacy
  endpoint is accounted for exactly once — no silent drop.
- A **"## What's Left"** section on `MODERNIZATION.md`: a fully-rebuilt context
  demotes to a **Boundary Contract**; a partially-rebuilt one is scoped IN as a
  residual ("3 of 7 endpoints remain"); a not-started one is specced in full.

Composes with `--feature` / `--context` (scope the delta to one slice) and
`--target` (design the residual toward a chosen stack).
