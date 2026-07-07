"""M1 Stage D — ``classify_lob`` (regex pack + LLM-judged fallback).

Walks the Stage B :class:`Table` inventory and assigns each table a
line-of-business classification. Two paths in order of preference:

1. **Regex pack** (deterministic, no LLM cost). The shipped
   :data:`LOB_PATTERNS` is loaded from
   ``primitives/db_schema_analyze/shared/lob_patterns.yaml`` at module
   import time. The pack is BFSI-shaped (customer / account /
   transaction / ledger / reference / audit / product / billing /
   compliance / risk / pricing) — the dominant M-tier audience is
   core-banking / insurance / capital-markets shops. Pattern matches
   are case-insensitive against the bare table name (no schema
   qualifier), first-match-wins.

2. **LLM-judged fallback** for ambiguous tables — only invoked when the
   regex pack produces zero matches. The prompt embeds the table's
   bare name + the column-name list (Stage B is deterministic so the
   list is reliable) and instructs the LLM to return one of the
   closed :data:`LOB_VOCABULARY` literals. The
   anti-hallucination guard below rejects out-of-vocabulary responses
   and surfaces a stderr warning, leaving ``lob_classification=None``
   (the table stays unclassified rather than carrying an invented
   label downstream).

Anti-hallucination guard
------------------------

Every LLM response is validated against the closed
:data:`LOB_VOCABULARY` set (which mirrors the ``lob:`` literals in
the YAML pack). A response that:

* is not valid JSON,
* is JSON but not an object,
* is missing the ``"lob"`` key or carries a non-string value,
* names a literal outside :data:`LOB_VOCABULARY`,

is **dropped**: the table's ``lob_classification`` stays ``None``
and a warning is logged on both the module logger and stderr
(matching the M2 ``bizlogic_classifier`` + M4 ``plan_synthesizer``
pattern so a CI operator running with ``--no-log-cli`` still sees
the dropped response).

Design spec §M1 Stage D + §"Anti-hallucination"
(``docs/superpowers/specs/2026-05-22-data-modernization-design.md``).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from primitives.db_schema_analyze.shared.models import Table

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pack loading
# ---------------------------------------------------------------------------


_PACK_PATH = Path(__file__).resolve().parent / "lob_patterns.yaml"


@dataclass(frozen=True)
class LOBPattern:
    """One entry in the regex pack — compiled at module load time.

    ``regex`` is compiled with :data:`re.IGNORECASE` so the YAML stays
    case-insensitive (most legacy schemas use ALL_CAPS naming but the
    occasional mixed-case fixture should still match). The compiled
    pattern is held alongside the literal LoB label so the classifier
    can return both the matched pattern (for telemetry) and the
    classification.
    """

    pattern_source: str
    regex: re.Pattern[str]
    lob: str


def _load_pack(path: Path) -> list[LOBPattern]:
    """Parse the YAML pack into compiled :class:`LOBPattern` entries.

    The pack is loaded exactly once at module import. A malformed pack
    is a programmer error, not a runtime error — :func:`yaml.safe_load`
    will raise and the module will fail to import, surfacing the
    problem immediately rather than silently degrading to LLM-only
    classification.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"lob_patterns.yaml must be a top-level list, got {type(raw).__name__}")
    pack: list[LOBPattern] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(
                f"lob_patterns.yaml entry {idx} must be a mapping, got {type(entry).__name__}"
            )
        pattern_source = entry.get("pattern")
        lob = entry.get("lob")
        if not isinstance(pattern_source, str) or not isinstance(lob, str):
            raise ValueError(
                f"lob_patterns.yaml entry {idx} must have 'pattern' (str) and 'lob' (str)"
            )
        pack.append(
            LOBPattern(
                pattern_source=pattern_source,
                regex=re.compile(pattern_source, re.IGNORECASE),
                lob=lob,
            )
        )
    return pack


# Module-level constants. Loading at import time means the YAML parse +
# regex compile happen once per process; thereafter classification is
# pure string scanning.
LOB_PATTERNS: list[LOBPattern] = _load_pack(_PACK_PATH)

# Closed vocabulary — drawn from the YAML pack. Every LLM response is
# validated against this set; out-of-vocabulary literals trigger the
# anti-hallucination guard. A dedicated lockstep test
# (``test_lob_yaml_vocabulary_matches_module``) confirms the two stay
# in sync as the pack evolves.
LOB_VOCABULARY: frozenset[str] = frozenset(p.lob for p in LOB_PATTERNS)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


# The prompt is narrow on purpose — see M2 :mod:`bizlogic_classifier` for
# the design rationale (the LLM is permitted to weight the signals but
# not to invent facts; the response shape is pinned JSON so the parser
# does not have to cope with Markdown fences or prose preambles).
SYSTEM_PROMPT = """\
You are a senior data architect classifying a database table's line-of-business role. \
You will be given the table's bare name and its column list (with raw DDL types). \
The table did not match any pattern in the deterministic regex pack, so we are asking \
you to judge from the table's shape.

Your task: return a JSON object naming exactly one classification from this CLOSED set:

- "customer":   customer / client / party identity (master records).
- "account":    account-of-record (deposit, savings, brokerage, etc.).
- "transaction": transactions, postings, movements, transfers.
- "ledger":     general-ledger / journal entries.
- "reference":  reference / lookup / code / enum tables.
- "audit":      audit, history, changelog records.
- "product":    product / instrument / security / asset catalog.
- "billing":    billing, invoicing, charge, payment.
- "compliance": regulatory, KYC, AML, sanctions data.
- "risk":       risk, exposure, limit, rating data.
- "pricing":    price, quote, rate, market-data.

Rules:

1. The column list is authoritative. Reason from the column names and types \
(e.g. an ``AMOUNT NUMBER(18,2)`` + ``TXN_DATE TIMESTAMP`` shape suggests "transaction"). \
Do not invent columns or types the input does not list.
2. Return ONLY valid JSON with the shape `{"lob": "<one-of-the-set>"}`. No Markdown \
fences, no prose preface, no rationale field.
3. If the inputs are insufficient to choose any of the listed values, return JSON \
with a value that is NOT in the set (e.g. `{"lob": "unknown"}`) and the downstream \
guard will drop it. Do not hallucinate a confident answer.
"""


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LOBResult:
    """Stage D output for a single :class:`Table`.

    Frozen so the orchestrator can cache the result keyed by table
    qualified name without worrying about downstream mutation.
    ``source`` records how the classification was reached so reporting
    can split regex-confident from LLM-confident counts. ``warning``
    carries the free-form note when the anti-hallucination guard fired.
    """

    table_name: str
    lob: str | None
    source: str  # "regex" | "llm" | "guard_dropped" | "unmatched"
    warning: str | None = None


# ---------------------------------------------------------------------------
# Regex matcher
# ---------------------------------------------------------------------------


def _match_regex(table_name: str) -> LOBPattern | None:
    """Return the first :class:`LOBPattern` matching ``table_name``.

    Patterns are walked in YAML order (first-match-wins). The match is
    case-insensitive — the YAML uses ALL_CAPS conventions but
    fixtures occasionally drop into mixed case (e.g. T-SQL
    ``[dbo].[CustomerOrder]``). ``None`` means the table did not match
    any pattern and is therefore ambiguous.
    """
    for pattern in LOB_PATTERNS:
        if pattern.regex.match(table_name):
            return pattern
    return None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_user_prompt(table: Table) -> str:
    """Render the user message Stage D sends to the LLM.

    Deterministic given the :class:`Table`; JSON-serialised so
    layout-sensitive Markdown cannot drift between runs and golden-file
    tests can key canned responses on the SHA-256 of the prompt.
    """
    payload: dict[str, object] = {
        "table": {
            "name": table.name,
            "schema": table.schema_,
        },
        "columns": [
            {"name": col.name, "type": col.type, "nullable": col.nullable} for col in table.columns
        ],
        "primary_key": list(table.primary_key),
        "allowed_classifications": sorted(LOB_VOCABULARY),
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Response parsing + guard
# ---------------------------------------------------------------------------


def _parse_and_guard(raw: str, *, table_name: str) -> tuple[str | None, str | None]:
    """Decode + validate the LLM response.

    Returns ``(lob, warning)``. The closed-vocabulary guard NEVER raises
    — a structural failure (non-JSON, wrong shape, missing key, non-
    string value, out-of-vocabulary literal) is treated as "the LLM
    gave us something we cannot use", which by contract maps to
    ``None`` with a warning. The orchestrator surfaces the warning to
    the user without re-running the call.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        warning = (
            f"Stage D LLM response for table={table_name!r} was not valid JSON "
            f"({exc.__class__.__name__}); first 200 chars: {raw[:200]!r}"
        )
        _log_guard(warning)
        return None, warning

    if not isinstance(data, dict):
        warning = (
            f"Stage D LLM response for table={table_name!r} was JSON but not an "
            f"object: type={type(data).__name__}"
        )
        _log_guard(warning)
        return None, warning

    raw_value = data.get("lob")
    if not isinstance(raw_value, str):
        warning = (
            f"Stage D LLM response for table={table_name!r} missing 'lob' string "
            f"(got type={type(raw_value).__name__})"
        )
        _log_guard(warning)
        return None, warning

    if raw_value not in LOB_VOCABULARY:
        warning = (
            f"Stage D LLM returned out-of-vocabulary lob for table={table_name!r}: "
            f"{raw_value!r}; dropping (classification stays None)"
        )
        _log_guard(warning)
        return None, warning

    return raw_value, None


def _log_guard(message: str) -> None:
    """Emit the anti-hallucination warning via the module logger.

    Logging goes through the module logger so pytest's ``caplog`` works
    and verbosity is controlled by the caller's logging configuration.
    """
    logger.warning(message)


# ---------------------------------------------------------------------------
# Skill-first protocol helpers (--extract-only / --continue paths)
# ---------------------------------------------------------------------------


def build_prompt_items(tables: list[Table]) -> list[tuple[str, Table]]:
    """Return (id, table) pairs for tables that miss the regex pack.

    These tables need LLM classification. Tables that match a regex
    pattern are excluded — they're already classified deterministically.
    Used by --extract-only to build the prompts envelope.
    """
    items: list[tuple[str, Table]] = []
    for table in tables:
        if _match_regex(table.name) is None:
            items.append((f"lob:{table.name}", table))
    return items


def apply_judgments(
    tables: list[Table],
    judgments: dict[str, Any],
) -> list[Table]:
    """Apply pre-computed LLM judgments to tables. Regex-first, then judgment lookup.

    The anti-hallucination guard runs on every judgment response — identical
    to the guard on live harness responses. Invalid responses result in
    lob_classification=None.

    Args:
        tables: Stage B inventory.
        judgments: dict mapping item id (e.g. "lob:customers") to the
            response dict (e.g. {"lob": "billing"}).

    Returns:
        New list of Table objects with lob_classification set.
    """
    out: list[Table] = []
    for table in tables:
        regex_hit = _match_regex(table.name)
        if regex_hit is not None:
            out.append(table.model_copy(update={"lob_classification": regex_hit.lob}))
            continue

        item_id = f"lob:{table.name}"
        response = judgments.get(item_id)
        if response is None:
            out.append(table.model_copy(update={"lob_classification": None}))
            continue

        # Run the same guard as the harness path
        raw_json = json.dumps(response) if isinstance(response, dict) else str(response)
        lob, _warning = _parse_and_guard(raw_json, table_name=table.name)
        out.append(table.model_copy(update={"lob_classification": lob}))
    return out


__all__ = [
    "LOB_PATTERNS",
    "LOB_VOCABULARY",
    "SYSTEM_PROMPT",
    "LOBPattern",
    "LOBResult",
    "apply_judgments",
    "build_prompt_items",
    "build_user_prompt",
]
