# /mod-modernization — multi-repo run designed toward a target stack

The orchestrator drives the analyzers (functional-map → db-schema-analyze →
api-contract-review → app-analysis) then the scope synthesizer, end-to-end. Run it
from the project root in your agent session.

## Setup

1. Wire the repos in `<project>/.foundry/repos.yaml` — see the scope examples:
   `../modernization_scope/references/examples/repos.multi-repo.yaml`.
2. (Optional) Drop stated-architecture docs in `<project>/.foundry/hla/` — see
   `hla/STATED-ARCHITECTURE.example.md`.

## Run

```text
/mod-modernization . --run-id run-1 \
  --target frontend=react --target backend=spring-boot \
  --target database=postgres --target cloud=aws
```

- `.` is the legacy repo root (the orchestrator finds `repos.yaml` under it).
- `--run-id` makes the run resumable — re-invoke with the same id to resume.
- `--target` (repeatable) declares the stack to design toward; omit for a purely
  descriptive run. Target dimensions + more examples:
  `../modernization_scope/references/examples/target-stack.spring-eks.md`.

Everything lands under `.foundry/context/` (analyzers) and
`.foundry/context/modernization/` (the synthesized package: `MODERNIZATION.md`,
per-context specs + BRDs, ADRs, `ROADMAP.md`).
