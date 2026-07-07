"""Per-layer scaffold renderers (#1031 seam; renderers land in #1033-#1038).

Importing this package self-registers every built-in ``(stack, layer)`` renderer
via :func:`primitives.test_scaffold.shared.registry.register_scaffold`, mirroring
the ``_sdet.stacks`` self-registration pattern. The keystone ships the package
with no renderers yet; each layer issue adds its renderer module and registers it
here. The CLI imports this package so the built-ins are present before a run.
"""

from __future__ import annotations

# Built-in layer renderers self-register on import.
from primitives.test_scaffold.shared.layers import (  # noqa: F401
    api_e2e,
    container,
    contract,
    data_parity,
    data_quality,
    data_seed,
    data_structural,
    integration,
    ui_e2e,
    unit,
)
