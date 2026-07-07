"""Declared route/surface -> e2e-spec map loader for S2 maintenance (#1029).

The consumer declares which e2e specs exercise which source surfaces in
``.foundry/e2e-map.yaml``; the maintainer reads it to flag impacted e2e specs on a
route/handler change (coverage maps almost never reach browser-driven e2e). The
loader is tolerant: a missing or malformed map yields no rules (a warning, never a
crash), so the maintainer behaves exactly as before. A spec/surface glob that escapes
the project root is the one hard error (SEC-10 containment discipline).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class E2ERule:
    """One declared (surface|route) -> e2e-spec-globs rule."""

    specs: tuple[str, ...]
    surface: str | None = None
    route: str | None = None


def _reject_escaping(glob: str) -> None:
    """Reject an absolute or ``..``-bearing glob BEFORE any resolution (SEC-10).

    An empty glob is a no-op (it matches nothing); only a containment violation
    raises.
    """
    if not glob:
        return
    pure = glob.replace("\\", "/")
    if pure.startswith("/") or pure.startswith("~") or ".." in pure.split("/"):
        msg = f"invalid e2e-map glob {glob!r}: escapes the project root"
        raise ValueError(msg)


def load_e2e_map(map_path: Path) -> tuple[E2ERule, ...]:
    """Parse + validate ``.foundry/e2e-map.yaml`` into rules.

    Returns ``()`` for a missing or malformed map (logged WARNING, never raised) so
    the maintainer degrades to "no e2e impact". Raises ``ValueError`` only when a
    rule's ``specs``/``surface`` glob escapes the project root.
    """
    if not map_path.is_file():
        return ()
    try:
        data = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        _log.warning("e2e-map %s unreadable (%s); ignoring", map_path, exc)
        return ()
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        _log.warning("e2e-map %s is not a {rules: [...]} object; ignoring", map_path)
        return ()

    rules: list[E2ERule] = []
    for raw in data["rules"]:
        if not isinstance(raw, dict):
            continue
        specs = raw.get("specs")
        if not isinstance(specs, list) or not specs:
            continue  # a rule with no specs contributes nothing
        non_str = [s for s in specs if not isinstance(s, str)]
        if non_str:
            _log.debug("e2e-map rule has non-string specs %r; coercing", non_str)
        spec_tuple = tuple(str(s) for s in specs)
        surface = raw.get("surface")
        route = raw.get("route")
        globs_to_check = (
            *spec_tuple,
            *((str(surface),) if isinstance(surface, str) else ()),
        )
        for g in globs_to_check:
            _reject_escaping(g)
        rules.append(
            E2ERule(
                specs=spec_tuple,
                surface=str(surface) if isinstance(surface, str) else None,
                route=str(route) if isinstance(route, str) else None,
            )
        )
    return tuple(rules)


def expand_specs(project_root: Path, globs: Iterable[str]) -> frozenset[str]:
    """Expand *globs* against the working tree -> repo-relative POSIX spec paths.

    Each glob is containment-checked; matches are confirmed to resolve under
    *project_root*. Globs matching nothing contribute nothing.
    """
    root = project_root.resolve()
    out: set[str] = set()
    for g in globs:
        _reject_escaping(g)
        for p in project_root.glob(g):
            if not p.is_file():
                continue
            resolved = p.resolve()
            if not resolved.is_relative_to(root):
                continue
            out.add(resolved.relative_to(root).as_posix())
    return frozenset(out)
