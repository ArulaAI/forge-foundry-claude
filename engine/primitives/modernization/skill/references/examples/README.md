# /mod-modernization — config & invocation examples

`/mod-modernization` is the app-mod **orchestrator**: it drives the analyzers
(functional-map → db-schema-analyze → api-contract-review → app-analysis) then the
scope synthesizer, and rolls up a summary. Run it in your agent session from the
legacy repo root. Everything is resumable via `--run-id`.

## Where each config lives

| Config | Location | Purpose |
|--------|----------|---------|
| `repos.yaml` | `<project>/.foundry/repos.yaml` | Multi-repo correlation (ui/api/db/iac). Examples under [`mod-modernization-scope`](../../../modernization_scope/skill/references/examples/). |
| `--target KEY=VALUE` | CLI flag (repeatable) | Design toward a stack. Examples under [`mod-modernization-scope`](../../../modernization_scope/skill/references/examples/). |
| `.foundry/hla/*.md` | `<project>/.foundry/hla/` | Stated target architecture → Architecture Conformance section. See [`hla/STATED-ARCHITECTURE.example.md`](hla/STATED-ARCHITECTURE.example.md). |

## Invocation recipes

| File | When to use |
|------|-------------|
| [`run.multi-repo-with-target.md`](run.multi-repo-with-target.md) | Whole-app run over a `repos.yaml` split, designed toward a target stack. |
| [`run.twin-repo.md`](run.twin-repo.md) | Legacy + in-progress-rewrite of the same app (auto what's-left). |
| [`run.feature-scoped.md`](run.feature-scoped.md) | Modernize one plain-English feature slice at a time. |

> All inputs are additive — a bare `/mod-modernization . --run-id run-1` is a
> descriptive whole-app run.
