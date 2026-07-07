"""Shared acceptance criteria + public API parser (primitives._shared).

Parses a C1-shaped spec Markdown file (no LLM calls) and extracts:
- Every acceptance item from the ``## Acceptance`` section — checkbox
  (``- [ ]`` / ``- [x]``), plain bullet (``-`` / ``*`` / ``+``), or numbered
  (``1.`` / ``1)``) list forms are all recognized (#954)
- Python symbol names from the ``## Public API`` code block

Used by: C5 /spec-validate (acceptance_consumer), S3 /test-from-spec.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------


def _slugify(text: str, max_len: int = 40) -> str:
    """Convert *text* to a lowercase hyphen-separated slug, truncated to *max_len*."""
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug[:max_len]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class AcceptanceCriterion(BaseModel):
    """A single acceptance criterion parsed from the spec."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str  # slugify(text)[:40]
    text: str


class AcceptanceDoc(BaseModel):
    """The parsed spec document (acceptance + public API)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec_title: str
    criteria: list[AcceptanceCriterion]
    public_api_symbols: list[str]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Matches ``## Acceptance`` section until the next ``##`` heading (or EOF)
_ACCEPTANCE_SECTION_RE = re.compile(
    r"^##\s+Acceptance\b[^\n]*\n(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Matches one acceptance list item in any common Markdown form so a section is
# never silently parsed as empty (#954):
#   - [ ] text   / - [x] text   (checkbox, unchecked or checked)
#   - text  /  * text  /  + text  (plain bullet)
#   1. text  /  1) text          (ordered list)
# The optional ``\[[ xX]?\]`` consumes a checkbox marker on a bullet item; a
# plain ``[link](url)`` is left in the captured text (the marker only matches a
# single space/x/X between brackets, not arbitrary bracket content).
_LIST_ITEM_RE = re.compile(
    r"""^[ \t]*
        (?:
            [-*+][ \t]+(?:\[[ xX]?\][ \t]+)?   # unordered bullet, optional checkbox marker
          | \d+[.)][ \t]+                       # ordered item: 1. or 1)
        )
        (\S.*)$""",
    re.MULTILINE | re.VERBOSE,
)

# Matches ``## Public API`` section until next ``##`` or EOF
_PUBLIC_API_SECTION_RE = re.compile(
    r"^##\s+Public\s+API\b[^\n]*\n(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Fenced code block: ```python ... ``` or ``` ... ```
_CODE_BLOCK_RE = re.compile(r"```[a-z]*\n(.*?)```", re.DOTALL)

# ``from <module> import (<symbols...>)`` multi-line block
_IMPORT_BLOCK_RE = re.compile(
    r"from\s+\S+\s+import\s+\(([^)]+)\)",
    re.DOTALL,
)

# ``from <module> import <name>`` single-line (no parens)
_IMPORT_LINE_RE = re.compile(r"from\s+\S+\s+import\s+(\w+)")

# Python identifiers (bare, e.g. ``ClassName`` or ``function_name``)
_BARE_IDENTIFIER_RE = re.compile(r"\b([A-Za-z_]\w*)\b")

_STOPWORDS = frozenset(
    [
        "import",
        "from",
        "as",
        "None",
        "True",
        "False",
        "if",
        "else",
        "return",
        "class",
        "def",
        "self",
        "and",
        "or",
        "not",
        "in",
        "is",
        "pass",
        "str",
        "int",
        "bool",
        "list",
        "dict",
        "tuple",
        "Any",
        "None",
    ]
)


def _extract_title(spec_markdown: str) -> str:
    """Return the top-level ``# Title`` from the spec, or empty string."""
    m = re.search(r"^#\s+(.+)$", spec_markdown, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _extract_criteria(spec_markdown: str) -> list[AcceptanceCriterion]:
    """Extract acceptance items from the ``## Acceptance`` section.

    Recognizes checkbox items (``- [ ]`` / ``- [x]``), plain bullets
    (``-`` / ``*`` / ``+``), and numbered lists (``1.`` / ``1)``) — so a valid
    acceptance section authored in any of those forms is never silently parsed
    as zero criteria (#954).
    """
    section_match = _ACCEPTANCE_SECTION_RE.search(spec_markdown)
    if section_match is None:
        raise ValueError("Spec is missing a '## Acceptance' section.")

    section_body = section_match.group(1)
    items = _LIST_ITEM_RE.findall(section_body)
    criteria: list[AcceptanceCriterion] = []
    seen_ids: dict[str, int] = {}
    for raw_text in items:
        text = raw_text.strip()
        base_id = _slugify(text)
        if base_id in seen_ids:
            seen_ids[base_id] += 1
            criterion_id = f"{base_id}_{seen_ids[base_id]}"
        else:
            seen_ids[base_id] = 0
            criterion_id = base_id
        criteria.append(AcceptanceCriterion(id=criterion_id, text=text))
    return criteria


def _extract_public_api_symbols(spec_markdown: str) -> list[str]:
    """Extract Python symbol names from the ``## Public API`` code block.

    Strategy:
    1. Find the ``## Public API`` section.
    2. Extract the first fenced code block.
    3. Try ``from ... import (...)`` multi-line import block first.
    4. Fall back to ``from ... import <name>`` per-line matching.
    5. Fall back to any bare identifier (minus stopwords).
    """
    section_match = _PUBLIC_API_SECTION_RE.search(spec_markdown)
    if section_match is None:
        return []

    section_body = section_match.group(1)
    code_match = _CODE_BLOCK_RE.search(section_body)
    if code_match is None:
        return []

    code = code_match.group(1)

    # Strategy 1: multi-line import block ``from X import (a, b, c)``
    symbols: list[str] = []
    block_match = _IMPORT_BLOCK_RE.search(code)
    if block_match:
        inner = block_match.group(1)
        for part in re.split(r"[,\n]", inner):
            name = part.strip().lstrip("#").strip()
            if name and re.match(r"^[A-Za-z_]\w*$", name):
                symbols.append(name)
        if symbols:
            return symbols

    # Strategy 2: single-line ``from X import Y``
    line_matches = _IMPORT_LINE_RE.findall(code)
    if line_matches:
        return [s for s in line_matches if s not in _STOPWORDS]

    # Strategy 3: bare identifiers
    all_names = _BARE_IDENTIFIER_RE.findall(code)
    return [n for n in all_names if n not in _STOPWORDS]


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def parse_acceptance(spec_markdown: str) -> AcceptanceDoc:
    """Parse a C1-shaped spec and return an :class:`AcceptanceDoc`.

    Args:
        spec_markdown: Full text of the spec Markdown file.

    Returns:
        :class:`AcceptanceDoc` with spec title, criteria list, and public API
        symbols.

    Raises:
        ValueError: If the spec is missing a ``## Acceptance`` section.
    """
    title = _extract_title(spec_markdown)
    criteria = _extract_criteria(spec_markdown)
    symbols = _extract_public_api_symbols(spec_markdown)

    return AcceptanceDoc(
        spec_title=title,
        criteria=criteria,
        public_api_symbols=symbols,
    )
