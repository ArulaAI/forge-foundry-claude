# Target stack — Spring Batch + REST + Oracle, containerized for Amazon EKS

Pass a multi-dimensional target with repeatable `--target KEY=VALUE` flags. Every
value below is in the bundled offline registry, so each resolves to a clean label
with **no** "unknown target" warning (versionless values like `rest`/`eks`/`docker`
resolve to a label with no version, which is expected).

```bash
/mod-modernization . --run-id run-eks \
  --target backend=spring-batch \
  --target api_style=rest \
  --target database=oracle \
  --target cloud=aws \
  --target container=docker \
  --target orchestration=eks
```

Or on the scope CLI directly (writes `target-stack.json` under `<state-dir>/<run-id>/`):

```bash
forge-validate mod-modernization-scope --code-root . \
  --target backend=spring-batch --target api_style=rest --target database=oracle \
  --target cloud=aws --target container=docker --target orchestration=eks \
  --extract-only
```

Resolved labels:

| Dimension | Value | Label |
|-----------|-------|-------|
| backend | spring-batch | Spring Batch 5.2 |
| api_style | rest | REST |
| database | oracle | Oracle 23ai |
| cloud | aws | AWS |
| container | docker | Docker |
| orchestration | eks | Amazon EKS |

With a target set, each spec renders a §8b Target REST API surface designed toward
Spring Batch, and the roadmap/ADRs sequence toward the stack. Omit `--target`
entirely for byte-identical *descriptive* output.

> Pin a specific version with `value@version` (e.g. `--target backend=spring-boot@3.2`);
> omit `@version` to take the registry's latest-known. Any value NOT in the registry
> is carried verbatim with a "verify at implementation time" warning — you are never
> blocked from naming a target.
