"""M-tier `/mod-app-analysis` — skill-first orchestrator primitive.

Orchestrates `/mod-playbook-discovery`, `/repo-digest`, and (web-tier only)
`/functional-map`, then runs the deterministic aggregator to produce
`app-digest.json` + `ANALYSIS.md`.

The canonical agent skill is under ``skill/SKILL.md``. The only Python
component is the post-discovery aggregator under ``shared/``, which operates
under the charter §4.1 orchestrator-shape carve-out (no ``ci.yaml``, no
``forge-validate`` command).
"""
