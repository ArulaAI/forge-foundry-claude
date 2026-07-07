"""Discovery engine — synthesis transport types and loader.

Defines the typed transport for the M5 harness synthesis round
(``synthesis.json`` / ``synthesis_prompts.json``). This module is
**pure validation/types/loading** — no model calls, no network, no LLM.

The synthesis round emits a ``synthesis_prompts.json`` (written by the
Python pipeline's ``--extract-only`` leg) which the in-session harness
reads, fills, and returns as a ``synthesis.json``. The pipeline's
``--continue`` leg calls :func:`load_synthesis` to validate and
deserialise the harness response before merging it into the rendered
``PLAYBOOK.md``.

Schema contracts live at:
  ``primitives/playbook_discovery/skill/references/schemas/synthesis_prompts.json``
  ``primitives/playbook_discovery/skill/references/schemas/synthesis.json``

Design spec: ``docs/superpowers/specs/2026-06-19-m5-playbook-harness-synthesis-design.md``
§5 (artifact schemas) and §3 (architecture / entry paths).

Issue: `#860 <https://github.com/arula-ai/forge-foundry-primitives/issues/860>`_.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The five playbook section numbers that are synthesis-driven (narrative
#: sections the deterministic engine cannot produce from static analysis).
SYNTHESIS_SECTION_NUMBERS: frozenset[int] = frozenset({1, 2, 8, 14, 15})

#: ``path:line`` citation format. A citation must have at least one path
#: character before the colon and at least one decimal digit after it.
#: Disk existence is NOT checked here — that is deferred to Phase D
#: refcheck (see ``primitives._shared.modernization.refcheck``).
_CITATION_RE: re.Pattern[str] = re.compile(r"^.+:\d+$")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SynthesisValidationError(ValueError):
    """Raised by :func:`load_synthesis` when ``synthesis.json`` is malformed.

    Callers should map this exception to CLI exit code 3 (input rejected),
    mirroring the exit-code convention of the other PLB primitives.

    The ``args[0]`` string is a human-readable description of the validation
    failure, suitable for a ``--verbose`` diagnostic message.
    """


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class SynthesisSection(BaseModel):
    """Harness-authored content for one synthesis-driven playbook section.

    Corresponds to one entry in the ``sections`` map of ``synthesis.json``.
    Both ``body`` and ``citations`` are required; extra keys are forbidden
    (``extra="forbid"``).

    ``citations`` entries must match ``path:line`` format (e.g.
    ``billing/engine.py:42``). Disk existence is NOT validated here — that is
    Phase D's job.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    body: str
    citations: list[str]

    @field_validator("citations", mode="after")
    @classmethod
    def _validate_citations(cls, values: list[str]) -> list[str]:
        for entry in values:
            if not _CITATION_RE.match(entry):
                raise ValueError(
                    f"citation {entry!r} does not match path:line format "
                    "(expected e.g. 'billing/engine.py:42')"
                )
        return values


class SynthesisJudgments(BaseModel):
    """Harness-returned synthesis judgments (top-level ``synthesis.json``).

    ``sections`` maps section numbers (as ``int``) to their
    :class:`SynthesisSection`. Keys must be drawn from
    :data:`SYNTHESIS_SECTION_NUMBERS` ({1, 2, 8, 14, 15}).

    A **missing** key is intentional and valid — the pipeline falls back to
    the deterministic ``⚠ UNVERIFIED`` stub for any absent section. An
    **extra** key (e.g. section 3) is rejected as a validation error.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sections: dict[int, SynthesisSection]

    @field_validator("sections", mode="after")
    @classmethod
    def _validate_section_keys(
        cls, mapping: dict[int, SynthesisSection]
    ) -> dict[int, SynthesisSection]:
        bad = set(mapping.keys()) - SYNTHESIS_SECTION_NUMBERS
        if bad:
            raise ValueError(
                f"synthesis.json contains unexpected section key(s): "
                f"{sorted(bad)} — allowed: {sorted(SYNTHESIS_SECTION_NUMBERS)}"
            )
        return mapping


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_synthesis(path: Path) -> SynthesisJudgments:
    """Read and validate a ``synthesis.json`` file produced by the harness.

    Args:
        path: Filesystem path to the harness-written ``synthesis.json``.

    Returns:
        A validated, frozen :class:`SynthesisJudgments` instance.

    Raises:
        SynthesisValidationError: If the file is missing, not valid JSON, or
            fails schema/type validation. Callers should map this to CLI exit
            code 3 (input rejected).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SynthesisValidationError(f"could not read synthesis.json at {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SynthesisValidationError(
            f"synthesis.json at {path} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise SynthesisValidationError(
            f"synthesis.json at {path} must be a JSON object, got {type(data).__name__}"
        )

    # The JSON schema stores section keys as strings ("1", "2", …); coerce to int
    # before passing to the pydantic model so the dict[int, SynthesisSection] type
    # is satisfied without requiring the harness to emit integer keys.
    sections_raw = data.get("sections")
    if sections_raw is not None and isinstance(sections_raw, dict):
        try:
            sections_coerced: dict[int, object] = {int(k): v for k, v in sections_raw.items()}
        except (ValueError, TypeError) as exc:
            raise SynthesisValidationError(
                f'synthesis.json section keys must be numeric strings (e.g. "1", "8"): {exc}'
            ) from exc
        data = {**data, "sections": sections_coerced}

    try:
        return SynthesisJudgments.model_validate(data)
    except ValidationError as exc:
        # Flatten pydantic's structured errors into a single readable message.
        errors = "; ".join(
            f"{' -> '.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise SynthesisValidationError(
            f"synthesis.json at {path} failed validation: {errors}"
        ) from exc


__all__ = [
    "SYNTHESIS_SECTION_NUMBERS",
    "SynthesisJudgments",
    "SynthesisSection",
    "SynthesisValidationError",
    "load_synthesis",
]
