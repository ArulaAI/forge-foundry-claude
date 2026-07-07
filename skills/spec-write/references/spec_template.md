# Spec: <!-- One-line title matching the q01_goal answer -->

> **Status:** Draft — pending self-review
> **Author:** <!-- Developer name or team -->
> **Date:** <!-- ISO date YYYY-MM-DD -->
> **Tier:** <!-- C1 | C2 | … | S1 | … -->

---

## Goal

<!-- State the single goal of this feature or system in one sentence.
     Include who benefits (from q02_users_constraints) and what is explicitly OUT of scope.
     Source: q01_goal + q02_users_constraints answers. -->

---

## Architecture

<!-- Describe the high-level technical approach in 3-6 sentences.
     Name the key services, their responsibilities, and the data flow between them.
     List technology choices and third-party dependencies.
     Include a Mermaid diagram if the flow is non-trivial.
     Source: q03_architecture answer. -->

---

## File structure

<!-- List every file that will be CREATED or MODIFIED by this task.
     Use a fenced code block with the tree layout.
     Mark new files with (NEW) and modified files with (MOD).
     Include deletions if any.
     Source: q04_file_scope answer. -->

```
<repo-root>/
  <path/to/new_file.py>         # (NEW) — brief one-line description
  <path/to/existing_file.py>    # (MOD) — what changes
```

---

## Acceptance criteria

<!-- List at least three measurable, falsifiable conditions for "done".
     Each criterion must be independently verifiable (automated test or manual check).
     Prefix each with "[ ]" so it can be checked off.
     Source: q05_acceptance_criteria answer. -->

- [ ] <!-- Criterion 1: specific, observable outcome -->
- [ ] <!-- Criterion 2: specific, observable outcome -->
- [ ] <!-- Criterion 3: specific, observable outcome -->

---

## Risks

<!-- Name the top 3-5 risks across technical, security, and operational dimensions.
     Include the rollback plan from q07_rollback as a dedicated row or sub-section.
     Source: q06_risks + q07_rollback answers. -->

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| <!-- risk 1 --> | <!-- Low/Med/High --> | <!-- Low/Med/High --> | <!-- how handled --> |
| <!-- risk 2 --> | <!-- Low/Med/High --> | <!-- Low/Med/High --> | <!-- how handled --> |
| <!-- risk 3 --> | <!-- Low/Med/High --> | <!-- Low/Med/High --> | <!-- how handled --> |

### Rollback plan

<!-- Describe the rollback mechanism, estimated time-to-rollback, data reversal steps,
     and the approval chain. Source: q07_rollback answer. -->

---

## Decision log

<!-- Two sub-sections: decisions already locked in and decisions still open.
     Source: q11_decisions answer. -->

### Decided

<!-- List decisions that are already made and must not be re-litigated.
     Include a one-sentence rationale for each. -->

- <!-- Decision 1: rationale -->

### Open

<!-- List decisions the reviewer should weigh in on before implementation starts.
     Frame each as a question with 2-3 candidate answers if possible. -->

- <!-- Open question 1: [Option A | Option B | Other] -->

---

## ADR appendix

<!-- Architecture Decision Records for choices made during this feature.
     Add one ADR block per significant decision.
     Source: q11_decisions + any decisions surfaced during review. -->

### ADR-001: <!-- Short title of decision -->

- **Status:** <!-- Proposed | Accepted | Deprecated | Superseded by ADR-XXX -->
- **Context:** <!-- What forces led to this decision? -->
- **Decision:** <!-- What was decided? -->
- **Consequences:** <!-- What are the trade-offs? -->
