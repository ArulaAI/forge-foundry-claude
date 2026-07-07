---
name: test-scaffold
description: >
  Full-cone test scaffolder. Consumes an S1 sdet-architect test-architecture.md
  (strategy + per-layer tooling picks + cone shape) and writes RUNNABLE,
  collectable-green test scaffolding for the whole cone — unit (delegating to S3
  test-from-spec), integration, container (Testcontainers/docker), contract
  (Pact / schema-driven, composing S40), API e2e (behavioural request->assert
  journeys), and UI e2e (Playwright) — across Python / JS-TS / Java, with a
  register_scaffold seam for other stacks. Follows SDET best practice: the
  inverted snow cone (integration/contract/API-heavy, unit-sparing) is the
  default for service/API repos, overridable by an explicit S1 pyramid. Use when
  the user asks to "scaffold the tests", "set up Playwright / Testcontainers /
  contract tests", "build the integration test harness", "create an e2e project",
  or "materialize the S1 test architecture into runnable tests".
allowed-tools: Read, Grep, Glob, Bash
---

# /test-scaffold

Materialize an S1 test architecture into runnable test scaffolding across the whole cone. This is a
**Python-load-bearing** primitive: the deterministic generators live in `shared/` and this body
delegates to `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" test-scaffold`. It never authors exhaustive suites — it seeds
collectable-green skeletons the engineer extends.

## When to invoke

- The user wants runnable test scaffolding (not just a strategy doc): a Playwright project, a
  Testcontainers/integration harness, contract tests, API-e2e journeys, or all of the above.
- After running S1 `/sdet-architect` — to materialize its `test-architecture.md` into real test files.
- When a repo has a service/API surface and needs the inverted-snow-cone layers (integration / contract
  / API) actually scaffolded, not just recommended.

Do **not** invoke for: deciding the test strategy (that is S1), generating unit tests from a spec in
isolation (that is S3 `/test-from-spec`, which this primitive composes for the unit layer), or
maintaining tests when code changes (that is S2 `/test-maintainer`).

## When invoked

1. **Resolve the project + architecture.** Take the project root (default the cwd) and, if the user
   produced one, the S1 `test-architecture.md` (or `s1-scaffold-plan.json`). Identify which cone layers
   to scaffold (default: the cone-enabled set) and any `--layers` subset the user asked for.
2. **Run the scaffolder (delegated).** Invoke `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" test-scaffold <path> [--from-architecture
   FILE] [--layers ...] [--cone-shape inverted|pyramid]`. The CLI detects the stack, resolves the cone
   shape (inverted snow cone by default for service/API repos), and runs the per-layer renderers,
   writing the scaffold idempotently. It never overwrites an existing test setup.
3. **Supply model fill-ins if requested.** When a layer renderer needs a stack-specific value the
   templates cannot know deterministically (service base-URL for e2e, the container image tag, the
   contract provider name, the representative API journey), the CLI emits a prompts file and exits
   awaiting judgments. Provide those values and re-invoke with `--continue --judgments <file>`.
4. **Report.** Read the scaffold report (stack, cone shape, per-layer written/skipped counts, warnings),
   summarise what was written for each layer, and point the user at the dormant CI overlays they can opt
   into. Note any layer marked `unsupported` (no renderer for the stack — the `register_scaffold` seam
   covers it), `no-stack` (an unrecognised stack — exit 2), or `errored` (a renderer raised — the
   "Errored layers" report section carries the message; the run still completed and the other layers
   still scaffolded, so report it but don't treat it as a total failure).

## How to invoke

Invoke as `/test-scaffold` with the project root. The body never scaffolds in-session — it shells
`"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" test-scaffold` and loops on the awaiting-judgments protocol only when a renderer needs a
fill-in. Useful flags: `--from-architecture FILE` (the S1 architecture), `--layers
unit,integration,container,contract,api-e2e,ui-e2e` (a subset), `--cone-shape inverted|pyramid`
(override), `--output FILE`. The full surface and the per-layer artifact set are in `SPEC.md` and the
design spec.

## Output

A scaffold report plus the runnable test files written under the project root (collectable-green,
idempotent). Exit `0` on success; `2` on an input error (bad path, unrecognised stack, malformed
`--layers`).

## Invariants

- **Idempotent** — never overwrites an existing file (augment or skip).
- **Containment-guarded** — every emitted file is path-validated under the output root.
- **Never modifies production source.**
- **Composes, never duplicates** — the unit layer delegates to S3; the contract layer composes S40.
- **SDET best practice** — the inverted snow cone is the default for service/API repos; an explicit S1
  pyramid overrides it.
