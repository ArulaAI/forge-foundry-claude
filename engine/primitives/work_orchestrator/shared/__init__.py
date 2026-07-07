"""C7 work-orchestrator control plane (zero-LLM, Python-load-bearing).

The recursive node tree, state persistence, stage sequencing, structured-data
marshalling, gating, and roll-up rendering all live here at ~0 model tokens. The
model only ever produces compact structured artifacts (a spec, a plan+verdict, a
diff judgment, a review verdict), round-tripped via the orchestrator-level
extract -> judge -> continue protocol.

Design spec:
``docs/superpowers/specs/2026-06-24-work-orchestrator-recursive-redesign.md``.
"""

from __future__ import annotations
