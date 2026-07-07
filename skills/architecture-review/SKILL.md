---
name: architecture-review
description: >
  System-shape review of a Python, Java, or TypeScript codebase — the geometry, not
  line-level correctness. Builds a static dependency graph, detects layer violations
  against a declared `.foundry/architecture.yaml`, computes afferent/efferent/instability
  coupling metrics, finds cyclic dependencies via Tarjan SCC, then LLM-evaluates abstraction
  quality, API design, and cross-cutting-concerns leakage, fires the C10/C11 §3.4 sub-calls
  in-session folding FM10 architectural-decay findings into the layer violations, and renders
  a Markdown, JSON, or SARIF report. Use when the user asks to "review the architecture",
  "check for cyclic dependencies", "analyze coupling", or "detect layer violations" — and as
  the C8 §3.4 sub-call that generative primitives (M3 mod-modernization-strategist and
  others) invoke as `/architecture-review <target>` on an emitted architectural artifact to
  get structural findings back.
allowed-tools: Read, Grep, Glob, Bash
---

# /architecture-review

System-shape review directly in this session: build the static dependency graph (Python / Java
/ TypeScript), detect layer violations against the declared `.foundry/architecture.yaml`, compute
coupling metrics, detect cyclic dependencies, LLM-evaluate abstraction quality / API design /
cross-cutting leakage, fire the C10/C11 §3.4 sub-calls in-session and fold the FM10
architectural-decay findings into the layer violations, then render the report. This reviews the
*geometry* of a codebase — module dependencies, layering, coupling, cycles — not line-level
correctness (that is C4 `/code-review`).

## When to invoke

- Before starting a major refactor — understand the current architecture shape.
- When cyclic imports or layer-boundary drift is suspected.
- After adding new modules — check for layer violations and coupling regressions.
- During review of architectural changes, or in CI to prevent coupling regressions.
- As the **§3.4 sub-call** invoked by a generative primitive (M3 mod-modernization-strategist
  runs `/architecture-review` on its emitted `decomposition.json`; other generative primitives
  follow the same Generative→Review auto-invoke) on an emitted architectural artifact, returning
  structural findings the caller folds into its own narrative.
- When the user asks to "review the architecture", "check for cyclic dependencies", or "detect
  layer violations".

## When invoked

C8 is a 2-round primitive. Round 1 performs the Stage E subjective review of the dependency-graph
shape; round 2 (when ≥ 2 issues exist) re-ranks the combined issue list. `--skip-subjective`
bypasses both LLM rounds and produces a pure-static report.

1. **Build the static dependency graph and detect violations (extract phase — Stages A-D).**
   Resolve the target path the user (or the calling generative primitive) gives — a directory or
   an artifact-bearing directory; the default is the current working directory and a missing path
   is a hard error. Run Stages A-D deterministically in-session: build the static dependency graph
   (Python AST / Java import / TypeScript ES-import), detect layer violations against
   `.foundry/architecture.yaml` (or `--arch-yaml`), compute per-module coupling metrics
   (afferent/efferent/instability; flag as **fragile junction** when instability > 0.8 AND
   efferent > 5), detect cyclic dependencies (Tarjan SCC, size ≥ 2), and load optional S60/S10
   runtime context. When `--skip-subjective` is set, assemble the report from the static results
   and proceed directly to rendering. Otherwise the result of this phase is one prompt item
   (id `review:subjective`) for the Stage E subjective review.
2. **Judge the subjective-review prompt item (round 1 — Stage E).** For the single item
   (id `review:subjective`):
   - Read the `system` and `user` fields. They describe the dependency-graph shape and the spec
     context (when given), asking for a subjective review across three issue kinds.
   - Respond with a JSON array matching `response_format.schema` — an array of `SubjectiveIssue`
     objects where each carries: `kind` (one of `abstraction`, `api_design`,
     `cross_cutting_leak`), optional `module` (a module name present in the dependency graph —
     do not invent module names not in the graph), `description`, and `severity`
     (`low`/`medium`/`high`/`critical`). When S60 context is present and the module is backed by
     a PII/PCI/PHI source file, set `exposes_pii: true`. Apply the anti-hallucination guard: omit
     `module` (or set it to null) rather than inventing a module name not in the graph; omit any
     issue whose `kind` is not one of the three vocabulary values. Do **not** wrap the JSON in
     markdown fences.
   - Collect the response into a `JudgmentEnvelope` and write it to
     `round-1-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "architecture-review",
       "round": 1,
       "items": [
         {"id": "review:subjective", "response": [<SubjectiveIssue array>]}
       ]
     }
     ```
3. **Apply subjective judgments and optionally produce the re-ranking prompt (continue after round
   1).** Apply the subjective judgments (anti-hallucination guard: drop any issue whose `kind` is
   not a vocabulary value or whose `module` is absent from the dependency graph). Combine the
   surviving subjective issues with the static layer violations and cycles into a single issue
   list. If the combined list has ≥ 2 issues, produce one prompt item (id `rank:issues`) for
   priority re-ranking (round 2). Otherwise assemble the `ArchitectureReport`, fire the C10/C11
   §3.4 sub-calls in-session (unless `--skip-ai-quality` is set) with FM10 folded into layer
   violations, render the report, and gate.
4. **Judge the re-ranking prompt item (round 2 — when ≥ 2 issues exist).** For the single item
   (id `rank:issues`):
   - Read the `system` and `user` fields. They present the combined issue list (numbered) and ask
     for a priority re-ranking.
   - Respond with a JSON array matching `response_format.schema` — one object per issue carrying
     `issue_index` (the issue's 0-based index from the numbered list), `priority_rank` (1-based
     rank; lower = more important), and `rationale` (a brief reason). Include every issue exactly
     once. The pipeline sorts by `priority_rank` ascending to derive the final order. Do **not**
     wrap the JSON in markdown fences.
   - Collect the response into a `JudgmentEnvelope` and write it to
     `round-2-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "architecture-review",
       "round": 2,
       "items": [
         {"id": "rank:issues", "response": [{"issue_index": 0, "priority_rank": 1, "rationale": "..."}, ...]}
       ]
     }
     ```
5. **Apply ranking, fire §3.4 sub-calls, render and gate (continue after round 2).** Apply the
   ranking to the combined issue list. Fire the C10/C11 §3.4 sub-calls in-session (unless
   `--skip-ai-quality` is set), folding any C11 FM10 architectural-decay findings into
   `layer_violations` with `source = "c11_fm10"` (original severity preserved) and removing them
   from the C11 list. Assemble the `ArchitectureReport`, render it in the requested format —
   `markdown` (default), `json`, or `sarif` — to stdout or `--output`. Summarize the layer
   violations (including any FM10-derived ones), cyclic dependencies, coupling metrics (fragile
   junctions), and subjective issues; surface any finding at or above `--max-severity`. In SARIF
   C8's own findings occupy `runs[0]`, C10 `runs[1]`, C11 `runs[2]` (latter two when sub-calls
   ran).

## How to invoke

Invoke as `/architecture-review <target>` — the user (or the calling generative primitive) passes
the path to review (a directory; default the current working directory). Options refine the run:
`--spec PATH` (spec file for Stage E context), `--arch-yaml PATH` (path to
`.foundry/architecture.yaml`; auto-discovered if absent), `--module-pattern GLOB` (scope the
modules), `--language python|java|ts|auto` (default `auto`), `--format markdown|json|sarif`
(default `markdown`), `--output PATH` (write to a file; default stdout), `--max-severity
high|critical` (the gate threshold), `--skip-ai-quality` (skip the C10/C11 §3.4 sub-calls),
and `--skip-subjective` (skip the Stage E LLM review for a pure-static run). You perform the
dep-graph construction, layer / coupling / cycle analysis, subjective review, re-ranking, and
rendering yourself — there is no CLI to shell out to. As a §3.4 sub-call, the calling primitive
invokes `/architecture-review` on the directory holding its emitted artifact and folds the
returned structural findings into its own report.

### Skill-first mode (in-session, default)

The body performs the extract→judge→continue loop directly in this session: builds the static
dependency graph and detects violations (extract), reviews the dep-graph shape subjectively and
optionally re-ranks the combined issue list (judge — up to two rounds), then applies the
judgments, fires the §3.4 C10/C11 sub-calls, and renders the report (continue).

### CI mode

In CI, the emitted workflow (from `ci.yaml`) invokes the agent CLI (Claude Code, Copilot, Codex,
or Antigravity), which loads this SKILL.md and runs the same extract → judge → continue protocol:
the deterministic `--extract-only` stage emits the subjective-review and optional re-ranking
prompts, the agent judges them, and `--continue --judgments` folds the verdicts back in. The
agent *is* the harness — there is no separate harness CLI flag.

## Output

An architecture-review report — Markdown, JSON, or SARIF — leading with a summary count, then the
layer violations (each tagged with its source: `c8_static` or `c11_fm10`), the cyclic
dependencies, the coupling-metrics table (with fragile junctions flagged), the LLM subjective
issues, and the C10/C11 AI-quality summary. The authoritative output contract (the pipeline
stages, the three subjective issue kinds, the severity scheme, the anti-hallucination guard, the
§3.4 FM10-fold behavior, the SARIF runs layout, the coupling thresholds, and the gate / exit
behavior) lives in `## Invariants` below.

## Invariants

- Subjective issues (Stage E): exactly three kinds — **abstraction**, **api_design**,
  **cross_cutting_leak** — severity **low/medium/high/critical**. Layer violations and FM10-derived
  violations carry the same four-level severity; cycles are treated as **high**. Ranking:
  layer violation > cycle > subjective issue within a severity. No confidence score — filter is
  severity only. Stage E is optional (`--skip-subjective`); §3.4 sub-call is optional
  (`--skip-ai-quality`).
- Anti-hallucination: any `SubjectiveIssue` whose `module` is absent from the dependency graph, or
  whose `kind` is unknown, is **dropped** with a noted drop.
- Coupling: instability = efferent / (afferent + efferent), range `[0.0, 1.0]`, 0.0 when isolated;
  **fragile junction** when instability > 0.8 AND efferent > 5.
- C8 auto-invokes C10 (FM1–FM5) + C11 (FM6–FM10) unless `--skip-ai-quality`. C11 **FM10**
  findings are **folded** into `layer_violations` with `source = "c11_fm10"`, original severity
  preserved (layer-violation type → `high`; cycle type → `medium`), and **removed** from the C11
  list — counted once in violations, not again in the AI-quality bundle. Only FM10 relocates
  (narrower than C4/S11's move-to-dedup-bucket).
- SARIF: C8 findings → `runs[0]`, C10 → `runs[1]`, C11 → `runs[2]` (latter two when sub-calls ran).
- `--max-severity` gate: any finding at or above the threshold stops the user; critical C10/C11
  findings also stop the user. CI exits 4 on severity gate, 5 on critical AI findings.
- Layer config: `.foundry/architecture.yaml` (auto-discovered when absent, or `--arch-yaml`).
- As the §3.4 sub-call target via `/architecture-review <target>`: a C8 failure propagates as
  the caller's failure; a C8 finding at exit 0 is content, not error.
- Exit codes: `0` success; `2` code path not found; `4` issues exceed `--max-severity`; `5`
  critical C10/C11 findings (CI/harness mode only); `7` `--extract-only` succeeded — prompts
  written, body must judge and call `--continue`. Exits other than `0` and `7` are hard stops.
