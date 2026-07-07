---
name: frontend-design
description: >
  Review frontend components across seven lenses — a11y (WCAG 2.2 AA), component_design,
  state_management, design_system_alignment, performance, i18n_readiness, mobile_responsive —
  grounding each finding under four anti-hallucination paths, then optionally aggregating
  C10/C11 AI-quality findings with dedup discipline. Emits compact JSON findings; the bundled
  script does scan/detect/filter/compliance-linkage/dedup/render — zero model tokens for
  deterministic stages. Use when the user asks to "review frontend components", "check
  accessibility", "audit for WCAG compliance", "review design system alignment", or before
  merging a PR that touches React/TSX/JSX components.
---

# /frontend-design

Frontend component design review across seven lenses for TypeScript/React codebases, directly
in this session: the bundled script enumerates files + detects components, you review each file
across the selected lenses and emit compact JSON findings, then the script applies anti-
hallucination guards, confidence filter, compliance linkage, C10/C11 dedup, and renders the
report.

## When to invoke

- Before merging a PR that touches React/TS/JSX/TSX components.
- When auditing a codebase for accessibility compliance (WCAG 2.2 AA).
- When validating design system alignment (no off-brand tokens or forbidden imports).
- When preparing for a frontend accessibility audit or compliance review.
- When the user asks to "review frontend components", "check accessibility", or "audit for
  WCAG compliance".

## When invoked

1. **Run the scan script to enumerate files and detect components (Stage A — pure Python,
   zero model tokens).** Resolve the target code path the user gives (default: the current
   working directory; a missing path is a hard error, exit 2). Invoke the bundled script in
   scan-only mode to enumerate `.tsx/.ts/.jsx/.js` files and detect React components per file
   (`<skill-dir>` is this skill's own deployed directory — the folder that contains this
   SKILL.md; substitute its path, or `cd` there and drop the prefix):
   ```bash
   python3 <skill-dir>/scripts/fe_review.py --scan-only --scan-root <CODE_PATH>
   ```
   The script skips noise dirs (`node_modules`, `dist`, `build`, `.next`, `__tests__`,
   `__mocks__`, etc.) and test/spec/stories files (`*.test.*`, `*.spec.*`, `*.stories.*`);
   narrows with a `--file-pattern` glob and, when `--git-diff` is given, to only changed
   files. It detects React components per file via heuristics — default-export function, named
   export function, `class X extends (React.)Component`, and arrow / `React.FC` components.
   Read the JSON output to obtain the reviewed-files set and per-file component-name map
   (the anti-hallucination ground truth for Stage B).
2. **Review each file across the selected lenses (Stage B — model judgment only).** Walk the
   seven lenses — **a11y** (WCAG 2.2 AA), **component_design**, **state_management**,
   **design_system_alignment**, **performance**, **i18n_readiness**, **mobile_responsive** —
   restricting to any named via `--lens` (default all seven; all-seven runs batched, one call
   per file). These seven are the **closed** lens set — there is no `error_handling` lens (report
   error-handling gaps under `component_design`); any unrecognized `lens` value in a finding is
   remapped to `component_design` with a stderr WARNING, so use the exact tokens above. For each
   file emit a compact JSON finding list. Each finding carries: `lens`,
   `file`, `line`, optional `component`, `severity` (`low`/`medium`/`high`/`critical`),
   `confidence_score` (float 0–1), `title`, `description`, `suggestion`, optional
   `wcag_criterion` (e.g. `1.1.1`, `2.5.5`), optional `wcag_level`, optional
   `design_token_violated`. Ground your findings in the scanned file list and component map
   from Step 1 — do not invent files or components not returned by the scan. Do **not**
   apply the anti-hallucination guards, confidence filter, compliance linkage, dedup, or
   rendering yourself; the script handles all of that.
3. **Hand off to the deterministic pipeline (Stages C, D, E, dedup, render — pure Python,
   zero model tokens).** Assemble all Stage B findings (plus any C10/C11 findings when not
   `--skip-ai-quality`) into one compact JSON payload:
   ```json
   {
     "findings": [
       {"lens": "a11y", "file": "src/Button.tsx", "line": 10, "severity": "high",
        "confidence_score": 0.85, "title": "...", "description": "...", "suggestion": "...",
        "wcag_criterion": "1.1.1", "component": "Button"}
     ],
     "c10_findings": [...],
     "c11_findings": [...]
   }
   ```
   Write it to a temp file, then run the script:
   ```bash
   python3 <skill-dir>/scripts/fe_review.py \
       --findings-json <payload.json> \
       --scan-root <CODE_PATH> \
       [--git-diff <diff-file>] \
       [--confidence-threshold low|medium|high] \
       [--design-system PATH] \
       [--compliance-context PATH] \
       [--format markdown|json|sarif] \
       [--output PATH]
   ```
   The script deterministically: **(C)** applies the four anti-hallucination guards — **file**
   DROP (finding not in reviewed set is removed), **component** CLEAR (not in detected set →
   None, finding kept), **wcag_criterion** CLEAR (fails `^\d+\.\d+\.\d+$` → None, kept),
   **design_token_violated** CLEAR (not in design-system token set → None, kept); **(D)**
   maps confidence score to level (`>0.7` high, `0.3-0.7` medium, `<0.3` low), drops findings
   below `--confidence-threshold` (default `medium`), and re-validates design tokens (Stage D
   re-validation pass); **(E)** links BFSI compliance controls via deterministic keyword/lens
   matching for `session_timeout`, `pci_masking`, `a11y_assistive` — no model involved; and
   **(dedup)** moves any C10 FM2 (hallucinated React APIs) or C11 FM6 (stale patterns — react
   class components) finding at the same (file, line) as an S30 finding to
   `ai_quality.deduplicated_findings` (S30 finding is canonical). It then renders markdown
   (default), json, or sarif. It needs only `python3` — no installed foundry CLI, no PyYAML.
   Do **not** re-implement any of these stages yourself.
4. **Relay the summary.** Read the script's output (written to `--output`, or stdout under
   `--dry-run`). Present the finding count, files reviewed, and any critical findings to the
   user. The in-session body reports and gates on the result — there is no process exit code
   (`fe_review.py` returns only `0`/`2`): flag for the user when any S30 finding is at or above
   `--max-severity`, and when a critical AI-quality finding remains after dedup.

## How to invoke

Invoke as `/frontend-design`. The user passes the code path (default: current working
directory); options refine the run: `--design-system PATH` (token definitions; auto-discovered
from `.foundry/design-system.yaml`), `--compliance-context PATH` (S70 controls; auto-discovered
from `.forge-foundry/context/compliance.yaml`), `--spec PATH` (spec context),
`--git-diff REF...REF` (narrow to a PR diff), `--file-pattern GLOB` (e.g. `src/components/*.tsx`),
`--lens NAME` (one of the seven lens names, repeatable; default all seven),
`--format markdown|json|sarif` (default `markdown`), `--confidence-threshold low|medium|high`
(default `medium`), `--max-severity high|critical` (in-session gate; the body reports the
breach, there is no process exit code),
`--skip-ai-quality` (skip the C10/C11 sub-calls), and `--output PATH` (write to a file; default
stdout). The model performs only Stage A (reads scan-script output) and Stage B (per-lens
WCAG/design judgment, emitting compact JSON findings); all deterministic stages — scan/detect,
anti-hallucination guards, confidence filter, token re-validation, compliance linkage, dedup,
and rendering — are offloaded to the bundled `scripts/fe_review.py` (stdlib-only; runs under
bare `python3`, no installed foundry CLI). Do not reimplement any deterministic stage yourself.

## Output

A frontend-design review report — Markdown, JSON, or SARIF — listing findings per lens, each
with a severity, confidence, optional WCAG criterion/level, optional design-token violation, and
any linked BFSI compliance controls, plus the aggregated C10/C11 AI-quality findings with their
deduplicated entries. The authoritative output contract (the seven lens names, the four
anti-hallucination paths, the confidence filter, and the dedup discipline) lives in
`## Invariants` below.

## Invariants

- Findings are produced across exactly these seven lenses: **a11y** (WCAG 2.2 AA),
  **component_design**, **state_management**, **design_system_alignment**, **performance**,
  **i18n_readiness**, **mobile_responsive**. This is a **closed** set — there is no
  `error_handling` lens; any unrecognized `lens` value is remapped to `component_design` with a
  stderr WARNING.
- Four anti-hallucination paths ground every finding: **file** — DROP a finding whose file is not
  in the reviewed-files set; **component** — CLEAR (finding retained) a component not in the
  file's detected components; **wcag_criterion** — CLEAR (retained) a criterion that fails
  `^\d+\.\d+\.\d+$`; **design_token_violated** — CLEAR (retained) a token not in the loaded
  `.foundry/design-system.yaml` token set. Only the file path DROPS; the other three CLEAR and
  retain.
- Every finding carries a severity of **low**, **medium**, **high**, or **critical**; confidence
  is mapped from the score (`> 0.7` high, `0.3-0.7` medium, `< 0.3` low) and findings below the
  `--confidence-threshold` (default `medium`) are dropped.
- C10 (FM1-FM5) and C11 (FM6-FM10) AI-quality findings are produced unless `--skip-ai-quality`
  is set; under the dedup discipline a C10 FM2 (hallucinated React APIs) or C11 FM6 (stale
  patterns — react class components) finding at the same (file, line) as an S30 finding is MOVED
  to `ai_quality.deduplicated_findings` (not double-counted), leaving the S30 finding canonical.
- `--max-severity` gates the run **in-session** (the body reports the breach when an S30 finding
  meets it — there is no process exit code; `fe_review.py` returns only `0`/`2`), and critical
  AI-quality findings remaining after dedup stop the user.
- Stage A (scan/component-detect), Stage C (anti-hallucination guards), Stage D (confidence
  filter + token re-validation), Stage E (compliance linkage), C10/C11 dedup, and rendering are
  all offloaded to `scripts/fe_review.py` (stdlib-only). The model supplies only Stage B
  per-lens judgment as compact JSON.
