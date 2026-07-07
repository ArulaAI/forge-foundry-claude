---
name: rules-gen
description: >
  Generate dual-harness coding-rule files for a consuming repo from curated YAML
  catalogs — `.claude/rules/<catalog>.md` (Claude Code, `paths:` YAML-list
  frontmatter) plus `.github/instructions/<catalog>.instructions.md` (Copilot,
  comma-separated `applyTo:` frontmatter) with byte-identical bodies. The work is
  deterministic Python (a template walker over hand-curated catalogs, no LLM); this
  body delegates to `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" rules-gen` rather than reproducing it. Use when
  the user asks to "generate rules", "scaffold .claude/rules", "create copilot
  instructions", "set up BFSI rules", or otherwise wants curated path-scoped coding
  rules wired into both Claude Code and GitHub Copilot from a single source.
allowed-tools: Read, Bash
---

# /rules-gen

Generate dual-harness coding rules for a consuming repository — by delegating to the
deterministic `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" rules-gen` CLI. Given a profile of catalog names, R1 writes
`.claude/rules/<catalog>.md` (Claude Code) and
`.github/instructions/<catalog>.instructions.md` (Copilot) with **byte-identical bodies**,
so an AI agent gets the same domain-specific advice regardless of harness. The catalog
loading, schema validation, and dual-harness rendering are Python load-bearing logic in
`primitives/rules_gen/shared/`; this body resolves the catalog selection, invokes the CLI,
reads what it wrote, and acts on the exit code. It does **not** reproduce the rendering
in-session and it authors no rules itself — the catalogs are hand-curated YAML.

## When to invoke

- A new BFSI repository needs PCI DSS / GDPR / SOX rules wired into Claude Code and Copilot
  before code lands.
- The team is adopting a new language or cloud stack and wants curated best-practice rules
  (Java 21+, AWS Well-Architected, etc.) without hand-writing them.
- Existing `.claude/rules/` and `.github/instructions/` have drifted and need to be
  re-generated from a single source.
- When the user asks to "generate rules", "scaffold .claude/rules", "create copilot
  instructions", or "set up BFSI rules".

## When invoked

1. **Resolve the catalog selection.** Take the project root the user gives (default: the
   current working directory) and the comma-separated profile of catalog names (e.g.
   `bfsi-core,java,aws`). Each name must match a file under
   `primitives/rules_gen/shared/catalogs/<name>.yaml`. Map any user-named options
   (output directory, dry-run preview, merge/overwrite behaviour) to the CLI flags listed
   under **How to invoke** below. The profile is required.
2. **Invoke the CLI via Bash.** Run `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" rules-gen <project_root> --profile <names>`,
   appending the resolved option flags. Do not reproduce the rendering in this session and do
   not author rules yourself — the renderer is a deterministic template walker over the curated
   YAML catalogs with no LLM call. Example:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" rules-gen . --profile bfsi-core,java,aws
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" rules-gen . --profile bfsi-core --dry-run --format detail
   ```
3. **Read what the CLI wrote.** On success the CLI prints one line per file, tagged `WROTE`
   (or `WOULD WRITE` under `--dry-run`, `SKIP` for an existing file under `--merge`). Read
   the emitted dual-harness rule files and summarize them for the user — one row per catalog,
   showing both the Claude Code (`.claude/rules/<catalog>.md`) and Copilot
   (`.github/instructions/<catalog>.instructions.md`) output paths.
4. **Act on the exit code.** Exit `0` means the rules were written; proceed. A non-zero exit
   is a hard stop: surface the CLI's stderr to the user and do not fabricate rule files. In
   particular, a `4` means a target file already exists and the user must opt in to `--merge`
   or `--force`. See the exit-code contract under **Invariants** below.

## How to invoke

Invoke as `/rules-gen`. The user passes the project root (default: the current working
directory) and a profile; options map to CLI flags: `--profile NAMES` (comma-separated
catalog names, **required**, e.g. `bfsi-core,java,aws`); `--output-dir PATH` (root where
`.claude/rules/` and `.github/instructions/` are written — defaults to `<project_root>`);
`--format list|detail` (`list` prints the file names, `detail` appends the full rendered
content of each file — default `list`); `--dry-run` (print what would be generated, write
nothing — pairs naturally with `--format detail` for a preview); `--merge` (skip an
already-present file rather than treating the collision as a hard error); `--force`
(overwrite existing rule files unconditionally; takes precedence over `--merge`). You
delegate to `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" rules-gen` — you do not load the catalogs or render the rules
yourself.

## Output

For each catalog in the profile, two files: `<output_dir>/.claude/rules/<catalog>.md`
(Claude Code) and `<output_dir>/.github/instructions/<catalog>.instructions.md` (Copilot).
The bodies are byte-identical; only the frontmatter shape differs (YAML-list `paths:` vs
comma-separated `applyTo:`). The authoritative output contract (the dual-harness file
scheme, the always-on-rule rendering, and the exit-code scheme) lives in `## Invariants`
below.

## Invariants

- The 16 curated catalogs live under `primitives/rules_gen/shared/catalogs/`: BFSI /
  security (`bfsi-core`, `bfsi-auth`, `bfsi-compliance`, `bfsi-security`), languages
  (`java`, `angular`, `react`, `nodejs`, `python`, `rust`), databases (`sql-ansi`,
  `oracle-plsql`, `mssql`, `postgres`), and cloud (`aws`, `azure`). Each is a Pydantic-v2
  validated YAML file; `--profile` names resolve against them.
- For each catalog, two files are written: `.claude/rules/<catalog>.md` (Claude Code,
  `paths:` YAML-list frontmatter) and `.github/instructions/<catalog>.instructions.md`
  (Copilot, comma-separated `applyTo:` frontmatter), with **byte-identical bodies**.
  Catalogs whose `paths` list is empty (e.g. `bfsi-core`) render as always-on rules — the
  output files omit the `paths:` / `applyTo:` frontmatter field entirely.
- Exit codes: `0` success; `1` no profile supplied (`--profile` missing or empty); `2`
  `<project_root>` not found, a requested catalog not found, or the catalog directory itself
  missing; `3` a matched catalog file failed schema validation; `4` a target rule file
  already exists and neither `--merge` nor `--force` was supplied. A non-zero exit is a hard
  stop, not a prompt to fabricate output.
- The generated rule files are **advisory** (per design spec §"Out of Scope"). Runtime
  enforcement is C10/C11 territory, not R1's. R1 emits rule files, not runtime code, so the
  charter §3.4 FM-audit sub-call does **not** apply — R1 fires no §3.4 sub-call.
- **R-tier carries no `mod-` prefix.** The slash command is `/rules-gen` with no prefix; the
  `mod-` namespace is reserved for M-tier modernization (charter §3.2.M / §3.2.R).
