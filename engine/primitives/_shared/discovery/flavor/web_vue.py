from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class WebVueFlavor:
    """Flavor pack for Vue.js single-page applications."""

    name = "web_vue"

    def detect(self, code_root: Path) -> str:
        """Return 'high' if this looks like a Vue.js project."""
        # package.json with vue dependency
        pkg = code_root / "package.json"
        if pkg.is_file():
            text = pkg.read_text(errors="replace")
            if '"vue"' in text:
                return "high"
        # .vue files present
        vue_files = list(code_root.rglob("*.vue"))
        if vue_files:
            return "high"
        # vue.config.js / vite.config.js with vue plugin
        for cfg_name in ("vue.config.js", "vue.config.ts"):
            cfg = code_root / cfg_name
            if cfg.is_file():
                return "high"
        return "none"

    def parse(self, file_path: Path, content: str) -> dict[str, list[object]]:
        from primitives.functional_map import run_sub_extractors  # noqa: PLC0415

        return run_sub_extractors(("vue_routes",), file_path, content)

    def recommended_section_branches(self) -> list[str]:
        return ["web_vue"]
