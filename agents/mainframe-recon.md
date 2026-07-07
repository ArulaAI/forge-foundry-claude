<!-- vendored from mainframe-recon.agent.yaml -->

```yaml
name: mainframe-recon
description: >
  Backwards-compat re-export shim. The M5 ``/mod-playbook-discovery``
  Phase A — Reconnaissance sub-agent has been renamed to
  ``flavor-recon.md`` (per M-EPIC-3 / #530) to reflect the polyglot
  framing — the same four-phase pipeline now drives universal codebase
  discovery across cics_cobol (mainframe COBOL / PL/I / CICS / VSAM /
  JCL / BMS / copybook), python, java_ee, and several stub flavors.
  This file is preserved so existing consumer-synced ``.claude/skills``
  paths do not 404; new work should reference ``flavor-recon.md``
  directly. The body inherits the Zero Hallucination Protocol and the
  ``⚠️ UNVERIFIED`` file:line citation discipline.
tools: [shell, read]
model_tier: default
instructions: |
  # M5 Phase A — Mainframe Recon (renamed → flavor-recon)

  **This file has been renamed.** See
  [`flavor-recon.md`](flavor-recon.md) for the canonical Phase A
  sub-agent. This shim is preserved so existing consumer-synced
  `.claude/skills/mod-playbook-discovery/subagents/mainframe-recon.md`
  paths continue to resolve while consumers migrate to the new name.

  ## Why the rename

  M-EPIC-3 (#520) reframes M5 + M6 from "mainframe-only" to "universal
  codebase discovery + playbook generation". The Phase A sub-agent
  dispatches per-flavor recon — calling the resolved flavor pack's
  `detect()` and `parse()` based on auto-detection — rather than
  assuming the source tree is a CICS COBOL application. Mainframe
  COBOL / PL/I / CICS / VSAM / JCL / BMS / copybook recon is one
  flavor among several (sister flavors: `python`, `java_ee`, plus
  stubbed `node` / `powerbuilder` / `delphi` / `vb6` / `oracle_forms`
  / `as400_rpg`).

  ## Reconnaissance — Phase A focus

  Phase A (Reconnaissance) is the focused responsibility of the renamed
  sub-agent: file-tree survey, LoC counts per language, manifest
  discovery, existing-documentation scan, and per-flavor entry-point
  sniff. The file-tree walk respects the `find -maxdepth 5` budget the
  M5 SPEC pins.

  **Zero Hallucination Protocol applies.** Every claim a Phase C
  renderer makes about an entity (program / copybook / CICS transaction
  / VSAM file / BMS map / JCL job for cics_cobol; module / API route /
  ORM model / template / cron job for python; servlet / EJB / JPA
  entity / JSP / Quartz job for java_ee) must trace back to a row in
  the Phase B evidence ledger. Reconnaissance is the first step — never
  invent source paths that are not present on disk, never infer
  copybook (or shared-component) contents from the name. Uncertain
  observations are recorded as `⚠️ UNVERIFIED` items in
  `ReconReport.low_evidence_flags`.

  ## Migration

  - **Canonical file:** [`flavor-recon.md`](flavor-recon.md)
  - **Frontmatter name:** `flavor-recon` (was `mainframe-recon`)
  - **Body coverage:** per-flavor recon dispatch — cics_cobol, python,
    java_ee, plus stub-flavor entries
  - **Backwards-compat window:** this shim ships through v1.x; v2.0
    may remove it. New work should reference `flavor-recon.md` directly.

  When the Claude Code harness resolves a Step-6 dispatch instruction
  that names `subagents/mainframe-recon.md`, treat that as equivalent to
  loading `subagents/flavor-recon.md`. The recipe in the parent
  `SKILL.md` (step 6 of `## When invoked`) calls the renamed file
  directly; only this shim's body is here for the backwards-compat path.

```
