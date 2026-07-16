"""SP4 `/mod-modernization` shared/ — the roll-up aggregator only.

Charter §4.1 orchestrator-shape carve-out: this skill-first orchestrator carries
a minimal `shared/` holding ONLY the deterministic roll-up renderer
(`rollup_aggregator.py`), which rolls the resumable per-stage state into a single
`MODERNIZATION-SUMMARY.md`.  There is no `ci.yaml` and no `forge-validate` command.
"""
