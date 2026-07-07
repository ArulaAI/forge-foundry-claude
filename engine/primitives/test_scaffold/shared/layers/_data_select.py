"""Shared data-backend resolution for the data-test layers (DT-2…DT-6).

The container layer and every data-test layer (data-structural / data-quality /
data-seed / data-parity) resolve which data backend(s) to scaffold for the same
way: an explicit ``--data-backend`` override (``fillins["data_backend"]``,
comma-separated) wins, else the backends are auto-detected from the project's
dependency manifests via the DT-2 ``_sdet`` registry, else Postgres. Centralized
here so the layers stay in lockstep (one resolution, one fixture-naming
convention) instead of each carrying a copy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives._sdet.stacks import detect_data_backends, parse_data_backends

if TYPE_CHECKING:
    from primitives.test_scaffold.shared.registry import ScaffoldContext

#: Fallback backend when none is detected or requested (historical default).
DEFAULT_BACKEND = "postgres"


def resolve_backends(context: ScaffoldContext) -> list[str]:
    """The data backends to scaffold for (non-empty, deterministic order).

    Order: explicit ``--data-backend`` override → manifest auto-detection →
    Postgres fallback. Both detection and the override parser return sorted,
    de-duplicated registered names.
    """
    override = context.fillins.get("data_backend")
    names = (
        parse_data_backends(override) if override else detect_data_backends(context.project_root)
    )
    return names or [DEFAULT_BACKEND]
