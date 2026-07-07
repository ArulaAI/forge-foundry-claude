---
name: code-gen
description: >
  Execute a C2-shaped implementation plan task by task using the PLB extract→author→continue
  protocol: parse the plan, emit authoring prompts (exit 7), author change-set JSON per the
  4-op schema, re-invoke continue to apply/test/commit, and revise on failure up to the retry
  bound. Python does all file writes, formatting, testing, and git operations — the body's only
  filesystem effect is authoring change-set JSON. Use when the user asks to "generate code",
  "implement the plan", "run code-gen", or "execute the implementation plan".
delegates-to-cli: true
allowed-tools: Read, Grep, Glob, Bash
---

# /code-gen

Plan-driven code generation using the **PLB extract→author→continue** protocol. This is a
**Python-load-bearing** primitive: `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" code-gen` runs the deterministic pipeline
(branch isolation, file application, test execution, git operations) and this body authors the
change-set JSON that describes what to write. The body never writes source files directly.

## SAFETY WARNING

This primitive writes files to the user's git repository and runs subprocesses — all enforced
by Python. The CLI:
- Creates an isolated branch before any write (`forge-code-gen-<plan-slug>-<timestamp>`).
- Refuses to run on a dirty working tree (uncommitted changes would ride onto the new branch).
- Skips any file that already has uncommitted user changes (`skipped_uncommitted`).
- NEVER commits with the `no-verify` flag (git hooks always run).
- NEVER pushes to remote — the user pushes explicitly after review.
- Confines all writes to paths declared in the task's `**Files:**` list (anti-hallucination guard).
- Enforces a retry bound (`--max-retries`, default 3); on exhaustion records `aborted_max_retries`.

These are Python-enforced invariants, not suggestions.

## When to invoke

- After C2 `/planner` produces an implementation plan — execute it task by task.
- When the user asks to "generate code", "implement the plan", "run code-gen", or "execute the
  implementation plan".
- When a C2 plan is ready and the user wants TDD-driven code generation with safety guards.

## When invoked

1. **Extract — parse the plan and emit authoring prompts (exit 7).** Run:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" code-gen --plan <PLAN> --extract-only --state-dir <state-dir>
   ```
   The CLI parses every task from the C2-shaped plan markdown (level-3 headers: id, title,
   `**Files:**`, optional `**Depends on:**`, code blocks for test + impl, test command, commit
   message). **Each task body MUST declare its files with backtick-quoted `- Create:` ``` `path` ```
   / `- Modify:` ``` `path` ``` lines** (plus optional `- Test:`/`- Delete:`) — that is what builds
   the per-task allowlist. A task with only its id/title header line and **no** file lines gives
   an empty allowlist, so every authored op is later dropped and the task reports `no_ops_applied`
   (exit 4), not a silent pass (#1193). If `--task-id` is given, only that task is extracted. The CLI creates a fresh
   branch (`forge-code-gen-<slug>-<timestamp>`) unless `--no-branch` is set, rejects a dirty
   tree, and writes `authoring_prompts.json` under `<state-dir>`. Each prompt item carries:
   `task_id`, `files` (the allowlisted paths), `current` (existing file contents), and `intent`
   (task title + test + impl code blocks). Hard stops: plan not found (exit 2); unparseable plan
   or unknown `--stack` (exit 3). Exits 7 ("awaiting judgments").

2. **Author — read `authoring_prompts.json` and write a change-set JSON per task.** For each
   prompt item in `authoring_prompts.json`, produce a change-set JSON at
   `<state-dir>/<task_id>_changeset.json` shaped as:
   ```json
   {
     "task_id": "<id>",
     "ops": [
       {"op": "create", "path": "src/foo.py", "content": "..."},
       {"op": "edit",   "path": "src/bar.py", "edits": [
           {"old": "<exact existing text to replace>", "new": "<replacement text>"}
       ]},
       {"op": "delete", "path": "src/old.py"},
       {"op": "move",   "from": "src/a.py", "to": "src/b.py"}
     ],
     "notes": "optional free-text rationale"
   }
   ```
   The four operations:
   - `create` — write a new file; `path` + `content` required.
   - `edit` — patch an existing file via anchored replacement; `path` + `edits` required. Each
     edit carries `old` (a unique substring of the current file) and `new` (its replacement).
     `old` MUST appear **exactly once** in the current file — use enough context to be unique.
   - `delete` — remove a file; `path` required.
   - `move` — rename/move a file; `from` + `to` required.
   Every `path` MUST appear in the task's `files` allowlist from the prompt item — the CLI drops
   any path outside the list (anti-hallucination guard). Author only the test file and the
   implementation file for each task; do not write ancillary files not declared in `**Files:**`.

3. **Continue — apply the change-set, run the test, commit on pass.** For each task's
   change-set, re-invoke:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" code-gen --plan <PLAN> --continue \
     --judgments <state-dir>/<task_id>_changeset.json \
     --state-dir <state-dir>
   ```
   The CLI applies every op in the change-set, runs the stack test command, and on pass:
   - Records `outcome: passed`, `attempts`, `test_output`, optional `commit_sha`.
   - When `--auto-commit` is set, commits only this task's files with the task's commit message.
   On failure the CLI writes `<state-dir>/revise_<task_id>.json` with the test output and
   affected regions, and returns exit 4 for the revise round (see step 4).

4. **Revise — author a patch change-set on failure, re-invoke continue.** When the continue leg
   writes `revise_<task_id>.json`:
   a. Read `revise_<task_id>.json` — it contains `task_id`, `test_output`, and `current_regions`
      (the current state of each file in the task, post-apply).
   b. Author a **patch** change-set at `<state-dir>/<task_id>_revise_<N>.json` using only `edit`
      ops (apply targeted fixes based on the test failure output).
   c. Re-invoke continue with the patch change-set:
      ```bash
      "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" code-gen --plan <PLAN> --continue \
        --judgments <state-dir>/<task_id>_revise_<N>.json \
        --state-dir <state-dir>
      ```
   Repeat up to `--max-retries` total attempts (counting the initial continue). Once the retry
   bound is exhausted the CLI records `outcome: aborted_max_retries` and exits 4 — surface the
   result to the user; the branch is left in a reviewable state.

## How to invoke

Invoke as `/code-gen`. The user passes the path to a C2-shaped plan; pass it as `--plan`. Key
flags: `--codebase-root PATH` (default cwd), `--task-id ID` (run one task), `--no-branch`
(skip branch creation, e.g. when already on the right branch), `--auto-commit` (commit each
passing task), `--max-retries N` (default 3), `--stack python|js|java` (defaults to
`python`), `--format markdown|json` (report format), `--state-dir PATH` (where to write
`authoring_prompts.json`, `*_changeset.json`, `revise_*.json`; default `<root>/.state`).

## Output

Per-task results: outcome (`passed` / `skipped_uncommitted` /
`aborted_max_retries` / `no_ops_applied`), attempt count, final test output, and optional commit
SHA — rendered as a markdown or JSON report by the CLI. The body relays the CLI's printed report to
the user. A `no_ops_applied` outcome (exit 4) means the task wrote nothing — usually a plan-grammar
gap (missing `- Create:/Modify:` file lines); surface the reason and dropped ops to the user.
Source files and test files are written by Python (never by the body); the branch is left in a
reviewable state for C4/C8 review before the user pushes.

## Invariants

- The body's ONLY filesystem effect is authoring change-set JSON to the state dir — Python does
  all file writes, test execution, formatting, and git operations.
- Every `path` in a change-set op must appear in the task's declared `**Files:**` list. The CLI
  rejects any path outside the allowlist (anti-hallucination guard).
- `edit` ops use anchored replacement (`old`/`new`): `old` MUST be unique in the current file.
  A zero-match or non-unique anchor is an `AnchorError` — the CLI writes a revise prompt.
- Git-safety is Python-enforced: branch isolation by default, dirty-tree refusal, uncommitted-
  change skip, NEVER `no-verify` bypass, no remote push.
- Retry bound is Python-enforced: at most `--max-retries` total apply+test cycles per task.
- The plan file allowlist for each task is built from Create/Modify/Test/DELETE lines in the
  `**Files:**` block — Delete lines are included so delete ops are not dropped. A task that declares
  no file lines (empty allowlist) or whose ops are all dropped applies zero ops and is reported
  `no_ops_applied` (exit 4), never a silent 0-op `passed` (#1193).
