"""Adapters — normalize upstream digest shapes into ``ModernizationDigest``.

Each adapter maps one upstream artifact format (app-analysis digest, API
contract, functional-map output, …) to the token-budgeted
:class:`~primitives._shared.modernization.models.ModernizationDigest` that
``correlate.py`` and ``sequencer.py`` consume.

Adapters are deterministic (no LLM, no I/O). Each module exposes a ``to_digest``
entry point, re-exported here under source-qualified aliases so callers pick one
unambiguously; import the module's ``to_digest`` directly when the source is clear.
"""

from __future__ import annotations

from primitives._shared.modernization.adapters.api import to_digest as api_to_digest
from primitives._shared.modernization.adapters.api import to_endpoints as api_to_endpoints
from primitives._shared.modernization.adapters.app_digest import (
    extract_data_field_mappings,
)
from primitives._shared.modernization.adapters.app_digest import (
    to_digest as app_digest_to_digest,
)
from primitives._shared.modernization.adapters.functional_map import (
    functional_map_to_inventory,
)
from primitives._shared.modernization.adapters.functional_map import (
    to_digest as functional_map_to_digest,
)
from primitives._shared.modernization.adapters.functional_map import (
    to_entry_points as functional_map_to_entry_points,
)
from primitives._shared.modernization.adapters.iac import (
    PROVISIONS_PREFIX,
    iac_to_digest,
)
from primitives._shared.modernization.adapters.runtime_journeys import (
    discover_runtime_journeys,
    runtime_journeys_section,
    runtime_journeys_to_flows,
)

__all__ = [
    "PROVISIONS_PREFIX",
    "api_to_digest",
    "api_to_endpoints",
    "app_digest_to_digest",
    "discover_runtime_journeys",
    "extract_data_field_mappings",
    "functional_map_to_digest",
    "functional_map_to_entry_points",
    "functional_map_to_inventory",
    "iac_to_digest",
    "runtime_journeys_section",
    "runtime_journeys_to_flows",
]
