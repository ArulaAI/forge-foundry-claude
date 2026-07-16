---
name: mod-sp-converter
description: >
  Convert one legacy stored procedure to a modern target framework on top of the
  M1 schema digest + M2 stored-proc digest: resolve the SP profile + its tables,
  synthesise a per-target migration plan (data deps, transaction-boundary
  translation, cursor→stream, side-effect mappings, commit sequence, test plan,
  gotchas), render the chosen target skeleton, emit a deterministic `plan.md` and
  a T-shirt-sized effort estimate. The five-stage conversion pipeline is
  deterministic Python (the single LLM call is the per-target plan synthesis);
  this body delegates to `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-sp-converter` rather than
  reproducing it. Use when the user asks to "convert a stored procedure",
  "generate a migration scaffold for an SP", "port PL/SQL / T-SQL / DB2 SQL PL to
  Spring Batch / FastAPI", or "build the M4 plan".
allowed-tools: Read, Bash
---

# /mod-sp-converter

Convert one legacy stored procedure to a modern target framework — by delegating
to the deterministic `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-sp-converter` CLI. The SP/table
resolution (Stage A), skeleton rendering (Stage C), `plan.md` rendering (Stage
D), and effort estimate (Stage E) are Python load-bearing logic in
`primitives/sp_converter/shared/`; the single LLM call is the per-target plan
synthesis (Stage B), guarded against hallucinated table/side-effect names. This
body resolves the context dir + `--sp` + `--target`, invokes the CLI, reads what
it wrote, and acts on the exit code. It does **not** reproduce the conversion
pipeline in-session. M4 is the second M-tier *Generative* primitive — it emits a
per-SP conversion plan plus optional starter scaffold that a human engineer fills
in from `plan.md` and the explicit `// TODO(<sp>)` / `# TODO(<sp>)` markers in
each scaffold file. M4 generates skeletons and plans, **not** finished
implementations.

## When to invoke

- A modernization engagement has produced M1 + M2 digests and the engineer needs
  to start rewriting the highest-risk stored procedures one at a time.
- A reviewer wants the per-SP migration plan + framework scaffold before a
  code-gen primitive is unleashed against the legacy database.
- The team needs an effort-sized starting point (T-shirt size S / M / L / XL with
  rationale) for one SP so they can sequence the rewrite and budget the work.
- When the user asks to "convert a stored procedure", "generate a migration
  scaffold for an SP", "port PL/SQL / T-SQL / DB2 SQL PL to Spring Batch /
  FastAPI", or "build the M4 plan".

## When invoked

1. **Verify the upstream M1/M2 digests are present, then pick the SP + target.**
   Resolve the context dir (default `.foundry/context/db/`). Confirm `schema.json`
   (M1) and `stored-procs.json` (M2) both exist there. If either is missing, stop
   and tell the user to run `/mod-db-schema-analyze` then `/mod-stored-proc-analyze`
   first — a missing upstream digest is a hard stop (the CLI exits 2). Map the
   user's SP to `--sp` (case-sensitive); if the user did not name one, suggest the
   **highest-risk SP** from the M2 summary and confirm before proceeding. Map the
   user's framework choice to `--target` (default `plan-only`).
2. **Run the extract phase.** Invoke via Bash:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-sp-converter --extract-only \
     --sp <NAME> --target <TARGET> --context-dir <dir> --output-dir <dir>
   ```
   This runs Stage A (SP/table resolution) deterministically, saves intermediate
   state under `<dir>/.state/`, writes `round-1-prompts.json` to that state
   directory (the CLI prints `WROTE <path>` for it), and exits `7`.
3. **If exit 7 — perform the in-session judgment round.** Read `round-1-prompts.json`
   from the path printed on stdout. For each item in `items`:
   - Read the `system` and `user` fields. They describe a single stored procedure
     requiring a per-target migration plan (Stage B).
   - Respond with a JSON object matching `response_format.schema` — the
     `ConversionPlan` structure. **Every list field below is a JSON array of
     strings**, even when it holds a single note — a bare string is salvaged into
     a one-element list with a stderr WARNING, so always emit an array:
     `data_deps` (array of M1 table names this SP touches), `txn_boundaries`
     (array of transaction-boundary translation notes — one string per note,
     including any cursor→stream translation; **not a single string**),
     `side_effect_mappings` (object keyed by the M2 side effects the SP actually
     carries, each mapping to a string note), `commit_sequence` (array of ordered
     steps), `test_plan` (array of top test cases), and `gotchas` (array of
     migration risk notes). Do **not** invent table names, column names, or SP
     names not present in the prompt's entity list.
   - Collect all responses into a `JudgmentEnvelope` and write it to
     `round-1-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "mod-sp-converter",
       "round": 1,
       "items": [
         {"id": "<item.id>", "response": { ... ConversionPlan fields ... }},
         ...
       ]
     }
     ```
4. **Run the continue phase.** Invoke via Bash:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-sp-converter --continue \
     --judgments <state-dir>/round-1-judgments.json \
     --sp <NAME> --target <TARGET> --context-dir <dir> --output-dir <dir>
   ```
   This applies the conversion plan (Stage B), renders the skeleton (Stage C),
   renders `plan.md` (Stage D), computes the effort estimate (Stage E), runs the
   anti-hallucination guard, and writes the final artifacts. The CLI prints
   `WROTE <path>` for each artifact.
5. **Read what the CLI wrote and summarize.** On exit `0` read `plan.md` and
   `conversion-plan.json` and summarize for the user: the chosen target, the T-shirt
   effort size + its dominant factor, the transaction-boundary and cursor→stream
   notes, the side-effect mapping table, the top test cases, and a walk of each
   scaffold file's role and its `TODO(<sp>)` markers. Recommend the next
   highest-risk SPs as the following conversion candidates (skip this when M4 runs
   inside the M0 `/mod-sql-modernization` orchestrator — M0 owns the top-N loop).
6. **Non-zero exit (not 7) is a hard stop.** Surface the CLI's stderr and do not
   fabricate a plan. See the exit-code contract under **Invariants** below.

## How to invoke

Invoke as `/mod-sp-converter`. Options map to CLI flags: `--sp NAME` (the SP to
convert — required, case-sensitive, must match a `StoredProc.name` in the M2
digest; exits `2` if not found); `--target NAME` (the renderer target, default
`plan-only`; built-in: `plan-only`, `spring-batch`, `fastapi`; `celery` /
`spring-boot` ship via FLEX-3 entry point); `--context-dir PATH` (holds the M1
`schema.json` + M2 `stored-procs.json`, default `.foundry/context/db/`; a missing
upstream digest exits `2`); `--output-dir PATH` (per-SP conversion tree, default
`<context-dir>/conversions/<sp>/`). You delegate to `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-sp-converter`
— you do not perform the resolution, plan synthesis, skeleton rendering, notes
rendering, or effort estimation yourself.

### Skill-first mode (in-session, default)

Use `--extract-only` for the first pass, then `--continue --judgments <path>` after
writing `round-1-judgments.json` as described under **When invoked** above. Both
invocations accept the same `--sp`, `--target`, `--context-dir`, and `--output-dir`
flags.

### CI mode

CI runs the *same* skill-first loop: the emitted workflow (rendered from `ci.yaml`)
invokes the agent CLI, which loads this SKILL.md, runs `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-sp-converter
--extract-only`, produces the Stage B plan-synthesis judgments, and re-invokes with
`--continue --judgments <path>`. The agent *is* the harness — there is no separate
harness CLI flag.

## Output

The CLI writes, under `--output-dir` (default `<context-dir>/conversions/<sp>/`):
`conversion-plan.json` (the `ConversionPlan`, Pydantic v2, `extra="forbid"`,
schema-versioned `"1.0"`, with the nested `EffortEstimate`), `plan.md` (the
deterministic migration narrative — data deps, transaction boundaries, cursor &
side-effect translation, commit sequence, test plan, gotchas, and the per-axis
effort factors), and the per-target skeleton tree (e.g. `pom.xml` + `*.java` for
`spring-batch`, `pyproject.toml` + `app/routers/<sp>.py` for `fastapi`;
`plan-only` emits no scaffold). The authoritative output contract lives in
`## Invariants` below; the `ConversionPlan` JSON Schema is at
`references/schemas/conversion_plan.json`.

## Invariants

- **Built-in targets** (FLEX-3 `_TARGET_REGISTRY`): `plan-only` (always-available default,
  returns `[]`), `spring-batch`, `fastapi` ship in-tree; `celery`/`spring-boot` ship via
  FLEX-3 entry point. `--target` validates against the registered set.
- **FLEX-3 plug-in entry point**:
  `[project.entry-points."forge_foundry.renderers.sp_converter_targets"]`; plugin **wins
  on name collision** over same-named built-in.
- **Anti-hallucination guard**: `data_deps` filtered to resolved M1 `Table.name`;
  `side_effect_mappings` key filtered to side effects the M2 SP actually carries;
  LLM-introduced names dropped with stderr warning. Structurally-broken response degrades to
  empty-field plan (scaffold + effort estimate still written).
- **Effort estimate** (deterministic): T-shirt `size` (S/M/L/XL) = weighted sum of
  `risk_total` (M2 `RiskScore.total`), `table_touch` (M1 table-touch count), `body_loc` (M2
  line count), plus per-target adjustment — surfaced in `factors`. Calibration in
  `docs/m4-effort-calibration.md`.
- **§3.4 sub-call deferred to T1.5** — C4 `/code-review` on emitted scaffold not wired in T1.
- **Single-SP conversion only** — batch is M0 (`/mod-sql-modernization`).
- Exit codes: `0` success; `2` `--sp` not in M2 digest, required upstream digest (`schema.json` /
  `stored-procs.json`) missing or unparseable, or the `--continue` judgments envelope lacks the
  expected item; `7` `--extract-only` succeeded — prompts written, body must judge and call
  `--continue`. Exits other than `0` and `7` are hard stops.
- Slash command: `/mod-sp-converter` (`mod-` prefix, charter §3.2.M — distinct from S60
  `data-flow-map`).
