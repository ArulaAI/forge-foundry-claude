from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import Confidence


class WebJsfFlavor:
    name = "web_jsf"

    def detect(self, code_root: Path) -> Confidence:
        if any(code_root.rglob("faces-config.xml")):
            return "high"
        for pom in code_root.rglob("pom.xml"):
            try:
                content = pom.read_text(errors="ignore")
                if "javax.faces" in content or "jakarta.faces" in content:
                    return "high"
            except OSError:
                pass
        return "low"

    def parse(self, file_path: Path, content: str) -> dict[str, list[object]]:
        from primitives.functional_map import run_sub_extractors  # noqa: PLC0415

        return run_sub_extractors(("jsf_beans", "spring_security"), file_path, content)

    def recommended_section_branches(self) -> list[str]:
        return ["web_jsf"]
