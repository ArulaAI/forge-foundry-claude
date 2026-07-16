from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from primitives.functional_map.shared.models import FunctionalDigest

Cell = Literal["allow", "deny", "public"]


@dataclass(frozen=True)
class AccessMatrix:
    roles: tuple[str, ...]
    capability_ids: tuple[str, ...]
    _allow: frozenset[tuple[str, str]]
    _public: frozenset[str]
    _gaps: tuple[str, ...]

    def cell(self, role: str, cap_id: str) -> Cell:
        if cap_id in self._public:
            return "public"
        return "allow" if (role, cap_id) in self._allow else "deny"

    def gaps(self) -> tuple[str, ...]:
        return self._gaps


def build_matrix(digest: FunctionalDigest) -> AccessMatrix:
    roles = tuple(digest.role_names())
    cap_ids = tuple(c.id for c in digest.capabilities)
    guarded_eps = {g.endpoint_id for g in digest.guards}
    public_eps = {g.endpoint_id for g in digest.guards if g.kind == "public"}

    allow: set[tuple[str, str]] = set()
    public: set[str] = set()
    gaps: list[str] = []
    for cap in digest.capabilities:
        if cap.endpoint_id in public_eps:
            public.add(cap.id)
            continue
        if cap.endpoint_id not in guarded_eps:
            # ungated non-public capability → security-relevant gap
            gaps.append(cap.id)
        for role in cap.roles:
            allow.add((role, cap.id))
    return AccessMatrix(
        roles=roles,
        capability_ids=cap_ids,
        _allow=frozenset(allow),
        _public=frozenset(public),
        _gaps=tuple(gaps),
    )
