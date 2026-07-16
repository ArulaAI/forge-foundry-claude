"""Skill-first protocol envelope models.

Defines the prompts and judgments envelope format used by the
extract→judge→continue protocol. Every primitive that supports
--extract-only / --continue uses these models to serialize
prompts for in-session judgment and deserialize the model's
responses.

See docs/superpowers/specs/2026-06-18-skill-first-harness-elimination-design.md §3.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

import click
from pydantic import BaseModel, Field, ValidationError, model_validator

if TYPE_CHECKING:
    from pathlib import Path

#: The judgments-envelope schema, documented once so the error message, the
#: emitted prompts file (``_schema`` hint), and the SKILL docs stay in sync (#885).
JUDGMENTS_ENVELOPE_SCHEMA_HINT = (
    '{"schema_version": "1.0", "primitive": "<name>", "round": <int>, '
    '"items": [{"id": "<prompt-item-id>", "response": <json-object|json-array|string>}]}'
)


class MalformedJudgmentsError(click.ClickException):
    """A judgments file did not match the :class:`JudgmentEnvelope` schema (#885).

    Raised by :func:`read_judgments` instead of letting a raw
    ``pydantic_core.ValidationError`` traceback escape. Subclasses
    :class:`click.ClickException` so Click renders the message to stderr and
    exits 1 (``exit_code``) — a friendly, actionable message rather than a
    stack trace — for both the real CLI and ``CliRunner``-based tests.
    """

    exit_code = 1


class ResponseFormat(BaseModel, frozen=True):
    """Constrains the model's response shape for a single prompt item.

    ``type="json"`` means the response must be valid JSON matching ``schema``.
    ``type="text"`` means the response is free-form text (narratives, analyses).
    """

    type: Literal["json", "text"]
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")

    model_config = {"populate_by_name": True}


class PromptItem(BaseModel, frozen=True):
    """One item in a prompts envelope — a single judgment task for the model."""

    id: str
    system: str
    user: str
    response_format: ResponseFormat
    model_tier: Literal["default", "deep"] = "default"


class PromptEnvelope(BaseModel, frozen=True):
    """The prompts file emitted by --extract-only or --continue (exit 7)."""

    schema_version: Literal["1.0"] = "1.0"
    primitive: str
    round: int = Field(ge=1)
    items: list[PromptItem]

    @model_validator(mode="after")
    def _items_nonempty(self) -> PromptEnvelope:
        if not self.items:
            raise ValueError("items must not be empty")
        return self


class JudgmentItem(BaseModel, frozen=True):
    """One item in a judgments envelope — the model's response to a prompt.

    ``response`` may be:
    - a JSON object (``dict``) — used by primitives that expect a structured result.
    - a JSON array (``list``) — used by primitives whose LLM response is an array of
      findings (e.g., S40 api-contract-review, C4 code-review, S11 security-review).
    - a plain string — used by primitives that expect free-form text.
    """

    id: str
    response: list[Any] | dict[str, Any] | str


class JudgmentEnvelope(BaseModel, frozen=True):
    """The judgments file written by the SKILL.md body, consumed by --continue."""

    schema_version: Literal["1.0"] = "1.0"
    primitive: str
    round: int = Field(ge=1)
    items: list[JudgmentItem]

    def by_id(self) -> dict[str, JudgmentItem]:
        """Return a lookup dict keyed by item id."""
        return {item.id: item for item in self.items}


def write_prompts(envelope: PromptEnvelope, path: Path) -> None:
    """Write a prompts envelope to disk as formatted JSON.

    A ``_schema`` hint key is injected at the top of the emitted file (#885) so
    the SKILL.md body can see, in-place, exactly what the *judgments* file it
    writes back must look like. The hint is a leading underscore key that
    :class:`JudgmentEnvelope`/:class:`PromptEnvelope` ignore on re-parse.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = envelope.model_dump(mode="json", by_alias=True)
    ordered = {
        "_schema": (
            "Write your judgments to a separate round-N-judgments.json file shaped as: "
            + JUDGMENTS_ENVELOPE_SCHEMA_HINT
            + ". One items[] entry per prompt item below, keyed by its `id`; "
            "`response` holds your judgment (JSON object/array or string per the "
            "item's response_format). Then re-invoke with `--continue --judgments "
            "round-N-judgments.json`."
        ),
        **payload,
    }
    path.write_text(json.dumps(ordered, indent=2), encoding="utf-8")


def read_judgments(path: Path) -> JudgmentEnvelope:
    """Read and validate a judgments envelope from disk.

    On a wrong-shaped file, raises :class:`MalformedJudgmentsError` with an
    actionable message naming the expected envelope schema instead of letting a
    raw ``pydantic_core.ValidationError`` traceback escape (#885).
    """
    raw = path.read_text(encoding="utf-8")
    try:
        return JudgmentEnvelope.model_validate_json(raw)
    except ValidationError as exc:
        # Summarise the first pydantic error in plain language (e.g.
        # "items: Field required") so the message stays short + actionable.
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = ".".join(str(p) for p in first.get("loc", ())) or "<root>"
            reason = first.get("msg", "invalid envelope")
        else:
            loc, reason = "<root>", "invalid envelope"
        raise MalformedJudgmentsError(
            f"malformed judgments file {path}: expected an envelope "
            f"{JUDGMENTS_ENVELOPE_SCHEMA_HINT}; got {loc}: {reason}. "
            "The id of each item must match a prompt-item id from the emitted "
            "round-N-prompts.json (see its `_schema` hint and the skill-first "
            "protocol docs in docs/skill-first-protocol.md)."
        ) from exc


def assert_matching_round(envelope: JudgmentEnvelope, expected_round: int, *, path: Path) -> None:
    """Reject a judgments file whose ``round`` does not match the pipeline's.

    Feeding a **stale prior-round** judgments file to ``--continue`` (e.g. the
    round-1 file when the pipeline is awaiting round-2 judgments) otherwise
    silently finalizes: none of the stale items match the current round's prompt
    ids, so the round is dropped and the run exits 0 with an incomplete report
    (#1199). This makes the mismatch a loud, actionable error instead. It raises
    :class:`MalformedJudgmentsError` (a ``click.ClickException`` — clean stderr
    message, exit 1), consistent with how :func:`read_judgments` reports any other
    bad-judgments input; no traceback, no silent finalize.
    """
    if envelope.round != expected_round:
        raise MalformedJudgmentsError(
            f"stale/wrong-round judgments file {path}: it is for round "
            f"{envelope.round}, but the pipeline is awaiting round {expected_round}. "
            f"Judge the current round's round-{expected_round}-prompts.json and re-invoke "
            "with that round's judgments — do not feed a prior round's file (which would "
            "silently drop the pending round)."
        )
