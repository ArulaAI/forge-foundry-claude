"""Deterministic engine for mod-modernization-scope (SP2, PLB).

The Python single source of truth. ``pipeline.run`` orchestrates artifact
discovery, the SP1 adapters, ``correlate.build_system_map``,
``deconstruct.render_deconstruction``, ``scope`` (spec/ADR renderers), and
``sequencer.order`` — plus the extract→continue synthesis protocol.
"""

from __future__ import annotations
