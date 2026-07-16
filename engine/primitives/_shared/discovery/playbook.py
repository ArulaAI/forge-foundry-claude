"""Discovery engine — Phase C deterministic 16-section PLAYBOOK.md renderer.

The polyglot, platform-agnostic successor to the mainframe-only Phase C
renderer family. This module turns a Phase B :class:`AnalysisDigest` (the
six abstract entity lists) into the verbose 16-section ``PLAYBOOK.md`` text
that would otherwise cost ~10-20k in-session tokens to format - moving that
formatting off the model and into Python (token-offload principle; see the
:mod:`primitives._shared.modernization.render` Layer-1 helpers it builds on).

**Pure-deterministic**: no LLM, no clock, no randomness, no filesystem. Every
list is sorted by ``name`` / id before rendering so the output is byte-stable
run-over-run and golden-file testable.

**No fabrication**: the renderer only ever emits names that are present on the
supplied :class:`AnalysisDigest`. Every entity name, kind, path, and id in the
output is derived from ``analysis`` (or deterministically generated from its
contents via :func:`~primitives._shared.modernization.render.assign_ids`) — the
renderer never invents a program / table / route / screen name.

**Mermaid only**: every diagram is emitted via the Layer-1 Mermaid helpers
(:func:`~primitives._shared.modernization.render.flowchart`,
:func:`~primitives._shared.modernization.render.er_diagram`). The renderer never
draws ASCII-art boxes.

The :func:`render_playbook` ``narratives`` parameter is the token-offload seam:
Python renders the deterministic *structure* of every section and the model (a
later phase) optionally supplies a compact prose paragraph keyed by section
number. When a narrative is absent the section renders its deterministic content
only — the structure stands on its own.

The :func:`render_playbook` ``synthesis`` parameter is the harness synthesis
seam (issue #861): for the five narrative-driven sections (§1, §2, §8, §14,
§15) the harness supplies judged prose and citations via a
:class:`~primitives._shared.discovery.synthesis.SynthesisJudgments` object.
When ``synthesis`` is provided and a section's key is present, its ``body``
replaces the ``⚠️ UNVERIFIED`` stub and its ``citations`` are merged into the
evidence block. Missing keys fall back to deterministic stubs. ``synthesis=None``
is byte-identical to the current output (regression-locked by test).

**§2 prose-vs-mechanical decision**: ``_section_2`` produces a Mermaid flowchart
as its *deterministic structural content* (topology, nodes, call edges). This
is already rich mechanical content — it is NOT an ``⚠️ UNVERIFIED`` stub. The
synthesis ``body`` for §2 therefore acts as **narrative prose appended after the
diagram**, not as a replacement for any existing stub. This preserves the
diagram for every run while allowing the harness to add an architectural
narrative paragraph when synthesis is provided.

Design spec §M5 Phase C
(``docs/superpowers/specs/2026-06-03-polyglot-discovery-design.md``
§"16-section PLAYBOOK.md structure")
+ issue [#639](https://github.com/arula-ai/forge-foundry-primitives/issues/639).
Synthesis round design spec:
``docs/superpowers/specs/2026-06-19-m5-playbook-harness-synthesis-design.md``
§3 (Phase C) and §5.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from primitives._shared.modernization.render import (
    TraceRow,
    assign_ids,
    er_diagram,
    flowchart,
    heading,
    md_table,
    traceability_matrix,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from primitives._shared.discovery.models import (
        AnalysisDigest,
        BatchUnit,
        DataStore,
        EntryPoint,
        ExecutionUnit,
        SharedComponent,
        UIElement,
    )
    from primitives._shared.discovery.synthesis import SynthesisJudgments
    from primitives._shared.modernization.models import FunctionalInventory

    #: ``number -> renderer`` dispatch signature; each renderer returns
    #: ``(deterministic_content, evidence_paths)``.
    _SectionRenderer = Callable[[AnalysisDigest], tuple[str, list[str]]]

# ---------------------------------------------------------------------------
# Section metadata — the 16-section spine (canonical title + audience tag)
# ---------------------------------------------------------------------------

#: ``number -> (title, audience-tag)`` for every section. Drives the heading +
#: audience-tag line emitted by every section and the §16 recap / completeness
#: gate. Order is the canonical 1..16 order the playbook renders in.
_SECTIONS: dict[int, tuple[str, str]] = {
    1: ("Executive Summary", "architects, modernization leads, onboarding engineers"),
    2: ("System Architecture", "architects, onboarding engineers"),
    3: ("Execution Unit Inventory", "architects, onboarding engineers"),
    4: ("The Three-Layer Architecture", "architects"),
    5: ("Call Chain Maps", "architects, onboarding engineers"),
    6: ("Data Architecture", "architects, data engineers"),
    7: ("Screen Navigation & UI Layer", "frontend engineers, UX"),
    8: ("Business Rules Catalog", "analysts, modernization leads"),
    9: ("Batch Processing", "operations, architects"),
    10: ("Security Architecture", "security engineers, architects"),
    11: ("Deployment Architecture", "platform engineers, operations"),
    12: ("Automation Layer", "platform engineers, operations"),
    13: ("Operational Patterns", "operations, SRE"),
    14: ("Technical Debt Inventory", "modernization leads, engineering managers"),
    15: ("Modernization Analysis", "modernization leads, architects"),
    16: ("Appendices", "all audiences"),
}

#: Rendered for any section whose deterministic content is narrative-driven and
#: not statically extractable from the abstract entity surface.
_UNVERIFIED = "⚠️ UNVERIFIED"

#: Number of highest-LoC execution units §15 surfaces as extraction candidates.
_TOP_N_MODERNIZATION = 5

#: Section numbers whose content is narrative-driven and eligible for harness
#: synthesis (#861). §2 receives synthesis as *appended prose* (its deterministic
#: content is the Mermaid topology diagram, not an ⚠️ UNVERIFIED stub); §1/§8/
#: §14/§15 receive synthesis as a *replacement* for the ⚠️ UNVERIFIED stub.
_SYNTHESIS_PROSE_SECTIONS: frozenset[int] = frozenset({1, 2, 8, 14, 15})

#: Synthesis-eligible sections whose deterministic content is a diagram (not a
#: stub), so synthesis body is *appended* rather than used as a replacement.
_SYNTHESIS_DIAGRAM_SECTIONS: frozenset[int] = frozenset({2})


class _Named(Protocol):
    """Structural view of the one field every abstract entity shares."""

    name: str


# ---------------------------------------------------------------------------
# Small deterministic helpers
# ---------------------------------------------------------------------------


def _by_name[T: _Named](entities: Sequence[T]) -> list[T]:
    """Return ``entities`` sorted by their ``name`` attribute (stable)."""
    return sorted(entities, key=lambda e: e.name)


def _evidence_block(paths: Sequence[str]) -> str:
    """Render a collapsible ``<details>`` evidence block.

    Lists the de-duplicated, sorted ``source_path`` citations the section's
    entities came from. An empty ``paths`` yields a block noting no static
    evidence (so every section carries an evidence affordance).
    """
    unique = sorted({p for p in paths if p})
    body = "\n".join(f"- `{p}`" for p in unique) if unique else "- _no static source evidence_"
    return "<details>\n<summary>Evidence</summary>\n\n" + body + "\n\n</details>"


def _section(
    number: int,
    *,
    content: str,
    evidence: Sequence[str],
    narratives: Mapping[int, str] | None,
    synthesis: SynthesisJudgments | None = None,
) -> str:
    """Assemble one full section: heading, audience tag, content, narrative, evidence.

    Args:
        number: the 1..16 section number (keys :data:`_SECTIONS`).
        content: the deterministic body of the section.
        evidence: ``source_path`` citations for the collapsible evidence block.
        narratives: optional model-supplied prose keyed by section number.
        synthesis: optional harness synthesis judgments (issue #861). For each
            of the five synthesis-driven sections (§1, §2, §8, §14, §15), if
            this section's number is present in ``synthesis.sections``, the
            judged ``body`` is rendered and the section's ``citations`` are
            merged into the evidence block. Missing section keys fall back to
            the deterministic stub (no crash). Sections outside the five
            synthesis-driven numbers are unaffected regardless of the value.

    Returns:
        The complete section block as a string (no trailing newline).
    """
    title, audience = _SECTIONS[number]

    # Resolve synthesis body and citation paths for this section (if applicable).
    # Only the five synthesis-driven sections are eligible; all others are
    # byte-identical regardless of the `synthesis` argument value.
    # §2 decision: its deterministic `content` is the Mermaid topology diagram
    # (not an ⚠️ UNVERIFIED stub), so synthesis supplies *additional narrative*
    # appended after the diagram.  §1/§8/§14/§15 deterministic content embeds the
    # ⚠️ UNVERIFIED stub, so synthesis *replaces* the entire `content` string with
    # the judged body.  See the module docstring for the full rationale.
    synthesis_body: str | None = None
    synthesis_citation_paths: list[str] = []
    if synthesis is not None and number in _SYNTHESIS_PROSE_SECTIONS:
        sec = synthesis.sections.get(number)
        if sec is not None:
            synthesis_body = sec.body.strip()
            synthesis_citation_paths = list(sec.citations)

    # Build the section parts list.
    parts: list[str] = [
        heading(f"{number}. {title}", level=2),
        f"*Audience: {audience}*",
    ]
    if number in _SYNTHESIS_DIAGRAM_SECTIONS or synthesis_body is None:
        # §2 (diagram section): always include the deterministic diagram.
        # All other sections with no synthesis: include deterministic content.
        parts.append(content)
    else:
        # §1/§8/§14/§15 with synthesis present: judged body replaces the stub.
        parts.append(synthesis_body)

    if narratives is not None and number in narratives:
        parts.append(narratives[number].strip())

    # Diagram sections with synthesis: append narrative prose after the diagram.
    if number in _SYNTHESIS_DIAGRAM_SECTIONS and synthesis_body is not None:
        parts.append(synthesis_body)

    # Merge synthesis citations into the evidence block alongside any static paths.
    merged_evidence = list(evidence) + synthesis_citation_paths
    parts.append(_evidence_block(merged_evidence))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Per-section renderers
# ---------------------------------------------------------------------------


#: Display label per AnalysisDigest entity-collection attribute (#747).
_ENTITY_COUNT_LABELS: dict[str, str] = {
    "execution_units": "Execution units",
    "entry_points": "Entry points",
    "data_stores": "Data stores",
    "ui_elements": "UI elements",
    "batch_units": "Batch units",
    "shared_components": "Shared components",
}


def _counts_table(analysis: AnalysisDigest, order: tuple[str, ...]) -> str:
    """Render the entity-count table in *order* (§1 and §16 differ only by order)."""
    rows = [[_ENTITY_COUNT_LABELS[k], str(len(getattr(analysis, k)))] for k in order]
    return md_table(["Entity", "Count"], rows)


def _section_1(
    analysis: AnalysisDigest,
    unit_paths: list[str] | None = None,
) -> tuple[str, list[str]]:
    """§1 Executive Summary — counts table + code root."""
    table = _counts_table(
        analysis,
        (
            "execution_units",
            "entry_points",
            "data_stores",
            "ui_elements",
            "batch_units",
            "shared_components",
        ),
    )
    content = f"Code root: `{analysis.code_root}`\n\n{table}"
    return content, unit_paths if unit_paths is not None else _unit_paths(analysis)


def _section_2(
    analysis: AnalysisDigest,
    call_edges: list[tuple[str, str, str]] | None = None,
    unit_paths: list[str] | None = None,
) -> tuple[str, list[str]]:
    """§2 System Architecture — flowchart of execution units + ``calls_to`` edges."""
    units = _by_name(analysis.execution_units)
    if not units:
        return "_No execution units extracted._", []
    nodes = [(u.name, u.name) for u in units]
    edges = call_edges if call_edges is not None else _call_edges(analysis)
    paths = unit_paths if unit_paths is not None else _unit_paths(analysis)
    return flowchart(nodes, edges, direction="TD"), paths


def _section_3(
    analysis: AnalysisDigest,
    unit_paths: list[str] | None = None,
) -> tuple[str, list[str]]:
    """§3 Execution Unit Inventory — table of (Name, Kind, LoC, Source)."""
    units = _by_name(analysis.execution_units)
    if not units:
        return "_No execution units extracted._", []
    rows = [[u.name, u.kind, str(u.loc), f"`{u.source_path}`"] for u in units]
    paths = unit_paths if unit_paths is not None else _unit_paths(analysis)
    return md_table(["Name", "Kind", "LoC", "Source"], rows), paths


def _section_4(
    analysis: AnalysisDigest,
    unit_paths: list[str] | None = None,
) -> tuple[str, list[str]]:
    """§4 The Three-Layer Architecture — presentation / business / data tables."""
    presentation = md_table(
        ["Entry Point", "Kind"],
        [[e.name, e.kind] for e in _by_name(analysis.entry_points)],
    )
    business = md_table(
        ["Execution Unit", "Kind"],
        [[u.name, u.kind] for u in _by_name(analysis.execution_units)],
    )
    data = md_table(
        ["Data Store", "Kind"],
        [[d.name, d.kind] for d in _by_name(analysis.data_stores)],
    )
    content = "\n\n".join(
        [
            heading("Presentation layer", level=3),
            presentation,
            heading("Business layer", level=3),
            business,
            heading("Data layer", level=3),
            data,
        ]
    )
    paths = unit_paths if unit_paths is not None else _unit_paths(analysis)
    return content, paths


def _section_5(
    analysis: AnalysisDigest,
    call_edges: list[tuple[str, str, str]] | None = None,
    unit_paths: list[str] | None = None,
) -> tuple[str, list[str]]:
    """§5 Call Chain Maps — flowchart of unit→unit ``calls_to`` edges."""
    edges = call_edges if call_edges is not None else _call_edges(analysis)
    paths = unit_paths if unit_paths is not None else _unit_paths(analysis)
    if not edges:
        return "_No call edges extracted._", paths
    names = {n for e in edges for n in (e[0], e[1])}
    nodes = [(n, n) for n in sorted(names)]
    return flowchart(nodes, edges, direction="LR"), paths


def _section_6(analysis: AnalysisDigest) -> tuple[str, list[str]]:
    """§6 Data Architecture — ER diagram of data stores + a (Name, Kind) table."""
    stores = _by_name(analysis.data_stores)
    entities: list[tuple[str, list[tuple[str, str]]]] = [(d.name, []) for d in stores]
    diagram = er_diagram(entities, [])
    table = md_table(
        ["Name", "Kind"],
        [[d.name, d.kind] for d in stores],
    )
    return f"{diagram}\n\n{table}", []


def _section_7(analysis: AnalysisDigest) -> tuple[str, list[str]]:
    """§7 Screen Navigation & UI Layer — UI table, or the API-only callout."""
    elements = _by_name(analysis.ui_elements)
    if not elements:
        return (
            "> **API-only, no UI surface.** No UI elements were extracted; "
            "this system exposes no user-facing screens.",
            [],
        )
    table = md_table(
        ["Name", "Kind"],
        [[e.name, e.kind] for e in elements],
    )
    return table, []


def _section_8(
    analysis: AnalysisDigest,
    unit_paths: list[str] | None = None,
    functional_map: FunctionalInventory | None = None,
) -> tuple[str, list[str]]:
    """§8 Business Rules Catalog.

    When a functional-map ``FunctionalInventory`` supplies real business rules
    (#1358 — the app-mod chain runs /functional-map alongside discovery), render
    those *verified* rules instead of the deterministic per-unit "Rule candidate in
    `<ClassName>` — pending verification" stubs. Without a functional-map (a
    standalone discovery run), the deterministic per-execution-unit stubs are the
    byte-identical fallback.
    """
    if functional_map is not None and functional_map.business_rules:
        ids = assign_ids("R", len(functional_map.business_rules))
        rows = [
            [
                rid,
                str(br.get("rule") or ""),
                f"`{br.get('step_ref') or '—'}` ({br.get('source') or 'functional-map'})",
            ]
            for rid, br in zip(ids, functional_map.business_rules, strict=True)
        ]
        table = md_table(["Rule ID", "Business Rule", "Source"], rows)
        return table, unit_paths if unit_paths is not None else _unit_paths(analysis)

    units = _by_name(analysis.execution_units)
    if not units:
        return "_No execution units to seed candidate business rules from._", []
    ids = assign_ids("R", len(units))
    rows = [
        [rid, f"Rule candidate in `{u.name}` — rule pending verification", _UNVERIFIED]
        for rid, u in zip(ids, units, strict=True)
    ]
    table = md_table(["Rule ID", "Candidate Rule", "Status"], rows)
    return table, unit_paths if unit_paths is not None else _unit_paths(analysis)


def _section_9(analysis: AnalysisDigest) -> tuple[str, list[str]]:
    """§9 Batch Processing — table of batch units, or a no-batch note."""
    batch = _by_name(analysis.batch_units)
    if not batch:
        return "_No scheduled/batch work detected._", []
    table = md_table(
        ["Name", "Kind"],
        [[b.name, b.kind] for b in batch],
    )
    return table, []


def _section_10(analysis: AnalysisDigest) -> tuple[str, list[str]]:
    """§10 Security Architecture — entry points carrying security requirements."""
    secured = [ep for ep in _by_name(analysis.entry_points) if ep.security_requirements]
    if not secured:
        return "_No declared security constraints found._", []
    rows = [[ep.name, ep.kind, ", ".join(ep.security_requirements)] for ep in secured]
    return md_table(["Route", "Kind", "Roles"], rows), []


def _section_11(analysis: AnalysisDigest) -> tuple[str, list[str]]:
    """§11 Deployment Architecture — deployment-topology objects from manifests (#949).

    Renders the k8s/infra deployment objects (``Deployment`` / ``StatefulSet`` /
    ``Service`` / …) the manifest scan discovered, each with its source manifest.
    Falls back to the narrative placeholder only when none were found.
    """
    deployments = _by_name(analysis.deployment_units)
    if not deployments:
        stub = (
            f"{_UNVERIFIED} — deployment topology not statically extractable; supply via narrative."
        )
        return stub, []
    rows = [
        [
            d.name,
            d.kind,
            str(d.replicas) if d.replicas is not None else "—",
            d.image or "—",
            d.source_path or "—",
        ]
        for d in deployments
    ]
    table = md_table(["Name", "Kind", "Replicas", "Image", "Source"], rows)
    summary = (
        f"Detected {len(deployments)} deployment-topology object(s) from "
        f"infrastructure manifests:\n\n{table}"
    )
    return summary, [d.source_path for d in deployments if d.source_path]


def _section_12(analysis: AnalysisDigest) -> tuple[str, list[str]]:
    """§12 Automation Layer — batch/CI/scripts summary (reuses batch units)."""
    batch = _by_name(analysis.batch_units)
    if not batch:
        summary = "_No automation / scheduled work detected; supply CI/scripts via narrative._"
        return summary, []
    table = md_table(
        ["Name", "Kind", "Source"],
        [[b.name, b.kind, b.source_path or "—"] for b in batch],
    )
    summary = (
        f"Detected {len(batch)} automation/batch unit(s):\n\n{table}\n\n"
        f"{_UNVERIFIED} — CI pipelines and helper scripts not statically "
        "extractable; supply via narrative."
    )
    return summary, [b.source_path for b in batch if b.source_path]


def _section_13(analysis: AnalysisDigest) -> tuple[str, list[str]]:
    """§13 Operational Patterns — narrative-driven placeholder."""
    del analysis
    return (
        f"{_UNVERIFIED} — operational patterns (retries, idempotency, "
        "circuit-breaking, observability) not statically extractable; supply "
        "via narrative.",
        [],
    )


def _section_14(analysis: AnalysisDigest) -> tuple[str, list[str]]:
    """§14 Technical Debt Inventory — table of security findings as debt rows."""
    findings = sorted(analysis.security_findings)
    if not findings:
        return "_No debt findings recorded._", []
    rows = [[f, "finding"] for f in findings]
    return md_table(["Finding", "Kind"], rows), []


def _section_15(
    analysis: AnalysisDigest,
    unit_paths: list[str] | None = None,
) -> tuple[str, list[str]]:
    """§15 Modernization Analysis — counts + top-N highest-LoC extraction candidates."""
    units = list(analysis.execution_units)
    counts = (
        f"Inventory: {len(units)} execution unit(s), "
        f"{len(analysis.entry_points)} entry point(s), "
        f"{len(analysis.data_stores)} data store(s)."
    )
    if not units:
        body = f"{counts}\n\n_No execution units to rank as extraction candidates._"
        return body, []
    # Highest LoC first; ties broken by name for determinism.
    ranked = sorted(units, key=lambda u: (-u.loc, u.name))[:_TOP_N_MODERNIZATION]
    rows = [[u.name, str(u.loc), f"`{u.source_path}`"] for u in ranked]
    table = md_table(["Candidate", "LoC", "Source"], rows)
    placeholder = (
        f"{_UNVERIFIED} — strategy (re-platform / strangler-fig / "
        "decompose-by-bounded-context) pending the M3 strategist narrative."
    )
    body = f"{counts}\n\nTop extraction candidates by size:\n\n{table}\n\n{placeholder}"
    return body, unit_paths if unit_paths is not None else _unit_paths(analysis)


def _section_16(
    analysis: AnalysisDigest,
    unit_paths: list[str] | None = None,
) -> tuple[str, list[str]]:
    """§16 Appendices — traceability matrix + entity-count recap."""
    units = _by_name(analysis.execution_units)
    ids = assign_ids("R", len(units))
    trace_rows = [TraceRow(requirement=rid, acceptance=(), tests=()) for rid in ids]
    matrix = (
        traceability_matrix(trace_rows)
        if trace_rows
        else "_No execution units to seed the traceability matrix._"
    )
    recap = _counts_table(
        analysis,
        (
            "execution_units",
            "shared_components",
            "entry_points",
            "data_stores",
            "ui_elements",
            "batch_units",
        ),
    )
    content = "\n\n".join(
        [
            heading("Traceability matrix", level=3),
            matrix,
            heading("Entity-count recap", level=3),
            recap,
        ]
    )
    return content, unit_paths if unit_paths is not None else _unit_paths(analysis)


# ---------------------------------------------------------------------------
# Cross-section helpers
# ---------------------------------------------------------------------------


def _call_edges(analysis: AnalysisDigest) -> list[tuple[str, str, str]]:
    """Return sorted ``(src, dst, "")`` unit→unit call edges.

    Only edges whose source unit appears on the digest are emitted (the
    destination may be an external/unresolved name the source declares it
    calls — surfaced as a node so the graph is honest about the edge). Sorted
    for byte-stability.
    """
    edges: list[tuple[str, str, str]] = []
    for unit in _by_name(analysis.execution_units):
        for dst in unit.calls_to:
            edges.append((unit.name, dst, ""))
    return sorted(edges)


def _unit_paths(analysis: AnalysisDigest) -> list[str]:
    """Return the de-duplicated, sorted ``source_path`` set of all execution units."""
    return sorted({u.source_path for u in analysis.execution_units if u.source_path})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

#: ``number -> renderer`` dispatch table; each renderer returns
#: ``(deterministic_content, evidence_paths)``.
_RENDERERS: dict[int, _SectionRenderer] = {
    1: _section_1,
    2: _section_2,
    3: _section_3,
    4: _section_4,
    5: _section_5,
    6: _section_6,
    7: _section_7,
    8: _section_8,
    9: _section_9,
    10: _section_10,
    11: _section_11,
    12: _section_12,
    13: _section_13,
    14: _section_14,
    15: _section_15,
    16: _section_16,
}


def render_playbook(
    analysis: AnalysisDigest,
    *,
    title: str = "Discovery Playbook",
    narratives: Mapping[int, str] | None = None,
    synthesis: SynthesisJudgments | None = None,
    functional_map: FunctionalInventory | None = None,
) -> str:
    """Render the full 16-section ``PLAYBOOK.md`` from an :class:`AnalysisDigest`.

    Pure-deterministic and byte-stable: all entity lists are sorted by name/id,
    no clock or randomness is consulted, and only names present on ``analysis``
    are ever emitted (no fabrication). Every diagram is Mermaid.

    Args:
        analysis: the Phase B :class:`AnalysisDigest` (the six abstract lists).
        title: the document ``# H1`` title.
        narratives: optional model-supplied prose keyed by section number
            (1-16). When a section's number is present its value is injected
            under that section's deterministic content; absent numbers render
            deterministic content only (the token-offload contract).
        synthesis: optional harness synthesis judgments (#861). When provided,
            the five narrative-driven sections (§1, §2, §8, §14, §15) are
            merged with the harness-authored ``body`` and ``citations`` for each
            present key. Missing section keys fall back to the deterministic
            ``⚠️ UNVERIFIED`` stub for that section — no crash. ``synthesis=None``
            produces output byte-identical to the no-synthesis path (regression-
            locked). The other 11 sections are byte-identical regardless of
            ``synthesis``.
        functional_map: optional functional-map ``FunctionalInventory`` (#1358).
            When it carries real business rules, §8 renders those verified rules
            instead of the per-execution-unit "candidate rule — pending
            verification" stubs. ``functional_map=None`` (a standalone discovery
            run) → byte-identical to the prior output. **Precedence:** §8 is also a
            synthesis-driven section — when a ``synthesis`` §8 body is *also*
            supplied, the harness-authored synthesis prose wins (it replaces the
            whole §8 content, functional-map table included), since synthesis is
            the deliberate narrative layer.

    Returns:
        The complete ``PLAYBOOK.md`` document as a string (trailing newline).
    """
    # Pre-compute shared derived values once so the 8 sections that consume
    # _unit_paths and the 2 that consume _call_edges do not each recompute them.
    precomputed_unit_paths = _unit_paths(analysis)
    precomputed_call_edges = _call_edges(analysis)

    blocks: list[str] = [heading(title, level=1)]
    for number in range(1, 17):
        renderer = _RENDERERS[number]
        # Thread pre-computed values into the sections that use them.
        if number in {2, 5}:
            content, evidence = renderer(  # type: ignore[call-arg]
                analysis,
                call_edges=precomputed_call_edges,
                unit_paths=precomputed_unit_paths,
            )
        elif number in {8}:
            # §8 also receives the optional functional-map channel (#1358): when a
            # FunctionalInventory carries real business rules, they replace the
            # per-unit candidate-rule stubs.
            content, evidence = _section_8(
                analysis,
                unit_paths=precomputed_unit_paths,
                functional_map=functional_map,
            )
        elif number in {1, 3, 4, 15, 16}:
            content, evidence = renderer(  # type: ignore[call-arg]
                analysis,
                unit_paths=precomputed_unit_paths,
            )
        else:
            content, evidence = renderer(analysis)
        blocks.append(
            _section(
                number,
                content=content,
                evidence=evidence,
                narratives=narratives,
                synthesis=synthesis,
            )
        )
    return "\n\n".join(blocks) + "\n"


def render_diagrams(analysis: AnalysisDigest) -> dict[str, str]:
    """Return the standalone ``.mmd`` diagram bodies keyed by filename.

    The bodies are raw Mermaid (no ``` ```mermaid ``` fence) so they can be
    written straight to ``.mmd`` files. Keys are zero-padded ``NN_name.mmd``
    matching the section the diagram belongs to.

    Args:
        analysis: the Phase B :class:`AnalysisDigest`.

    Returns:
        ``{"02_system_architecture.mmd": "flowchart ...", ...}`` — present only
        for sections that actually produced a diagram (a unit-less digest still
        yields the keys, degrading to a minimal but valid mermaid body).
    """
    diagrams: dict[str, str] = {}

    # Compute call edges once — used by both the architecture diagram and the
    # call-chain diagram.  Previously _call_edges(analysis) was called twice,
    # re-sorting the edge list each time.
    edges = _call_edges(analysis)

    units = _by_name(analysis.execution_units)
    arch_nodes = [(u.name, u.name) for u in units]
    diagrams["02_system_architecture.mmd"] = flowchart(
        arch_nodes, edges, direction="TD", fenced=False
    )

    chain_names = {n for e in edges for n in (e[0], e[1])}
    chain_nodes = [(n, n) for n in sorted(chain_names)]
    diagrams["05_call_chain_maps.mmd"] = flowchart(chain_nodes, edges, direction="LR", fenced=False)

    stores = _by_name(analysis.data_stores)
    er_entities: list[tuple[str, list[tuple[str, str]]]] = [(d.name, []) for d in stores]
    diagrams["06_data_architecture.mmd"] = er_diagram(er_entities, [], fenced=False)

    return diagrams


# Keep the abstract-entity imports referenced for type-checkers / readers even
# though they are consumed only via attribute access in the renderers above.
__all__ = ["render_diagrams", "render_playbook"]

if TYPE_CHECKING:  # pragma: no cover - import-surface documentation only
    _entity_surface: tuple[
        type[ExecutionUnit],
        type[SharedComponent],
        type[EntryPoint],
        type[DataStore],
        type[UIElement],
        type[BatchUnit],
    ]
