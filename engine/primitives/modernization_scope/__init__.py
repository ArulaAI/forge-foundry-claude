"""mod-modernization-scope (SP2) — the app-tier modernization synthesizer (PLB).

Correlates the required analysis artifacts (functional-map S61, db-schema M1,
api-contract S40, app-digest from mod-app-analysis) into the four modernization
deliverables: a deconstruction/system-map, role-complete specs, standalone ADRs,
and a wave-by-wave roadmap — reusing the vendored ``primitives._shared.modernization``
engine (adapters, ``build_system_map``, ``render_deconstruction``, ``scope``,
``sequencer.order``, ``refcheck``).

See ``docs/superpowers/specs/2026-07-09-app-modernization-design.md`` §3.2, §4, §6.
"""

from __future__ import annotations
