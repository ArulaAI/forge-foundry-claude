---
name: mod-playbook-discovery
description: >
  Walk any source root — mainframe COBOL/CICS/JCL/VSAM, modern Python web service
  (FastAPI/Django/Flask), legacy Java EE monolith (servlets/EJB/JAX-RS), Node mesh,
  PowerBuilder, Delphi, VB6, Oracle Forms, AS/400 RPG — and emit a single exhaustive
  16-section `PLAYBOOK.md` grounded in real source citations, plus a Phase-D §Validation
  Report that reports accuracy as a percentage rather than silently dropping content. The
  deterministic pipeline (recon → analyze → render → audit) extracts facts in Python; this
  body drives the synthesis round that fills the five narrative-driven sections (§1 Executive
  Summary, §2 System Architecture, §8 Business Rules Catalog, §14 Technical Debt Inventory,
  §15 Modernization Analysis) with real, grounded analysis rather than `⚠️ UNVERIFIED` stubs.
  Polyglot via the `forge_foundry.playbook_discovery.flavors` entry-point registry. M5 is
  Context-class — the §3.4 generative-→-review C8 sub-call does NOT auto-fire. Use when the
  user asks to "discover a codebase", "build a discovery playbook", "produce PLAYBOOK.md for a
  legacy system", "walk a polyglot source tree", or needs the ground-truth playbook M6
  `/mod-synth-logs` consumes.
allowed-tools: Read, Bash
delegates-to-cli: true
---

# /mod-playbook-discovery

Walk any source root and emit a single exhaustive 16-section `PLAYBOOK.md` — by driving the
`"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-playbook-discovery` CLI through a two-phase synthesis loop. The
deterministic pipeline (Phase A recon / file-tree walk + flavor resolution, Phase B deep
analysis, Phase C the 16-section render, Phase D self-audit) is Python load-bearing logic in
`primitives/playbook_discovery/shared/pipeline.py` over the cross-primitive discovery engine
(`primitives/_shared/discovery/`). The five narrative-driven sections (§1 Executive Summary, §2
System Architecture, §8 Business Rules Catalog, §14 Technical Debt Inventory, §15
Modernization Analysis) require judgment that a deterministic extractor cannot supply; this body
fills them in-session using the digest and a bounded set of Python-selected source files, then
passes the typed `synthesis.json` back to the CLI for Phase C render and Phase D citation audit.

## When to invoke

- A modernization or knowledge-gap engagement starts and the engineer needs a complete,
  evidence-grounded picture of a legacy system — a CICS/COBOL mainframe, a Java EE monolith, a
  Python microservice mesh, a PowerBuilder client/server app — before any rewrite is greenlit.
- A new engineer joins and needs a single onboarding document that explains what the system
  does, how the pieces fit together, and where the dragons are.
- Leadership wants a modernization-readiness report (entity inventory, technical-debt callouts,
  modernization analysis) to size a rewrite without guessing at blast radius.
- A reviewer wants the downstream M6 (`/mod-synth-logs`) synthetic operational evidence
  generator to operate on a *current* ground-truth playbook rather than re-deriving entities.
- The user asks to "discover a codebase", "build a discovery playbook", "produce PLAYBOOK.md
  for a legacy system", or "walk a polyglot source tree".

## When invoked

1. **Resolve the source tree and the platform flavor.** Take the `--code-root` directory the
   user names (or `.foundry/manifest.yaml`'s `code_root:` key; M5 is file-mode only at v1 — a
   missing or non-directory path exits `2`). Choose a stable `--run-id` (e.g. a short slug or
   timestamp slug) and record it — the same `--state-dir` and `--run-id` must be passed to
   BOTH the extract leg and the continue leg. Resolve the platform flavor: the default `auto`
   lets the CLI's Phase A run the flavor-pack registry's detect-and-pick (every registered
   flavor's cheap manifest/extension sniff, highest confidence wins, lex tie-break); set
   `--platform-flavor` explicitly (e.g. `cics_cobol`, `python`, `java_ee`, `generic`) when the
   stack is known. Map any user-named options to the CLI flags under **How to invoke** below.
2. **Extract phase (exit 7).** Run via Bash:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-playbook-discovery --code-root <path> --state-dir <dir> --run-id <id>
   ```
   (`--extract-only` is the default; the flag is optional but may be passed for clarity.) This
   runs Phase A + B, writes the `AnalysisDigest` to `<state-dir>/<run-id>/analysis.json`, and
   writes the synthesis prompts to `<state-dir>/<run-id>/synthesis_prompts.json`, then exits `7`.
   On any non-7 non-0 exit from this leg, surface the CLI's stderr and stop — do not proceed to
   synthesis.
3. **Read the prompts and bounded source files.** Read
   `<state-dir>/<run-id>/synthesis_prompts.json` (schema at
   `references/schemas/synthesis_prompts.json`). For each entry in `sections`, use the `Read`
   tool to read the source files listed under `source_files` (paths are relative to `code_root`;
   these are Python-selected, bounded to ~5 files per section — never walk the whole tree).
4. **Synthesize in-session — produce `synthesis.json`.** For each of the five synthesis-driven
   sections (§1 Executive Summary, §2 System Architecture, §8 Business Rules Catalog, §14
   Technical Debt Inventory, §15 Modernization Analysis):
   - Analyze the `digest_slice` and the files you read. Ground every non-obvious claim in what
     you actually found. Every non-obvious claim must carry a `path:line` citation that resolves
     to a real file under `code_root` — the `citation_rule` field in each prompt states this.
     Use **repo-root-relative** paths (e.g. `billing/db/fn_rate.sql:13`), not bare basenames, and
     write any `path:line` you mention **inline in the prose** identically to its `citations[]`
     entry. Phase D resolves a bare-basename prose ref against the section's `citations[]`, but a
     directory-qualified path that matches an array entry is the safe form.
   - Produce prose within the section's `word_budget` (stated in the prompt). The body should be
     substantive and specific — this is where the playbook's value lives.
   - Do **not** invent entity names, file paths, or line numbers not present in the digest or the
     files you read.
   Write the result to `<state-dir>/<run-id>/synthesis.json` conforming to the schema at
   `references/schemas/synthesis.json`:
   ```json
   {
     "sections": {
       "1":  {"body": "## Executive Summary\n…", "citations": ["path/to/file.py:42", "…"]},
       "2":  {"body": "…", "citations": ["…"]},
       "8":  {"body": "…", "citations": ["…"]},
       "14": {"body": "…", "citations": ["…"]},
       "15": {"body": "…", "citations": ["…"]}
     }
   }
   ```
   A missing section key causes the pipeline to fall back to the deterministic `⚠️ UNVERIFIED`
   stub for that section rather than failing the run — but all five should be provided.
5. **Continue phase.** Run via Bash:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-playbook-discovery --code-root <path> --state-dir <dir> --run-id <id> \
     --continue --judgments <state-dir>/<run-id>/synthesis.json
   ```
   This validates `synthesis.json` (exit `3` if malformed / schema fails), merges the synthesis
   bodies into Phase C render, runs Phase D citation audit (refchecks every `path:line` in the
   synthesis bodies against `code_root`), and writes the final `PLAYBOOK.md`, `audit.json`, and
   `diagrams/*.mmd`. The CLI prints `WROTE <path>` for each artifact.
6. **Read and summarize.** Read `PLAYBOOK.md` and `audit.json` from the run directory. Present
   a **terse** summary to the user: the resolved flavor, the Phase D accuracy %, the finding
   count, and the written paths. Do **not** echo the full synthesis prose back into the session —
   the deliverable is `PLAYBOOK.md`, not the conversation. Then act on the exit code: exit `0`
   means the playbook was written; recommend running M6 `/mod-synth-logs` next when the entity
   inventory is large or the audit surfaced coupling/debt findings.
7. **Act on exit codes.** Any non-0 exit from the continue leg is a hard stop; surface the
   CLI's stderr. See the exit-code contract under **Invariants** below.

## How to invoke

Invoke as `/mod-playbook-discovery`. You drive `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-playbook-discovery` across
two legs — you do not perform the recon, analysis, render, or audit yourself, and the Python CLI
never calls an LLM. Options map to CLI flags: `--code-root PATH` (the source-tree directory to
discover — required; a missing or non-directory path exits `2`); `--state-dir PATH` (where the
per-run artefacts land, default `.foundry/context/playbook`); `--platform-flavor NAME` (default
`auto` runs the flavor-pack detect-and-pick; an explicit name short-circuits detection);
`--run-id ID` (per-run subdirectory name under `--state-dir`, default `run` — **must be
identical on both legs**); `--strict-audit` (fail with exit `6` on ANY Phase D finding, not only
when accuracy is below the floor); `--extract-only` (explicit form of the default first leg);
`--continue` (second leg; requires `--judgments`); `--judgments PATH` (path to your
`synthesis.json`, required with `--continue`); `--no-synthesis` (deterministic escape hatch —
see below).

### Deterministic escape hatch (`--no-synthesis`)

Pass `--no-synthesis` to run the legacy single-shot A→B→C→D pipeline with `⚠️ UNVERIFIED` stubs
for §1/§2/§8/§14/§15. Output is byte-identical to the pre-synthesis behavior. Useful for:
CI smoke runs that must not invoke an agent; M6 `/mod-synth-logs` ground-truth regeneration that
does not need narrative sections; headless tests. When `--no-synthesis` is passed, skip steps
3–5 above — the CLI runs to completion in a single invocation and exits `0` (or `6`).

### CI mode

The emitted workflow invokes the agent CLI, which loads this SKILL.md and drives the two-phase
loop: first leg exits `7`, the agent fills `synthesis.json`, second leg exits `0`/`6`. The agent
is the harness — there is no separate harness CLI flag.

## Output

The CLI writes the per-run artefacts under `--state-dir/<run-id>/` (default
`.foundry/context/playbook/run/`): `PLAYBOOK.md` (the 16-section discovery playbook with the
Phase D §Validation Report appended — §1/§2/§8/§14/§15 are now synthesis-authored prose,
refchecked by Phase D), `diagrams/<name>.mmd` (standalone Mermaid bodies for the
diagram-bearing sections), `audit.json` (the Phase D `AuditReport`), plus the Phase B artefacts
`analysis.json` (`AnalysisDigest`), `evidence-ledger.jsonl`, and `analysis-digest.yaml`. The
synthesis round also leaves `synthesis_prompts.json` and `synthesis.json` in the run directory
for auditability. The authoritative output contract (the 16 sections, polyglot flavor
resolution, and the exit-code scheme) lives in `## Invariants` below.

## Invariants

- **16 mandatory sections in fixed order**: §1 Executive Summary, §2 System Architecture, §3
  Execution Unit Inventory, §4 The Three-Layer Architecture, §5 Call Chain Maps, §6 Data
  Architecture, §7 Screen Navigation & UI Layer, §8 Business Rules Catalog, §9 Batch Processing,
  §10 Security Architecture, §11 Deployment Architecture, §12 Automation Layer, §13 Operational
  Patterns, §14 Technical Debt Inventory, §15 Modernization Analysis, §16 Appendices — plus
  §Validation Report (Phase D). Every diagram is Mermaid.
- **`delegates-to-cli: true`** — delegates to `primitives/playbook_discovery/shared/pipeline.py`
  (which orchestrates `primitives/_shared/discovery/`). The Python CLI never calls an LLM; the
  in-session harness (this body) performs the synthesis round.
- **Flavor resolution**: dispatch via `forge_foundry.playbook_discovery.flavors` entry-point
  group; `--platform-flavor auto` runs each flavor's `detect()`, picks highest confidence (lex
  tie-break); `generic` flavor is the always-available fallback.
- **Synthesis grounding**: every non-obvious claim in a synthesis section body must be backed by
  a `path:line` citation in `code_root`. Phase D refchecks every citation; unverifiable ones
  become findings that lower the accuracy %. Do not invent file paths or line numbers.
- **Zero Hallucination Protocol**: every entity name must be in `AnalysisDigest` or the
  bounded source files read in step 3; ungrounded inferences → `⚠️ UNVERIFIED`. Phase D
  accuracy floor default 60%; below floor → exit `6`; `--strict-audit` makes ANY finding exit
  `6`.
- **§3.4 C8 sub-call is OPT-IN only** — M5 is Context-class; §3.4 generative-→-review auto-fire
  does NOT apply. Any `/architecture-review` pass is a separate explicit invocation.
- Exit codes: `0` success (PLAYBOOK.md emitted, synthesis merged or deterministic stubs under
  `--no-synthesis`); `1` generic error; `2` `--code-root` not found / not a directory /
  unreadable, or `--continue` cannot find the run dir's `analysis.json`; `3` `--judgments` file
  malformed / fails `synthesis.json` schema; `6` Phase D accuracy below floor or any finding
  under `--strict-audit`; `7` extract-only wrote `synthesis_prompts.json` — awaiting judgments
  (harness must synthesize and call `--continue`). Non-zero exits other than `7` (on the extract
  leg) are hard stops.
- Slash command: `/mod-playbook-discovery` (`mod-` prefix, charter §3.2.M — distinct from S60
  `data-flow-map`).
