---
name: mod-synth-logs
description: >
  Generate a deterministic 72-hour bundle of cross-correlated synthetic
  operational evidence (logs) for an application or platform — a stand-in for
  real production telemetry when none is available — by delegating to the
  `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-synth-logs` CLI. The four-phase pipeline (Phase A ingest
  → B scenario plan → C render + run `generate_logs.py` → D
  validate) is deterministic Python (Phase B is deterministic: seeded random, fixed constants, zero LLM calls); this body
  resolves the input, invokes the CLI, reads the emitted generator + evidence,
  and acts on the exit code rather than reproducing the pipeline. v1 ships the
  mainframe CICS/VSAM/JCL log catalog as the proven default; per-platform
  catalogs land via the same flavor-pack registry M5 uses. Use when the user
  asks to "generate synthetic operational evidence", "synthesise 72-hour
  application logs", "produce a cross-correlated log bundle", "stand up a
  synthetic production-failure observability fixture", or "synthesise
  CICS / VSAM / JCL mainframe logs" — or wants the M6 evidence generator for a
  modernization engagement.
allowed-tools: Read, Bash
---

# /mod-synth-logs

Synthesise a 72-hour bundle of cross-correlated synthetic operational evidence
(logs) that documents how an application or platform behaves under load and
incident — by delegating to the deterministic `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-synth-logs`
CLI. The four-phase pipeline (Phase A ingest → B scenario plan → C render +
run the generator script → D validate), the closed-universe anti-hallucination
layering, and the per-flavor log catalogs are Python load-bearing logic in
`primitives/synth_logs/shared/` (`ingest.py` consumes the M5 `PLAYBOOK.md` or
re-uses M5's recon/inventory chain from `primitives._shared.discovery` against
`--code-root`). This body resolves the playbook/codebase input, invokes the CLI,
reads the emitted generator script plus its evidence, and acts on the exit code.
It does **not** reproduce the synthesis pipeline in-session.

## When to invoke

- A modernization engagement (or any reverse-engineering exercise) has produced
  an M5 `PLAYBOOK.md` and the team needs realistic operational logs to test a
  Playbook Discovery / dependency-analysis run against a known-good ground truth.
- The team is staging a C11 `/production-failure-audit` dry-run against a target
  codebase and needs a realistic 3-day incident arc (for the v1 mainframe
  catalog, a day-3 cascading ASRA / AEY9 abend sequence).
- A consumer wants a reproducible synthetic-observability fixture baked into
  their repo — the generator script is committed; the evidence is a build
  artefact regenerated on demand from the same `--seed`.
- The user asks to "generate synthetic operational evidence", "synthesise
  72-hour application logs", "produce a cross-correlated log bundle", or "stand
  up a synthetic production-failure observability fixture".

## When invoked

1. **Resolve the playbook or codebase input.** Prefer `--playbook` — an M5
   `PLAYBOOK.md` grounds every log entity in real source citations and is the
   anti-hallucination layer's closed universe. **Check the catalog-coverage
   matrix below before invoking**: v1 ships only the `cics_cobol` (mainframe
   CICS/VSAM/JCL/Enterprise Server) catalog; a playbook from a Python, Java EE,
   or other non-mainframe codebase exits `6` with "no log catalog registered for
   flavor 'X'; v1 supports: cics_cobol" — that is a clean, informative stop, not
   a crash. If the user has not run M5, point them at `./PLAYBOOK.md` first. When
   no playbook exists, fall back to `--code-root` (a **mainframe** source tree the
   Phase A ingest recons via `primitives._shared.discovery`). A `--code-root` walk
   now detects its real platform flavor too, so a **non-mainframe** source tree
   exits `6` consistently with the same "no log catalog registered" message
   (#1198) — it no longer masquerades as `cics_cobol`. Without either readable
   input the CLI exits `2`. Then settle the scenario arc (`--scenario`,
   default `incident`), traffic volume (`--scale`, default `mid`), and a `--seed`
   if reproducibility matters (suggest `--seed 42` for the BankDemo reference).
   Map any user-named options to the CLI flags listed under **How to invoke** below.
2. **Invoke the CLI via Bash.** Run `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-synth-logs` with the
   resolved `--playbook` (or `--code-root`) plus the option flags. All four
   phases (A ingest, B scenario plan, C render, D validate) are **deterministic**
   — Phase B is seeded-random with fixed constants and makes zero LLM calls (see
   the determinism contract in `shared/plan.py`). Pass `--seed N` if
   reproducibility matters. Do not reproduce the synthesis pipeline in this
   session. Example:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-synth-logs --playbook ./PLAYBOOK.md --seed 42
   "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-synth-logs --code-root ./mainframe-src --scenario baseline
   ```
   Surface any anti-hallucination warnings on stderr — Phase A's `entities.json`
   is the closed universe and the renderer's Jinja2 `StrictUndefined` fails
   loudly if Phase B referenced a program / transaction / file / map / job that
   does not exist in the playbook.
3. **Read the emitted generator and evidence.** On success the CLI renders
   `generate_logs.py` (the committable, reproducible generator; per-platform
   catalogs may rename this in a future release),
   runs it per `--run-script` to emit the catalog's evidence files under
   `--output-dir` (default `./logs/`), and writes `logs-manifest.json`. Read
   `logs-manifest.json` and summarise: scenario + scale + window + seed, the
   per-file line counts, the Phase D `correlation_rate` (95% floor), the
   timeline-consistency verdict, and `validation_status` (`pass` / `fail`).
4. **Act on the exit code.** Exit `0` means the generator + evidence were
   written and Phase D validation passed; proceed. A non-zero exit is a hard
   stop: surface the CLI's stderr and do not fabricate evidence. See the
   exit-code contract under **Invariants** below.

## How to invoke

Invoke as `/mod-synth-logs`. The user passes the playbook/codebase input;
options map to CLI flags: `--playbook PATH` (M5 `PLAYBOOK.md` — preferred, the
closed-universe grounding); `--code-root PATH` (source tree fallback when
`--playbook` is absent or unreadable — at least one of the two must resolve or
the CLI exits `2`); `--output-dir PATH` (destination for the evidence files +
`logs-manifest.json`, default `./logs/`); `--window-hours INT` (total scenario
duration, default `72`, minimum `24`, recommended multiples of 24); `--scenario
{baseline,peak,incident}` (narrative arc, default `incident` — `baseline` =
day-1 normal across the window, `peak` = month-end / heavy-batch overlay,
`incident` = 3-day arc with day-3 cascading abend); `--scale {small,mid,large}`
(traffic volume, default `mid`; warn the user that `large` multiplies per-file
line counts and wall-time, so pair it with `--run-script skip` if Phase C would
be slow); `--seed INT` (deterministic seed for reproducible generation; omit for
wall-clock-seeded non-reproducible runs); `--run-script {subprocess,import,skip}` (Phase C execution
model, default `subprocess` — `import` for fast in-process runs with
traceback-rich errors, `skip` to render `generate_logs.py` and
exit without executing it, for a "render now, run later" CI flow). You delegate
to `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-synth-logs` — you do not perform Phase A ingest, Phase B
planning, Phase C rendering, or Phase D validation yourself.

## Output

The CLI renders `generate_logs.py` (the committable generator
script — re-running it with the same `--seed` reproduces the evidence bytes) and,
unless `--run-script skip`, executes it to emit the active catalog's evidence
files plus `logs-manifest.json` under `--output-dir` (default `./logs/`). The v1
mainframe catalog emits ten log files (five text, four JSON-Lines, one
Prometheus) — `cics-transaction.log`, `cics-access.log`, `network.log`,
`database.log`, `enterprise-server.log`, `downstream-calls.log`,
`upstream-requests.log`, `cics-queues.log`, `metrics.log`, `security-audit.log`;
file count and shapes are properties of the active catalog, not of M6 itself.
`logs-manifest.json` carries scenario metadata + the Phase D validation report
(`correlation_rate`, `timeline_consistency`, `content_accuracy`,
`validation_status`). The authoritative output contract (the generator name, the
per-flavor evidence catalog and the mainframe default, the line-count floors, and
the exit-code scheme) lives in `## Invariants` below.

## Invariants

- Persisted JSON schemas: `EntitiesDigest`, `ScenarioPlan`, `ValidationReport`, `ManifestRecord`
  — flavor-agnostic. Content comes from a per-platform **log catalog**. v1 ships the mainframe
  (CICS/VSAM/JCL/Enterprise Server) **log catalog** of **ten log formats**; additional catalogs
  land via M5's flavor-pack registry with no schema change.
  Phase A entity walk uses `primitives._shared.discovery` recon/inventory when `--code-root` is given.
- **Catalog coverage (v1)** — only `cics_cobol` has a registered log catalog. All other flavors
  (`python`, `java_ee`, `jcl_batch`, `ims_db`, `generic`) exit `6` with "no log catalog registered
  for flavor 'X'; v1 supports: cics_cobol". The catalog-dispatch guard in `shared/catalog.py`
  fires **before** Phase B so the error is specific, not generic. T2 catalogs land as individual
  PRs adding an entry in `REGISTERED_CATALOGS` + `shared/catalog/<name>/` + templates.
- **Closed-universe anti-hallucination**: Phase A's `entities.json` is the closed universe; Phase
  B uses only entities from Phase A; Phase C renderer uses Jinja2 `StrictUndefined` (undefined
  reference raises at render time); Phase D validates every log entity against `entities.json`.
  All four phases are deterministic (Phase B seeded random, zero LLM calls). `generate_logs.py`
  is consumer-committable; same seed → same bytes. Evidence files are build artefacts, not committed.
- **Line minimums are floors**: under v1 mainframe catalog all ten files emit unconditionally
  (absence of one is a Phase D failure); `--scale large` multiplies per-file line counts.
- **§3.4 operational-evidence exemption** — M6 emits operational evidence, not architectural
  artefacts; §3.4 auto-sub-call does **NOT** fire. No §3.4 sub-call issued. A consumer wanting
  C11 `/production-failure-audit` against the synthesised scenario invokes it manually. Exemption
  applies across every catalog (artefact-class rule, not platform-specific).
- Exit codes: `0` success (generator emitted, evidence emitted unless `--run-script skip`, Phase D
  passed); `2` neither `--playbook` nor `--code-root` resolves to a readable path; `3` unknown
  `--scenario`/`--scale` enum; `4` Phase D content-accuracy below floor (entity doesn't resolve
  against `entities.json`) — also transitional code for not-yet-wired stub; `5` Phase C
  `--run-script subprocess` non-zero (stderr in `logs-manifest.json`); `6` **two triggers**: Phase D
  correlation rate below 95% floor, OR the resolved playbook flavor has no registered catalog
  (see catalog coverage above). Non-zero is a hard stop.
- Slash command: `mod-synth-logs` (`mod-` prefix, charter §3.2.M — distinct from S60
  `data-flow-map`).
