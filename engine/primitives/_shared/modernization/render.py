"""String-rendering helpers (Layer 1) -- terse structured data -> verbose Markdown/Mermaid.

Generalized from ``mfx``'s deliverable-formatting layer (design spec §3, Layer 1).
:mod:`render` turns structured Python data into the verbose Markdown and Mermaid
deliverable text that would otherwise be produced by the model itself.  Moving
formatting off the model drastically reduces per-call token cost (token-offload
principle) and makes the output golden-file testable.

Pure and deterministic: no LLM, no clock, no filesystem.

**Mermaid node-id safety:** Mermaid node/actor ids must match ``[A-Za-z0-9_]``
(plus a few others in some contexts, but the safest portable subset is that
set).  Real ids like ``external:MISSING``, ``lib:common-util``, or
``src/app.py`` contain ``:``, ``/``, ``.``, ``-`` which break the parser.
Every diagram emitter maps each distinct input id to a synthetic safe token
(``n0``, ``n1``, ... for flowcharts/ER) in first-seen order and places the
ORIGINAL id text in a quoted node label so the diagram still communicates the
real identity.  Actor names in :func:`sequence_diagram` are sanitized instead:
non-``[A-Za-z0-9_]`` chars are replaced by ``_`` and a
``participant <sanitized> as "original"`` alias line is emitted so both the safe
token and the human-readable name appear in the diagram.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_VALID_DIRECTIONS = frozenset({"TD", "TB", "LR", "RL", "BT"})
_VALID_PREFIX_RE = re.compile(r"^[A-Z]+$")
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_]")
_MAX_HEADING_LEVEL = 6


def _escape_cell(text: str) -> str:
    """Escape ``|`` and replace newlines in a Markdown table cell."""
    return text.replace("|", r"\|").replace("\n", " ").replace("\r", " ")


def _quote_label(label: str) -> str:
    """Quote a mermaid node label, escaping interior ``"`` as ``&quot;``."""
    return '"' + label.replace('"', "&quot;") + '"'


# ---------------------------------------------------------------------------
# Markdown scaffolding
# ---------------------------------------------------------------------------


def heading(text: str, level: int = 2) -> str:
    """Return a Markdown heading string.

    Args:
        text: the heading text.
        level: heading depth 1-6.

    Returns:
        ``"## text"`` (with the appropriate number of ``#`` characters).

    Raises:
        ValueError: if *level* is not in 1-6.
    """
    if level < 1 or level > _MAX_HEADING_LEVEL:
        raise ValueError(f"heading level must be 1-6, got {level!r}")
    return "#" * level + " " + text


def md_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render a GitHub Markdown table.

    Args:
        headers: column header names.
        rows: data rows.  Each row must have ``<= len(headers)`` cells; shorter
            rows are padded with empty strings.  Rows longer than ``headers``
            raise :class:`ValueError`.

    Returns:
        The table as a string (no trailing newline).  ``|`` characters inside
        cells are escaped as ``\\|``; newlines are replaced with a space.

    Raises:
        ValueError: if any row is longer than *headers*.
    """
    ncols = len(headers)
    escaped_headers = [_escape_cell(h) for h in headers]
    separator = ["---"] * ncols

    lines: list[str] = []
    lines.append("| " + " | ".join(escaped_headers) + " |")
    lines.append("| " + " | ".join(separator) + " |")

    for i, row in enumerate(rows):
        if len(row) > ncols:
            raise ValueError(f"row {i} has {len(row)} cells but only {ncols} headers are defined")
        padded = list(row) + [""] * (ncols - len(row))
        escaped = [_escape_cell(str(c)) for c in padded]
        lines.append("| " + " | ".join(escaped) + " |")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mermaid: id-safety utilities
# ---------------------------------------------------------------------------


class _IdMapper:
    """Maps arbitrary string ids to safe synthetic tokens ``<prefix>0``, ``<prefix>1``, ..."""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._map: dict[str, str] = {}

    def safe(self, raw: str) -> str:
        """Return (or create) the safe token for *raw*."""
        if raw not in self._map:
            self._map[raw] = f"{self._prefix}{len(self._map)}"
        return self._map[raw]

    @property
    def items(self) -> list[tuple[str, str]]:
        """Return ``(raw, safe)`` pairs in first-seen order."""
        return list(self._map.items())


# ---------------------------------------------------------------------------
# Mermaid: flowchart
# ---------------------------------------------------------------------------


def _wrap(body: str, *, fenced: bool) -> str:
    """Wrap *body* in a ```` ```mermaid ```` fence for Markdown, or return it raw.

    A ``.mmd`` file must hold raw Mermaid source — the leading ```` ```mermaid ````
    line and trailing fence break ``mmdc`` and any tool treating ``.mmd`` as raw
    Mermaid (#947). Markdown embedding still needs the fence, so ``fenced=True``
    stays the default.
    """
    return f"```mermaid\n{body}\n```" if fenced else body


def flowchart(
    nodes: Sequence[tuple[str, str]],
    edges: Sequence[tuple[str, str, str]],
    *,
    direction: str = "TD",
    fenced: bool = True,
) -> str:
    """Render a Mermaid flowchart, fenced for Markdown or raw for a ``.mmd`` file.

    Args:
        nodes: ``(node_id, label)`` pairs.
        edges: ``(src_id, dst_id, edge_label)`` triples.  An empty *edge_label*
            produces an unlabeled arrow; a non-empty one produces
            ``n0 -- "label" --> n1``.  An edge may reference a node id that
            does not appear in *nodes*; it will receive a stable safe token.
        direction: one of ``TD``, ``TB``, ``LR``, ``RL``, ``BT``.
        fenced: when ``True`` (default) the body is wrapped in a ```` ```mermaid ````
            code block for embedding into Markdown; when ``False`` the raw Mermaid
            source is returned for writing straight to a ``.mmd`` file (#947).

    Returns:
        A Mermaid diagram string — fenced for Markdown, or raw when ``fenced=False``.

    Raises:
        ValueError: if *direction* is not in the allowed set.
    """
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {sorted(_VALID_DIRECTIONS)}, got {direction!r}")

    mapper = _IdMapper("n")

    # Register declared nodes first (preserves input order for safe-token assignment).
    for node_id, _label in nodes:
        mapper.safe(node_id)

    # Ensure edge endpoints also get tokens even if not in nodes.
    for src, dst, _label in edges:
        mapper.safe(src)
        mapper.safe(dst)

    lines: list[str] = [f"flowchart {direction}"]

    for node_id, label in nodes:
        safe = mapper.safe(node_id)
        lines.append(f"  {safe}[{_quote_label(label)}]")

    for src, dst, edge_label in edges:
        s = mapper.safe(src)
        d = mapper.safe(dst)
        if edge_label:
            lines.append(f'  {s} -- "{edge_label}" --> {d}')
        else:
            lines.append(f"  {s} --> {d}")

    return _wrap("\n".join(lines), fenced=fenced)


# ---------------------------------------------------------------------------
# Mermaid: sequence diagram
# ---------------------------------------------------------------------------


def _sanitize_actor(name: str) -> str:
    """Replace non-``[A-Za-z0-9_]`` chars with ``_``."""
    return _SANITIZE_RE.sub("_", name)


def sequence_diagram(
    messages: Sequence[tuple[str, str, str]],
    *,
    participants: Sequence[str] | None = None,
    fenced: bool = True,
) -> str:
    """Render a Mermaid sequenceDiagram, fenced for Markdown or raw for a ``.mmd`` file.

    Actor names are sanitized to ``[A-Za-z0-9_]`` (non-matching chars replaced
    with ``_``) and each unique actor gets a ``participant <safe> as "original"``
    alias so the diagram shows the human-readable name.

    Args:
        messages: ``(from_actor, to_actor, text)`` triples.
        participants: explicit participant order.  When ``None``, participants
            are inferred from *messages* in first-seen order.
        fenced: ``True`` (default) wraps the body in a ```` ```mermaid ```` block for
            Markdown; ``False`` returns raw Mermaid for a ``.mmd`` file (#947).

    Returns:
        A Mermaid diagram string — fenced for Markdown, or raw when ``fenced=False``.
    """
    # Determine participant order.
    actor_names: list[str]
    if participants is not None:
        actor_names = list(participants)
        # Also register any actors that appear in messages but not in participants.
        seen: set[str] = set(actor_names)
        for frm, to, _ in messages:
            for a in (frm, to):
                if a not in seen:
                    actor_names.append(a)
                    seen.add(a)
    else:
        seen_order: dict[str, None] = {}
        for frm, to, _ in messages:
            seen_order.setdefault(frm, None)
            seen_order.setdefault(to, None)
        actor_names = list(seen_order)

    # Build safe-name mapping: sanitize each actor, use index-based pN on collision.
    safe_map: dict[str, str] = {}
    used_tokens: set[str] = set()
    for idx, name in enumerate(actor_names):
        sanitized = _sanitize_actor(name)
        if sanitized not in used_tokens:
            safe_map[name] = sanitized
            used_tokens.add(sanitized)
        else:
            # Collision: fall back to index-based token.
            token = f"p{idx}"
            safe_map[name] = token
            used_tokens.add(token)

    lines: list[str] = ["sequenceDiagram"]

    for name in actor_names:
        safe = safe_map[name]
        lines.append(f'  participant {safe} as "{name}"')

    for frm, to, text in messages:
        sf = safe_map.get(frm, _sanitize_actor(frm))
        st = safe_map.get(to, _sanitize_actor(to))
        lines.append(f"  {sf}->>{st}: {text}")

    return _wrap("\n".join(lines), fenced=fenced)


# ---------------------------------------------------------------------------
# Mermaid: ER diagram
# ---------------------------------------------------------------------------


def _sanitize_entity(name: str) -> str:
    """Return a mermaid-valid entity identifier (letters, digits, underscore)."""
    return _SANITIZE_RE.sub("_", name)


def er_diagram(
    entities: Sequence[tuple[str, Sequence[tuple[str, str]]]],
    relationships: Sequence[tuple[str, str, str, str]],
    *,
    fenced: bool = True,
) -> str:
    """Render a Mermaid erDiagram, fenced for Markdown or raw for a ``.mmd`` file.

    Args:
        entities: ``(name, [(attr_type, attr_name), ...])`` pairs.
        relationships: ``(left_entity, cardinality, right_entity, label)`` tuples
            where *cardinality* is a mermaid ER token (e.g. ``||--o{``).
        fenced: ``True`` (default) wraps the body in a ```` ```mermaid ```` block for
            Markdown; ``False`` returns raw Mermaid for a ``.mmd`` file (#947).

    Returns:
        A Mermaid diagram string — fenced for Markdown, or raw when ``fenced=False``.
    """
    lines: list[str] = ["erDiagram"]

    for name, attrs in entities:
        safe = _sanitize_entity(name)
        lines.append(f"  {safe} {{")
        for attr_type, attr_name in attrs:
            lines.append(f"    {attr_type} {attr_name}")
        lines.append("  }")

    for left, cardinality, right, label in relationships:
        sl = _sanitize_entity(left)
        sr = _sanitize_entity(right)
        lines.append(f"  {sl} {cardinality} {sr} : {label}")

    return _wrap("\n".join(lines), fenced=fenced)


# ---------------------------------------------------------------------------
# Stable ID assignment
# ---------------------------------------------------------------------------


def assign_ids(
    prefix: str,
    count: int,
    *,
    width: int = 3,
    start: int = 1,
) -> list[str]:
    """Return a list of zero-padded stable ids.

    Args:
        prefix: an all-uppercase prefix, e.g. ``"R"`` -> ``["R-001", "R-002", ...]``.
        count: number of ids to generate (``>= 0``).
        width: zero-pad width for the numeric part (``>= 1``).
        start: starting integer (``>= 1``).

    Returns:
        ``["<prefix>-<NNN>", ...]`` zero-padded to *width*.

    Raises:
        ValueError: if *prefix* does not match ``^[A-Z]+$``, or any of *count*,
            *width*, *start* violate their constraints.
    """
    if not _VALID_PREFIX_RE.match(prefix):
        raise ValueError(f"prefix must match ^[A-Z]+$, got {prefix!r}")
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count!r}")
    if width < 1:
        raise ValueError(f"width must be >= 1, got {width!r}")
    if start < 1:
        raise ValueError(f"start must be >= 1, got {start!r}")
    return [f"{prefix}-{n:0{width}d}" for n in range(start, start + count)]


# ---------------------------------------------------------------------------
# Traceability matrix
# ---------------------------------------------------------------------------


class TraceRow(NamedTuple):
    """One row of a traceability matrix.

    Fields:
        requirement: a stable requirement id (e.g. ``"R-001"``).
        acceptance: zero or more acceptance-criteria ids (e.g. ``("AC-001",)``).
        tests: zero or more test ids (e.g. ``("T-001", "T-002")``).
    """

    requirement: str
    acceptance: tuple[str, ...]
    tests: tuple[str, ...]


def traceability_matrix(rows: Sequence[TraceRow]) -> str:
    """Render a traceability-matrix Markdown table.

    Columns: ``Requirement | Acceptance Criteria | Tests``.  Empty id lists
    render as ``--`` (em-dash placeholder).

    Args:
        rows: :class:`TraceRow` instances in display order.

    Returns:
        The Markdown table string produced by :func:`md_table` (no trailing
        newline).
    """
    headers = ["Requirement", "Acceptance Criteria", "Tests"]
    table_rows: list[list[str]] = []
    for row in rows:
        ac_str = ", ".join(row.acceptance) if row.acceptance else "—"
        t_str = ", ".join(row.tests) if row.tests else "—"
        table_rows.append([row.requirement, ac_str, t_str])
    return md_table(headers, table_rows)
