---
name: security-review
description: >
  Deep security review of Python, TypeScript, and Java across seven OWASP/NIST-grounded
  lenses — authn, authz, crypto, input_handling, data_exposure, secrets_mgmt, owasp_top10 —
  with confidence-based filtering, OWASP-category + CWE tagging, and compliance cross-refs.
  Detects security-sensitive areas, enumerates the target files (optionally narrowed to a PR
  diff), reviews each across the selected lenses producing a severity + confidence per finding,
  drops findings below the confidence threshold, annotates compliance refs, fires the C10/C11
  §3.4 sub-calls in-session and dedups overlaps against the S11 findings, then renders a
  Markdown, JSON, or SARIF report. Use when the user asks to "run a security review", "check
  for vulnerabilities", "audit auth code", "review the crypto implementation", or before
  shipping a security-sensitive feature.
allowed-tools: Read, Grep, Glob, Bash
---

# /security-review

Deep security review with seven OWASP/NIST-grounded lenses, directly in this session: detect the
security-sensitive areas, enumerate the target files (optionally narrowed to a PR diff), review
each across the selected lenses producing a severity and a confidence per finding, filter out
findings below the confidence threshold, annotate compliance refs, fire the C10/C11 §3.4
AI-quality sub-calls in-session and dedup overlaps against the S11 findings, then render the
report. This goes beyond C4's surface `security_smell` lens to full OWASP Top 10 coverage with
CWE ids and compliance references.

## When to invoke

- Before major releases or PR merges touching auth, crypto, or payment code.
- When a compliance audit requires evidence of security analysis.
- When adding new authentication, authorization, or data-handling features.
- For a vulnerability assessment before a pentest.
- When the user asks to "run a security review", "check for vulnerabilities", or "audit auth code".

## When invoked

1. **Detect sensitive areas and enumerate the files to review (extract phase).** Resolve the
   target path the user gives (default: the current working directory; a missing path is a hard
   error). First scan for security-sensitive areas — filename heuristics (`auth*`, `login*`,
   `session*`, `jwt*`, `crypto*`, `encrypt*`, `cipher*`, `hash*`, `secrets*`, `password*`,
   `credential*`, `key*`, `token*`) and directory heuristics (under `*/auth/`, `*/security/`,
   `*/crypto/`, `*/secrets/`, `*/credentials/`, `*/login/`) — each classified by kind (**auth**,
   **crypto**, **secret**, **data**, **general**) with a reason. When `.foundry/security.yaml` is
   present beside the path (or at `--security-yaml`), fold its declared `sensitive_paths` /
   `auth_modules` / `crypto_modules` / `secret_locations` into the sensitive-area set. Then
   enumerate the source files, honoring an optional language filter and an optional glob
   (`--file-pattern`). When a PR-diff range is given, narrow the set to the files that range
   changed. If a `--spec` path is given, read it as context for relevance. The result of this
   phase is one prompt item per source file (id prefix `review:<relative-path>`).
2. **Judge each prompt item (per-file security lens review).** For each item in
   `round-1-prompts.json` (one item per source file):
   - Read the `system` and `user` fields. They describe a single source file and the security
     lenses to apply to it.
   - Respond with a JSON array matching `response_format.schema` — a `findings` array where each
     finding carries: `lens` (one of the seven security lens names), `file` (the file under review
     — do not invent a different path), `line` (1-indexed), optional `end_line`, `severity`
     (`low`/`medium`/`high`/`critical`), `confidence_score` (a float in `[0,1]`; the
     pipeline maps it to the categorical `confidence`), `title`,
     `description`, optional `suggestion`, optional `owasp_category` (`A01`–`A10`), and optional
     `cwe_ids` array. Walk all selected lenses: **authn** (login flows, session mgmt, password
     storage, MFA, JWT; NIST SP 800-63B), **authz** (RBAC/ABAC, IDOR, privilege escalation;
     OWASP A01), **crypto** (algorithm selection, key sizes, IV/nonce reuse; NIST 800-53 SC),
     **input_handling** (SQL injection, command injection, path traversal, XSS; OWASP A03),
     **data_exposure** (PII in logs, sensitive error messages, debug mode; GDPR Art. 32),
     **secrets_mgmt** (hardcoded secrets, credential rotation; NIST SP 800-57), and
     **owasp_top10** (OWASP 2021 A01–A10 mapping). Apply the anti-hallucination guards: set
     `file` exactly to the file under review; drop any `compliance_refs` not present in the
     loaded `compliance.yaml`. Do **not** wrap the JSON in markdown fences.
   - Collect all responses into a `JudgmentEnvelope` and write it to
     `round-1-judgments.json` in the same state directory. The envelope format is:
     ```json
     {
       "schema_version": "1.0",
       "primitive": "security-review",
       "round": 1,
       "items": [
         {"id": "review:<relative-path>", "response": [<findings array>]},
         ...
       ]
     }
     ```
3. **Apply judgments and render (continue phase).** Apply the confidence filter — `> 0.7` high,
   `[0.3, 0.7]` inclusive medium, `< 0.3` low; drop every finding below `--confidence-threshold`
   (default **medium**). When a `compliance.yaml` is present (at `--compliance-yaml` or
   auto-detected at `.foundry/compliance.yaml`), validate and annotate each finding's
   `compliance_refs` against the known control set, dropping unknown refs. Fire the C10/C11 §3.4
   sub-calls in-session and dedup (unless `--skip-ai-quality` is set), scoped to C10 **FM2**/**FM4**
   and C11 **FM7**/**FM8**: for each such finding at the same (file, line) as an S11 finding,
   **MOVE** it to `ai_quality.deduplicated_findings` — the S11 finding stays canonical. Render the
   report in the requested format — `markdown` (default), `json`, or `sarif` — to stdout or
   `--output`. Summarize the findings, surface any finding at or above `--max-severity`, and stop
   the user on critical S11 findings or on critical C10/C11 findings remaining after dedup. In
   SARIF the S11 findings occupy `runs[0]`, C10 `runs[1]`, C11 `runs[2]` (latter two when
   sub-calls ran).

## How to invoke

Invoke as `/security-review`. The user passes the path to review (default: the current working
directory); options refine the run: `--spec PATH` (spec file for relevance context),
`--security-yaml PATH` (path to `.foundry/security.yaml`; default auto-detect), `--compliance-yaml
PATH` (path to `.foundry/compliance.yaml` for compliance-ref annotation; default auto-detect),
`--git-diff REF...REF` (narrow to a PR diff, e.g. `HEAD~1...HEAD`), `--file-pattern GLOB` (filter
files, e.g. `src/*.py`), `--language auto|python|java|ts` (default `auto`), `--lens
authn|authz|crypto|input_handling|data_exposure|secrets_mgmt|owasp_top10` (one lens, repeatable;
default all seven), `--confidence-threshold low|medium|high` (default `medium`), `--max-severity
high|critical` (the gate threshold), `--skip-ai-quality` (skip the C10/C11 sub-calls), `--format
markdown|json|sarif` (default `markdown`), `--output PATH` (write to a file; default stdout), and
`--include-vendored` (re-include vendored harness trees in the sensitive-area + file-enumeration
walks; default off, dependency/build dirs stay excluded).
You perform the sensitive-area detection, file enumeration, per-lens review, confidence filter,
compliance annotation, in-session C10/C11 aggregation + dedup, and rendering yourself — there is
no CLI to shell out to.

### Skill-first mode (in-session, default)

The body performs the extract→judge→continue loop directly in this session: detects sensitive areas
and enumerates files (extract), reviews each against the seven security lenses producing findings
in the `round-1-prompts.json` prompt format (judge), then applies the confidence filter, compliance
annotation, and dedup discipline and renders the report (continue).

### CI mode

CI runs the *same* skill-first loop: the emitted workflow (rendered from `ci.yaml`) invokes the
agent CLI, which loads this SKILL.md, runs the deterministic `--extract-only` stage, produces
the per-file judgments, and re-invokes with `--continue --judgments <path>`. The agent *is* the
harness — there is no separate harness CLI flag.

## Scan exclusions

`detect_sensitive_areas` uses `primitives._shared.scan_exclusions.is_excluded` to exclude `.venv`, `node_modules`, `__pycache__`, `.git`, build dirs, and the wholly-vendored harness dirs (`.foundry`, `.claude`) and the emitted-harness subpaths (`.github/skills`, `.agents/skills`, `.agent/skills`, `.cursor/rules`) — `.github/workflows/`, `.github/CODEOWNERS`, etc. are still scanned from the sensitive-area scan. Symlinks are also skipped (SEC-10). Pass `--include-vendored` (threaded end-to-end to `include_vendored=True`) to re-include the vendored dirs + emitted subpaths when auditing emitted artifacts. See SPEC.md §Scan exclusions.

## Output

A security-review report — Markdown, JSON, or SARIF — leading with the detected sensitive areas,
then each finding by lens with its severity, confidence level + score, OWASP category, CWE ids,
and compliance refs, followed by the aggregated C10/C11 AI-quality findings and their
deduplicated entries. The authoritative output contract (the seven lenses, the severity levels,
the confidence filter, the anti-hallucination guards, the §3.4 dedup discipline, the SARIF runs
layout, and the gate behavior) lives in `## Invariants` below.

## Invariants

- Seven lenses — **authn**, **authz**, **crypto**, **input_handling**, **data_exposure**,
  **secrets_mgmt**, **owasp_top10** — all by default; `--lens` selects a subset.
- Every finding: severity **low/medium/high/critical**, confidence `[0,1]`, optional OWASP
  category `A01`–`A10`, any CWE ids. Anti-hallucination: wrong `file` → dropped; `compliance_refs`
  not in loaded `compliance.yaml` → dropped.
- Confidence thresholds: `> 0.7` high, `[0.3, 0.7]` inclusive medium, `< 0.3` low; findings
  below `--confidence-threshold` (default `medium`) dropped.
- C10 (FM1–FM5) + C11 (FM6–FM10) aggregated unless `--skip-ai-quality`; dedup scoped to C10
  **FM2**/**FM4** and C11 **FM7**/**FM8** — same (file, line) as an S11 finding → **MOVED** to
  `ai_quality.deduplicated_findings`, S11 canonical.
- SARIF: S11 → `runs[0]`, C10 → `runs[1]`, C11 → `runs[2]` (latter two when sub-calls ran).
- `--max-severity` gate: critical S11 findings, and critical C10/C11 surviving dedup, stop the user.
- Exit codes: `0` success; `2` code path not found; `4` findings exceed `--max-severity`; `5`
  critical C10/C11 findings surviving dedup (CI/harness mode only); `7` `--extract-only`
  succeeded — prompts written, body must judge and call `--continue`. Exits other than `0` and
  `7` are hard stops.
