"""FLEX-3 (#175): per-primitive output-renderer plugin registry.

Each review primitive ships its own ``RendererRegistry`` for its report type. The
built-in markdown/json/sarif renderers self-register at import time. Entry-point
plugins under ``forge_foundry.renderers.<primitive>`` override same-named
built-ins, so a consumer can ship a JUnit XML or CycloneDX renderer by declaring
it in their own ``pyproject.toml`` — no edits to this repo.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_ENTRY_POINT_GROUP_PREFIX = "forge_foundry.renderers"


class RendererRegistry[R]:
    """Built-ins + entry-point plugins, keyed by format name.

    Instantiate one per primitive: ``RendererRegistry("code_review")`` discovers
    plugins from ``forge_foundry.renderers.code_review``. The registry stays
    instance-scoped so two primitives can't accidentally share renderers.
    """

    def __init__(self, primitive_name: str) -> None:
        self._primitive_name = primitive_name
        self._builtins: dict[str, Callable[[R], str]] = {}

    @property
    def entry_point_group(self) -> str:
        return f"{_ENTRY_POINT_GROUP_PREFIX}.{self._primitive_name}"

    def register(self, name: str, fn: Callable[[R], str]) -> None:
        """Register *fn* under *name*. Last-writer-wins among built-ins."""
        self._builtins[name] = fn

    def discover(self) -> dict[str, Callable[[R], str]]:
        """Built-ins overlaid with entry-point plugins (plugin wins on same name)."""
        merged: dict[str, Callable[[R], str]] = dict(self._builtins)
        for ep in entry_points(group=self.entry_point_group):
            merged[ep.name] = ep.load()
        return dict(sorted(merged.items()))

    def list_formats(self) -> list[str]:
        return list(self.discover().keys())

    def render(self, report: R, format_name: str) -> str:
        renderers = self.discover()
        if format_name not in renderers:
            raise ValueError(
                f"unknown output format {format_name!r} for {self._primitive_name}; "
                f"available: {list(renderers.keys())}. Install a plugin that declares "
                f'`[project.entry-points."{self.entry_point_group}"]`.'
            )
        return renderers[format_name](report)
