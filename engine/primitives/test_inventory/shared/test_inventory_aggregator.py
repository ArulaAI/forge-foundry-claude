"""M-test-inventory post-walk aggregator — ``test-inventory.json``.

Invoked by the skill body after resolving the repo root::

    python -m primitives.test_inventory.shared.test_inventory_aggregator \\
        --repo-root <path> \\
        --context-dir <output-dir>

Walks the source repository for **legacy test files** across the common
Java / Python / JS-TS test frameworks (JUnit / TestNG, pytest / unittest,
Jest / Vitest / Mocha) using the shared discovery walk
(:func:`primitives._shared.scan_exclusions.walk_source_files`), so the #1359
build-output exclusions are honored — compiled or vendored tests under
``target/`` / ``build/`` / ``node_modules/`` are never ingested.

For every test it can identify, it records:

* ``name``            — the test method / function / ``it(...)`` case name.
* ``unit``            — the target class / module / function the test exercises
                        (best-effort from the file name and the target under test).
* ``endpoint``        — an HTTP route the test exercises, when one is detectable
                        from a request literal in the test body (else ``null``).
* ``assertion_intent``— a short phrase describing what the test checks, derived
                        from the first assertion in the case body.
* ``file`` / ``line`` — the ``path:line`` of the test declaration (a citation).

Writes ``<context-dir>/test-inventory.json`` in the contracted shape (§"Output
shape" of ``SPEC.md``).

Skill-first split
-----------------
This deterministic aggregator does the **walk + parse + roll-up**.  Deeper
intent extraction (nuanced ``assertion_intent`` phrasing, endpoint→unit binding
the regexes cannot infer) is the skill body's in-session job — it may enrich the
emitted inventory before it is consumed by the scope slice.  The aggregator never
invents a test: every entry is grounded in a real ``file:line`` declaration.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Literal

from primitives._shared.scan_exclusions import walk_source_files

_SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Framework / file classification
# ---------------------------------------------------------------------------

#: Suffixes we walk for test files (superset across the supported stacks).
_TEST_EXTENSIONS: set[str] = {".java", ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

#: A file is a *test file* when its name matches one of these framework
#: conventions.  Applied to the file name only (path-independent).
_TEST_FILE_PATTERNS: list[re.Pattern[str]] = [
    # Java: FooTest.java / TestFoo.java / FooTests.java / FooIT.java
    re.compile(r".*(?:Test|Tests|IT|TestCase)\.java$"),
    re.compile(r"Test[A-Z].*\.java$"),
    # Python: test_foo.py / foo_test.py
    re.compile(r"(?:test_.*|.*_test)\.py$"),
    re.compile(r"conftest\.py$"),
    # JS/TS: foo.test.ts / foo.spec.js / __tests__/foo.js
    re.compile(r".*\.(?:test|spec)\.(?:js|jsx|ts|tsx|mjs|cjs)$"),
]


def _is_test_file(path: Path) -> bool:
    name = path.name
    if any(pat.match(name) for pat in _TEST_FILE_PATTERNS):
        return True
    # __tests__/ directory convention (JS): any source file under it.
    return "__tests__" in path.parts and path.suffix in _TEST_EXTENSIONS


def _framework_for(path: Path, text: str) -> str:
    """Best-effort framework label from file suffix + import/annotation signals."""
    suffix = path.suffix
    if suffix == ".java":
        if "org.testng" in text:
            return "testng"
        return "junit"
    if suffix == ".py":
        if re.search(r"\bimport\s+unittest\b|\bunittest\.TestCase\b", text):
            return "unittest"
        return "pytest"
    # JS / TS
    if re.search(r"\bfrom\s+['\"]vitest['\"]|\bvitest\b", text):
        return "vitest"
    if re.search(r"\bmocha\b|\brequire\(['\"]mocha['\"]\)", text):
        return "mocha"
    return "jest"


# ---------------------------------------------------------------------------
# Per-framework test-case extraction
# ---------------------------------------------------------------------------

# Java JUnit/TestNG: an @Test annotation followed by a method declaration.
# ``^`` (MULTILINE) anchors at the line start so ``m.start()`` yields the correct
# line number for the method declaration (not the preceding newline).
_JAVA_TEST_METHOD_RE = re.compile(
    r"^[ \t]*(?:public\s+|private\s+|protected\s+)?void\s+([A-Za-z_]\w*)\s*\(",
    re.MULTILINE,
)
_JAVA_ANNOTATION_RE = re.compile(r"@Test\b")
_JAVA_CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_]\w*)")

# Python pytest / unittest: def test_* function/method. ``[ \t]*`` (not ``\s*``)
# so the anchor stays on the ``def`` line — ``\s`` would span blank lines and
# report the wrong line number.
_PY_TEST_FN_RE = re.compile(r"^[ \t]*def\s+(test\w*)\s*\(", re.MULTILINE)

# JS/TS: it("...") / test("...") / it('...').
_JS_TEST_RE = re.compile(r"""\b(?:it|test)\s*\(\s*(['"`])(.+?)\1""")

# Assertion signals (used to derive assertion_intent).
_ASSERT_SIGNALS: list[re.Pattern[str]] = [
    # Java: assertEquals(...), assertThat(...).isEqualTo(...), assertTrue(...)
    re.compile(r"\bassert(\w+)\s*\("),
    re.compile(r"\bassertThat\s*\(\s*([^),]+)"),
    # Python: self.assertEqual(...), assert <expr>
    re.compile(r"\bself\.assert(\w+)\s*\("),
    re.compile(r"^\s*assert\s+(.+?)(?:,|$)", re.MULTILINE),
    # JS: expect(...).toBe(...), expect(...).toEqual(...)
    re.compile(r"\bexpect\s*\(([^)]*)\)\s*\.\s*(\w+)"),
]

# HTTP route signals in a test body (mockMvc, requests, supertest, fetch).
_ENDPOINT_RE = re.compile(
    r"""(?:get|post|put|delete|patch)\s*\(\s*['"]([/][A-Za-z0-9_/{}.:-]*)['"]""",
    re.IGNORECASE,
)


def _line_of(text: str, index: int) -> int:
    """1-indexed line number of *index* in *text*."""
    return text.count("\n", 0, index) + 1


def _unit_under_test(path: Path, framework: str) -> str:
    """Best-effort target unit name from the test file name.

    ``OwnerControllerTest.java`` → ``OwnerController``; ``test_owner_service.py``
    → ``owner_service``; ``owner.service.spec.ts`` → ``owner.service``.
    """
    stem = path.name
    if framework in {"junit", "testng"}:
        base = stem.removesuffix(".java")
        base = re.sub(r"(?:Test|Tests|IT|TestCase)$", "", base)
        base = re.sub(r"^Test", "", base)
        return base or stem
    if framework in {"pytest", "unittest"}:
        base = stem.removesuffix(".py")
        base = re.sub(r"^test_", "", base)
        base = re.sub(r"_test$", "", base)
        return base or stem
    # JS/TS
    base = re.sub(r"\.(?:test|spec)\.(?:js|jsx|ts|tsx|mjs|cjs)$", "", stem)
    return base or stem


def _assertion_intent(body: str) -> str:
    """Derive a short assertion-intent phrase from a case body.

    Returns the first meaningful assertion phrase, capped in length.  Empty
    string when no assertion is detected (the skill body may enrich it).
    """
    for pat in _ASSERT_SIGNALS:
        m = pat.search(body)
        if m:
            # Prefer a captured expression; fall back to the matched assert name.
            groups = [g for g in m.groups() if g]
            phrase = " ".join(g.strip() for g in groups) if groups else m.group(0).strip()
            phrase = re.sub(r"\s+", " ", phrase).strip()
            if phrase:
                return f"asserts {phrase}"[:160]
    return ""


def _endpoint_in(body: str) -> str | None:
    m = _ENDPOINT_RE.search(body)
    if m:
        return m.group(1)
    return None


def _case_body(text: str, start: int, end: int) -> str:
    return text[start:end]


def _parse_java(text: str, path: Path, rel: str) -> list[dict[str, Any]]:
    tests: list[dict[str, Any]] = []
    unit_hint = _unit_under_test(path, "junit")
    class_m = _JAVA_CLASS_RE.search(text)
    unit = class_m.group(1) if class_m else unit_hint
    unit = re.sub(r"(?:Test|Tests|IT|TestCase)$", "", unit) or unit_hint
    # Find every @Test annotation, then the next method declaration after it.
    method_positions = [(m.start(), m.group(1)) for m in _JAVA_TEST_METHOD_RE.finditer(text)]
    for ann in _JAVA_ANNOTATION_RE.finditer(text):
        after = ann.end()
        following = [(pos, name) for pos, name in method_positions if pos >= after]
        if not following:
            continue
        pos, name = following[0]
        # Body spans from the method start to the next @Test or EOF (bounded slice).
        next_ann = _JAVA_ANNOTATION_RE.search(text, pos)
        end = next_ann.start() if next_ann else len(text)
        body = _case_body(text, pos, end)
        tests.append(
            {
                "name": name,
                "unit": unit,
                "endpoint": _endpoint_in(body),
                "assertion_intent": _assertion_intent(body),
                "file": rel,
                "line": _line_of(text, pos),
            }
        )
    return tests


def _parse_python(text: str, path: Path, rel: str) -> list[dict[str, Any]]:
    tests: list[dict[str, Any]] = []
    unit = _unit_under_test(path, "pytest")
    matches = list(_PY_TEST_FN_RE.finditer(text))
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = _case_body(text, start, end)
        tests.append(
            {
                "name": name,
                "unit": unit,
                "endpoint": _endpoint_in(body),
                "assertion_intent": _assertion_intent(body),
                "file": rel,
                "line": _line_of(text, start),
            }
        )
    return tests


def _parse_js(text: str, path: Path, rel: str) -> list[dict[str, Any]]:
    tests: list[dict[str, Any]] = []
    unit = _unit_under_test(path, "jest")
    matches = list(_JS_TEST_RE.finditer(text))
    for i, m in enumerate(matches):
        name = m.group(2)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = _case_body(text, start, end)
        tests.append(
            {
                "name": name,
                "unit": unit,
                "endpoint": _endpoint_in(body),
                "assertion_intent": _assertion_intent(body),
                "file": rel,
                "line": _line_of(text, start),
            }
        )
    return tests


def _parse_test_file(path: Path, repo_root: Path) -> list[dict[str, Any]]:
    """Parse one test file into a list of test-entry dicts."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        rel = str(path.relative_to(repo_root))
    except ValueError:
        rel = str(path)
    framework = _framework_for(path, text)
    if framework in {"junit", "testng"}:
        entries = _parse_java(text, path, rel)
    elif framework in {"pytest", "unittest"}:
        entries = _parse_python(text, path, rel)
    else:
        entries = _parse_js(text, path, rel)
    for entry in entries:
        entry["framework"] = framework
    return entries


# ---------------------------------------------------------------------------
# Top-level assembly
# ---------------------------------------------------------------------------


def build_inventory(repo_root: Path) -> dict[str, Any]:
    """Walk *repo_root* for test files and assemble the ``test-inventory.json`` dict.

    Reuses :func:`primitives._shared.scan_exclusions.walk_source_files` so the
    #1359 build-output exclusions (``target/``, ``build/``, ``node_modules/``, …)
    are honored — compiled / vendored tests are never ingested.

    Returns the contracted dict (deterministic ordering: by ``file`` then ``line``).
    """
    tests: list[dict[str, Any]] = []
    files_scanned = 0
    for path in walk_source_files(repo_root, _TEST_EXTENSIONS):
        if not _is_test_file(path):
            continue
        files_scanned += 1
        tests.extend(_parse_test_file(path, repo_root))

    tests.sort(key=lambda t: (t.get("file", ""), t.get("line", 0), t.get("name", "")))

    frameworks = sorted({t["framework"] for t in tests if t.get("framework")})
    return {
        "schema_version": _SCHEMA_VERSION,
        "repo": repo_root.name,
        "test_file_count": files_scanned,
        "test_count": len(tests),
        "frameworks": frameworks,
        "tests": tests,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="test_inventory_aggregator",
        description=(
            "M-test-inventory post-walk aggregator: walks --repo-root for legacy "
            "test files and writes test-inventory.json to --context-dir."
        ),
    )
    parser.add_argument(
        "--repo-root",
        required=True,
        type=Path,
        metavar="DIR",
        help="Absolute path to the source repository to inventory.",
    )
    parser.add_argument(
        "--context-dir",
        required=True,
        type=Path,
        metavar="DIR",
        help="Output directory (receives test-inventory.json).",
    )
    return parser


def main(argv: list[str] | None = None) -> Literal[0, 2]:
    """CLI entry point.  Returns exit code (0 success, 2 input-not-found)."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    context_dir: Path = args.context_dir.resolve()

    if not repo_root.is_dir():
        print(f"ERROR: --repo-root {repo_root} is not a directory.", file=sys.stderr)
        return 2

    context_dir.mkdir(parents=True, exist_ok=True)

    inventory = build_inventory(repo_root)

    out_path = context_dir / "test-inventory.json"
    out_path.write_text(
        json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"WROTE {out_path} ({inventory['test_count']} tests across "
        f"{inventory['test_file_count']} files; frameworks: "
        f"{', '.join(inventory['frameworks']) or 'none'})",
        file=sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "build_inventory",
    "main",
]
