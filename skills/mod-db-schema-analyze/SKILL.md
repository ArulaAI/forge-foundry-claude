---
name: mod-db-schema-analyze
description: >
  Profile a legacy database's DDL exports (CREATE TABLE / VIEW / SEQUENCE / INDEX /
  CONSTRAINT) into a structured `SchemaDigest` — per-table object inventory, foreign-key
  dependency DAG, line-of-business classification, and hotspot / orphan flags — for
  downstream M2/M3/M4 and S60 consumption. The multi-dialect SQL parse (Oracle, SQL Server,
  DB2, PostgreSQL, MySQL via sqlglot) is deterministic Python; this body delegates to
  `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-db-schema-analyze` rather than reproducing it. Use when the user asks
  to "analyze a database schema", "profile a legacy DDL dump", "find schema hotspots",
  "build the M1 digest", or wants an object inventory + dependency DAG of a legacy database.
allowed-tools: Read, Bash
---

# /mod-db-schema-analyze

Walk a legacy database's DDL exports and emit a structured digest (`SchemaDigest`) — by
delegating to the deterministic `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-db-schema-analyze` CLI. The five-dialect
sqlglot parse, object inventory, dependency DAG, line-of-business classification, and
hotspot/orphan detection are Python load-bearing logic in
`primitives/db_schema_analyze/shared/`; this body resolves the DDL input and dialect, invokes
the CLI, reads what it wrote, and acts on the exit code. It does **not** reproduce the parse
pipeline in-session.

## When to invoke

- A modernization engagement starts and the engineer needs the structural shape of a legacy
  schema (hubs, orphans, cycles, line-of-business groupings) before sequencing the rewrite.
- A reviewer wants the M3 strategist (`/mod-sql-strategist`) to operate on a
  *current* digest rather than an out-of-date one.
- A data-flow auditor (S60 `/data-flow-map`) needs PII-classified column metadata anchored to
  a canonical schema view.
- The team needs a structured artifact (`schema.json`, schema-versioned at `"1.0"`) that
  downstream BFSI dashboards can read without parsing the original DDL.
- The user asks to "analyze a database schema", "profile a legacy DDL dump", or "find schema
  hotspots".

## When invoked

1. **Resolve the DDL input and the dialect.** Take the `.sql` file or directory of DDL exports
   the user names (directories are walked recursively; symlinks are not followed) and the
   source SQL dialect. The dialect defaults to `auto` (sqlglot tries each supported dialect and
   picks the highest-confidence parse); set it explicitly to `oracle`, `mssql`, `tsql`, `db2`,
   `postgres`, or `mysql` when the source is known. Choose an `--output-dir` (default
   `.foundry/context/db`). Map any user-named options to the CLI flags listed under **How to
   invoke** below.
2. **Run the extract phase.** Invoke via Bash:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-db-schema-analyze --extract-only \
     --ddl-path <path> --dialect <dialect> --output-dir <dir>
   ```
   This runs Stages A (sqlglot parse) and B (object inventory) deterministically, saves
   intermediate state under `<dir>/.state/`, writes `round-1-prompts.json` to that state
   directory (the CLI prints `WROTE <path>` for it), and exits `7`.
3. **If exit 7 — perform the in-session judgment round.** Read `round-1-prompts.json` from the
   path printed on stdout. For each item in `items`:
   - Read the `system` and `user` fields. They describe a single table whose line-of-business
     category is ambiguous (the regex pack did not match it).
   - Respond with a JSON object matching `response_format.schema` — exactly
     `{"lob": "<label>"}` where `<label>` is one of the closed LoB vocabulary values from
     the system prompt. Do **not** wrap the JSON in markdown fences and do **not** include
     any rationale outside the JSON object.
   - Collect all responses into a `JudgmentEnvelope` and write it to
     `round-1-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "mod-db-schema-analyze",
       "round": 1,
       "items": [
         {"id": "<item.id>", "response": {"lob": "<label>"}},
         ...
       ]
     }
     ```
4. **Run the continue phase.** Invoke via Bash:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-db-schema-analyze --continue \
     --judgments <state-dir>/round-1-judgments.json \
     --ddl-path <path> --dialect <dialect> --output-dir <dir>
   ```
   This applies the judgments (Stages D→E), runs the anti-hallucination guard, and writes
   the final artifacts. The CLI prints `WROTE <path>` for each artifact.
5. **Read what the CLI wrote and summarize.** On exit `0` read `schema-summary.md` and
   summarize for the user: hotspot count, orphan count, dependency-cycle count, parse
   failures. Surface any dropped-table or line-of-business warnings the CLI emitted on
   stderr — the M1-10 anti-hallucination guard logs every name it rejected for
   transparency. If hotspots are many or cycles are present, recommend running
   M3 `/mod-sql-strategist` next.
6. **Non-zero exit (not 7) is a hard stop.** Surface the CLI's stderr and do not fabricate
   a digest. See the exit-code contract under **Invariants** below.

## How to invoke

Invoke as `/mod-db-schema-analyze`. The user passes the DDL source; options map to CLI flags:
`--ddl-path PATH` (file or directory of `.sql` DDL exports — required); `--connect URI` (live
DB; **not implemented at T1** — exits `6` with a T1.5 deferral message, reserved so the CLI
surface stays stable); `--dialect {auto,oracle,mssql,tsql,db2,postgres,mysql}` (source SQL
dialect, default `auto`); `--output-dir PATH` (destination, default `.foundry/context/db`);
`--hotspot-threshold N` (incoming FK in-degree at or above which a table is flagged a hotspot,
default `5`); `--format {both,markdown,json}` (which artifact(s) to emit, default `both`;
`diagrams/schema-dag.mmd` is emitted only under `--format both`). You delegate to
`"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-db-schema-analyze` — you do not perform the parse, inventory, DAG,
classification, or hotspot detection yourself.

### Skill-first mode (in-session, default)

Use `--extract-only` for the first pass, then `--continue --judgments <path>` after
writing `round-1-judgments.json` as described under **When invoked** above. Both invocations
accept the same `--ddl-path`, `--dialect`, and `--output-dir` flags.

### CI mode

CI runs the *same* skill-first loop: the emitted workflow (rendered from `ci.yaml`) invokes the
agent CLI, which loads this SKILL.md, runs `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-db-schema-analyze --extract-only`,
produces the Stage D LoB-classification judgments, and re-invokes with
`--continue --judgments <path>`. The agent *is* the harness — there is no separate harness CLI
flag.

## Output

By default (`--format both`) the CLI writes three files under `--output-dir` (default
`.foundry/context/db/`): `schema.json` (the `SchemaDigest`), `schema-summary.md` (human-readable
summary), and `diagrams/schema-dag.mmd` (Mermaid hotspot DAG); `--format json` / `--format markdown`
write only that single artifact. The authoritative output contract (the file names and what each
carries, the supported dialects, and the exit-code scheme) lives in `## Invariants` below.

## Invariants

- Supported dialects (T1): Oracle, SQL Server (`mssql`/`tsql`), DB2, PostgreSQL, MySQL.
  `--dialect auto` picks the highest-confidence parse.
- `--format both` writes under `--output-dir` (default `.foundry/context/db/`):
  - `schema.json` — `SchemaDigest` (Pydantic v2, `extra="forbid"`, schema-versioned `"1.0"`,
    round-trippable via `SchemaDigest.model_validate_json`). Each `Table`: columns (with
    `pii_classification`), primary key, foreign keys, indexes, line-of-business classification,
    row-count estimate, hotspot/orphan flags.
  - `schema-summary.md` — table counts per line-of-business, top hotspots, orphan list,
    dependency cycles, parse failures.
  - `diagrams/schema-dag.mmd` — Mermaid `graph TD` of top-N hotspot tables and inbound FK neighbours.
- Per-file parse failures exit `0`; unparseable statements land with `parse_status="unparseable"`.
  Anti-hallucination guard (M1-10): closed-vocab line-of-business classification + AST-validated
  table references.
- Exit codes: `0` success; `1` generic error; `2` `--ddl-path` not found or not supplied;
  `3` sqlglot rejected dialect; `4` LLM structural error during classification (CI/harness mode
  only); `5` no parseable DDL objects; `6` `--connect URI` requested but `_db_connector` not
  installed; `7` `--extract-only` succeeded — prompts written, body must judge and call
  `--continue`. Exits other than `0` and `7` are hard stops.
- M1 is a context primitive — §3.4 FM-audit sub-call does **NOT** apply; downstream M3/S60/S70
  invoke their own §3.4 sub-calls on this digest.
