"""Java import-statement depgraph walker.

Resolves 'import com.x.Y;', wildcards 'import com.x.*;', and
'import static com.x.Y.method;' to file paths under Maven/Gradle
src/main/java + src/test/java layouts. Recognises test files by
name (*Test.java / *Tests.java / *IT.java) or location (src/test/java/).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_IMPORT_LINE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)(\.\*)?\s*;", re.MULTILINE)
_TEST_FILENAME = re.compile(r"(Test|Tests|IT)\.java$")
_JAVA_SRC_ROOTS = ("src/main/java", "src/test/java")

_SKIP_DIRS = frozenset({"target", "build", ".gradle", ".git", "node_modules"})


def _is_test_file(p: Path) -> bool:
    if _TEST_FILENAME.search(p.name):
        return True
    return "src/test/java" in str(p).replace("\\", "/")


def _class_to_paths(fqn: str, project_root: Path) -> list[Path]:
    """Resolve fully-qualified class name like com.x.Y to candidate file paths."""
    parts = fqn.split(".")
    rel = "/".join(parts) + ".java"
    candidates: list[Path] = []
    for src_root in _JAVA_SRC_ROOTS:
        candidates.append(project_root / src_root / rel)
    # Also try the raw layout (project_root/parts/Y.java)
    candidates.append(project_root / rel)
    return candidates


def _wildcard_paths(pkg_fqn: str, project_root: Path) -> list[Path]:
    """For 'import com.x.*;' return all .java files in com/x/ under each src root."""
    pkg_rel = "/".join(pkg_fqn.split("."))
    candidates: list[Path] = []
    for src_root in _JAVA_SRC_ROOTS:
        pkg_dir = project_root / src_root / pkg_rel
        if pkg_dir.is_dir():
            candidates.extend(pkg_dir.glob("*.java"))
    return candidates


def _extract_imports(file: Path) -> list[tuple[str, bool]]:
    """Return list of (fqn, is_wildcard)."""
    try:
        content = file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return []
    out: list[tuple[str, bool]] = []
    for match in _IMPORT_LINE.finditer(content):
        fqn = match.group(1)
        is_wildcard = bool(match.group(2))
        # For static import like 'import static com.x.Y.method' the regex captures
        # 'com.x.Y.method'. Strip the last segment to get the class FQN.
        if "static" in match.group(0):
            parts = fqn.split(".")
            if len(parts) >= 2:  # noqa: PLR2004
                fqn = ".".join(parts[:-1])
        out.append((fqn, is_wildcard))
    return out


def _collect_java_files(project_root: Path) -> list[Path]:
    """Return all .java files under project_root, skipping build artifact dirs."""
    result: list[Path] = []
    for p in project_root.rglob("*.java"):
        if not any(part in _SKIP_DIRS for part in p.parts):
            result.append(p.resolve())
    return result


def _build_forward_graph(
    all_files: list[Path],
    project_root: Path,
) -> dict[Path, set[Path]]:
    """Build a file → {imported files} adjacency map for all_files."""
    forward: dict[Path, set[Path]] = {}
    for f in all_files:
        targets: set[Path] = set()
        for fqn, is_wildcard in _extract_imports(f):
            if is_wildcard:
                candidates = _wildcard_paths(fqn, project_root)
            else:
                candidates = _class_to_paths(fqn, project_root)
            for c in candidates:
                if c.is_file():
                    targets.add(c.resolve())
        forward[f] = targets
    return forward


def _test_reaches(
    test_file: Path,
    target: Path,
    forward: dict[Path, set[Path]],
) -> bool:
    """Return True if test_file transitively imports target via the forward graph."""
    visited: set[Path] = set()
    stack = list(forward.get(test_file, set()))
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        if cur == target:
            return True
        stack.extend(forward.get(cur, set()) - visited)
    return False


def find_tests_reaching_java(
    source_file: Path,
    project_root: Path,
) -> set[Path]:
    """Walk Java source + test files under project_root. Return the set of test
    files (path under src/test/java/, or filename matching *Test.java / *Tests.java
    / *IT.java) that transitively import source_file.

    Strategy:
        - Parse ``import com.x.Y;`` statements via regex.
        - Resolve each import to a file path: look for it under src/main/java/ and
          src/test/java/ (and the project root if no src/ layout).
        - Wildcard imports (``import com.x.*;``) match any file in the corresponding
          package directory.
        - ``import static com.x.Y.method;`` is treated as importing class Y.
        - Build forward graph; BFS from each test file.
    """
    project_root = project_root.resolve()
    source_file = source_file.resolve()

    all_files = _collect_java_files(project_root)
    forward = _build_forward_graph(all_files, project_root)

    return {f for f in all_files if _is_test_file(f) and _test_reaches(f, source_file, forward)}
