"""Importable deterministic engine for the S3 ``test_from_spec`` primitive.

:mod:`~primitives.test_from_spec.shared.engine` is the canonical importable
source of truth for the spec-parse + stack-detect + static-review logic.
The standalone ``skill/scripts/spec_parse.py`` (stdlib-only, no package
imports) is kept alive for the SKILL's consumer-repo use until issue B4.
"""
