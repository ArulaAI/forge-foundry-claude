---
name: mod-modernization-strategist
description: >
  Synthesise an architectural decomposition plan on top of the M1 schema digest +
  M2 stored-proc digest: cluster bounded contexts, split owned vs shared tables,
  sequence a strangler-fig extraction order, detect modernization anti-patterns
  (cross-context FKs, distributed transactions, view-of-views chains, multi-context
  SPs), synthesise a `modernization-plan.md` narrative with decomposition /
  sequencing / ownership Mermaid views, and — optionally — fold an in-session C8
  `/architecture-review` pass into the risk section per charter §3.4. The decomposition pipeline is
  deterministic Python; this body delegates to `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-modernization-strategist`
  rather than reproducing it. Use when the user asks to "plan a modernization",
  "decompose a legacy monolith into bounded contexts", "build the M3 plan", "sequence
  a strangler-fig extraction", or "identify modernization anti-patterns".
allowed-tools: Read, Bash
delegates-to-cli: true
---

# /mod-modernization-strategist

Read the M1 `schema.json` + M2 `stored-procs.json` digests and synthesise an architectural
`DecompositionPlan` — by delegating to the deterministic `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate"
mod-modernization-strategist` CLI. The bounded-context clustering, ownership split,
strangler-fig sequencing, and antipattern detection (Stages A-D) are Python load-bearing
logic in `primitives/modernization_strategist/shared/`; the single LLM call is the
per-context narrative synthesis (Stage E), guarded against hallucinated names. This body
resolves the context dir, invokes the CLI, reads what it wrote, and acts on the exit code.
It does **not** reproduce the decomposition pipeline in-session. M3 is the first M-tier
*Generative* primitive — where M1 / M2 produce inventory facts, M3 synthesises a proposal
that downstream M4 (`/mod-sp-converter`) and C7 (`/work-orchestrator`) consume.

## When to invoke

- A modernization engagement has produced M1 + M2 digests and the engineer needs the
  architectural shape — bounded contexts, ownership conflicts, extraction order — before
  sequencing the rewrite.
- A reviewer wants the M4 converter (`/mod-sp-converter`) to operate on a *current*
  decomposition rather than guessing context boundaries.
- The team needs a human-narrative `modernization-plan.md` with Mermaid diagrams to
  circulate for architectural review before greenlighting the migration.
- When the user asks to "plan a modernization", "decompose a legacy monolith into bounded
  contexts", "sequence a strangler-fig extraction", or "build the M3 plan".

## When invoked

1. **Verify the upstream M1/M2 digests are present.** Resolve the context dir (default
   `.foundry/context/db/`). Confirm `schema.json` (M1) and `stored-procs.json` (M2) both
   exist there. If either is missing, stop and tell the user to run `/mod-db-schema-analyze`
   then `/mod-stored-proc-analyze` first — a missing upstream digest is a hard stop (the CLI
   exits 2). Map any user-named options to the CLI flags under **How to invoke** below.
2. **Run the extract phase.** Invoke via Bash:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-modernization-strategist --extract-only \
     --context-dir <dir> --output-dir <dir>
   ```
   This runs Stages A-D (context detection, ownership analysis, sequencing, antipattern
   detection) deterministically, saves intermediate state under `<dir>/.state/`, writes
   `round-1-prompts.json` to that state directory (the CLI prints `WROTE <path>` for it),
   and exits `7`.
3. **If exit 7 — perform the in-session judgment round.** Read `round-1-prompts.json` from
   the path printed on stdout. For each item in `items`:
   - Read the `system` and `user` fields. They describe a single bounded context whose
     narrative synthesis (Stage E) requires a per-context modernization narrative.
   - Respond with the narrative text as described by `response_format` — a prose summary
     covering the recommended target stack, sequencing rationale, antipattern callouts, and
     migration risks for this context. Do **not** include JSON fences and do **not** invent
     table, service, or stored-procedure names not present in the prompt's entity list.
   - Collect all responses into a `JudgmentEnvelope` and write it to
     `round-1-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "mod-modernization-strategist",
       "round": 1,
       "items": [
         {"id": "<item.id>", "response": "<narrative text for this context>"},
         ...
       ]
     }
     ```
4. **Run the continue phase.** Invoke via Bash:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-modernization-strategist --continue \
     --judgments <state-dir>/round-1-judgments.json \
     --context-dir <dir> --output-dir <dir>
   ```
   This applies the narratives (Stage E), renders the three Mermaid diagrams (Stage F), runs
   the anti-hallucination guard, and writes the final artifacts. The CLI prints `WROTE <path>`
   for each artifact.
5. **Optional §3.4 C8 architecture-review fold (in-session).** The CLI produces the
   decomposition + plan + diagrams deterministically; it does **not** sub-call another LLM
   primitive (the Python CLI never invokes an LLM). To include the charter §3.4 C8 fold, **you**
   run `/architecture-review` in-session over the **source code under modernization** — the
   application/source tree C8 builds its static dependency graph from (its required `CODE_PATH`
   input). Do **not** point `/architecture-review` at `decomposition.json`: C8 consumes a source
   tree, not the JSON artifact, and has no mode to read it. Then fold C8's findings against the
   decomposition's Stage-D antipatterns — `decomposition.json` is the **correlation target**, not
   C8's input — writing the result into the plan's **"Risks per C8 review"** section, deduping any
   finding that overlaps a Stage-D antipattern (C8 wins; move the M3 entry rather than
   double-count). This pass is optional and applies only when a reviewable source tree exists;
   **when you skip it the "Risks per C8 review" heading is absent** and the plan instead carries a
   "Detected antipatterns" roll-up of M3's own Stage-D findings (the CLI never emits a
   "Risks per C8 review" section by itself). Surface a short note to the user when you ran it
   (C8 overlap count, any C8-only risks).
6. **Read what the CLI wrote and summarize.** On exit `0` read `modernization-plan.md` and
   `decomposition.json` and summarize for the user: bounded-context count, the head of the
   strangler-fig extract order, the top antipatterns by kind, and — when you ran the in-session
   C8 pass — the C8 overlap count and any C8-only risks. Surface any `⚠️ UNVERIFIED` markers the
   narrative carries (the anti-hallucination guard replaced an LLM-invented name) and
   recommend the head-of-order contexts as the next M4 conversion candidates.
7. **Non-zero exit (not 7) is a hard stop.** Surface the CLI's stderr and do not fabricate a
   plan. See the exit-code contract under **Invariants** below.

## How to invoke

Invoke as `/mod-modernization-strategist`. The user passes the context dir; options map to CLI
flags: `--context-dir PATH` (holds the M1 `schema.json` + M2 `stored-procs.json`, default
`.foundry/context/db/`; a missing upstream digest exits `2`); `--output-dir PATH` (where the
decomposition + plan + diagrams are written, default `--context-dir`). The charter §3.4 C8
architecture-review fold is **not** a CLI flag — it is an optional in-session `/architecture-review`
pass you run over the **source code under modernization** (C8's `CODE_PATH`), folding its findings
against the emitted `decomposition.json` antipatterns (see step 5 under **When invoked**). You
delegate to `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-modernization-strategist` — you do not perform the clustering,
ownership split, sequencing, antipattern detection, narrative synthesis, or diagram rendering
yourself.

### Skill-first mode (in-session, default)

Use `--extract-only` for the first pass, then `--continue --judgments <path>` after writing
`round-1-judgments.json` as described under **When invoked** above. Both invocations accept the
same `--context-dir` and `--output-dir` flags. The optional §3.4 C8 fold is an in-session
`/architecture-review` pass over the **source code under modernization** (C8's `CODE_PATH`),
correlated against the emitted `decomposition.json` antipatterns (step 5) — there is no CLI flag
for it.

### CI mode

CI runs the *same* skill-first loop: the emitted workflow (rendered from `ci.yaml`) invokes the
agent CLI, which loads this SKILL.md, runs `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-modernization-strategist
--extract-only`, produces the Stage E narrative-synthesis judgments, and re-invokes with
`--continue --judgments <path>`. The agent *is* the harness — there is no separate harness CLI
flag.

## Output

The CLI writes, under `--output-dir` (default `--context-dir`): `decomposition.json` (the
`DecompositionPlan`, Pydantic v2, `extra="forbid"`, schema-versioned `"1.0"`),
`modernization-plan.md` (the narrative — per-context summary + ownership tables, the sequencing
roadmap, and a final risk roll-up: a "Detected antipatterns" section by default, replaced by a
"Risks per C8 review" section **only** when you ran the optional in-session C8 pass), and the
three Mermaid views
`diagrams/01_decomposition.mmd`, `diagrams/02_sequencing.mmd`, and `diagrams/03_ownership.mmd`.
The authoritative output contract lives in `## Invariants` below.

## Invariants

- **§3.4 C8 fold is an optional in-session pass, not a CLI flag**: the Python CLI never invokes
  an LLM, so it cannot sub-call C8. To include the fold, the skill body runs `/architecture-review`
  in-session over the **source code under modernization** (C8's `CODE_PATH` — a source tree, **not**
  `decomposition.json`, which C8 cannot consume) and folds the findings against the decomposition's
  Stage-D antipatterns. Overlap with antipattern evidence → M3 entry **moves** to the
  `reviewed_by_c8` bucket (C8 wins, not double-counted). The pass is optional: **when it is not
  run, the "Risks per C8 review" heading is absent** — the CLI's deterministic plan instead carries
  a "Detected antipatterns" roll-up, so the M0 roll-up never inherits phantom C8 findings (#946).
- Exactly three Mermaid views, deterministic byte-identical across runs:
  `diagrams/01_decomposition.mmd`, `diagrams/02_sequencing.mmd`, `diagrams/03_ownership.mmd`.
- Sequencing partitions into `extract_order` waves (risk-weighted by M2 `RiskScore.total`),
  `deferred` (cycle-trapped), and `never_extract`.
- Anti-hallucination: back-ticked table/context name not in plan's universe → `⚠️ UNVERIFIED`
  with stderr warning.
- Exit codes: `0` success; `2` required upstream digest (`schema.json` / `stored-procs.json`)
  missing or unparseable; `7` `--extract-only` succeeded —
  prompts written, body must judge and call `--continue`. Exits other than `0` and `7` are
  hard stops.
- Slash command: `/mod-modernization-strategist` (`mod-` prefix, charter §3.2.M).
