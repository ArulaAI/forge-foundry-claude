"""M1 Stage F — ``report_renderer`` (JSON + markdown + Mermaid DAG).

Renders a :class:`SchemaDigest` to three artifacts on disk:

* ``schema.json`` — machine-readable digest (Pydantic v2
  ``model_dump_json``). Round-trippable through
  ``SchemaDigest.model_validate_json``.
* ``schema-summary.md`` — human-readable markdown summary. Sections:
  header (dialect, counts, generated_at), table inventory grouped by
  ``lob_classification``, hotspots (sorted by inbound FK in-degree,
  top 10), orphans, dependency cycles, view inventory.
* ``schema-dag.mmd`` — Mermaid flowchart of the top-N hotspot tables +
  their inbound neighbours. ``graph TD`` (top-down) per the spec's
  Mermaid invariant (CLAUDE.md: "Mermaid for diagrams — never ASCII").

The three renderers are pure functions of the
:class:`SchemaDigest` + an output ``Path``; they create parent
directories as needed (the CLI in M1-11 wires ``--output-dir`` to
``.foundry/context/db/`` by default). Determinism is preserved by
honouring Stage B's insertion order and by always sorting derived
groupings (lob_classification, hotspot in-degree, cycle members) for
byte-stable output run-over-run.

Design spec §M1 Outputs + SPEC.md §"File contract".
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.db_schema_analyze.shared.models import SchemaDigest, Table


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_TOP_N_HOTSPOTS = 20
"""Default Mermaid-diagram cap (per SPEC.md §"Input flags").

Twenty is the SPEC.md default for ``--top-n-hotspots``; large enough to
catch the BFSI hub tables (typically 5-15 hotspots in a core-banking
schema) without rendering an unreadable graph. The JSON digest always
carries the full inventory; only the Mermaid output is capped.
"""

_UNKNOWN_LOB = "unclassified"
"""Group key used for tables whose ``lob_classification`` is ``None``.

The string appears verbatim in the markdown output so an operator can
spot un-classified tables (Stage D dropped them via the
anti-hallucination guard, or the table missed both the regex pack and
the LLM fallback). Held as a private constant so the test suite can
assert against the literal without re-parsing the rendered file.
"""

_MARKDOWN_HOTSPOT_LIMIT = 10
"""Top-N hotspots shown in the markdown summary (separate from the
Mermaid cap). The markdown reader is a human; ten is the BFSI-friendly
default that keeps the section scannable on a phone."""


# ---------------------------------------------------------------------------
# JSON renderer
# ---------------------------------------------------------------------------


def render_json(digest: SchemaDigest, out_path: Path) -> None:
    """Write the digest as pretty-printed JSON, round-trippable.

    Uses :meth:`SchemaDigest.model_dump_json` with ``indent=2`` so the
    file is human-diffable in code review and so future Pydantic-v2
    serialiser changes do not silently re-order keys. Round-trip is
    asserted by :func:`test_render_json_is_roundtrippable`.

    Args:
        digest: The :class:`SchemaDigest` to write.
        out_path: Destination file path. Parent directories are created
            as needed; an existing file is overwritten.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = digest.model_dump_json(indent=2, by_alias=True)
    out_path.write_text(payload, encoding="utf-8")


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def render_markdown(digest: SchemaDigest, out_path: Path) -> None:
    """Write a human-readable markdown summary of the digest.

    Sections (per SPEC.md §"Outputs"):

    * Header — dialect, table / view / sequence counts, generated_at.
    * Table inventory grouped by ``lob_classification``.
    * Hotspots — top :data:`_MARKDOWN_HOTSPOT_LIMIT` sorted by inbound
      FK in-degree (descending; tied tables sorted by name for
      determinism).
    * Orphans — tables with ``is_orphan=True``.
    * Dependency cycles — sourced from the edge list (SCCs of size
      >= 2 or self-loops). The renderer derives cycles from the edge
      list rather than re-requiring them as an argument so the
      contract stays minimal.
    * View inventory.

    Args:
        digest: The :class:`SchemaDigest` to summarise.
        out_path: Destination file path. Parent directories are created
            as needed.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_build_markdown(digest), encoding="utf-8")


def _build_markdown(digest: SchemaDigest) -> str:
    lines: list[str] = []
    lines += _build_markdown_header(digest)
    lines += _build_markdown_inventory(digest)
    lines += _build_markdown_hotspots(digest)
    lines += _build_markdown_orphans(digest)
    lines += _build_markdown_cycles(digest)
    lines += _build_markdown_views(digest)
    # Always end with a newline so POSIX text-file conventions are met.
    return "\n".join(lines) + "\n"


def _build_markdown_header(digest: SchemaDigest) -> list[str]:
    """Top-of-file metadata block."""
    generated = digest.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    return [
        "# Schema Digest",
        "",
        f"**Source dialect:** `{digest.source_dialect}`  ",
        f"**Schema version:** `{digest.schema_version}`  ",
        f"**Generated:** {generated}",
        "",
        "## Summary",
        "",
        "| Object | Count |",
        "|---|---|",
        f"| Tables | {len(digest.tables)} |",
        f"| Views | {len(digest.views)} |",
        f"| Sequences | {len(digest.sequences)} |",
        f"| Dependency edges | {len(digest.dependency_dag_edges)} |",
        "",
    ]


def _build_markdown_inventory(digest: SchemaDigest) -> list[str]:
    """Tables grouped by ``lob_classification`` (``_UNKNOWN_LOB`` for None)."""
    if not digest.tables:
        return ["## Table inventory", "", "_No tables._", ""]
    groups: dict[str, list[Table]] = defaultdict(list)
    for table in digest.tables:
        groups[table.lob_classification or _UNKNOWN_LOB].append(table)

    out = ["## Table inventory by line-of-business", ""]
    for lob in sorted(groups):
        members = sorted(groups[lob], key=lambda t: (t.schema_, t.name))
        out.append(f"### {lob} ({len(members)})")
        out.append("")
        out.append("| Table | Columns | PK | FKs |")
        out.append("|---|---|---|---|")
        for table in members:
            pk = ", ".join(table.primary_key) or "—"
            fks = len(table.foreign_keys)
            out.append(
                f"| `{_qn(table.schema_, table.name)}` | {len(table.columns)} | {pk} | {fks} |"
            )
        out.append("")
    return out


def _build_markdown_hotspots(digest: SchemaDigest) -> list[str]:
    """Top :data:`_MARKDOWN_HOTSPOT_LIMIT` hotspots by inbound FK in-degree."""
    in_degree = _in_degree(digest.dependency_dag_edges)
    flagged = [t for t in digest.tables if t.is_hotspot]
    if not flagged:
        return ["## Hotspots", "", "_No hotspot tables flagged._", ""]
    # Sort by in-degree descending, then by qualified name for determinism.
    flagged.sort(key=lambda t: (-in_degree.get(_qn(t.schema_, t.name), 0), t.schema_, t.name))
    out = [
        "## Hotspots",
        "",
        f"Top {min(len(flagged), _MARKDOWN_HOTSPOT_LIMIT)} tables by inbound FK in-degree.",
        "",
        "| Table | LoB | Inbound FK count |",
        "|---|---|---|",
    ]
    for table in flagged[:_MARKDOWN_HOTSPOT_LIMIT]:
        qn = _qn(table.schema_, table.name)
        lob = table.lob_classification or _UNKNOWN_LOB
        out.append(f"| `{qn}` | {lob} | {in_degree.get(qn, 0)} |")
    out.append("")
    return out


def _build_markdown_orphans(digest: SchemaDigest) -> list[str]:
    """Tables with ``is_orphan=True``, sorted by qualified name."""
    orphans = sorted(
        (t for t in digest.tables if t.is_orphan),
        key=lambda t: (t.schema_, t.name),
    )
    if not orphans:
        return ["## Orphan tables", "", "_No orphan tables flagged._", ""]
    out = [
        "## Orphan tables",
        "",
        f"{len(orphans)} table(s) with no FKs in either direction.",
        "",
        "| Table | LoB |",
        "|---|---|",
    ]
    for table in orphans:
        out.append(
            f"| `{_qn(table.schema_, table.name)}` | {table.lob_classification or _UNKNOWN_LOB} |"
        )
    out.append("")
    return out


def _build_markdown_cycles(digest: SchemaDigest) -> list[str]:
    """Dependency cycles derived from the edge list."""
    cycles = _find_cycles(digest.dependency_dag_edges)
    if not cycles:
        return ["## Dependency cycles", "", "_No dependency cycles detected._", ""]
    out = [
        "## Dependency cycles",
        "",
        f"{len(cycles)} cycle(s) detected. Legacy schemas often carry these — "
        "they do not block the digest run.",
        "",
    ]
    for idx, cycle in enumerate(cycles, start=1):
        members = " -> ".join([*cycle, cycle[0]])
        out.append(f"{idx}. `{members}`")
    out.append("")
    return out


def _build_markdown_views(digest: SchemaDigest) -> list[str]:
    """View inventory + the tables each view references."""
    if not digest.views:
        return ["## Views", "", "_No views._", ""]
    out = [
        "## Views",
        "",
        "| View | Referenced tables |",
        "|---|---|",
    ]
    for view in sorted(digest.views, key=lambda v: (v.schema_, v.name)):
        refs = ", ".join(f"`{r}`" for r in view.referenced_tables) or "—"
        out.append(f"| `{_qn(view.schema_, view.name)}` | {refs} |")
    out.append("")
    return out


# ---------------------------------------------------------------------------
# Mermaid renderer
# ---------------------------------------------------------------------------


def render_mermaid_dag(
    digest: SchemaDigest,
    out_path: Path,
    top_n: int = DEFAULT_TOP_N_HOTSPOTS,
) -> None:
    """Write a Mermaid ``graph TD`` of the top-N hotspot tables + neighbours.

    The diagram shows each hotspot and every table that FK-references
    it (inbound edges only — outbound edges are usually noise on hub
    tables). When there are fewer hotspots than ``top_n`` the diagram
    renders all of them. When there are no hotspots, the diagram still
    parses (a comment line + a single placeholder node) so downstream
    Mermaid renderers do not error on an empty graph.

    Args:
        digest: Source digest.
        out_path: Destination ``.mmd`` file. Parent directories are
            created as needed.
        top_n: Cap on hotspot tables rendered (default
            :data:`DEFAULT_TOP_N_HOTSPOTS`). Clamped to >= 1 so a
            non-positive caller-supplied value does not produce an
            empty diagram.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_build_mermaid(digest, top_n=top_n), encoding="utf-8")


def _build_mermaid(digest: SchemaDigest, *, top_n: int) -> str:
    effective_n = max(1, top_n)
    in_degree = _in_degree(digest.dependency_dag_edges)
    hotspots = [t for t in digest.tables if t.is_hotspot]
    hotspots.sort(key=lambda t: (-in_degree.get(_qn(t.schema_, t.name), 0), t.schema_, t.name))
    selected = hotspots[:effective_n]

    lines: list[str] = ["graph TD"]
    if not selected:
        # Per the Mermaid invariant (CLAUDE.md): we still emit a valid
        # ``graph TD`` so consumers do not have to special-case empty
        # outputs. A single comment + placeholder keeps it parseable.
        lines.append("    %% no hotspot tables detected")
        lines.append("    none[No hotspots]")
        return "\n".join(lines) + "\n"

    # Build a quick lookup from qualified name → safe-id used for
    # Mermaid node labels. Mermaid IDs cannot contain dots; we replace
    # the schema/name separator + any non-alphanumeric character with
    # an underscore to keep the IDs valid while preserving the human-
    # readable label.
    selected_qns: set[str] = {_qn(t.schema_, t.name) for t in selected}

    # Collect inbound neighbours for each selected hotspot. The
    # neighbour list is sorted for determinism and de-duplicated (a
    # source that references the same hotspot via multiple FKs only
    # appears once).
    neighbours_for: dict[str, list[str]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for source, target in digest.dependency_dag_edges:
        if target in selected_qns and (source, target) not in seen:
            seen.add((source, target))
            neighbours_for[target].append(source)
    for neighbour_list in neighbours_for.values():
        neighbour_list.sort()

    # Emit nodes (hotspots with explicit labels so the diagram is
    # readable even when a neighbour shadows a hotspot's bare name).
    for hotspot in selected:
        qn = _qn(hotspot.schema_, hotspot.name)
        nid = _mermaid_id(qn)
        in_count = in_degree.get(qn, 0)
        lines.append(f'    {nid}["{qn} (in={in_count})"]')

    # Emit edges (neighbour → hotspot).
    for hotspot in selected:
        target_qn = _qn(hotspot.schema_, hotspot.name)
        target_id = _mermaid_id(target_qn)
        for source_qn in neighbours_for.get(target_qn, []):
            source_id = _mermaid_id(source_qn)
            # If the source itself is a hotspot, the source-node label
            # was already emitted above; otherwise the bare ID renders
            # with the qualified name as its label by default — emit a
            # one-line node decl so the label is explicit either way.
            if source_qn not in selected_qns:
                lines.append(f'    {source_id}["{source_qn}"]')
            lines.append(f"    {source_id} --> {target_id}")

    return "\n".join(lines) + "\n"


def _mermaid_id(qn: str) -> str:
    """Return a Mermaid-safe node ID derived from a qualified name.

    Mermaid IDs accept alphanumerics + underscore; everything else
    (dot, hyphen, space) is mapped to ``_``. The mapping is
    deterministic so multiple references to the same qualified name
    resolve to the same node in the diagram.
    """
    return "".join(c if c.isalnum() else "_" for c in qn)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _in_degree(edges: list[tuple[str, str]]) -> dict[str, int]:
    """Return ``target_qn → in_degree`` from the Stage C edge list."""
    counter: Counter[str] = Counter()
    for _, target in edges:
        counter[target] += 1
    return dict(counter)


def _qn(schema: str, name: str) -> str:
    """Mirror of :func:`hotspot_detector._qn` / :func:`dependency_graph._qn`."""
    if schema:
        return f"{schema}.{name}"
    return name


def _find_cycles(edges: list[tuple[str, str]]) -> list[list[str]]:
    """Tarjan SCC on the edge list — returns SCCs of size >= 2 or self-loops.

    The renderer needs cycles for the markdown summary but does not
    take them as an argument (the caller passes a digest, not a tuple).
    Re-deriving them here keeps the public signature minimal at the
    cost of a second SCC pass; the cost is negligible against the JSON
    serialisation already happening.

    The implementation mirrors
    :func:`primitives.db_schema_analyze.shared.dependency_graph._find_cycles`
    so behaviour matches Stage C's cycle list byte-for-byte.
    """
    if not edges:
        return []
    graph: dict[str, set[str]] = {}
    nodes: set[str] = set()
    for src, tgt in edges:
        graph.setdefault(src, set()).add(tgt)
        nodes.add(src)
        nodes.add(tgt)
    for node in nodes:
        graph.setdefault(node, set())

    sccs = _tarjan_scc(graph)
    cycles: list[list[str]] = []
    for scc in sccs:
        if len(scc) > 1 or any(node in graph.get(node, set()) for node in scc):
            cycles.append(sorted(scc))
    cycles.sort(key=lambda c: (c[0], c))
    return cycles


def _tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]:
    """Iterative Tarjan SCC — mirror of ``dependency_graph._tarjan_scc``."""
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    result: list[list[str]] = []

    def strongconnect(start: str) -> None:
        work: list[tuple[str, list[str]]] = [(start, sorted(graph.get(start, set())))]
        indices[start] = index_counter[0]
        lowlinks[start] = index_counter[0]
        index_counter[0] += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, successors = work[-1]
            advanced = False
            while successors:
                succ = successors.pop(0)
                if succ not in indices:
                    indices[succ] = index_counter[0]
                    lowlinks[succ] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(succ)
                    on_stack.add(succ)
                    work.append((succ, sorted(graph.get(succ, set()))))
                    advanced = True
                    break
                if succ in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[succ])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlinks[parent] = min(lowlinks[parent], lowlinks[node])
            if lowlinks[node] == indices[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                result.append(sorted(component))

    for node in sorted(graph):
        if node not in indices:
            strongconnect(node)
    return result


__all__ = [
    "DEFAULT_TOP_N_HOTSPOTS",
    "render_json",
    "render_markdown",
    "render_mermaid_dag",
]
