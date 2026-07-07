---
name: code-review
description: >
  Line-level code review of Python, TypeScript, and Java with confidence-based filtering
  across six lenses — bug, error_handling, convention, documentation, security_smell,
  test_quality — then aggregating C10/C11 AI-quality findings with dedup discipline. Enumerates
  the target files (optionally narrowed to a PR diff), reviews each across the selected lenses
  producing a severity + confidence per finding, drops findings below the confidence threshold,
  fires the C10/C11 §3.4 sub-calls in-session and dedups overlaps against the C4 findings, then
  renders a Markdown, JSON, or SARIF report. Use when the user asks to "review this code", "run
  a code review", "check code quality", "review my PR", or before merging a feature branch.
allowed-tools: Read, Grep, Glob, Bash
---

# /code-review

Line-level code review with confidence-based filtering, directly in this session: enumerate the
target files (optionally narrowed to a PR diff), review each across the six lenses producing a
severity and a confidence per finding, filter out findings below the confidence threshold, fire
the C10/C11 §3.4 AI-quality sub-calls in-session and dedup overlaps against the C4 findings, then
render the report.

## When to invoke

- After implementing a feature or bugfix — catch issues before they reach review.
- Before opening a pull request, or while reviewing one.
- When reviewing security-sensitive code (focus with the `security_smell` lens).
- For changed files only, by narrowing to a PR diff.
- When the user asks to "review this code", "run a code review", or "review my PR".

## When invoked

1. **Enumerate the files to review (extract phase).** Resolve the target path the user gives
   (default: the current working directory; a missing path is a hard error). Enumerate the source
   files under it, honoring an optional language filter (**auto** default, or **python** / **java**
   / **ts**) and an optional glob (`--file-pattern`). When a PR-diff range is given (`--git-diff
   REF...REF`, e.g. `origin/main...HEAD`), narrow the set to the files that range changed; if the
   diff resolution fails, log a note and review all files instead. If a `--spec` path is given,
   read it as context for relevance. Optionally fold in runtime context: when
   `.forge-foundry/context/compliance.yaml` (S70) is present its framework list + control IDs replace
   the generic compliance placeholder, and when `.forge-foundry/context/data-flow.json` (S60) is present
   the files it marks PII/PCI/PHI become the scoped set — both are optional and silently skipped
   when absent. The result of this phase is one prompt item per source file (id prefix
   `review:<relative-path>`).
2. **Judge each prompt item (per-file lens review).** For each item in `round-1-prompts.json`
   (one item per source file):
   - Read the `system` and `user` fields. They describe a single source file and the lenses to
     apply to it.
   - Respond with a JSON array matching `response_format.schema` — a `findings` array where each
     finding carries: `lens` (one of the six lens names), `file` (the file under review — do not
     invent a different path), `line` (1-indexed), optional `end_line`, `severity`
     (`low`/`medium`/`high`/`critical`), `confidence_score` (a float in `[0,1]`; the
     pipeline maps it to the categorical `confidence`), `title`,
     `description`, optional `suggestion`, and optional `cwe_ids` array. Walk all selected lenses:
     **bug** (logic errors: off-by-one, null deref, type confusion, async/await, races),
     **error_handling** (broad `except`, swallowed errors, missing rollback), **convention**
     (naming, signature shape, dead code, public/private leaks), **documentation** (missing or
     stale docstrings on public functions, misleading comments), **security_smell** (SQL injection,
     command injection, hardcoded secrets), and **test_quality** (`assert True`, weak assertions,
     missing edge cases). Apply the anti-hallucination guard: set `file` exactly to the file under
     review — never invent a different path. When both S70 and S60 context are present and the
     file is in S60's scoped set, populate `linked_controls` with the matching S70 control IDs
     (PCI → `pci-dss-4.0`, PHI → `gdpr`, PII → both, intersected with the frameworks S70 actually
     mapped). Do **not** wrap the JSON in markdown fences.
   - Collect all responses into a `JudgmentEnvelope` and write it to
     `round-1-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "code-review",
       "round": 1,
       "items": [
         {"id": "review:<relative-path>", "response": [<findings array>]},
         ...
       ]
     }
     ```
3. **Apply judgments and render (continue phase).** Apply the confidence filter to the collected
   findings — `> 0.7` is high, `[0.3, 0.7]` inclusive is medium, `< 0.3` is low; drop every
   finding below the `--confidence-threshold` (default **medium**). Fire the C10/C11 §3.4
   sub-calls in-session and dedup (unless `--skip-ai-quality` is set): for each C10/C11 finding
   at the same (file, line) as a C4 finding, **MOVE** it to `ai_quality.deduplicated_findings`
   with the note `(duplicate of C4 finding at file:line)` — the C4 finding stays canonical.
   Render the report in the requested format — `markdown` (default), `json`, or `sarif` — to
   stdout or `--output`. Summarize the findings for the user, surface any finding at or above
   `--max-severity`, and stop the user on critical C4 findings or on critical C10/C11 findings
   remaining after dedup.

## How to invoke

Invoke as `/code-review`. The user passes the path to review (default: the current working
directory); options refine the run: `--spec PATH` (spec file for relevance context), `--git-diff
REF...REF` (narrow to a PR diff, e.g. `origin/main...HEAD`), `--file-pattern GLOB` (filter files,
e.g. `src/*.py`), `--language auto|python|java|ts` (default `auto`), `--lens
bug|error_handling|convention|documentation|security_smell|test_quality` (one lens, repeatable;
default all six), `--confidence-threshold low|medium|high` (default `medium`), `--max-severity
high|critical` (the gate threshold), `--skip-ai-quality` (skip the C10/C11 sub-calls), `--format
markdown|json|sarif` (default `markdown`), and `--output PATH` (write to a file; default stdout).
You perform the file enumeration, per-lens review, confidence filter, in-session C10/C11
aggregation + dedup, and rendering yourself — there is no CLI to shell out to.

### Skill-first mode (in-session)

The body performs the extract→judge→continue loop directly in this session: enumerates files
(extract), reviews each against the six lenses producing findings in the `round-1-prompts.json`
prompt format (judge), then applies the confidence filter and dedup discipline and renders the
report (continue). `--extract-only` emits the prompts and exits 7; `--continue --judgments <path>`
feeds the session's judgments back for the deterministic filter and render.

### CI mode

CI runs the *same* skill-first loop: the emitted workflow (rendered from `ci.yaml`) invokes the
agent CLI, which loads this SKILL.md, runs the deterministic `--extract-only` stage, produces
the per-file judgments, and re-invokes with `--continue --judgments <path>`. The agent *is* the
harness — there is no separate harness CLI flag.

## Scan exclusions

`enumerate_files` derives `ALWAYS_EXCLUDED_DIRS` from `primitives._shared.scan_exclusions.DEFAULT_EXCLUDED_DIRS`, excluding `.venv`, `node_modules`, `__pycache__`, `.git`, build dirs, and the wholly-vendored harness dirs (`.foundry`, `.claude`) and the emitted-harness subpaths (`.github/skills`, `.agents/skills`, `.agent/skills`, `.cursor/rules`) — `.github/workflows/`, `.github/CODEOWNERS`, etc. are still scanned by default. Vendored files are never reviewed. (`enumerate_files` accepts an `include_vendored` kwarg; the C4 CLI does not surface it.) See SPEC.md §Scan exclusions.

## Output

A code-review report — Markdown, JSON, or SARIF — listing each finding by lens with its severity,
confidence level + score, and an optional suggestion, followed by the aggregated C10/C11
AI-quality findings and their deduplicated entries. The authoritative output contract (the six
lenses, the severity levels, the confidence filter, the §3.4 dedup discipline, the SARIF runs
layout, and the gate behavior) lives in `## Invariants` below.

## Invariants

- Six lenses — **bug**, **error_handling**, **convention**, **documentation**, **security_smell**,
  **test_quality** — all by default; `--lens` selects a subset.
- Every finding: severity **low/medium/high/critical**, confidence `[0,1]`; wrong `file` →
  dropped (anti-hallucination). S60-scoped files get `linked_controls` from S70
  (PCI → `pci-dss-4.0`, PHI → `gdpr`, PII → both, intersected with S70's mapped frameworks).
- Confidence thresholds: `> 0.7` high, `[0.3, 0.7]` inclusive medium, `< 0.3` low; findings
  below `--confidence-threshold` (default `medium`) dropped.
- C10 (FM1–FM5) + C11 (FM6–FM10) aggregated unless `--skip-ai-quality`; same (file, line) as a
  C4 finding → **MOVED** to `ai_quality.deduplicated_findings`, C4 canonical.
- SARIF: C4 → `runs[0]`, C10 → `runs[1]`, C11 → `runs[2]` (latter two when sub-calls ran).
- `--max-severity` gate: critical C4 findings, and critical C10/C11 surviving dedup, stop the user.
- Exit codes: `0` success; `2` code path not found; `4` issues exceed `--max-severity`; `5`
  critical C10/C11 findings surviving dedup (CI/harness mode only); `7` `--extract-only`
  succeeded — prompts written, body must judge and call `--continue`. Exits other than `0` and
  `7` are hard stops.
