"""M6 /mod-synth-logs — Jinja2 template package.

This package holds the Jinja2 templates Phase C consumes when rendering
the standalone ``generate_logs.py`` script that ships to the consumer
repo (design spec §"Phases", Phase C row).

- ``generate_logs.py.j2`` — the script skeleton + config-dict baking
  (M6-TEMPLATE-1 / #469). The skeleton imports stdlib only, bakes the
  ``ScenarioPlan`` + ``EntitiesDigest`` into module-level constants, and
  wires a per-``LogFileSpec`` dispatch loop to ``_render_<format>()``
  helpers. The per-format helpers themselves ship as stubs raising
  ``NotImplementedError`` pointing at the matching follow-up issues
  (#470 text formats, #471 JSONL formats, #472 Prometheus); the issues
  flip them on one format at a time.

The package is intentionally a Python package (carries ``__init__.py``)
so ``importlib.resources`` can resolve the ``.j2`` templates regardless
of how ``forge-foundry-sync`` deploys them in a consumer repo (zip-safe).
"""
