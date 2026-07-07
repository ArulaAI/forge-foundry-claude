"""Deterministic generators for the S4 ``/test-scaffold`` primitive (#1031).

The PLB runtime: stack detection (via :mod:`primitives._sdet.stacks`), S1
architecture parsing, the cone-shape strategy engine, the per-layer renderers,
and the idempotent, containment-guarded write sink. The CLI never calls an LLM.
"""

from __future__ import annotations
