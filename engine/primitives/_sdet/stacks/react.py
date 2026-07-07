"""React test stack detection + S3 generation metadata (#1112).

React and Angular both live behind ``package.json``; this module detects React by
the presence of ``react`` / ``react-dom`` deps and supplies the S3 ``test-from-spec``
:class:`UnitTestMeta` (React Testing Library on vitest/jest). Detection + metadata
only — no execution adapter (mirrors the #1027-chain scope, see ``dotnet.py``).

Disambiguation when a project carries BOTH React and Angular deps is the resolver's
job (:func:`primitives._sdet.stacks.nodejs.resolve_jsts_framework`, angular > react >
plain nodejs); the per-framework ``detect_*`` functions here independently report
their own framework when its marker dep is present.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class ReactStackProfile:
    """Detected React test stack profile."""

    language: Literal["typescript", "javascript"]
    framework: Literal["rtl"]
    test_runner: Literal["vitest", "jest"]
    test_glob: str
    coverage_tool: Literal["v8", "jest", "none"]


def _load_deps(project_root: Path) -> dict[str, str] | None:
    """Flat dep map from ``package.json``, or ``None`` if absent/unparseable."""
    pkg_json = project_root / "package.json"
    if not pkg_json.is_file():
        return None
    try:
        pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, PermissionError):
        return None
    if not isinstance(pkg, dict):
        return None
    from primitives._sdet.stacks.nodejs import _all_deps  # noqa: PLC0415

    return _all_deps(pkg)


def detect_react_stack(project_root: Path) -> ReactStackProfile | None:
    """Detect a React test stack; ``None`` unless ``react``/``react-dom`` is a dep."""
    deps = _load_deps(project_root)
    if deps is None or ("react" not in deps and "react-dom" not in deps):
        return None
    language: Literal["typescript", "javascript"] = (
        "typescript" if "typescript" in deps else "javascript"
    )
    runner: Literal["vitest", "jest"] = (
        "jest" if "jest" in deps and "vitest" not in deps else "vitest"
    )
    return ReactStackProfile(
        language=language,
        framework="rtl",
        test_runner=runner,
        test_glob="*.test.tsx" if language == "typescript" else "*.test.jsx",
        coverage_tool="v8" if runner == "vitest" else "jest",
    )


# Self-register on import (#1112).
from primitives._sdet.stacks import register_stack, register_unit_meta  # noqa: E402
from primitives._sdet.stacks.contract import UnitTestMeta  # noqa: E402

register_stack("react", detect_react_stack)

register_unit_meta(
    "react",
    UnitTestMeta(
        test_framework="rtl",
        template_id="react_rtl",
        stub_raise="throw new Error('not implemented')",
    ),
)
