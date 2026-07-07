"""M5 ``/mod-playbook-discovery`` — Python-load-bearing shared package.

Holds the deterministic orchestrator (:mod:`.pipeline`) that the
``forge-validate mod-playbook-discovery`` CLI delegates to. The heavy
per-file loops live in the cross-primitive discovery engine
(:mod:`primitives._shared.discovery`); this package is the thin
M5-specific glue that sequences the four engine phases (recon → analyze
→ render → audit) into a single deterministic run.
"""
