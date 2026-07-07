"""S4 ``/test-scaffold`` — full-cone runnable test scaffolding (#1030).

Consumes an S1 ``sdet_architect`` ``test-architecture.md`` (strategy + per-layer
tooling picks + cone shape) plus the detected stack, and writes runnable test
scaffolding for the whole cone: unit (delegates to S3 ``test_from_spec``),
integration, container, contract, API e2e, and UI e2e. Python-load-bearing: the
deterministic generators live in ``shared/`` and the skill body delegates to
``forge-validate test-scaffold``.

See ``docs/superpowers/specs/2026-06-25-test-scaffold-primitive-design.md``.
"""

from __future__ import annotations
