"""M4 Stage B — the conversion-plan prompt + judgment helpers.

M4's deterministic Stages A / C / D / E surround a single model consultation:
the SKILL.md body provides the interpretive bridge Python can't synthesize —
how the SP's data dependencies, transaction boundaries, cursor and side-effect
handling, commit sequence, test plan, and migration gotchas map onto the chosen
target framework. ``build_prompt_item_for_sp`` emits that prompt over a *bounded
compact* slice (the resolved SP profile + its tables, never the whole digest)
so the per-call token cost stays flat (token-offload), and ``apply_plan_judgment``
folds the judged response back with the anti-hallucination guard.

Anti-hallucination guard
-------------------------

The model is told the closed *universe* of names (the SP name + every resolved
table name + the M2 :class:`SideEffect` literals it actually carries) and
instructed to ground its answer in those. After the call:

* ``data_deps`` is filtered to resolved table names (LLM-introduced tables
  dropped with a stderr warning);
* ``commit_sequence`` entries naming an unknown SP/table are kept as prose but
  any back-ticked unknown is *not* trusted as a cross-reference;
* ``side_effect_mappings`` keys are filtered to the SP's actual side effects.

The Stage E :class:`EffortEstimate` is then folded in. A structurally-broken
response (non-JSON / wrong shape) degrades to an empty-field plan rather than
raising — the deterministic scaffold + effort estimate are still useful.

Prompt construction is deterministic (responses keyed by the SHA-256 of
the system+user prompts), so the stage is golden-file testable offline.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from primitives.sp_converter.shared.effort_estimator import estimate
from primitives.sp_converter.shared.models import ConversionPlan

if TYPE_CHECKING:
    from primitives._shared.protocol import PromptItem
    from primitives.sp_converter.shared.models import Target
    from primitives.sp_converter.shared.sp_resolver import ResolvedSp

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are a senior modernization engineer writing a per-stored-procedure migration \
plan for converting ONE legacy SQL stored procedure to a target application \
framework. You will be given the procedure's profile (name, classification, IO \
shape, side effects, risk score, touched tables with their columns), the chosen \
target framework, and the CLOSED universe of table names and side-effect kinds \
you are allowed to reference.

Your task: return a JSON object describing how to port this procedure. Shape:

{
  "data_deps": ["<table>", ...],          // tables the port reads/writes
  "txn_boundaries": ["<note>", ...],      // where COMMIT/ROLLBACK maps in the target
  "side_effect_mappings": {"<kind>": "<target API>"},  // e.g. "email": "JavaMailSender"
  "commit_sequence": ["<step>", ...],     // suggested human commit order
  "test_plan": ["<case>", ...],           // per-stage tests to add to the scaffold
  "gotchas": ["<risk>", ...]              // cursor->stream, cross-DB link, dynamic SQL
}

Rules:

1. Reference ONLY table names and side-effect kinds from the supplied universe. \
Do NOT invent tables, columns, procedures, or framework APIs the input does not \
support — a downstream guard drops any data_dep or side-effect-mapping key \
outside the universe.
2. side_effect_mappings keys MUST be drawn from the procedure's listed side \
effects only. Map each to the idiomatic target-framework mechanism.
3. Return ONLY valid JSON with the shape above. No Markdown fences, no prose \
preface, no extra keys.
4. If the input is thin, return short lists rather than inventing detail.
"""


def _universe(resolved: ResolvedSp) -> dict[str, object]:
    """The closed set of names + kinds the model may reference."""
    return {
        "tables": resolved.table_names,
        "side_effects": [s for s in resolved.proc.side_effects if s != "none"],
    }


def build_user_prompt(resolved: ResolvedSp, *, target: Target | str) -> str:
    """Render the bounded compact user message for one SP.

    Deterministic + JSON-serialised so golden-file tests can key canned
    responses on the prompt and layout drift can't change the hash.
    """
    proc = resolved.proc
    payload: dict[str, object] = {
        "procedure": {
            "name": proc.name,
            "language": proc.language,
            "classification": proc.classification,
            "io_shape": proc.io_shape,
            "side_effects": [s for s in proc.side_effects if s != "none"],
            "body_loc": proc.body_loc,
            "risk_total": proc.risk_score.total,
        },
        "tables": [
            {"name": t.name, "columns": [c.name for c in t.columns]} for t in resolved.tables
        ],
        "target": str(target),
        "universe": _universe(resolved),
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _coerce_str_list(
    value: object,
    *,
    field: str | None = None,
    sp_name: str | None = None,
) -> list[str]:
    """Return *value* as a ``list[str]``.

    A recognized field passed as a **bare string** (a common mis-shaping when
    the SKILL prose reads like one note, e.g. ``txn_boundaries``) is **salvaged**
    into a single-element list with a stderr WARNING rather than silently coerced
    to ``[]`` and dropped (#948). Any other non-list type warns and yields ``[]``.
    A ``None``/absent value is a legitimate empty and warns nothing. When *field*
    is ``None`` the helper is used internally and stays quiet.
    """
    if value is None or isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str)] if value else []
    if isinstance(value, str):
        if field is not None:
            _warn(
                f"plan for SP={sp_name!r}: field {field!r} was a string but a "
                f"list[str] is expected — wrapping the single value; pass a JSON "
                f"array to silence this and avoid losing content."
            )
        return [value] if value else []
    if field is not None:
        _warn(
            f"plan for SP={sp_name!r}: field {field!r} has unexpected type "
            f"{type(value).__name__} (expected list[str]); dropping."
        )
    return []


def _guard_data_deps(raw: object, *, allowed: set[str], sp_name: str) -> list[str]:
    """Keep only data-dep entries naming a resolved table; warn on drops."""
    out: list[str] = []
    for dep in _coerce_str_list(raw, field="data_deps", sp_name=sp_name):
        if dep in allowed:
            out.append(dep)
        else:
            _warn(f"plan for SP={sp_name!r} referenced unknown table {dep!r}; dropping")
    return out


def _guard_side_effects(raw: object, *, allowed: set[str], sp_name: str) -> dict[str, str]:
    """Keep only side-effect-mapping keys the SP actually carries; warn on drops."""
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key in allowed:
            out[key] = value
        else:
            _warn(f"plan for SP={sp_name!r} mapped unknown side effect {key!r}; dropping")
    return out


def _warn(message: str) -> None:
    """Emit an anti-hallucination warning via the module logger."""
    logger.warning(message)


def build_prompt_item_for_sp(
    resolved: ResolvedSp,
    *,
    target: Target | str,
) -> PromptItem:
    """Build the PromptItem for one SP's conversion plan (skill-first mode).

    Response format is JSON (the model must return the structured plan object),
    with the full schema for the six-field ConversionPlan shape.

    Args:
        resolved: Stage-A SP resolution (universe of table/side-effect names).
        target: The conversion target framework.

    Returns:
        A PromptItem with id "narrative:<sp_name>".
    """
    from primitives._shared.protocol import PromptItem, ResponseFormat  # noqa: PLC0415

    user = build_user_prompt(resolved, target=target)
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "data_deps": {"type": "array", "items": {"type": "string"}},
            "txn_boundaries": {"type": "array", "items": {"type": "string"}},
            "side_effect_mappings": {"type": "object"},
            "commit_sequence": {"type": "array", "items": {"type": "string"}},
            "test_plan": {"type": "array", "items": {"type": "string"}},
            "gotchas": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "data_deps",
            "txn_boundaries",
            "side_effect_mappings",
            "commit_sequence",
            "test_plan",
            "gotchas",
        ],
    }
    return PromptItem(
        id=f"narrative:{resolved.proc.name}",
        system=SYSTEM_PROMPT,
        user=user,
        response_format=ResponseFormat(type="json", schema=schema),
        model_tier="default",
    )


def apply_plan_judgment(
    resolved: ResolvedSp,
    *,
    target: Target | str,
    response: dict[str, object],
) -> ConversionPlan:
    """Apply a pre-computed judgment to produce a ConversionPlan.

    Runs the same anti-hallucination guards (_guard_data_deps,
    _guard_side_effects) as the harness path. The effort estimate (Stage E)
    is computed deterministically and folded in.

    Args:
        resolved: Stage-A SP resolution (universe of allowed names).
        target: The conversion target framework.
        response: The model's JSON response dict from the judgments envelope.

    Returns:
        A ConversionPlan with guarded fields and the Stage-E effort estimate.
    """
    allowed_tables = set(resolved.table_names)
    allowed_side_effects: set[str] = {str(s) for s in resolved.proc.side_effects if s != "none"}

    effort = estimate(resolved, target=target)
    return ConversionPlan(
        sp_name=resolved.proc.name,
        target=target,  # type: ignore[arg-type]
        data_deps=_guard_data_deps(
            response.get("data_deps"), allowed=allowed_tables, sp_name=resolved.proc.name
        ),
        txn_boundaries=_coerce_str_list(
            response.get("txn_boundaries"), field="txn_boundaries", sp_name=resolved.proc.name
        ),
        side_effect_mappings=_guard_side_effects(
            response.get("side_effect_mappings"),
            allowed=allowed_side_effects,
            sp_name=resolved.proc.name,
        ),
        commit_sequence=_coerce_str_list(
            response.get("commit_sequence"), field="commit_sequence", sp_name=resolved.proc.name
        ),
        test_plan=_coerce_str_list(
            response.get("test_plan"), field="test_plan", sp_name=resolved.proc.name
        ),
        gotchas=_coerce_str_list(
            response.get("gotchas"), field="gotchas", sp_name=resolved.proc.name
        ),
        effort=effort,
    )


__all__ = [
    "SYSTEM_PROMPT",
    "apply_plan_judgment",
    "build_prompt_item_for_sp",
    "build_user_prompt",
]
