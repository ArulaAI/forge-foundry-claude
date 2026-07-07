"""Deterministic engine for the S3 ``test_from_spec`` primitive.

Importable counterpart to ``skill/scripts/spec_parse.py``. Carries all the
same pure functions verbatim in behavior, replacing the hard-coded
``_MANIFESTS`` stack detection with ``_sdet``-backed detection so the primitive
queries the full four-stack (python / JS/TS-family / java / dotnet) registry.

Temporary duplication of the pure logic vs ``spec_parse.py`` is intentional:
the standalone script stays in place until issue B4 removes it; this module is
the new source of truth for the importable package path.

Bug-fix inventory (carried from ``spec_parse.py``):

- #1091 any-heading-level + fence-aware ``_extract_section``
- #1092 ``class``/``def`` public-api extraction
- #1094 ``__future__`` skip in ``_unknown_symbol_findings``
- #1095 ``pytest.raises`` / ``unittest`` assertion idioms
- #1096 importable ``spec_dir`` slug
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._sdet.stacks.contract import UnitTestMeta

# ---------------------------------------------------------------------------
# Stage A — parse the spec (deterministic, no LLM)
# ---------------------------------------------------------------------------

#: Stop words excluded when falling back to bare-identifier extraction.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "import",
        "from",
        "as",
        "def",
        "class",
        "return",
        "pass",
        "and",
        "or",
        "not",
        "in",
        "is",
        "if",
        "else",
        "for",
        "while",
        "with",
        "try",
        "except",
        "finally",
        "raise",
        "yield",
        "lambda",
        "None",
        "True",
        "False",
    }
)


@dataclass(frozen=True)
class Criterion:
    """One acceptance criterion with a slugified id."""

    id: str
    text: str


@dataclass(frozen=True)
class ParseResult:
    """Output of Stage A + B."""

    criteria: list[Criterion]
    public_api: list[str]
    stack: str | None
    spec_dir: str = "spec"


def spec_dir_slug(spec_filename: str) -> str:
    """Importable test-package dir name derived from the spec filename (#1096).

    Strips the extension, a leading ``YYYY-MM-DD-`` date prefix, and a trailing
    ``-design`` suffix, then collapses all non-alphanumerics (incl. hyphens) to
    ``_`` and prefixes ``spec_``. The result is always a valid Python identifier,
    so the generated ``tests/<spec_dir>/`` package is importable.
    """
    stem = spec_filename.rsplit(".", 1)[0]
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem)
    stem = re.sub(r"-design$", "", stem)
    body = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
    return f"spec_{body}" if body else "spec"


def _slugify(text: str) -> str:
    """Lowercase, collapse non-alphanumerics to ``_``, truncate to 40 chars."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:40]


def _unique_slug(base: str, seen: dict[str, int]) -> str:
    """Return *base* if unseen; otherwise append an incrementing numeric suffix."""
    if base not in seen:
        seen[base] = 0
        return base
    seen[base] += 1
    candidate = f"{base[:38]}_{seen[base]}"
    return candidate


def _extract_section(text: str, name: str) -> str | None:
    """Return the body of the ``# … <name>`` section at ANY heading level (#1091).

    The canonical reference spec nests ``### Acceptance`` / ``### Public API``
    under a ``## N. The feature`` heading, so matching only H2 (the old
    behaviour) made those specs parse to nothing (parse-mode exit 3). This
    matches a heading of any level (``#`` through ``######``) and returns
    everything up to the next heading of the **same or higher** level (a
    deeper sub-heading stays inside the section), or EOF.
    """
    match = re.search(rf"^(#{{1,6}})\s+{re.escape(name)}\b", text, re.MULTILINE)
    if not match:
        return None
    level = len(match.group(1))
    section = text[match.end() :]
    # End at the next heading whose level is <= this section's level — but a
    # ``#`` COMMENT line inside a fenced code block is not a heading (#1092),
    # so track the fence state line by line rather than matching ``^#`` blindly.
    heading_re = re.compile(rf"^#{{1,{level}}}\s")
    fence_re = re.compile(r"^\s*```")
    in_fence = False
    pos = 0
    for line in section.splitlines(keepends=True):
        if fence_re.match(line):
            in_fence = not in_fence
        elif not in_fence and heading_re.match(line):
            return section[:pos]
        pos += len(line)
    return section


def _extract_criteria(text: str) -> list[Criterion]:
    """Extract ``- [ ]`` checklist items from the ``Acceptance`` section.

    Returns an empty list when no ``Acceptance`` heading exists (at any level).
    """
    section = _extract_section(text, "Acceptance")
    if section is None:
        return []

    items: list[str] = re.findall(r"^\s*- \[ \] (.+)", section, re.MULTILINE)
    seen: dict[str, int] = {}
    criteria: list[Criterion] = []
    for item in items:
        base = _slugify(item.strip())
        slug = _unique_slug(base, seen)
        criteria.append(Criterion(id=slug, text=item.strip()))
    return criteria


def _extract_public_api(text: str) -> list[str]:
    """Extract Public API symbol names from the first fenced code block in ``## Public API``.

    Collects *structured* symbols — names that unambiguously denote a public
    symbol — and returns them deduped in first-seen order:

    1. ``from X import (a, b, c)`` multi-symbol form — the parenthesised names.
    2. ``from X import Y`` single-symbol form(s) — Y.
    3. ``class Name`` / ``def name(`` definition forms (#1092).

    Only when NO structured symbol is found does it fall back to (4) bare
    identifiers minus stop-words. Returns an empty list when there is no
    ``Public API`` section or it has no recognisable symbols.
    """
    section = _extract_section(text, "Public API")
    if section is None:
        return []

    # Find the first fenced code block.
    fence_match = re.search(r"```[^\n]*\n(.*?)```", section, re.DOTALL)
    if not fence_match:
        return []
    block = fence_match.group(1)

    symbols: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        name = name.strip().rstrip(",")
        if name and name not in seen and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            seen.add(name)
            symbols.append(name)

    # 1. multi-symbol `from X import (a, b, c, ...)`.
    for multi in re.finditer(r"from\s+\S+\s+import\s*\(([^)]+)\)", block, re.DOTALL):
        for part in re.split(r"[,\n]", multi.group(1)):
            stripped = part.strip()
            if stripped and not stripped.startswith("#"):
                _add(stripped.split()[0])

    # 2. single-line `from X import Y`.
    for single in re.findall(r"from\s+\S+\s+import\s+(\w+)", block):
        _add(single)

    # 3. `class Name` / `def name(` (incl. `async def`) definition forms (#1092).
    for name in re.findall(r"^\s*class\s+([A-Za-z_]\w*)", block, re.MULTILINE):
        _add(name)
    for name in re.findall(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", block, re.MULTILINE):
        _add(name)

    if symbols:
        return symbols

    # 4. Fallback: bare identifiers minus stop-words (e.g. a `Name OtherName` block).
    words = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", block)
    return [w for w in words if w not in _STOPWORDS]


# ---------------------------------------------------------------------------
# Stage B — detect the stack via _sdet (replaces hard-coded _MANIFESTS)
# ---------------------------------------------------------------------------


def detect_stack(
    codebase_root: Path,
    stack: str | None = None,
    framework: str | None = None,
) -> str | None:
    """Return the resolved stack/framework name for *codebase_root*.

    Resolution order:

    1. *stack* override — returned as-is when provided (accepts: ``python``,
       ``js``, ``java``, ``dotnet``, ``react``, ``angular``, ``nodejs``).
    2. *framework* override — returned as-is (caller already resolved the
       JS/TS framework name).
    3. Auto-detection via ``_sdet`` in priority order:
       python → JS/TS family (→ ``"angular"`` / ``"react"`` / ``"nodejs"``)
       → java → dotnet.
    4. ``None`` — nothing matched and no override was given.

    Unlike the script's hard-coded ``_MANIFESTS`` check, detection here
    delegates to the real ``detect_<lang>_stack`` functions registered in
    the ``_sdet`` registry, so dotnet + Angular + React are first-class.
    """
    if stack is not None:
        return stack
    if framework is not None:
        return framework

    # Lazy imports so the module-level import stays side-effect-light and
    # the engine can be imported without dragging in the full _sdet tree.
    from primitives._sdet.stacks.dotnet import detect_dotnet_stack  # noqa: PLC0415
    from primitives._sdet.stacks.java import detect_java_stack  # noqa: PLC0415
    from primitives._sdet.stacks.nodejs import resolve_jsts_framework  # noqa: PLC0415
    from primitives._sdet.stacks.python import detect_python_stack  # noqa: PLC0415

    if detect_python_stack(codebase_root) is not None:
        return "python"
    js_fw = resolve_jsts_framework(codebase_root)
    if js_fw is not None:
        return js_fw
    if detect_java_stack(codebase_root) is not None:
        return "java"
    if detect_dotnet_stack(codebase_root) is not None:
        return "dotnet"
    return None


def resolve_target(
    codebase_root: Path,
    stack: str | None = None,
    framework: str | None = None,
) -> tuple[str | None, UnitTestMeta | None]:
    """Resolve the stack name and :class:`UnitTestMeta` for *codebase_root*.

    Returns ``(stack_name, meta)``. *stack_name* is ``None`` when nothing is
    detected. *meta* is ``None`` when the stack has no registered unit-test
    metadata (e.g. an unknown override name).

    The ``"js"`` override name maps to the ``"nodejs"`` registry key for the
    metadata lookup (the registry was renamed in v3; ``"js"`` is kept as a
    valid override for backward compatibility).
    """
    from primitives._sdet.stacks import unit_test_meta  # noqa: PLC0415

    name = detect_stack(codebase_root, stack=stack, framework=framework)
    if name is None:
        return None, None
    meta_key = "nodejs" if name == "js" else name
    return name, unit_test_meta(meta_key)


def parse_spec(
    spec_path: Path,
    codebase_root: Path,
    stack_override: str | None,
) -> ParseResult:
    """Run Stage A + B and return a :class:`ParseResult`."""
    text = spec_path.read_text(encoding="utf-8")
    criteria = _extract_criteria(text)
    public_api = _extract_public_api(text)
    stack = detect_stack(codebase_root, stack=stack_override)
    return ParseResult(
        criteria=criteria,
        public_api=public_api,
        stack=stack,
        spec_dir=spec_dir_slug(spec_path.name),
    )


# ---------------------------------------------------------------------------
# Stage D static sub-checks (deterministic, no LLM)
# ---------------------------------------------------------------------------

#: Regex that detects a Python ``assert True`` or ``assert False`` at the
#: statement level (possibly with trailing comment).
_ASSERT_TRIVIAL_PY = re.compile(r"^\s*assert\s+(True|False)\s*(#.*)?$", re.MULTILINE)

#: Regex that matches ANY assert statement (Python).
_ASSERT_ANY_PY = re.compile(r"^\s*assert\b", re.MULTILINE)

#: Non-``assert`` assertion idioms that still make a Python test real, not theater
#: (#1095): ``pytest.raises(...)`` / ``pytest.warns(...)`` context managers and
#: unittest ``self.assertX(...)`` methods (assertEqual, assertRaises, …).
_ASSERTION_CALL_PY = re.compile(r"\bpytest\.(?:raises|warns)\s*\(|\bself\.assert\w+\s*\(")

#: Regex for JS/TS expect(...) calls (vitest/jest).
_EXPECT_ANY_JS = re.compile(r"\bexpect\s*\(")

#: Regex for Java JUnit assertions.
_ASSERT_ANY_JAVA = re.compile(r"\bassert(True|False|Equals|NotNull|Throws|That)\s*\(")


@dataclass(frozen=True)
class Finding:
    """One static-review finding on a generated test file."""

    file: str
    kind: str  # "test_theater" | "unknown_symbol"
    detail: str


def _is_test_theater(path: Path) -> Finding | None:
    """Return a ``test_theater`` finding if the file looks like test theater.

    Checks:

    - Python: zero ``assert`` statements, or every ``assert`` is ``assert True``
      / ``assert False``. ``pytest.raises`` / ``pytest.warns`` and
      ``unittest self.assertX(...)`` are real assertions (#1095).
    - JS/TS: zero ``expect(...)`` calls.
    - Java: zero JUnit assertion calls.
    """
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".py":
        has_assertion_call = bool(_ASSERTION_CALL_PY.search(text))
        all_asserts = _ASSERT_ANY_PY.findall(text)
        if not all_asserts and not has_assertion_call:
            return Finding(
                file=str(path),
                kind="test_theater",
                detail="no assert statements or assertion calls found",
            )
        trivial = _ASSERT_TRIVIAL_PY.findall(text)
        if all_asserts and len(trivial) == len(all_asserts) and not has_assertion_call:
            return Finding(
                file=str(path),
                kind="test_theater",
                detail="all assertions are bare assert True/assert False",
            )
    elif suffix in {".ts", ".js"}:
        if not _EXPECT_ANY_JS.search(text):
            return Finding(
                file=str(path),
                kind="test_theater",
                detail="no expect(...) calls found",
            )
    elif suffix == ".java":
        if not _ASSERT_ANY_JAVA.search(text):
            return Finding(
                file=str(path),
                kind="test_theater",
                detail="no JUnit assertion calls found",
            )
    return None


def _unknown_symbol_findings(path: Path, public_api: list[str]) -> list[Finding]:
    """Return ``unknown_symbol`` findings for any name in the file not in ``public_api``.

    The check is only meaningful when ``public_api`` is non-empty; returns an
    empty list when there is nothing to check against.
    """
    if not public_api:
        return []
    text = path.read_text(encoding="utf-8")
    api_set = set(public_api)
    imported: set[str] = set()
    # Python: `from X import A, B` or `from X import (A, B)`. The module name
    # is captured so `from __future__ import …` is skipped — it is never a
    # Public-API symbol and produced a false positive on every generated file
    # (#1094).
    for m in re.finditer(r"from\s+(\S+)\s+import\s*\(?([^);\n]+)\)?", text):
        if m.group(1) == "__future__":
            continue
        for name in re.split(r"[,\s]+", m.group(2)):
            clean = name.strip().rstrip(",")
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", clean):
                imported.add(clean)
    # JS: `import { A, B } from 'X'`
    for m in re.finditer(r"import\s*\{([^}]+)\}", text):
        for name in re.split(r"[,\s]+", m.group(1)):
            clean = name.strip()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", clean):
                imported.add(clean)
    # Java: infer from all bare CamelCase identifiers.
    if path.suffix == ".java":
        imported.update(re.findall(r"\b([A-Z][A-Za-z0-9]+)\b", text))

    unknown = sorted(imported - api_set - _STOPWORDS)
    if not unknown:
        return []
    return [
        Finding(
            file=str(path),
            kind="unknown_symbol",
            detail=f"references names not in Public API: {', '.join(unknown)}",
        )
    ]


@dataclass
class CheckResult:
    """Output of Stage D static sub-checks."""

    findings: list[Finding] = field(default_factory=list)


def check_test_files(test_files: list[Path], public_api: list[str]) -> CheckResult:
    """Run Stage D static sub-checks against each generated test file."""
    result = CheckResult()
    for path in test_files:
        if not path.exists():
            result.findings.append(
                Finding(file=str(path), kind="test_theater", detail="file not found")
            )
            continue
        theater = _is_test_theater(path)
        if theater:
            result.findings.append(theater)
        result.findings.extend(_unknown_symbol_findings(path, public_api))
    return result
