"""C8 /architecture-review — Stage E: subjective review prompt + judgment.

Builds the Stage E prompt (abstraction quality, API design cohesion,
cross-cutting concerns leakage) and applies the model's in-session judgment.

Anti-hallucination validator: any SubjectiveIssue.module not in dep_graph.modules
is dropped with a stderr warning.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from primitives.architecture_review.shared.models import DepGraph, SubjectiveIssue
from primitives.architecture_review.shared.prompts import (
    STAGE_E_SYSTEM_PROMPT,
    build_dep_graph_summary,
    build_stage_e_user_message,
)

if TYPE_CHECKING:
    from primitives._shared.protocol import PromptItem
    from primitives.architecture_review.shared.runtime_context import RuntimeContext


#: SubjectiveIssue kinds eligible for the ``exposes_pii`` cross-primitive
#: flag. Per the issue spec, only abstraction / API / cross-cutting-leak
#: findings get decorated — these are the kinds where touching a PII
#: source materially changes the recommendation. (All current kinds
#: qualify; this set is explicit so future kinds opt in deliberately.)
_PII_ELIGIBLE_KINDS: frozenset[str] = frozenset({"abstraction", "api_design", "cross_cutting_leak"})


def _parse_issues_from_llm(
    raw_response: str,
    known_modules: set[str],
    module_paths: dict[str, str] | None = None,
    runtime_ctx: RuntimeContext | None = None,
) -> list[SubjectiveIssue]:
    """Parse and validate LLM JSON response into SubjectiveIssue objects.

    Drops any issue whose module is not in known_modules (anti-hallucination guard).
    Defensive parse: skips malformed entries.

    When *runtime_ctx* carries S60 PII source files and *module_paths*
    resolves the issue's module to a file, ``SubjectiveIssue.exposes_pii``
    is set to True for abstraction / API / cross-cutting-leak kinds.
    Missing context → flag stays False (backward compatible).
    """
    issues: list[SubjectiveIssue] = []

    # Strip markdown code fences if present
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first and last fence lines
        lines = [ln for ln in lines if not ln.startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        data: Any = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        print(
            f"[architecture-review] WARNING: Stage E LLM response is not valid JSON: {exc}",
            file=sys.stderr,
        )
        return []

    if not isinstance(data, list):
        print(
            "[architecture-review] WARNING: Stage E LLM response is not a JSON array",
            file=sys.stderr,
        )
        return []

    for item in data:
        if not isinstance(item, dict):
            continue

        kind = item.get("kind")
        module = item.get("module")  # may be None
        description = item.get("description", "")
        severity = item.get("severity", "medium")

        # Validate kind
        valid_kinds = {"abstraction", "api_design", "cross_cutting_leak"}
        if kind not in valid_kinds:
            print(
                f"[architecture-review] WARNING: Stage E dropped issue with unknown kind={kind!r}",
                file=sys.stderr,
            )
            continue

        # Validate severity
        valid_severities = {"low", "medium", "high", "critical"}
        if severity not in valid_severities:
            severity = "medium"

        # Anti-hallucination validator: drop issues referencing unknown modules
        if module is not None and module not in known_modules:
            print(
                f"[architecture-review] WARNING: Stage E dropped issue referencing unknown "
                f"module {module!r} (not in dep graph). Known modules: "
                f"{sorted(known_modules)[:10]}...",
                file=sys.stderr,
            )
            continue

        if not description:
            continue

        # Cross-primitive enrichment: ask S60 whether the issue's module
        # touches a PII source file. Only abstraction / api_design /
        # cross_cutting_leak kinds carry the flag (all current kinds).
        exposes_pii = False
        if (
            runtime_ctx is not None
            and module is not None
            and module_paths is not None
            and kind in _PII_ELIGIBLE_KINDS
        ):
            module_file = module_paths.get(module)
            exposes_pii = runtime_ctx.file_touches_pii(module_file)

        try:
            issue = SubjectiveIssue(
                kind=kind,
                module=module,
                description=description,
                severity=severity,
                exposes_pii=exposes_pii,
            )
            issues.append(issue)
        except ValidationError as exc:
            print(
                f"[architecture-review] WARNING: Stage E dropped malformed issue: {exc}",
                file=sys.stderr,
            )

    return issues


def build_prompt_item_for_subjective(
    dep_graph: DepGraph,
    spec_text: str | None,
    runtime_ctx: RuntimeContext | None = None,  # noqa: ARG001 — reserved for future enrichment
) -> PromptItem:
    """Build a single PromptItem for the Stage E subjective review (skill-first mode).

    The caller includes this item in the round-1-prompts.json envelope and the
    SKILL.md body fills in the response.  The id is always ``"review:subjective"``.
    """
    from primitives._shared.protocol import PromptItem, ResponseFormat  # noqa: PLC0415

    summary = build_dep_graph_summary(dep_graph)
    user_msg = build_stage_e_user_message(dep_graph_summary=summary, spec_text=spec_text)

    subjective_issue_schema: dict[str, object] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["abstraction", "api_design", "cross_cutting_leak"],
                },
                "module": {"type": ["string", "null"]},
                "description": {"type": "string"},
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                },
            },
            "required": ["kind", "description", "severity"],
        },
    }

    return PromptItem(
        id="review:subjective",
        system=STAGE_E_SYSTEM_PROMPT,
        user=user_msg,
        response_format=ResponseFormat(type="json", schema=subjective_issue_schema),
    )


def apply_subjective_judgment(
    response_array: list[object],
    dep_graph: DepGraph,
    runtime_ctx: RuntimeContext | None = None,
) -> list[SubjectiveIssue]:
    """Apply a model's in-session judgment for the Stage E subjective review.

    *response_array* is the parsed JSON array from the judgments envelope.
    Delegates to ``_parse_issues_from_llm`` so the same anti-hallucination
    guard (unknown-module drop, kind/severity validation) runs on both paths.
    """
    known_modules = dep_graph.module_names
    module_paths: dict[str, str] = {m.name: m.path for m in dep_graph.modules}

    return _parse_issues_from_llm(
        json.dumps(response_array),
        known_modules,
        module_paths=module_paths,
        runtime_ctx=runtime_ctx,
    )
