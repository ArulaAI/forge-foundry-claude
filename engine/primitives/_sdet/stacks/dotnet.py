""".NET / C# test stack detection + S3 generation metadata (#1111).

Scope (the #1027 S3 chain): this module supplies **detection** (``detect_dotnet_stack``
→ :class:`DotnetStackProfile`) and the S3 ``test-from-spec`` :class:`UnitTestMeta`
(xUnit). It deliberately does **not** ship a full
:class:`~primitives._sdet.stacks.contract.TestStackAdapter` execution adapter
(``dotnet test`` + coverage parsing) — that is S2 *execution* territory and is not
needed to generate unit tests. So ``dotnet`` joins the **detection** registry
(``register_stack`` / ``all_stacks``) and the **unit-meta** registry, but not
``_ADAPTER_ORDER`` (the execution-adapter resolution used by S2's coverage/run loop).

Detection sniffs ``*.csproj`` (or ``*.sln``) and the test framework from
``PackageReference`` entries (xUnit / NUnit / MSTest), defaulting to xUnit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

if TYPE_CHECKING:
    from pathlib import Path

DotnetFramework: TypeAlias = Literal["xunit", "nunit", "mstest", "none"]

#: Directories never descended when sniffing for a .csproj (build output / deps).
_SKIP_DIRS = frozenset({"bin", "obj", ".git", ".vs", "node_modules", "packages"})

#: Max directory depth scanned for a project file (bounded, like the other adapters).
_MAX_DEPTH = 4


@dataclass(frozen=True)
class DotnetStackProfile:
    """Detected .NET test stack profile."""

    language: Literal["csharp"]
    framework: DotnetFramework
    framework_version: str | None
    test_glob: str
    coverage_tool: Literal["coverlet", "none"]


def _iter_csproj(project_root: Path) -> list[Path]:
    """Return ``*.csproj`` files under *project_root* (bounded depth, skip build dirs)."""
    found: list[Path] = []
    root_depth = len(project_root.parts)
    for path in sorted(project_root.rglob("*.csproj")):
        if path.is_symlink() or not path.is_file():
            continue
        rel_parts = path.parts[root_depth:]
        if len(rel_parts) > _MAX_DEPTH:
            continue
        if any(part in _SKIP_DIRS for part in rel_parts[:-1]):
            continue
        found.append(path)
    return found


def _framework_from_csproj(text: str) -> tuple[DotnetFramework, str | None]:
    """Resolve the test framework + version from a .csproj's PackageReferences."""
    # <PackageReference Include="xunit" Version="2.6.1" />
    for include, framework in (
        ("xunit", "xunit"),
        ("nunit", "nunit"),
        ("mstest.testframework", "mstest"),
        ("microsoft.net.test.sdk", "mstest"),  # weak signal; only if nothing stronger
    ):
        match = re.search(
            rf'<PackageReference\s+Include="(?P<pkg>{re.escape(include)})"'
            r'(?:\s+Version="(?P<ver>[^"]+)")?',
            text,
            re.IGNORECASE,
        )
        if match:
            return framework, match.group("ver")  # type: ignore[return-value]
    return "none", None


def detect_dotnet_stack(project_root: Path) -> DotnetStackProfile | None:
    """Detect a .NET/C# test stack in *project_root*; ``None`` if not a .NET project."""
    csprojs = _iter_csproj(project_root)
    if not csprojs:
        return None

    framework: DotnetFramework = "none"
    version: str | None = None
    coverage_tool: Literal["coverlet", "none"] = "none"
    for csproj in csprojs:
        try:
            text = csproj.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError, FileNotFoundError):
            continue
        fw, ver = _framework_from_csproj(text)
        if fw != "none" and (framework == "none" or fw in {"xunit", "nunit", "mstest"}):
            framework, version = fw, ver
        if re.search(r'Include="coverlet\.', text, re.IGNORECASE):
            coverage_tool = "coverlet"
        if framework in {"xunit", "nunit", "mstest"} and framework != "mstest":
            break  # a strong framework signal wins; stop early

    # A .csproj with no test-framework reference still resolves as dotnet with the
    # default (xUnit) so a project being scaffolded gets sensible generation.
    if framework == "none":
        framework = "xunit"

    return DotnetStackProfile(
        language="csharp",
        framework=framework,
        framework_version=version,
        test_glob="*Tests.cs",
        coverage_tool=coverage_tool,
    )


# Self-register on import so all_stacks() + unit_test_meta() pick us up (#1111).
from primitives._sdet.stacks import register_stack, register_unit_meta  # noqa: E402
from primitives._sdet.stacks.contract import UnitTestMeta  # noqa: E402

register_stack("dotnet", detect_dotnet_stack)

register_unit_meta(
    "dotnet",
    UnitTestMeta(
        test_framework="xunit",
        template_id="xunit",
        stub_raise='throw new System.NotImplementedException("not implemented");',
    ),
)
