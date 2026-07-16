from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import Confidence


class WebRailsFlavor:
    """Deferred stub — real routes.rb extraction is future work; generic fallback covers it."""

    def detect(self, code_root: Path) -> Confidence:  # noqa: ARG002
        return "low"

    def parse(self, file_path: Path, content: str) -> dict[str, list[object]]:  # noqa: ARG002
        return {"endpoints": [], "guards": [], "roles": [], "nav_edges": []}

    def recommended_section_branches(self) -> list[str]:
        return []
