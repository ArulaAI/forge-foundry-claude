---
name: threat-model
description: >
  Enumerate security threats for a system spec using STRIDE, score each with DREAD, and
  suggest mitigations — optionally aggregating C10/C11 AI-quality findings when a code path
  is given. Loads the spec (data flows, public API, trust boundaries) plus optional
  .foundry/architecture.yaml, walks the six STRIDE categories, scores damage/reproducibility/
  exploitability/affected-users/discoverability, and renders a Markdown, JSON, or SARIF report.
  Use when the user asks to "run a threat model", "enumerate threats", "do STRIDE analysis",
  "assess security risks", or when designing security controls for a new feature.
---

# /threat-model

STRIDE/DREAD threat enumeration on a system spec, directly in this session: load the spec and
optional architecture context, enumerate threats across the six STRIDE categories, assign the
five DREAD dimensions per threat, hand the compact JSON payload to the bundled renderer script
for deterministic scoring + rendering, optionally aggregate C10/C11 AI-quality findings when a
code path is given, then relay the results.

## When to invoke

- During spec review for security-sensitive features — model threats before implementation.
- Before a pentest engagement — enumerate known threats first.
- For compliance reviews requiring formal threat documentation (PCI, SOC 2).
- When designing security controls for a new feature.
- When the user asks to "run a threat model", "enumerate threats", or "do STRIDE analysis".

## When invoked

1. **Load the spec and architecture context.** Read the C1-shaped spec at the path the user
   gives (default: the current working directory) — parse out its title, summary, scope, file
   structure, and public API. If a `--arch-yaml` path is given, or `.foundry/architecture.yaml`
   is found beside the spec or in the cwd, load it into typed layers + components (each
   component's `layer` must appear in the `layers` list; a malformed file is a hard error, a
   missing file is silently skipped and the spec context is used alone).
2. **Enumerate STRIDE threats and assign DREAD dimensions (judgment — your work).** Walk all
   six STRIDE categories in order — **Spoofing**, **Tampering**, **Repudiation**, **Information
   disclosure**, **Denial of service**, **Elevation of privilege** — and enumerate the threats
   in each, given the spec and architecture context. Each threat carries: a unique id, the
   STRIDE category, a title, description, affected component, a mitigation, a residual-risk
   note, optional CWE ids, and the five DREAD integer dimensions on a 1–10 scale — **Damage**,
   **Reproducibility**, **Exploitability**, **Affected users**, **Discoverability**. A threat
   you leave without a DREAD block will have its dimensions defaulted to 5/5/5/5/5 by the
   renderer script.
3. **Optionally gather AI-quality findings (when a code path is given).** When `--code-path`
   is provided and `--skip-ai-quality` is not set, run C10 (`audit_path`, FM1–FM5 AI failure
   modes) and C11 (`audit_production_path`, FM6–FM10 production failure modes) over that path
   in static-only mode and collect their findings as `ai_quality_runs` entries for the payload.
   (PASTA methodology, when requested, additionally runs PASTA stages 1 + 4 only — objectives
   and threat analysis — and the output is flagged as not audit-grade for regimes needing the
   missing stages.)
4. **Hand off to the deterministic renderer (pure Python, zero model tokens).** Assemble your
   judgment into one compact JSON payload with this **exact shape** — the DREAD dimensions MUST
   be a nested `"dread"` block (flat top-level dread keys are silently ignored → 5/5/5/5/5), the
   CWE field MUST be the array key `"cwe_ids"` (a bare `"cwe"` is salvaged with a WARNING), and
   `"category"` MUST be one of the six enum tokens `spoofing`, `tampering`, `repudiation`,
   `info_disclosure`, `denial_of_service`, `elevation_of_privilege` (an unrecognized value is
   dropped from the grouped report). **Assign each threat a `"file"` and `"line"`** pointing at
   the source location (in the spec or code) the threat concerns — the JSON output carries them
   through so a downstream composer (S12 `/red-team-review` ingests `threat.json` expecting
   `severity`/`file`/`line` per threat) can attribute and dedup threats by location. `"severity"`
   is optional: when omitted it is derived from the DREAD label (`critical`/`high`/`medium`/`low`);
   supply it explicitly only to override:
   ```json
   {
     "spec_title": "Checkout service",
     "methodology": "stride",
     "threats": [
       {
         "id": "T-001",
         "category": "info_disclosure",
         "title": "Session token leaked in logs",
         "file": "src/auth/logger.py",
         "line": 42,
         "description": "...",
         "component": "auth-gateway",
         "mitigation": "...",
         "residual_risk": "...",
         "cwe_ids": ["CWE-532"],
         "dread": {
           "damage": 8, "reproducibility": 7, "exploitability": 6,
           "affected_users": 9, "discoverability": 5
         }
       }
     ]
   }
   ```
   Then invoke the bundled script (`<skill-dir>` is this skill's own deployed directory — the
   folder that contains this SKILL.md; substitute its path, or `cd` there and drop the prefix):
   ```bash
   python3 <skill-dir>/scripts/threat_score.py \
       --threats-json <payload.json> \
       --format <markdown|json|sarif> \
       [--output <FILE>] \
       [--max-severity <high|critical>]
   ```
   The script does all deterministic work: computes DREAD means
   (`risk = mean(damage, reproducibility, exploitability, affected_users, discoverability)`),
   applies risk labels (CRITICAL ≥ 9.0, HIGH ≥ 7.0, MEDIUM ≥ 4.0, else LOW), checks the
   `--max-severity` gate (exits 4 when any threat breaches the threshold), and renders the
   report in the requested format. In SARIF, threat-model findings occupy `runs[0]`; each
   AI-quality auditor that produced findings is appended as its own `runs[]` entry (C10 before
   C11). It needs only `python3` — no installed foundry CLI, no third-party dependencies. Do
   **not** compute DREAD means, risk labels, or render any format yourself.
5. **Relay the results.** Read the script's output (or its JSON summary when `--output` was
   used) and present it to the user; surface any threat at or above `--max-severity` and stop
   on critical AI-quality findings.

## How to invoke

Invoke as `/threat-model`. The user passes the path to a C1-shaped spec; options refine the
run: `--code-path PATH` (source directory that triggers the C10/C11 AI-quality sub-calls),
`--arch-yaml PATH` (architecture context; auto-detected from `.foundry/architecture.yaml`),
`--methodology stride|pasta` (default `stride`), `--format markdown|json|sarif` (default
`markdown`), `--output FILE` (write to a file; default stdout), `--max-severity high|critical`
(gate: `high` = DREAD ≥ 7.0, `critical` = DREAD ≥ 9.0), and `--skip-ai-quality` (skip the
C10/C11 sub-calls). You perform spec load, STRIDE enumeration, DREAD dimension assignment, and
optional AI-quality sub-calls; the deterministic remainder — DREAD means, risk labels, the
severity gate, and all three renderers — is offloaded to the bundled
`scripts/threat_score.py` (stdlib-only; runs under bare `python3`,
no installed foundry CLI).

## Output

A threat-enumeration report — Markdown, JSON, or SARIF — listing each threat by STRIDE
category with its DREAD risk score and a suggested mitigation. When a code path is given, the
report also carries the aggregated C10/C11 AI-quality findings, which in SARIF land in
separate `runs[]` entries. The authoritative output contract (the six STRIDE categories, the
DREAD dimensions, mitigations, the SARIF runs separation, and the severity-threshold
behavior) lives in `## Invariants` below.

## Invariants

- Threats are enumerated across exactly these six STRIDE categories: **Spoofing**,
  **Tampering**, **Repudiation**, **Information disclosure**, **Denial of service**,
  **Elevation of privilege**.
- Every threat is scored across the five DREAD dimensions on a 1–10 scale — **Damage**,
  **Reproducibility**, **Exploitability**, **Affected users**, **Discoverability** — and its
  risk is the mean of the five (CRITICAL ≥ 9.0, HIGH ≥ 7.0, MEDIUM ≥ 4.0, else LOW),
  computed deterministically by `scripts/threat_score.py`. A threat with no DREAD block
  defaults to 5/5/5/5/5.
- Every threat carries an affected component, a suggested mitigation, and a residual-risk note.
- Payload shape is exact: DREAD dims live under a nested `"dread"` block (flat keys default to
  5/5/5/5/5); CWE ids under the array key `"cwe_ids"` (a bare `"cwe"` is salvaged with a WARNING);
  `"category"` is one of the enum tokens `spoofing`, `tampering`, `repudiation`, `info_disclosure`,
  `denial_of_service`, `elevation_of_privilege` (an unrecognized value is dropped from the grouped
  report).
- C10 (FM1–FM5) and C11 (FM6–FM10) AI-quality findings are produced only when `--code-path` is
  given and `--skip-ai-quality` is unset; in SARIF the threat-model findings occupy `runs[0]`
  and each auditor that produced findings is appended as its own additional `runs[]` entry (C10
  before C11) — consumers must match runs by tool/role, not by fixed index.
- `--max-severity` gates the run: `high` flags any threat with DREAD ≥ 7.0, `critical` flags
  DREAD ≥ 9.0; critical C10/C11 findings stop the user.
