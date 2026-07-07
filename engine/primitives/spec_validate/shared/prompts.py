"""C5 /spec-validate — LLM prompt templates.

Stage B: Code-element search per acceptance criterion.
Stage E: Scope drift detection (unmentioned public features).

C1 regression guard: all MatchStatus Literal values ("satisfied", "partial",
"unsatisfied", "not_applicable") and all CodeElementKind Literal values
("function", "class", "method", "endpoint", "test", "constant", "other")
appear verbatim in the Stage B prompt.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Stage B — Code-element search + match classification
# ---------------------------------------------------------------------------

STAGE_B_SYSTEM_PROMPT = """\
You are a senior software engineer performing spec-to-code traceability analysis.

Given an acceptance criterion and a codebase summary (top-level files and public symbols),
identify the code element(s) that implement the criterion.

Return JSON and NOTHING else — no markdown fences, no explanation:
{
  "status": "satisfied" | "partial" | "unsatisfied" | "not_applicable",
  "code_elements": [
    {
      "file": "<relative file path>",
      "line": <int>,
      "name": "<symbol name>",
      "kind": "function" | "class" | "method" | "endpoint" | "test" | "constant" | "other"
    }
  ],
  "rationale": "<brief explanation of match or why unsatisfied>",
  "confidence": "low" | "medium" | "high"
}

Status definitions:
- satisfied: The criterion is fully implemented — the code element(s) exist and implement it.
- partial: The criterion is partially implemented — some code elements exist but not all aspects.
- unsatisfied: No code element implementing the criterion was found.
- not_applicable: The criterion does not apply to the provided code
  (e.g., a documentation-only criterion in a code review).

Rules:
- kind MUST be exactly one of: function, class, method, endpoint, test, constant, other
- status MUST be exactly one of: satisfied, partial, unsatisfied, not_applicable
- confidence MUST be exactly one of: low, medium, high
- file MUST be one of the files listed in the codebase summary
  (anti-hallucination: only reference files that exist)
- line MUST be a positive integer from that file
- name MUST be a symbol visible in the file (anti-hallucination: only reference symbols that exist)
- If unsatisfied, return code_elements = []
- Return not_applicable only when the criterion cannot be evaluated from source code
"""

# ---------------------------------------------------------------------------
# Stage E — Scope drift detection
# ---------------------------------------------------------------------------

STAGE_E_SYSTEM_PROMPT = """\
You are a senior software engineer performing spec coverage analysis.

Given the spec text and a list of public-API features in the implementation,
identify features that are NOT mentioned or implied in the spec.

Return JSON and NOTHING else — no markdown fences, no explanation:
[
  {
    "file": "<relative file path>",
    "line": <int>,
    "name": "<symbol name>",
    "kind": "<function|class|method|endpoint|constant|other>",
    "description": "<brief description of what this feature does and why it lacks spec backing>"
  }
]

Rules:
- Only include features whose purpose is NOT described anywhere in the spec
- file MUST be one of the files listed in the implementation summary (anti-hallucination)
- Return [] if all public features are mentioned or implied in the spec
- Omit private helpers (names starting with _) from scope-drift consideration
"""
