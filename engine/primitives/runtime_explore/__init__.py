"""runtime_explore (mod-runtime-explore) — runtime web-app feature discovery.

Skill-first orchestrator (charter §4.1 carve-out): the skill body drives a live app
with local Playwright; the minimal ``shared/`` holds the deterministic crawler, the
agent-driven step-CLI, and the journey aggregator. No ci.yaml, no forge-validate cmd.
"""
