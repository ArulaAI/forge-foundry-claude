"""The declared-e2e impact leg for S2 maintenance (#1029).

Matches a diff's changed paths against the declared ``.foundry/e2e-map.yaml`` rules
and emits an ``ImpactEntry(source="declared-e2e")`` per (matched surface, impacted
e2e spec). A route change thus flags the e2e specs that exercise it -- the
association coverage maps miss. Deterministic + map-driven (no subprocess); returns
[] when no map exists or nothing matches.
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING

from primitives.test_maintainer.shared.e2e_map import expand_specs, load_e2e_map
from primitives.test_maintainer.shared.impact import ImpactClass, ImpactEntry

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._sdet.stacks.contract import ChangedSet


def compute_e2e_impacted(
    project_root: Path,
    changed_set: ChangedSet,
    *,
    map_path: Path | None = None,
) -> list[ImpactEntry]:
    """``ImpactEntry(source="declared-e2e")`` for each (surface, impacted e2e spec).

    *symbol* is the matched surface glob; *test_id* is the spec file path (Playwright's
    selection granularity is file-based). Returns [] when the map is absent or no rule
    matches a changed path.
    """
    mp = map_path if map_path is not None else project_root / ".foundry" / "e2e-map.yaml"
    rules = load_e2e_map(mp)
    if not rules:
        return []

    changed_paths = changed_set.paths()
    entries: list[ImpactEntry] = []
    seen: set[tuple[str, str]] = set()
    for rule in rules:
        if rule.surface is None:
            # route-only rules need parsed routes; deferred (best-effort future leg).
            continue
        if not any(fnmatch.fnmatch(p, rule.surface) for p in changed_paths):
            continue
        for spec in sorted(expand_specs(project_root, rule.specs)):
            key = (rule.surface, spec)
            if key in seen:
                continue
            seen.add(key)
            entries.append(
                ImpactEntry(
                    symbol=rule.surface,
                    test_id=spec,
                    impact_class=ImpactClass.BEHAVIOR,
                    source="declared-e2e",
                )
            )
    return entries
