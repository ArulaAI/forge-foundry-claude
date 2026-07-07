"""M2 Stage E — the business-logic-class prompt + judgment helpers.

The SKILL.md body classifies each **parsed** stored procedure into one of
the closed
:data:`primitives.stored_proc_analyze.shared.models.Classification`
literals; this module builds the prompts and folds the judged responses
back. The literals are:

* ``crud_wrapper`` — thin INSERT/UPDATE/DELETE wrapper.
* ``validation`` — guards on inputs / referential integrity checks.
* ``calculation`` — derives a numeric / boolean result without
  side-effects.
* ``orchestration`` — coordinates other SPs (out-calls > 0).
* ``reporting`` — reads-only SELECT / aggregation for human consumption.
* ``etl`` — bulk read-transform-write pipeline.
* ``trigger_handler`` — fires from a DML trigger context.
* ``unknown`` — the anti-hallucination fallback (returned when the model
  response is malformed or out-of-vocabulary).

The prompt embeds the Stage B-D structured profile (cyclomatic, IO
shape, side effects, touched tables, out-call count) so the model has a
fact-set it is *told not to override or invent*.

Two anti-hallucination guards apply (mirroring the SPEC.md
§"Anti-hallucination" contract):

1. **Closed-vocabulary guard** — a judged response that is not one of
   the eight :data:`Classification` Literal values is replaced with
   ``"unknown"`` and a warning is logged to stderr. The classifier
   never raises on a bad literal.
2. **Unparseable-skip guard** — Stage A SPs tagged
   ``parse_status="unparseable"`` are *not* prompted; they short-circuit
   to ``"unknown"`` with a dedicated note. This keeps the prompt count
   equal to the parsed-SP count exactly (issue #209 acceptance criterion).

Design spec §M2 Stage E
(``docs/superpowers/specs/2026-05-22-data-modernization-design.md``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from primitives.stored_proc_analyze.shared.models import (
    _ALL_CLASSIFICATIONS,
    Classification,
)

if TYPE_CHECKING:
    from primitives.stored_proc_analyze.shared.models import IOShape, SideEffect
    from primitives.stored_proc_analyze.shared.sp_parser import ParsedSP
    from primitives.stored_proc_analyze.shared.static_profile import StaticProfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Closed-vocabulary set
# ---------------------------------------------------------------------------

# Drawn from the :data:`Classification` Literal at module load time so a
# typo here would fail the dedicated exhaustiveness test. Tracking the
# tuple from :mod:`models` keeps the source of truth in one place; the
# test `test_bizlogic_vocabulary_matches_models_literal` locks the two
# together so the prompt + guard cannot silently diverge from the data
# model.
_ALLOWED_CLASSIFICATIONS: frozenset[str] = frozenset(_ALL_CLASSIFICATIONS)


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BizLogicResult:
    """Stage E output for a single :class:`ParsedSP`.

    Frozen so the orchestrator can cache the result keyed by SP name
    without worrying about downstream mutation. ``warning`` carries the
    free-form note Stage E emitted when the anti-hallucination guard
    fired — it is ``None`` on the happy path.
    """

    sp_name: str
    classification: Classification
    warning: str | None = None


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

# The prompt is narrow on purpose:
#
# * It enumerates the eight allowed Classification literals verbatim so
#   the LLM cannot return an unrecognised value without violating an
#   explicit instruction.
# * It tells the LLM the static profile is authoritative — the LLM is
#   permitted to weight the signals but not to *invent* a new fact
#   (e.g. claim cursor presence when Stage C did not detect one).
# * It pins the JSON-only response shape so :func:`_parse_response`
#   does not have to cope with Markdown fences or prose preambles.
#
# Listed literals must stay in lock-step with :data:`_ALL_CLASSIFICATIONS`
# — the lockstep test enforces it.
SYSTEM_PROMPT = """\
You are a senior database engineer classifying a stored procedure's business-logic role. \
You will be given the SP's static profile (parameters, cyclomatic complexity, IO shape, \
side effects, touched tables, out-call count, transaction-statement count) and its body \
text.

Your task: return a JSON object naming exactly one classification from this CLOSED set:

- "crud_wrapper":   thin INSERT / UPDATE / DELETE / MERGE wrapper around a single table.
- "validation":     guards on inputs, referential-integrity / business-rule checks; may \
read but rarely writes.
- "calculation":    derives a scalar / aggregate result without external side effects; \
typical of FUNCTION bodies.
- "orchestration":  coordinates other SPs (out-call count > 0); the body's primary work \
is calling sub-procedures.
- "reporting":      reads-only SELECT / aggregation intended for human / dashboard \
consumption.
- "etl":            bulk read-transform-write pipeline; often cursor-driven, often spans \
multiple tables.
- "trigger_handler": fires from a DML trigger context (the parse kind is TRIGGER).
- "unknown":        only when the inputs are insufficient to choose any of the above.

Rules:

1. The static profile is authoritative. Do not claim cursor presence, side-effects, or \
table touches the profile does not list — the profile was extracted deterministically \
from the AST and overrides any guess you might form from the body text.
2. Return ONLY valid JSON with the shape `{"classification": "<one-of-the-set>"}`. No \
Markdown fences, no prose preface, no rationale field.
3. If you would return a literal that is not in the closed set, return "unknown" instead. \
The downstream consumer expects the closed vocabulary and will replace any out-of-set \
literal with "unknown" anyway.
"""


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_user_prompt(
    parsed_sp: ParsedSP,
    *,
    profile: StaticProfile,
    side_effects: list[SideEffect],
    io_shape: IOShape,
) -> str:
    """Render the user message Stage E sends to the LLM.

    Deterministic given the inputs; JSON-serialised so layout-sensitive
    Markdown cannot drift between runs and golden-file tests can key
    canned responses on the SHA-256 of the prompt.
    """
    payload: dict[str, object] = {
        "stored_procedure": {
            "name": parsed_sp.name,
            "schema": parsed_sp.schema,
            "kind": parsed_sp.kind,
            "source_dialect": parsed_sp.source_dialect,
        },
        "static_profile": {
            "loc": profile.loc,
            "cyclomatic": profile.cyclomatic,
            "touched_tables": list(profile.touched_tables),
            "table_touch_count": profile.table_touch_count,
            "out_calls": profile.out_calls,
            "transaction_statements": profile.transaction_statements,
        },
        "io_shape": io_shape,
        "side_effects": list(side_effects),
        "body": parsed_sp.raw_body,
        "allowed_classifications": list(_ALL_CLASSIFICATIONS),
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Response parsing + guard
# ---------------------------------------------------------------------------


def _parse_and_guard(raw: str, *, sp_name: str) -> tuple[Classification, str | None]:
    """Decode + validate the LLM response.

    Returns ``(classification, warning)``. The closed-vocabulary guard
    NEVER raises — a structural failure (non-JSON, wrong shape, missing
    key, non-string value) is treated as "the LLM gave us something we
    cannot use", which by contract maps to ``"unknown"`` with a warning.
    The orchestrator surfaces the warning to the user without
    re-running the call.

    The distinction between "unknown the LLM chose" and "unknown the
    guard substituted" is visible to the caller via the ``warning``
    field — when the LLM legitimately answered ``"unknown"`` the
    warning is ``None``; when the guard substituted, the warning
    carries the original raw value (truncated for safety).
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        warning = (
            f"Stage E LLM response for sp={sp_name!r} was not valid JSON "
            f"({exc.__class__.__name__}); first 200 chars: {raw[:200]!r}"
        )
        _log_guard(warning)
        return "unknown", warning

    if not isinstance(data, dict):
        warning = (
            f"Stage E LLM response for sp={sp_name!r} was JSON but not an object: "
            f"type={type(data).__name__}"
        )
        _log_guard(warning)
        return "unknown", warning

    raw_value = data.get("classification")
    if not isinstance(raw_value, str):
        warning = (
            f"Stage E LLM response for sp={sp_name!r} missing 'classification' string "
            f"(got type={type(raw_value).__name__})"
        )
        _log_guard(warning)
        return "unknown", warning

    if raw_value not in _ALLOWED_CLASSIFICATIONS:
        warning = (
            f"Stage E LLM returned out-of-vocabulary classification "
            f"for sp={sp_name!r}: {raw_value!r}; substituting 'unknown'"
        )
        _log_guard(warning)
        return "unknown", warning

    # ``raw_value`` is in :data:`_ALLOWED_CLASSIFICATIONS`, which is
    # the runtime mirror of the :data:`Classification` Literal — the
    # cast is sound and the Literal is narrowed for downstream
    # consumers.
    classification: Classification = raw_value  # type: ignore[assignment]
    return classification, None


def _log_guard(message: str) -> None:
    """Emit the anti-hallucination warning via the module logger.

    Logging goes through the module logger so test capture works and
    verbosity is controlled by the caller's logging configuration.
    """
    logger.warning(message)


# ---------------------------------------------------------------------------
# Skill-first protocol helpers (--extract-only / --continue paths)
# ---------------------------------------------------------------------------


def build_prompt_items(
    parsed_sps: list[ParsedSP],
    _profiles: dict[str, Any],
    _side_effects: dict[str, list[SideEffect]],
    _io_shapes: dict[str, IOShape],
) -> list[tuple[str, ParsedSP]]:
    """Return (id, parsed_sp) pairs for SPs that need LLM classification.

    Unparseable SPs are skipped — they short-circuit to "unknown" without
    an LLM call (matching the ``classify_bizlogic`` unparseable-skip guard).
    Used by --extract-only to build the prompts envelope.

    Args:
        parsed_sps: Stage A parsed SP list.
        _profiles: dict mapping SP name → StaticProfile (ignored here — passed
            for symmetry with ``apply_judgments``; the prompt is built by the
            CLI using the profile objects directly).
        _side_effects: dict mapping SP name → list[str] side effect labels.
        _io_shapes: dict mapping SP name → IOShape string.

    Returns:
        List of (id, parsed_sp) pairs where id uses the ``bizlogic:`` prefix.
    """
    items: list[tuple[str, ParsedSP]] = []
    for sp in parsed_sps:
        if sp.parse_status == "unparseable":
            continue
        items.append((f"bizlogic:{sp.name}", sp))
    return items


def apply_judgments(
    parsed_sp: ParsedSP,
    _profile: Any,
    _side_effects: list[SideEffect],
    _io_shape: IOShape,
    judgments: dict[str, Any],
) -> BizLogicResult:
    """Apply a pre-computed LLM judgment to a single SP.

    The anti-hallucination guard runs on the judgment response — identical
    to the guard on live harness responses. Invalid responses result in
    classification="unknown".

    Args:
        parsed_sp: Stage A parsed SP.
        _profile: Stage B StaticProfile (unused in guard, passed for symmetry).
        _side_effects: Stage D side effects list (unused, passed for symmetry).
        _io_shape: Stage C IO shape string (unused, passed for symmetry).
        judgments: dict mapping item id (e.g. "bizlogic:add_employee") to
            the response dict (e.g. {"classification": "crud_wrapper"}).

    Returns:
        BizLogicResult with classification from the judgment or "unknown"
        if the SP is unparseable or no judgment was found.
    """
    if parsed_sp.parse_status == "unparseable":
        note = (
            f"Stage E skipped LLM call for sp={parsed_sp.name!r}: "
            f"parse_status='unparseable' (Stage A graceful-degradation)"
        )
        logger.warning(note)
        return BizLogicResult(
            sp_name=parsed_sp.name,
            classification="unknown",
            warning=note,
        )

    item_id = f"bizlogic:{parsed_sp.name}"
    response = judgments.get(item_id)
    if response is None:
        note = f"Stage E: no judgment found for sp={parsed_sp.name!r}; using 'unknown'"
        logger.warning(note)
        return BizLogicResult(
            sp_name=parsed_sp.name,
            classification="unknown",
            warning=note,
        )

    # Run the same guard as the harness path
    raw_json = json.dumps(response) if isinstance(response, dict) else str(response)
    classification, warning = _parse_and_guard(raw_json, sp_name=parsed_sp.name)
    return BizLogicResult(
        sp_name=parsed_sp.name,
        classification=classification,
        warning=warning,
    )


__all__ = [
    "SYSTEM_PROMPT",
    "BizLogicResult",
    "apply_judgments",
    "build_prompt_items",
    "build_user_prompt",
]
