from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import Confidence

_PATH = re.compile(r"""path\(\s*['"](?P<path>[^'"]*)['"]""")


class WebDjangoFlavor:
    def detect(self, code_root: Path) -> Confidence:
        if (code_root / "manage.py").is_file():
            return "high"
        return "low"

    def parse(self, file_path: Path, content: str) -> dict[str, list[object]]:
        endpoints: list[object] = []
        for m in _PATH.finditer(content):
            line = content[: m.start()].count("\n") + 1
            endpoints.append(
                {
                    "path": "/" + m.group("path").lstrip("/"),
                    "method": "ANY",
                    "handler": "view",
                    "file": str(file_path),
                    "line": line,
                }
            )
        return {"endpoints": endpoints, "guards": [], "roles": [], "nav_edges": []}

    def recommended_section_branches(self) -> list[str]:
        return ["User Flows — Django URLConf"]
