from __future__ import annotations

import re
import shutil
from pathlib import Path

from scripts._vendor.copy_skills import PrimitiveSrc

_PYPROJECT_TEMPLATE = '''\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "forge-foundry-claude-engine"
version = "{version}"
description = "Bundled deterministic engine for the forge-foundry-claude plugin (vendored)."
requires-python = ">=3.11"
dependencies = [
{deps}
]

[project.scripts]
forge-validate = "forge_validate.cli:main"

[tool.setuptools]
packages = ["forge_validate", "primitives"]

[tool.setuptools.package-dir]
"" = "."
'''


def _copy_tree_no_symlinks(src: Path, dst: Path) -> None:
    for item in sorted(src.rglob("*")):
        if item.is_symlink():
            continue
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.suffix == ".pyc" or "__pycache__" in item.parts:
            continue
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _pinned_deps(src_repo: Path) -> list[str]:
    """Read runtime deps from the source pyproject [project].dependencies."""
    text = (src_repo / "pyproject.toml").read_text()
    m = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if not m:
        return ["click>=8", "pydantic>=2"]
    items = re.findall(r'["\']([^"\']+)["\']', m.group(1))
    return items


def copy_engine(src_repo: Path, prims: list[PrimitiveSrc], out_engine: Path,
                *, version: str = "0.1.0") -> None:
    src_repo = Path(src_repo)
    out_engine = Path(out_engine)
    # forge_validate
    _copy_tree_no_symlinks(src_repo / "forge_validate", out_engine / "forge_validate")
    # primitives package root + all shared underscore-prefixed modules/packages
    # (_shared, _sdet, _renderer_registry.py, _response_schema_validator.py,
    #  _tool_version.py, _xml_safety.py, …) — cross-primitive engine deps.
    (out_engine / "primitives").mkdir(parents=True, exist_ok=True)
    src_prim_init = src_repo / "primitives" / "__init__.py"
    (out_engine / "primitives" / "__init__.py").write_text(
        src_prim_init.read_text() if src_prim_init.is_file() else "")
    for shared in sorted((src_repo / "primitives").glob("_*")):
        if shared.name == "__pycache__" or shared.is_symlink():
            continue
        dest = out_engine / "primitives" / shared.name
        if shared.is_dir():
            _copy_tree_no_symlinks(shared, dest)
        else:
            shutil.copy2(shared, dest)
    # each primitive's shared/ (hybrid + PLB)
    for p in prims:
        if not p.has_shared:
            continue
        shared = src_repo / "primitives" / p.name / "shared"
        _copy_tree_no_symlinks(shared, out_engine / "primitives" / p.name / "shared")
        pkg_init = out_engine / "primitives" / p.name / "__init__.py"
        if not pkg_init.exists():
            pkg_init.parent.mkdir(parents=True, exist_ok=True)
            src_pkg_init = src_repo / "primitives" / p.name / "__init__.py"
            pkg_init.write_text(
                src_pkg_init.read_text() if src_pkg_init.is_file() else "")
    # generated pyproject
    deps = _pinned_deps(src_repo)
    deps_block = "\n".join(f'    "{d}",' for d in deps)
    (out_engine / "pyproject.toml").write_text(
        _PYPROJECT_TEMPLATE.format(version=version, deps=deps_block))
