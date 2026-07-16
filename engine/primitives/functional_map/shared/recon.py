from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from primitives._shared.discovery.flavor import FLAVOR_PACKS
from primitives._shared.scan_exclusions import walk_source_files

if TYPE_CHECKING:
    from pathlib import Path

_WEB_EXTENSIONS = {
    ".java",
    ".kt",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".vue",
    ".py",
    ".rb",
    ".php",
    ".cs",
    ".jsp",
    ".jspx",
    ".xhtml",
    ".html",
    ".ftl",
    ".ftlh",
    ".xml",
    ".wsdl",
    ".graphql",
    ".gql",
    ".graphqls",
    ".proto",
    ".yaml",
    ".yml",
    ".json",
    ".properties",
}
_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


@dataclass(frozen=True)
class ReconReport:
    code_root: Path
    flavor: str
    source_files: tuple[Path, ...]
    has_spec: bool


def resolve_flavor(code_root: Path, requested: str) -> str:
    if requested != "auto":
        return requested
    best_name, best_rank = "generic", 0
    for name, pack in FLAVOR_PACKS.items():
        if not name.startswith("web_"):
            continue
        rank = _CONFIDENCE_RANK.get(pack.detect(code_root), 0)
        if rank > best_rank:
            best_name, best_rank = name, rank
    return best_name if best_rank >= _CONFIDENCE_RANK["medium"] else "generic"


def run_recon(
    code_root: Path,
    *,
    stack_flavor: str = "auto",
    spec: Path | None = None,
) -> ReconReport:
    flavor = resolve_flavor(code_root, stack_flavor)
    files = tuple(walk_source_files(code_root, _WEB_EXTENSIONS))
    return ReconReport(
        code_root=code_root,
        flavor=flavor,
        source_files=files,
        has_spec=bool(spec and spec.is_file()),
    )
