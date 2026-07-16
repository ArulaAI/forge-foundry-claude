---
name: planner
description: >
  Turn a finished C1-shaped spec into a bite-sized TDD implementation plan: derive the
  file structure, decompose the work into ordered 2-5 minute tasks, flesh each task with a
  failing test, minimal implementation, test command, expected output, and conventional-commit
  message, then self-review for placeholders and ordering. Use when the user asks to "plan",
  "break into tasks", "create an implementation plan", "decompose the spec", or "plan from spec".
---

# /planner

Generate a TDD implementation plan from a C1-shaped
spec, directly in this session: derive the files, decompose into ordered tasks, flesh each
task with real test + implementation code, then hand off the structured payload to the bundled
Python script for template rendering and static self-review before handing off to C3 `/code-gen`.

## When to invoke

- After C1 `/spec-write` produces a spec — turn the design into an executable roadmap.
- Before C3 `/code-gen`, which consumes the plan as its required input.
- When the user asks to "plan", "break into tasks", or "decompose the spec into TDD tasks".

## When invoked

1. **Parse the spec.** Read the C1-shaped spec at the path the user gives — the output of
   `/spec-write`. It carries the level-2 sections `Goal`, `Architecture`, `File structure`
   (fenced code block), `Acceptance criteria` (checklist items), `Risks`, and `Decision log`,
   plus an `ADR appendix` with `ADR-NNN:` entries. The planner needs at minimum `Goal`,
   `Architecture`, `File structure`, and `Acceptance criteria`. If any of those four is
   missing, stop and tell the user which one — do not invent it.
2. **Stage A — derive the file structure (judgment + optional script scan).** From the spec's
   Goal, Architecture, and File structure, list every **source** file that must be created or
   modified, each with a one-sentence (≤120 char) responsibility and an action of `create` or
   `modify`. Include at least one entry per architecture component. Do **not** list test files
   here — those are added per-task in Stage B. If a codebase root is available, run the
   bundled script in scan mode first so the file paths match the real project layout:
   ```bash
   python3 <skill-dir>/scripts/plan_render.py --scan-only --codebase-root <root>
   ```
   The script prints `{"tech_stack": "..."}` — use that value as `tech_stack` in your payload.
   You only supply the file-structure judgment; the script does the filesystem scan.
3. **Stage B — decompose into ordered tasks (judgment).** Break the implementation into ordered
   2-5 minute TDD tasks. Each task: a short id (`T01`, `T02`, … in execution order), a title
   that describes what the *implementation* does (not the test), 1-3 source files it touches,
   a `depends_on` list of earlier task ids (may be empty), and `expected_tests` ≥ 1. Never
   emit a task with 0 expected tests.
4. **Stage C — flesh each task (judgment, N tasks).** For each skeleton, in order, produce the
   full task body as a `steps` list: (a) "Write failing test: …" with the complete pytest
   function; (b) "Confirm test fails: `uv run pytest …`"; (c) "Implement (minimal): …" with
   the minimal implementation code; (d) "Confirm test passes: `uv run pytest …`" with expected
   output; (e) "Commit: `feat(scope): description`" with a conventional-commits message. Use
   only symbols introduced in earlier tasks or the standard library — never forward-reference a
   later task. No `TBD`, `TODO`, placeholder, or `...` in any step text.
5. **Stage D — hand off to the deterministic renderer + static checker (pure Python, zero model
   tokens).** Assemble your Stage A/B/C judgment into one compact JSON payload:
   ```json
   {
     "title": "<plan title>",
     "goal": "<goal from spec>",
     "architecture": "<architecture summary>",
     "tech_stack": "<from scan or your judgment>",
     "file_structure": "<Markdown list of source files>",
     "tasks": [
       {
         "id": "T01",
         "title": "<task title>",
         "files": ["path/to/file.py"],
         "depends_on": [],
         "steps": [
           "Write failing test: `def test_foo(): assert Foo().value == 1`",
           "Confirm test fails: `uv run pytest tests/test_foo.py -x`",
           "Implement (minimal): add `Foo` class with `value = 1`",
           "Confirm test passes: `uv run pytest tests/test_foo.py -x`",
           "Commit: `feat(foo): add Foo class`"
         ]
       }
     ]
   }
   ```
   Write the payload to a temp file, then run the bundled script to render the plan from
   `references/plan_template.md` and run static self-review checks:
   ```bash
   python3 <skill-dir>/scripts/plan_render.py \
       --payload <payload.json> \
       --output <output-path>
   ```
   The script fills `{{title}}`, `{{goal}}`, `{{architecture}}`, `{{tech_stack}}`,
   `{{file_structure}}`, and `{{tasks}}` in `references/plan_template.md`; each task renders
   as `- [ ]` step lines. It then runs two **static checks** — `placeholder` (TBD/TODO/`...`
   literals in task steps or prose) and `missing_section` (a required template section absent
   from the rendered plan) — and exits non-zero when issues are found. It needs only `python3`
   — no installed foundry CLI, no third-party packages. Do **not** do template rendering or
   static checks yourself.
6. **Relay the summary.** Read the script's JSON summary (title, task count, review issues)
   from stdout. If the script exits non-zero, present each review issue to the user and fix the
   flagged tasks in the payload, then re-run the script. Mark the plan ready only when the
   script exits 0. (Skip re-run only if the user explicitly asks to skip self-review.)
7. **Emit decomposition verdict (structured output).** After the plan is finalised, judge whether
   the work represents **more than one independent deliverable**. When the spec's acceptance
   criteria decompose naturally into units that can be developed and reviewed separately (no
   shared state at the task boundary, each unit is releasable alone), emit `"split"` with one
   `children` entry per independent unit. When the work is a single coherent deliverable, emit
   `"atomic"`. Accuracy drives the verdict — never split artificially to hit a number, never
   keep atomic to avoid the extra description.

   Verdict shape:
   ```json
   { "schema_version": "1.0", "decomposition": "atomic" }
   ```
   or
   ```json
   {
     "schema_version": "1.0",
     "decomposition": "split",
     "children": [
       {
         "title": "...",
         "spec_seed": "...",
         "rationale": "..."
       }
     ]
   }
   ```
   Each `children` entry carries:
   - `title` — short human-readable name for the child unit.
   - `spec_seed` — a brief goal + scope statement that C1 `/spec-write` can expand into a
     full spec for this child.
   - `rationale` — one or two sentences explaining why this unit is independent.

   Write the verdict JSON to `<output-dir>/decomposition-verdict.json` (alongside the plan
   Markdown), then validate it with the bundled script:
   ```bash
   python3 <skill-dir>/scripts/plan_render.py --validate-verdict <verdict-path>
   ```
   The script prints `{"verdict_valid": bool, "errors": [...]}` to stdout and exits 0 if valid,
   1 if invalid, 2 on unreadable/unparseable input. The verdict schema is at
   `references/schemas/decomposition-verdict.schema.json`. Do **not** write the verdict
   until the plan itself exits 0 from Stage D. When `atomic`, existing planner behavior is
   unchanged — the verdict is supplementary structured output that the C7 work-orchestrator
   uses; it does not alter the rendered plan Markdown.

## How to invoke

Invoke as `/planner`. The user passes the path to a C1-shaped spec (the output of
`/spec-write`); if they pass a target output directory, write the plan there, otherwise default
to `docs/superpowers/plans/<today>-<slug>.md`. The model performs only the judgment stages
(spec parse, file-structure derivation, task decomposition, task fleshing, decomposition
verdict); the deterministic remainder — manifest scan, template rendering, static self-review,
and verdict validation — is offloaded to the bundled `scripts/plan_render.py` (stdlib-only;
runs under bare `python3`, no installed foundry CLI). The script's flags: `--scan-only
--codebase-root` (Stage A scan), `--payload` (Stage D render), `--output` (write path),
`--dry-run` (print; write nothing), `--validate-verdict` (Stage 7 verdict validation).

## Output

A Markdown implementation plan written to the output path, plus a
`decomposition-verdict.json` alongside it, plus a Stage D self-review findings list presented
in the session when issues are found. The authoritative output contract (plan layout, task
shape, no-placeholders rule, verdict schema) lives in `## Invariants` below.

## Invariants

- Tasks are ordered and bite-sized: each is a self-contained 2-5 minute TDD unit
  (failing test → minimal implementation → commit), in execution order.
- Every task step shows real, runnable code — no `TBD`, `TODO`, placeholder, or `...`; a task
  never forward-references a symbol or file defined in a later task.
- Each task carries `expected_tests` ≥ 1; the plan covers every acceptance criterion from the spec.
- The plan layout comes from `references/plan_template.md` by path — the bundled script renders
  it; the model does not inline or duplicate the template.
- The decomposition verdict is accuracy-driven: split whenever the work is genuinely >1
  independent unit; keep atomic when it is not. Never split artificially. The verdict conforms
  to `references/schemas/decomposition-verdict.schema.json` (schema_version `"1.0"`) and
  is validated via `python3 <skill-dir>/scripts/plan_render.py --validate-verdict <path>`.
- When `atomic`, the plan Markdown and rendering pipeline are unchanged — the verdict is
  supplementary structured output only.
