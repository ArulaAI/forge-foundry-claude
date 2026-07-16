"""Twin-repo modernization delta models (#1621 D3).

Data models for the migration-delta.json output produced by the same-app
correlator + completeness assessor.  The roll-up function ``delta_to_states``
converts a ``MigrationDelta`` into the existing ``MigrationStates`` vocabulary
(``ConfirmedMigrationState`` / ``Band``) so the downstream what's-left filter
(``resolve_whats_left``) and sequencer (``sequencer.order``) can be reused
without modification.

Additive: this module has no effect on single-repo or tiered multi-repo runs
(those paths never construct a ``MigrationDelta``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from primitives._shared.discovery.models.entities import Confidence  # noqa: TC001
from primitives._shared.modernization.evidence import (
    ConfirmedMigrationState,
    MigrationStates,
)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

Disposition = Literal["present-complete", "present-stubbed", "absent"]

# ---------------------------------------------------------------------------
# Data models (frozen, extra=forbid — matches _shared/modernization/models.py style)
# ---------------------------------------------------------------------------


class EndpointDelta(BaseModel):
    """One legacy endpoint's disposition in the target repo."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    legacy: str
    legacy_evidence: str = ""
    target: str | None = None
    target_evidence: str | None = None
    disposition: Disposition
    confidence: Confidence


class EntityDelta(BaseModel):
    """One legacy entity/domain-object's disposition in the target repo."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    legacy: str
    target: str | None = None
    disposition: Disposition
    confidence: Confidence


class ContextDelta(BaseModel):
    """All endpoint + entity deltas for one legacy bounded context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context: str
    match_confidence: Confidence
    endpoints: tuple[EndpointDelta, ...] = ()
    entities: tuple[EntityDelta, ...] = ()


class MigrationDelta(BaseModel):
    """The full twin-repo migration delta (migration-delta.json payload)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    legacy_repo: str
    target_repo: str
    contexts: tuple[ContextDelta, ...] = ()


class ContextPairing(BaseModel):
    """Outcome of pairing one legacy context against candidate target contexts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    legacy_context: str
    target: str | None = None
    match_confidence: Confidence


# ---------------------------------------------------------------------------
# Task 6: Roll-up → ConfirmedMigrationState
# ---------------------------------------------------------------------------


def delta_to_states(
    delta: MigrationDelta,
    context_to_code_root: dict[str, str],
) -> MigrationStates:
    """Convert a ``MigrationDelta`` into the existing ``MigrationStates`` vocabulary.

    Banding rules per context:
    - all endpoints+entities present-complete AND high match_confidence →
      ``modern``/``deterministic``
    - any present-complete but also some absent/stubbed →
      ``partial``/``deterministic``
    - nothing complete → ``legacy``/``deterministic``
    - any ``low`` match_confidence (regardless of dispositions) → ``source="unverified"``

    Contexts whose ``code_root`` is missing from *context_to_code_root* are silently
    skipped (they have no anchor in the system-map).
    """
    confirmed: list[ConfirmedMigrationState] = []
    for ctx in delta.contexts:
        code_root = context_to_code_root.get(ctx.context)
        if code_root is None:
            continue

        dispositions = [e.disposition for e in ctx.endpoints] + [
            e.disposition for e in ctx.entities
        ]
        complete = [d == "present-complete" for d in dispositions]

        if not dispositions:
            band: str = "legacy"
            source: str = "deterministic"
        elif all(complete):
            band = "modern"
            source = "deterministic"
        elif any(complete):
            band = "partial"
            source = "deterministic"
        else:
            band = "legacy"
            source = "deterministic"

        # Low match_confidence overrides source → unverified (harness must confirm).
        if ctx.match_confidence == "low":
            source = "unverified"

        confirmed.append(
            ConfirmedMigrationState(
                code_root=code_root,
                band=band,  # type: ignore[arg-type]
                confidence=ctx.match_confidence,
                source=source,  # type: ignore[arg-type]
            )
        )
    return MigrationStates(states=tuple(confirmed))
