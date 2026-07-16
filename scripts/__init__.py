"""Vendor tooling for the forge-foundry-claude plugin.

Making ``scripts`` a real package lets the parity tests import ``scripts._vendor.*``
deterministically. The vendor ENTRY POINT (``vendor.py``) deliberately imports
``_vendor.*`` bare (from its own directory) instead, so it stays immune to a
shadowing ``scripts`` package from the source library on ``sys.path``.
"""
