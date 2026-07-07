---
name: mod-stored-proc-analyze
description: >
  Profile a legacy database's stored-procedure / function / trigger source
  (CREATE PROCEDURE / FUNCTION / TRIGGER) into a per-SP structured `StoredProcDigest`
  — static profile (LOC, cyclomatic complexity, table touch count), IO-shape
  classification, side-effect surface, LLM-judged business-logic class, and a
  composite conversion-risk score — so the rewrite can be sequenced risk-descending,
  for downstream M3/M4 and S60 consumption. The multi-dialect SQL parse (Oracle
  PL/SQL, T-SQL, DB2 SQL PL via sqlglot) and the risk scoring are deterministic
  Python; this body delegates to `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-stored-proc-analyze` rather than
  reproducing them. Use when the user asks to "analyze stored procedures", "profile
  legacy stored procs", "score conversion risk", "build the M2 digest", or wants a
  per-SP risk-scored profile of a legacy database's procedure source.
allowed-tools: Read, Bash
delegates-to-cli: true
---

# /mod-stored-proc-analyze

Walk stored-procedure / function / trigger source from a legacy database (Oracle
PL/SQL, T-SQL, DB2 SQL PL) and emit a per-SP structured digest (`StoredProcDigest`) —
by delegating to the deterministic `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-stored-proc-analyze` CLI. The
three-dialect sqlglot parse, static profile, IO-shape classification, side-effect
detection, business-logic classification, and composite conversion-risk score are
Python load-bearing logic in `primitives/stored_proc_analyze/shared/`; this body
resolves the SP source and dialect, invokes the CLI, reads what it wrote, and acts on
the exit code. It does **not** reproduce the parse or scoring pipeline in-session.

## When to invoke

- A modernization engagement starts and the engineer needs to score every SP in a
  legacy Oracle / SQL Server / DB2 dump so they can sequence the rewrite by risk.
- A reviewer wants the M3 strategist (`/mod-modernization-strategist`) or M4 converter
  (`/mod-sp-converter`) to operate on a *current* digest rather than an out-of-date one.
- The team needs a structured artifact (`stored-procs.json`, schema-versioned at `"1.0"`)
  that downstream BFSI dashboards can read without parsing the original DDL.
- The user asks to "analyze stored procedures", "profile legacy stored procs", "score
  conversion risk", or "build the M2 digest".

## When invoked

1. **Resolve the SP source and the dialect.** Take the `.sql` file or directory of
   CREATE PROCEDURE / FUNCTION / TRIGGER source the user names (directories are walked
   recursively; symlinks are not followed) and the source SQL dialect. The dialect
   defaults to `auto` (sqlglot runs each candidate through its dialect detector and
   picks the highest-confidence parse); set it explicitly to `oracle`, `tsql`, `mssql`,
   or `db2` when the source is known. Confirm with the user when a fixture mixes
   dialects so a misclassification cannot silently drop procedures. Choose an
   `--output-dir` (default `.forge-foundry/context/db`). Map any user-named options to the
   CLI flags listed under **How to invoke** below.
2. **Run the extract phase.** Invoke via Bash:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-stored-proc-analyze --extract-only \
     --ddl-path <path> --dialect <dialect> --output-dir <dir>
   ```
   This runs Stages A (sqlglot parse), B (static profile), C (IO-shape classification),
   and D (side-effect detection) deterministically, saves intermediate state under
   `<dir>/.state/`, writes `round-1-prompts.json` to that state directory (the CLI
   prints `WROTE <path>` for it), and exits `7`. Surface any dropped-procedure or
   parse warnings the CLI emitted on stderr — the M2-11 anti-hallucination guard logs
   every name it rejected for transparency.
3. **If exit 7 — perform the in-session judgment round.** Read `round-1-prompts.json`
   from the path printed on stdout. For each item in `items`:
   - Read the `system` and `user` fields. They describe a single stored procedure whose
     business-logic category is ambiguous (the rule-based classifier did not resolve it).
   - Respond with a JSON object matching `response_format.schema` — exactly
     `{"classification": "<label>"}` where `<label>` is one of the eight closed
     vocabulary values from the system prompt:
     `crud_wrapper`, `validation`, `calculation`, `orchestration`,
     `reporting`, `etl`, `trigger_handler`, or `unknown`.
     Do **not** wrap the JSON in markdown fences and do **not** include any rationale
     outside the JSON object.
   - Collect all responses into a `JudgmentEnvelope` and write it to
     `round-1-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "mod-stored-proc-analyze",
       "round": 1,
       "items": [
         {"id": "<item.id>", "response": {"classification": "<label>"}},
         ...
       ]
     }
     ```
4. **Run the continue phase.** Invoke via Bash:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-stored-proc-analyze --continue \
     --judgments <state-dir>/round-1-judgments.json \
     --ddl-path <path> --dialect <dialect> --output-dir <dir>
   ```
   This applies the judgments (Stage E business-logic), runs the risk scorer, applies
   the anti-hallucination guard, and writes the final artifacts. The CLI prints
   `WROTE <path>` for each artifact.
5. **Read what the CLI wrote and report.** On exit `0` read `stored-procs-summary.md`
   and report the **top-10 high-risk stored procedures** (sorted by `risk_score.total`
   descending). For each, name the procedure, its dialect, its composite risk score, and
   the dominant contributing factor (complexity / side-effect breadth / cursor depth /
   dynamic SQL / cross-DB links / write-surface breadth). If the top-3 procedures all score above the high-risk
   threshold, recommend running M4 `/mod-sp-converter` next.
6. **Non-zero exit (not 7) is a hard stop.** Surface the CLI's stderr and do not
   fabricate a digest. See the exit-code contract under **Invariants** below.

## How to invoke

Invoke as `/mod-stored-proc-analyze`. The user passes the SP source; options map to CLI
flags: `--ddl-path PATH` (file or directory of CREATE PROCEDURE / FUNCTION / TRIGGER
source — required; directories are walked recursively, symlinks not followed);
`--dialect {auto,oracle,tsql,mssql,db2}` (source SQL dialect, default `auto`);
`--include-pattern GLOB` (restrict analysis to SP names matching the glob — repeatable,
union semantics, applies only when narrowing the candidate set; default include all);
`--exclude-pattern GLOB` (drop SP names matching the glob — repeatable, applied after
`--include-pattern` via set difference, only when filtering; default exclude none);
`--output-dir PATH` (destination, default `.forge-foundry/context/db`); `--format {both,json,markdown}`
(which artifact(s) to emit, default `both`; `--format json` / `--format markdown` write
only that single artifact). You delegate to `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-stored-proc-analyze` —
you do not perform the parse, static profile, IO classification, side-effect detection,
business-logic classification, or risk scoring yourself.

### Skill-first mode (in-session, default)

Use `--extract-only` for the first pass, then `--continue --judgments <path>` after
writing `round-1-judgments.json` as described under **When invoked** above. Both
invocations accept the same `--ddl-path`, `--dialect`, and `--output-dir` flags.

### CI mode

CI runs the *same* skill-first loop: the emitted workflow (rendered from `ci.yaml`)
invokes the agent CLI, which loads this SKILL.md, runs `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate"
mod-stored-proc-analyze --extract-only`, produces the Stage E business-logic
classification judgments, and re-invokes with `--continue --judgments <path>`. The agent
*is* the harness — there is no separate harness CLI flag.

## Output

By default (`--format both`) the CLI writes two files under `--output-dir` (default
`.forge-foundry/context/db/`): `stored-procs.json` (the `StoredProcDigest`) and
`stored-procs-summary.md` (human-readable summary, procedures sorted by
`risk_score.total` descending); `--format json` / `--format markdown` write only that
single artifact. The authoritative output contract (the file names and what each carries,
the supported dialects, the risk-score composition, and the exit-code scheme) lives in
`## Invariants` below.

## Invariants

- Supported dialects (T1): Oracle PL/SQL, T-SQL (`tsql`/`mssql`), DB2 SQL PL.
  `--dialect auto` picks highest-confidence parse. `source_dialect` is free-form on digest.
- `--format both` writes under `--output-dir` (default `.forge-foundry/context/db/`):
  - `stored-procs.json` — `StoredProcDigest` (Pydantic v2, `extra="forbid"`, schema-versioned
    `"1.0"`, round-trippable via `StoredProcDigest.model_validate_json`). Each `StoredProc`:
    name, schema, language/dialect, `body_loc`, `cyclomatic`, `io_shape`
    (`reads_only`/`reads_writes`/`writes_only`/`cursor_heavy`/`cross_db_link`), side-effect
    surface, `classification` (`crud_wrapper`/`validation`/`calculation`/`orchestration`/
    `reporting`/`etl`/`trigger_handler`/`unknown`), `risk_score` (`RiskScore`: complexity +
    side-effect breadth + cursor depth + dynamic-SQL surface + cross-DB links +
    write-surface breadth; weight matrix v2 `total = complexity×3 + side_effects×2 +
    cursor_depth×4 + dynamic_sql×3 + cross_db×4 + write_breadth×2`, where `write_breadth
    = min(table_touch_count // 2 + (1 if reads_writes else 0), 5)` — #886),
    `touched_tables`, `parse_status`.
  - `stored-procs-summary.md` — procedures sorted by `risk_score.total` descending.
- Per-SP parse failures exit `0`; unparseable procedures land with `parse_status="unparseable"`.
  Anti-hallucination guard (M2-11): closed-vocab classification + AST-validated `touched_tables`.
- Exit codes: `0` success; `1` generic error; `2` `--ddl-path` not found or no
  CREATE PROCEDURE/FUNCTION/TRIGGER source; `3` sqlglot rejected dialect; `4` LLM structural
  error during classification (CI/harness mode only); `5` `--include-pattern`/`--exclude-pattern`
  reduced to zero procedures; `7` `--extract-only` succeeded — prompts written, body must judge
  and call `--continue`. Exits other than `0` and `7` are hard stops.
- M2 is a context primitive — §3.4 FM-audit sub-call does **NOT** apply; downstream
  M3/S60/S70 invoke their own §3.4 sub-calls on this digest.
