---
name: spec-write
description: >
  Run a 12-question engineering interview (architecture, ops, observability, risk)
  and draft a structured spec.md with 6 sections plus an ADR appendix, then self-review
  it for placeholders, contradictions, ambiguities, and scope creep. Use when the user
  asks to "write a spec", "draft a design", "run the interview", or "spec out a feature".
---

# /spec-write

Conduct the C1 interview-to-spec workflow directly in this session: interview the user,
draft a reviewable `spec.md`, and self-review it before handing off to C2 `/planner`.

## When to invoke

- Before any non-trivial feature implementation — make the design explicit and reviewable.
- When the user asks to "write a spec", "spec out", or "document the design".
- Before handing off to C2 `/planner`, which consumes a C1-shaped spec.

## When invoked

1. **Interview.** Read `references/interview_questions.yaml`. Ask the 12 questions **one at a
   time**, in order, carrying all prior answers as context so each question builds on the last.
   Never ask more or fewer than the 12 questions. If the user answers tersely, ask one targeted
   follow-up before moving on.
2. **Draft.** Read `references/spec_template.md`. Fill every section from the interview answers,
   mapping each `qNN_*` answer to its target section as the template comments indicate. Produce
   `spec.md` with the 6 required level-2 sections (`Goal`, `Architecture`, `File structure`,
   `Acceptance criteria`, `Risks`, `Decision log`) plus the ADR appendix. Write it to
   `spec.md` in the target directory (default: current working directory).
3. **Self-review.** Re-read the drafted `spec.md` with fresh eyes and flag: placeholders
   (`TBD`/`TODO`/empty sections), missing required sections, ambiguities (a requirement readable
   two ways), contradictions between sections, and scope creep (anything beyond the q01 goal).
   Present findings as a numbered list and prompt the user to resolve any `missing_section` or
   placeholder findings. After the user resolves findings, re-draft only the flagged sections
   and repeat this self-review **at most twice**; mark the spec ready when the re-review passes
   clean (no placeholder or missing-section findings remain), or — if findings persist after two
   passes — present the spec with the remaining concerns flagged as explicit open questions for
   the user. If the user declines to answer a clarifying question or gives contradictory answers,
   record the gap in the spec as an explicit assumption (`Assumed X based on Y; confirm before
   implementation`) and proceed rather than blocking.

## How to invoke

Invoke as `/spec-write`. The user may pass a target directory as the first argument
(e.g. `/spec-write src/features/auth`); otherwise use the current working directory.
If the user provides a feature description inline with the invocation, use it to seed q01
(goal) and ask the remaining questions as needed. If no interactive session is available to
answer the interview questions, say so and stop rather than inventing answers. There is no
Python pipeline — you perform the interview, drafting, and review yourself.

## Output

A `spec.md` file written to the target directory, plus a numbered self-review findings list
presented in the session. The authoritative output contract (section set and ADR appendix)
lives in `## Invariants` below.

## Invariants

- Exactly 12 interview questions — never more, never fewer.
- `spec.md` carries the 6 required level-2 sections in order — `Goal`, `Architecture`,
  `File structure`, `Acceptance criteria`, `Risks`, `Decision log` — plus an ADR appendix.
  This is the authoritative output contract (the T10 completeness gate keys on it).
- Reference `references/interview_questions.yaml` and `references/spec_template.md` by path;
  do not inline or duplicate their content.
