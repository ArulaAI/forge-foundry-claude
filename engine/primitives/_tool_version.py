"""Single source of truth for the installed foundry library version.

Used by every report renderer to stamp the `tool.version` field in SARIF
output and similar metadata. Returns ``default`` (``"0.0.0"`` unless overridden)
when the package is not installed — typically only in dev-environments where
the package was checked out but not `pip install`-ed.

Pulled out per issue #89 (QUAL-1) to replace 10 byte-for-byte copies of the
same try/except/import block scattered across primitives/*/shared/report_renderer.py.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def get_tool_version(default: str = "0.0.0") -> str:
    """Return the installed `forge-foundry-primitives` package version."""
    try:
        return version("forge-foundry-primitives")
    except PackageNotFoundError:
        return default


__all__ = ["get_tool_version"]
