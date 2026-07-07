# Vendored from forge-prowess-primitives@v0.4.0 — see CHANGELOG-VENDOR.md
"""C10 /failure-mode-audit — Report renderer format registry (FLEX-3).

The actual rendering is handled by the per-format modules in
``primitives.failure_mode_audit.shared.output``.  This module exposes
``list_formats()`` so the CLI ``--list-formats`` flag has a stable lookup
target without duplicating the format list inline in the command.
"""

from __future__ import annotations

_FORMATS: tuple[str, ...] = ("markdown", "json", "sarif")


def list_formats() -> list[str]:
    """Return the list of supported output formats (FLEX-3)."""
    return list(_FORMATS)
