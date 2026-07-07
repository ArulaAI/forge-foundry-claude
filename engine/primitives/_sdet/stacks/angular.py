"""Angular test stack detection + S3 generation metadata (#1112).

Detects Angular by the ``@angular/core`` dep in ``package.json`` and supplies the
S3 ``test-from-spec`` :class:`UnitTestMeta` (TestBed, ``*.spec.ts``). Detection +
metadata only — no execution adapter (mirrors the #1027-chain scope).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from primitives._sdet.stacks.react import _load_deps

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class AngularStackProfile:
    """Detected Angular test stack profile."""

    language: Literal["typescript"]
    framework: Literal["testbed"]
    test_runner: Literal["karma", "jest"]
    test_glob: str
    coverage_tool: Literal["istanbul", "jest", "none"]


def detect_angular_stack(project_root: Path) -> AngularStackProfile | None:
    """Detect an Angular test stack; ``None`` unless ``@angular/core`` is a dep."""
    deps = _load_deps(project_root)
    if deps is None or "@angular/core" not in deps:
        return None
    runner: Literal["karma", "jest"] = "jest" if "jest" in deps else "karma"
    return AngularStackProfile(
        language="typescript",
        framework="testbed",
        test_runner=runner,
        test_glob="*.spec.ts",
        coverage_tool="jest" if runner == "jest" else "istanbul",
    )


# Self-register on import (#1112).
from primitives._sdet.stacks import register_stack, register_unit_meta  # noqa: E402
from primitives._sdet.stacks.contract import UnitTestMeta  # noqa: E402

register_stack("angular", detect_angular_stack)

register_unit_meta(
    "angular",
    UnitTestMeta(
        test_framework="testbed",
        template_id="angular_testbed",
        stub_raise="throw new Error('not implemented')",
    ),
)
