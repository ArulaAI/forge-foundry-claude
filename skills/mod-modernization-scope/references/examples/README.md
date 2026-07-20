# mod-modernization-scope — config examples

Copy-pasteable config files and invocation recipes for `/mod-modernization-scope`
(and the `/mod-modernization` orchestrator that drives it). Grab a file, adapt the
paths/values, drop it where noted.

## `.foundry/repos.yaml` — multi-repo correlation

Place at `<project>/.foundry/repos.yaml`. The chain correlates the listed repos
into one system-map. `role` must equal the map key; `../sibling` paths are the
idiomatic form.

| File | When to use |
|------|-------------|
| [`repos.multi-repo.yaml`](repos.multi-repo.yaml) | Full 4-tier enterprise split (ui/api/db/iac), sibling paths. |
| [`repos.db-plus-api.yaml`](repos.db-plus-api.yaml) | Headless batch/DB system — just `api` + `db`, no UI/IaC. |
| [`repos.with-state-hint.yaml`](repos.with-state-hint.yaml) | Add the optional `state:` (legacy/partial/modern) untrusted-prior hint. |
| [`repos.allowed-alt-roots.yaml`](repos.allowed-alt-roots.yaml) | Repos parked outside the siblings tree — opt in via `allowed_alt_roots:`. |

## `--target` — design toward a modern stack

Repeatable `--target KEY=VALUE` flags (serialized to `target-stack.json` under
the run's state dir). Dimensions: `frontend`, `backend`, `database`, `api_style`,
`cloud`, `container`, `orchestration`.

| File | When to use |
|------|-------------|
| [`target-stack.spring-eks.md`](target-stack.spring-eks.md) | Spring Batch + REST + Oracle, containerized (Docker) for Amazon EKS. |
| [`target-stack.react-aws.md`](target-stack.react-aws.md) | React SPA + Spring Boot REST + PostgreSQL on AWS. |

## `--legacy` / `--twin-target` — twin-repo delta

| File | When to use |
|------|-------------|
| [`twin-repo.md`](twin-repo.md) | A legacy monolith + an in-progress rewrite of the SAME app — auto-derive what's-left. |

> All these inputs are **additive**: omit them and the run is byte-identical to a
> plain single-repo descriptive run.
