"""Digest compressor (Layer 1) — full ``facts`` → token-budgeted digest.

Generalized from ``mfx``'s ``extract/__init__.py::build_digest`` (design spec
§3, Layer 1). :func:`compress` reduces a full :class:`EngineFacts` to a
:class:`ModernizationDigest` that stays within a small token budget (target
≤1.5k tokens) by keeping only cross-unit-relevant fields and replacing full
per-entity records with name-only top-N lists plus counts.

Pure and deterministic: no LLM, no clock, no filesystem. The digest's
``generated_at`` is carried straight from ``facts`` so the same facts always
compress to the same digest (golden-file testable). Ordering is stable —
interfaces keep source order, key entities sort by descending weight with the
name as a tiebreaker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives._shared.modernization.models import ModernizationDigest

if TYPE_CHECKING:
    from primitives._shared.modernization.models import EngineFacts

#: Default caps on the digest's truncated lists. Chosen so a realistic unit's
#: digest serializes well under the ≤1.5k-token budget while still naming the
#: entities cross-unit reasoning actually references.
DEFAULT_MAX_INTERFACES = 20
DEFAULT_MAX_ENTITIES = 20
DEFAULT_MAX_DEBT = 15
DEFAULT_MAX_SHARED = 20


def compress(
    facts: EngineFacts,
    *,
    max_interfaces: int = DEFAULT_MAX_INTERFACES,
    max_entities: int = DEFAULT_MAX_ENTITIES,
    max_debt: int = DEFAULT_MAX_DEBT,
    max_shared: int = DEFAULT_MAX_SHARED,
) -> ModernizationDigest:
    """Compress *facts* into a token-budgeted :class:`ModernizationDigest`.

    Args:
        facts: the full, per-entity facts for one unit.
        max_interfaces: cap on each of ``exposed_interfaces`` / ``consumed_interfaces``.
        max_entities: cap on ``key_entities`` (the highest-weight units).
        max_debt: cap on ``debt_summary``.

    Returns:
        A digest carrying identity + stack/migration summary + name-only
        interface/entity top-N lists + interpretive pass-throughs + full counts.
    """
    exposed = [i.name for i in facts.interfaces if i.direction == "exposed"]
    consumed = [i.name for i in facts.interfaces if i.direction == "consumed"]
    # Highest-weight units first; name breaks ties so the order is total + stable.
    key_units = sorted(facts.units, key=lambda u: (-u.weight, u.name))

    return ModernizationDigest(
        code_root=facts.code_root,
        flavor=facts.flavor,
        loc_by_language=dict(facts.loc_by_language),
        migration_state=facts.migration_state,
        exposed_interfaces=exposed[:max_interfaces],
        consumed_interfaces=consumed[:max_interfaces],
        domains=list(facts.domains),
        key_entities=[u.name for u in key_units[:max_entities]],
        tier_hint=facts.tier_hint,
        debt_summary=list(facts.debt[:max_debt]),
        shared_components=list(facts.shared_components[:max_shared]),
        unit_count=len(facts.units),
        shared_component_count=len(facts.shared_components),
        generated_at=facts.generated_at,
    )
