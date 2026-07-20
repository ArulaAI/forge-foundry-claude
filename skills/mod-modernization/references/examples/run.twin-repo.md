# /mod-modernization — twin-repo run (legacy + in-progress rewrite)

When a rewrite of the same app is already underway, point the orchestrator at both
repos and it auto-derives what's-left-to-modernize at endpoint/entity granularity.

```text
/mod-modernization . --run-id run-1 \
  --legacy ../legacy \
  --twin-target ../rewrite
```

- `.` / the positional is the legacy repo (what gets deconstructed).
- `--legacy` and `--twin-target` are **both-or-neither**; both are SEC-7 validated.
- Absent both, the run is byte-identical to a normal single-repo run.

Produces `migration-delta.json` (auditable per-endpoint present-complete/absent
record) + a "## What's Left" section on `MODERNIZATION.md`. Full contract +
what each disposition means:
`../modernization_scope/references/examples/twin-repo.md`.

Composes with `--target` (design the residual toward a stack) and `--feature` /
`--context` (scope the delta to one slice).
