"""FM1 (hallucinated dependencies) detector for Java.

Foundry-native (prowess U1 had Java rules in YAML but no handler). Reads
pom.xml (XML), build.gradle (Groovy DSL regex), build.gradle.kts (Kotlin
DSL regex), plus the standard Java built-in package whitelist.

Match strategy: a Java import like `org.springframework.boot.X` is OK if
any declared dependency groupId is a *prefix* of the import (so groupId
`org.springframework.boot` covers any package starting with that string).
This is loose by design — false negatives over false positives, since
Maven groupId vs Java package name don't always match perfectly.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from primitives._shared.scan_exclusions import walk_source_files
from primitives._xml_safety import XmlSafetyError, parse_pom_path
from primitives.failure_mode_audit.shared.findings import Finding

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.failure_mode_audit.shared.config import AuditConfig

_logger = logging.getLogger(__name__)

_JAVA_BUILTINS: frozenset[str] = frozenset(
    [
        "java.",
        "javax.",
        "jakarta.",
        "sun.",
        "com.sun.",
        "org.w3c.",
        "org.xml.",
        "org.ietf.",
        "org.omg.",
    ]
)

_IMPORT_LINE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;")

_GRADLE_DEP = re.compile(
    r"""(?:implementation|api|compile|testImplementation|runtimeOnly|compileOnly)\s*[\(]?\s*['"]([^'"]+)['"]"""
)


def _import_to_pkg_prefix(fqn: str) -> str:
    """Strip the trailing class name to get the package prefix."""
    parts = fqn.split(".")
    if parts and parts[-1] and parts[-1][0].isupper():
        parts = parts[:-1]
    return ".".join(parts)


def extract_imports_java(line: str) -> set[str]:
    """Extract Java import package prefixes from a single line."""
    packages: set[str] = set()
    match = _IMPORT_LINE.match(line)
    if match:
        fqn = match.group(1)
        packages.add(_import_to_pkg_prefix(fqn))
    return packages


def _parse_pom(pom_path: Path) -> set[str]:
    """Parse pom.xml, return groupIds of all <dependency> entries."""
    deps: set[str] = set()
    try:
        root = parse_pom_path(pom_path)
    except XmlSafetyError as exc:
        _logger.warning("Skipping %s: %s", pom_path, exc)
        return deps
    if root is None:
        return deps
    ns_uri = ""
    if root.tag.startswith("{"):
        ns_uri = root.tag[1:].split("}", 1)[0]
    ns = f"{{{ns_uri}}}" if ns_uri else ""
    for dep in root.iter(f"{ns}dependency"):
        gid_elem = dep.find(f"{ns}groupId")
        if gid_elem is not None and gid_elem.text:
            deps.add(gid_elem.text.strip())
    return deps


def _parse_gradle(gradle_path: Path) -> set[str]:
    """Extract group prefixes from a build.gradle / build.gradle.kts file."""
    deps: set[str] = set()
    try:
        content = gradle_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return deps
    for match in _GRADLE_DEP.finditer(content):
        coord = match.group(1)
        if ":" in coord:
            group_id = coord.split(":", 1)[0]
            deps.add(group_id)
    return deps


def read_declared_deps_java(project_root: Path) -> set[str]:
    """Read declared groupIds from pom.xml + Gradle files; return a set of prefixes."""
    deps: set[str] = set(_JAVA_BUILTINS)

    pom = project_root / "pom.xml"
    if pom.is_file():
        deps.update(_parse_pom(pom))

    for gradle_name in ("build.gradle", "build.gradle.kts"):
        gradle_file = project_root / gradle_name
        if gradle_file.is_file():
            deps.update(_parse_gradle(gradle_file))

    return deps


def _is_declared(import_pkg: str, declared: set[str]) -> bool:
    """True if any declared dep groupId is a prefix of *import_pkg*."""
    if any(import_pkg.startswith(prefix) for prefix in _JAVA_BUILTINS):
        return True
    return any(import_pkg == d or import_pkg.startswith(d + ".") for d in declared)


def audit_java_fm1(
    path: Path,
    *,
    project_root: Path,
    config: AuditConfig,
) -> list[Finding]:
    """Audit *path* for FM1, .java files only."""
    findings: list[Finding] = []
    declared = read_declared_deps_java(project_root)
    declared.update(config.project.ok_toplevels)

    java_files = list(walk_source_files(path, {".java"}, include_vendored=config.include_vendored))

    for f in java_files:
        try:
            content = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        for line_num, line in enumerate(content.splitlines(), 1):
            for pkg in extract_imports_java(line):
                if not _is_declared(pkg, declared):
                    findings.append(
                        Finding(
                            mode="fm1",
                            severity="medium",
                            file=str(f),
                            line=line_num,
                            message=(
                                f"Import `{pkg}` not covered by any declared "
                                "groupId or built-in package (possible hallucinated dep)"
                            ),
                            snippet=line.strip(),
                            rule_id="fm1.java.import-not-in-deps",
                        )
                    )
    return findings
