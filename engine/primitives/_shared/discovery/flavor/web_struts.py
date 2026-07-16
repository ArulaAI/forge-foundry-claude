from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import Confidence


class WebStrutsFlavor:
    name = "web_struts"

    def detect(self, code_root: Path) -> Confidence:
        for pattern in ("struts.xml", "struts-config.xml"):
            if any(code_root.rglob(pattern)):
                return "high"
        return "low"

    def parse(self, file_path: Path, content: str) -> dict[str, list[object]]:
        from primitives.functional_map import run_sub_extractors  # noqa: PLC0415

        merged = run_sub_extractors(
            ("struts_actions", "jsp_forms", "spring_security"), file_path, content
        )
        return {k: v for k, v in merged.items() if v}

    def recommended_section_branches(self) -> list[str]:
        return ["web_struts"]
