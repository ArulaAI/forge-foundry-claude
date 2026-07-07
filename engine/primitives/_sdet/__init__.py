"""Internal SDET infrastructure shared by S1, S2, and S3.

This is NOT a standalone primitive — it has no SPEC.md, adapters, or EXAMPLES.md.
The leading underscore signals internal-only use. Contains:

- ``test_layer.py`` — TestLayer enum + ConeRatios dataclass (canonical test-cone model)
- ``depgraph/`` — Per-stack dependency-graph walkers (Python AST, JS/TS regex, Java imports)
- ``stacks/`` — Stack detection and configuration
- ``config.py`` — SDET configuration loader (.foundry/sdet.yaml)
"""
