"""M-tier `/mod-test-inventory` — skill-first legacy-test / parity-oracle analyzer.

Walks a source repository for its **existing tests** (JUnit / TestNG, pytest /
unittest, Jest / Vitest / Mocha) and emits ``test-inventory.json`` — per test its
name, the unit/endpoint it exercises, and its assertion intent. The scope stage
slices these by bounded context and the ``/mod-modernization-scope`` synthesizer
carries them into the Test Spec (§11) as **parity oracles**
(``is_parity_oracle=True``), grounding acceptance against actual legacy behavior.

The canonical agent skill is under ``skill/SKILL.md``. The only Python component
is the deterministic post-walk aggregator under ``shared/``, which operates under
the charter §4.1 orchestrator-shape carve-out (no ``ci.yaml``, no
``forge-validate`` command).
"""
