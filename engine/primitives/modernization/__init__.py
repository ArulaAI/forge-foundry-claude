"""SP4 `/mod-modernization` — skill-first orchestrator primitive.

Drives the full app-modernization chain end-to-end by composing five analyzer
skills in order — S61 `/functional-map` → M1 `/mod-db-schema-analyze` → S40
`/api-contract-review` → SP3 `/mod-app-analysis` → SP2 `/mod-modernization-scope`
— persisting each stage's durable artifact to the conventional
`.foundry/context/` discovery path the scope synthesizer reads from, tracking
per-stage pass/fail/skip in a resumable state file, and rolling a top-level
summary.

The canonical agent skill is under ``skill/SKILL.md``.  The only Python component
is the roll-up renderer under ``shared/`` (``rollup_aggregator.py``), which
operates under the charter §4.1 orchestrator-shape carve-out (no ``ci.yaml``, no
``forge-validate`` command).
"""
