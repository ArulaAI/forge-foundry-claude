---
name: repo-digest
description: >
  Compress a repository into 6 token-budgeted context files under `.foundry/context/`
  for AI-assistant consumption, or federate peer-repo digests for multi-repo platforms
  (10-25 repos sharing one logical app). The work is deterministic Python (repo
  federation + digest extraction); this body delegates to `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" repo-digest`
  rather than reproducing it. Use when the user asks to "generate a repo digest",
  "compress the repo for context", "federate peer digests", "create repo context files",
  or when onboarding to a new codebase and needing a quick orientation.
allowed-tools: Read, Bash
---

# /repo-digest

Compress a repository into 6 token-budgeted context files for AI-assistant consumption, or
federate peer-repo digests for multi-repo platforms — by delegating to the deterministic
`"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" repo-digest` CLI. The digest extraction and peer federation are Python
load-bearing logic in `primitives/repo_digest/shared/`; this body resolves the target and the
mode, invokes the CLI, reads what it wrote, and acts on the exit code. It does **not**
reproduce the extraction in-session.

## When to invoke

- At the start of a new AI session — give the assistant full repo context.
- When onboarding to a new codebase — compress it for quick orientation.
- For multi-repo platforms — federate peer digests into a unified context.
- When `.foundry/context/` files are stale or missing.
- When the user asks to "generate a repo digest", "compress the repo for context", or
  "federate peer digests".

## When invoked

1. **Resolve the target and the mode.** Take the path the user gives (default: the current
   working directory) and decide which of the two mutually-exclusive modes applies: **produce**
   (generate this repo's digest files) or **federate** (fetch peer-repo digests per
   `.foundry/platform.yaml`). Exactly one is required. Map any user-named options to the CLI
   flags listed under **How to invoke** below.
2. **Run the extract phase.** Invoke via Bash:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" repo-digest <path> --produce --extract-only
   # or
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" repo-digest <path> --federate --extract-only [--shallow]
   ```
   This runs the deterministic extraction pipeline fully in Python (template mode is always
   deterministic), saves intermediate state, and — when `.foundry/repo-digest.yaml` configures
   `llm` mode — writes `round-1-prompts.json` to the state directory (the CLI prints
   `WROTE <path>` for it) and exits `7`. In `template` mode (the default) the CLI exits `0`
   with all six context files already written; skip to step 5.
3. **If exit 7 (`llm` mode) — perform the in-session judgment round.** Read
   `round-1-prompts.json` from the path printed on stdout. For each item in `items`:
   - Read the `system` and `user` fields. They carry a bounded deterministic content block
     (e.g. the raw `repo-digest.md` or `recent-deltas.md` section) requiring a short
     narrative summary.
   - Respond with a JSON object matching `response_format.schema` — exactly:
     ```json
     {"prose": "<2–3 sentence narrative summary grounded in the content block>"}
     ```
     Every back-ticked identifier in the prose must appear in the deterministic content the
     prompt supplies. Do **not** invent identifiers, file names, or framework names not present
     in the prompt's content block — the Zero-Hallucination guard will replace fabricated
     names with `⚠️ UNVERIFIED`.
   - Collect all responses into a `JudgmentEnvelope` and write it to
     `round-1-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "repo-digest",
       "round": 1,
       "items": [
         {"id": "<item.id>", "response": {"prose": "<narrative summary>"}},
         ...
       ]
     }
     ```
4. **Run the continue phase** (only when exit 7 occurred). Invoke via Bash:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" repo-digest <path> --produce --continue \
     --judgments <state-dir>/round-1-judgments.json
   ```
   This splices the approved narrative summaries into the context files, applies the
   Zero-Hallucination guard (replacing fabricated names with `⚠️ UNVERIFIED`), and writes
   the final six context files. The CLI prints `WROTE <path>` for each artifact.
5. **Read what the CLI wrote and summarize.** On exit `0` read the files under
   `.foundry/context/` (or `--output-dir`) and summarize the digest — or the federated peer
   set — for the user. Surface any `⚠️ UNVERIFIED` markers the narrative carries.
6. **Non-zero exit (not 7) is a hard stop.** Surface the CLI's stderr to the user and do not
   fabricate a digest. See the exit-code contract under **Invariants** below.

## How to invoke

Invoke as `/repo-digest`. The user passes the path to digest (default: the current working
directory); options map to CLI flags: `--produce` (generate this repo's digest files) and
`--federate` (fetch peer-repo digests per `.foundry/platform.yaml`) are mutually exclusive and
exactly one is required; `--output-dir DIR` (produce only) overrides the default output directory
(`.foundry/context/`); `--shallow` (federation only) sparse-checkouts each peer's
`.foundry/context/`; `--max-tokens N` overrides the total token budget from
`.foundry/repo-digest.yaml`; `--format markdown` selects the output format (only `markdown` in
v0.4.0); `--include-vendored` re-includes the vendored harness trees (`.foundry`, `.claude`,
`.github/skills`, …) in the source-file walks (default off; dependency/build dirs stay excluded).
You delegate to `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" repo-digest` — you do not perform the extraction or
federation yourself.

### Skill-first mode (in-session, default)

Use `--extract-only` for the first pass. In `template` mode (the default) this exits `0` with
all files already written — the judgment round is skipped. In `llm` mode (opt-in via
`.foundry/repo-digest.yaml`) this exits `7` with `round-1-prompts.json` written; then write
`round-1-judgments.json` and call `--continue --judgments <path>` as described above.

### CI mode

CI runs the *same* skill-first loop: the emitted workflow (rendered from `ci.yaml`) invokes the
agent CLI, which loads this SKILL.md, runs `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" repo-digest --extract-only` (in `llm`
mode), produces the narrative judgments, and re-invokes with `--continue --judgments <path>`. The
agent *is* the harness — there is no separate harness CLI flag.

## Output

`--produce` writes 6 token-budgeted files into `.foundry/context/`; `--federate` writes peer
digest files under `.foundry/context/peers/<peer>/`. The authoritative output contract (the
6 file names and what each carries, the federation layout, and the exit-code scheme) lives in
`## Invariants` below.

## Scan exclusions

All five extractors exclude `.venv`, `node_modules`, `__pycache__`, `.git`, build dirs, the wholly-vendored harness dirs (`.foundry`, `.claude`), and the emitted-harness subpaths (`.github/skills`, `.agents/skills`, `.agent/skills`, `.cursor/rules`) by default. `.github`, `.agents`, `.agent`, `.cursor` themselves are **not** whole-dir excludes — `.github/workflows/`, `.github/CODEOWNERS`, `dependabot.yml`, etc. are still scanned; only the emitted skill/rule subpaths are dropped. Pass `--include-vendored` (threaded end-to-end to `include_vendored=True`) to re-include the vendored dirs + emitted subpaths when intentionally auditing emitted artifacts. Dependency dirs (`.venv`, `node_modules`, etc.) are never re-included. See `primitives/_shared/scan_exclusions.py` and SPEC.md §Scan exclusions.

## Invariants

- `--produce` writes exactly these 6 files into `.foundry/context/` (or the `--output-dir`
  override):
  - `repo-digest.md` — project name, language counts, a `## LOC by Language` table
    (line counts), a weighted `## Modules` inventory, frameworks, purpose,
    structure, and a `## Data Access` section (DB tables from JPA `@Table`/`@Entity`,
    DDL, and embedded SQL) — the shapes the SP3 `mod-app-analysis` aggregator parses,
    including the load-bearing `data:<table>` join keys (#1405)
  - `conventions.md` — lint/format configs + test framework + naming patterns
  - `api-surface.md` — exported symbols + OpenAPI specs + CLI commands
  - `entry-points.md` — main()s, HTTP handlers, Lambda handlers, pytest fixtures
  - `recent-deltas.md` — last N days of commits + most-changed files
  - `dependency-map.md` — internal module graph + external deps
- `--federate` writes peer digest files under `.foundry/context/peers/<peer>/`, per the
  `member_repos` list in `.foundry/platform.yaml` (validated against the SEC-1 URL policy).
- Exactly one of `--produce` / `--federate` is required (mutually exclusive); passing both or
  neither exits `2`. The total token budget defaults to ~30K, configurable per-file via
  `.foundry/repo-digest.yaml`.
- Exit `0` = digests (or federation) written; `7` = `--extract-only` in `llm` mode succeeded —
  prompts written, body must judge and call `--continue`. Exits other than `0` and `7` are hard
  stops — surface CLI stderr and do not fabricate output.
