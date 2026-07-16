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
from itertools import pairwise
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

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


def _escape_mermaid_text(text: str) -> str:
    """Neutralize a mermaid string value: escape ``"`` and strip line breaks.

    An unescaped ``"`` closes the mermaid string; a raw newline breaks out of the
    current statement and lets an attacker-authored label inject a following mermaid
    directive (e.g. ``click``). Escaping the quote as ``&quot;`` and collapsing
    newlines to spaces keeps any label — including agent-authored ones — a single
    inert string. Shared by node and edge labels.
    """
    return text.replace('"', "&quot;").replace("\n", " ").replace("\r", " ")


def _quote_label(label: str) -> str:
    """Quote a mermaid node label, escaping ``"`` and stripping line breaks."""
    return '"' + _escape_mermaid_text(label) + '"'


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
            lines.append(f'  {s} -- "{_escape_mermaid_text(edge_label)}" --> {d}')
        else:
            lines.append(f"  {s} --> {d}")

    return _wrap("\n".join(lines), fenced=fenced)


# ---------------------------------------------------------------------------
# Mermaid: C4-style component diagram
# ---------------------------------------------------------------------------

#: The target-stack dimensions surfaced as C4 component tiers, in
#: presentation → application → data render order.  Keys match
#: :class:`~primitives._shared.modernization.target_stack.TargetDimension.key`.
_C4_TARGET_TIERS: tuple[tuple[str, str], ...] = (
    ("frontend", "SPA"),
    ("backend", "Service"),
    ("database", "Database"),
)


#: The Mermaid class name + classDef used to distinctly style the Track B
#: (#1427) inbound entry-point tier so it reads as a separate band from the
#: bounded-context / target-component nodes.
_ENTRY_POINT_CLASS: str = "entrypoint"
_ENTRY_POINT_CLASSDEF: str = (
    f"  classDef {_ENTRY_POINT_CLASS} fill:#fef3c7,stroke:#d97706,stroke-width:2px;"
)


def component_diagram(
    contexts: Sequence[tuple[str, str]],
    edges: Sequence[tuple[str, str, str]],
    target: object | None = None,
    *,
    entry_points: Sequence[tuple[str, str, str]] | None = None,
    direction: str = "TD",
    fenced: bool = True,
) -> str:
    """Render a C4-style component :func:`flowchart` from bounded contexts + edges.

    Reuses the :func:`flowchart` emitter (so mermaid-id-safety and label
    quoting are shared, not reimplemented). Each context is one node; each edge
    is a labeled dependency arrow. When *target* carries target-stack
    dimensions, a leading target-component tier (e.g. ``Angular SPA`` →
    ``Spring Boot Service`` → ``Oracle Database``) is prepended and wired to the
    first context so the diagram shows the components the system modernizes onto.

    Args:
        contexts: ``(context_id, label)`` pairs — the bounded contexts /
            system-map units. May be empty (then only target tiers, if any,
            render).
        edges: ``(src_id, dst_id, edge_label)`` dependency triples between
            context ids.
        target: an optional object exposing ``dimensions`` (a
            :class:`~primitives._shared.modernization.target_stack.TargetStack`).
            Each dimension whose ``key`` is a known C4 tier contributes a
            component node labeled from its ``label`` (falling back to
            ``value``). ``None`` or an empty stack → no target tier (structural
            behavior unchanged).
        entry_points: Track B (#1427) typed inbound triggers —
            ``(name, kind, target_id)`` triples. Each renders as a
            distinctly-styled inbound node (an entry-point tier) wired into its
            ``target_id`` context (or, when the target is unknown/empty, into the
            first context). ``None``/empty → no entry-point tier and NO
            ``classDef`` (the diagram is byte-identical to prior output).
        direction: flowchart direction (see :func:`flowchart`).
        fenced: wrap in a ```` ```mermaid ```` block (default) or return raw.

    Returns:
        A Mermaid flowchart string.
    """
    nodes: list[tuple[str, str]] = []
    extra_edges: list[tuple[str, str, str]] = []

    target_tier_ids: list[str] = []
    for dim_key, tier_word in _C4_TARGET_TIERS:
        label = _target_component_label(target, dim_key, tier_word)
        if label:
            node_id = f"target::{dim_key}"
            nodes.append((node_id, label))
            target_tier_ids.append(node_id)

    # Wire the target tiers top-down (SPA → Service → Database).
    for upper, lower in pairwise(target_tier_ids):
        extra_edges.append((upper, lower, ""))

    # The bounded contexts follow the target tiers.
    for ctx_id, label in contexts:
        nodes.append((ctx_id, label))

    # Connect the target's data/application tier to the first context so the
    # diagram is one connected component, not two disjoint graphs.
    if target_tier_ids and contexts:
        anchor = target_tier_ids[-1]
        extra_edges.append((anchor, contexts[0][0], "hosts"))

    # Track B (#1427): the inbound entry-point tier. Each entry point is a node
    # ``ep::<name>`` wired into its target context (falling back to the first
    # context when the named target is not a known node). The nodes are given a
    # distinct Mermaid class so they read as a separate inbound band.
    entry_point_ids: list[str] = []
    known_ctx_ids = {ctx_id for ctx_id, _ in contexts}
    for name, kind, ep_target in entry_points or ():
        node_id = f"ep::{name}"
        label = f"{name} ({kind})" if kind else name
        nodes.append((node_id, label))
        entry_point_ids.append(node_id)
        dst = ep_target if ep_target in known_ctx_ids else (contexts[0][0] if contexts else "")
        if dst:
            extra_edges.append((node_id, dst, "triggers"))

    if not entry_point_ids:
        # No entry points → exactly the historical output (no classDef band).
        return flowchart(nodes, [*extra_edges, *edges], direction=direction, fenced=fenced)

    # Build raw, then append the classDef + class-assignment lines for the
    # entry-point tier before (re-)wrapping. Reuses the shared id-safety by
    # asking ``flowchart`` for the safe token of each entry-point node id.
    raw = flowchart(nodes, [*extra_edges, *edges], direction=direction, fenced=False)
    mapper = _IdMapper("n")
    for node_id, _ in nodes:
        mapper.safe(node_id)
    safe_ep_ids = [mapper.safe(node_id) for node_id in entry_point_ids]
    styled = "\n".join(
        [
            raw,
            _ENTRY_POINT_CLASSDEF,
            f"  class {','.join(safe_ep_ids)} {_ENTRY_POINT_CLASS};",
        ]
    )
    return _wrap(styled, fenced=fenced)


def _target_component_label(target: object | None, dim_key: str, tier_word: str) -> str:
    """Return ``"<label> <tier_word>"`` for *dim_key* on *target*, or ``""``.

    Reads the target's ``dimensions`` by attribute (duck-typed), matching the
    dimension whose ``key`` equals *dim_key*. Uses the dimension ``label`` (e.g.
    ``"Spring Boot 3.3"``) when present, else its ``value``. Returns ``""`` when
    *target* is ``None`` or declares no such dimension.
    """
    if target is None:
        return ""
    for dim in getattr(target, "dimensions", ()) or ():
        if getattr(dim, "key", "") == dim_key:
            base = getattr(dim, "label", "") or getattr(dim, "value", "")
            return f"{base} {tier_word}" if base else ""
    return ""


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
# Mermaid: business-audience diagrams (Track C, #1429)
#
# Four business-register diagram emitters for the per-context BRD.md. They consume
# the SAME extracted structure the technical diagrams do (flows, entry points, nav
# edges + per-page data, contexts, actors, entity states) but render it with
# plain-language labels for a business reader — distinct from the spec's technical
# C4 / ER / sequence diagrams. Each reuses the shared :func:`flowchart` emitter (so
# mermaid-id-safety + label quoting are shared, never re-implemented). No fabrication:
# empty inputs → ``""`` (the emitter omits, its BRD section renders nothing).
# ---------------------------------------------------------------------------


def business_process_diagram(
    journeys: Sequence[tuple[str, Sequence[str]]],
    entry_points: Mapping[str, str] | None = None,
    *,
    fenced: bool = True,
) -> str:
    """Render an end-to-end business-process / user-journey flow (Track C, #1429).

    Each journey is a labeled left-to-right chain of plain-language steps. When a
    journey has an entry point (``entry_points[journey_label]``), that entry point
    is rendered as the FIRST node and wired into the journey's first step — the
    journey literally *starts at its entry point* (the "New Owner button →
    Register owner" business framing). Reuses :func:`flowchart`.

    Args:
        journeys: ``(journey_label, [step_label, ...])`` pairs. A journey with no
            steps contributes only its entry-point→journey node (if any).
        entry_points: optional ``{journey_label: entry_point_label}`` map — each
            journey's inbound trigger in business terms. ``None``/missing → the
            journey starts at its first step.
        fenced: wrap in a ```` ```mermaid ```` block (default) or return raw.

    Returns:
        A Mermaid flowchart string, or ``""`` when *journeys* is empty.
    """
    if not journeys:
        return ""
    eps = entry_points or {}
    nodes: list[tuple[str, str]] = []
    edges: list[tuple[str, str, str]] = []
    for j_idx, (journey, steps) in enumerate(journeys):
        step_ids: list[str] = []
        ep_label = eps.get(journey)
        if ep_label:
            ep_id = f"ep{j_idx}"
            nodes.append((ep_id, ep_label))
            step_ids.append(ep_id)
        for s_idx, step in enumerate(steps):
            node_id = f"j{j_idx}s{s_idx}"
            nodes.append((node_id, f"{journey}: {step}" if s_idx == 0 and not ep_label else step))
            step_ids.append(node_id)
        for src, dst in pairwise(step_ids):
            edges.append((src, dst, ""))
    return flowchart(nodes, edges, direction="LR", fenced=fenced)


def screen_flow_diagram(
    nav_edges: Sequence[tuple[str, str, str]],
    page_data: Mapping[str, Sequence[str]] | None = None,
    *,
    fenced: bool = True,
) -> str:
    """Render a screen / page-flow + navigation map for the BRD (Track C, #1429).

    Shows how a user moves between screens; each screen is annotated with its real
    per-page data (Track D) in business terms — the screen node label carries the
    page name plus its fields (e.g. ``Owner Detail — Name, Pets, Visits``). Reuses
    :func:`flowchart`; the navigation kind labels each transition arrow.

    Args:
        nav_edges: ``(from_screen, to_screen, kind)`` triples (the extracted nav
            graph).
        page_data: optional ``{screen: [field, ...]}`` — the real fields each page
            reads/writes, from Track-D per-view data. ``None`` → screens render
            with no data annotation.
        fenced: wrap in a ```` ```mermaid ```` block (default) or return raw.

    Returns:
        A Mermaid flowchart string, or ``""`` when *nav_edges* is empty.
    """
    if not nav_edges:
        return ""
    data = page_data or {}

    def _label(screen: str) -> str:
        fields = data.get(screen)
        return f"{screen} — {', '.join(fields)}" if fields else screen

    screens: dict[str, str] = {}
    for frm, to, _kind in nav_edges:
        screens.setdefault(frm, _label(frm))
        screens.setdefault(to, _label(to))
    nodes = list(screens.items())
    edges = [(frm, to, kind) for frm, to, kind in nav_edges]
    return flowchart(nodes, edges, direction="LR", fenced=fenced)


def business_capability_map(
    contexts: Sequence[str],
    relationships: Sequence[tuple[str, str, str]] | None = None,
    *,
    fenced: bool = True,
) -> str:
    """Render a business-capability / context map for the BRD (Track C, #1429).

    Each bounded context is a business capability node; each relationship is a
    labeled dependency between two capabilities. Reuses :func:`flowchart` (so a
    single capability with no relationships still renders as one node).

    Args:
        contexts: the bounded contexts as business-capability labels.
        relationships: optional ``(src_capability, dst_capability, label)`` triples.
            ``None``/empty → capabilities render as standalone nodes.
        fenced: wrap in a ```` ```mermaid ```` block (default) or return raw.

    Returns:
        A Mermaid flowchart string, or ``""`` when *contexts* is empty.
    """
    if not contexts:
        return ""
    rels = relationships or []
    nodes = [(ctx, ctx) for ctx in dict.fromkeys(contexts)]
    edges = [(src, dst, label) for src, dst, label in rels]
    return flowchart(nodes, edges, direction="TD", fenced=fenced)


def actor_lifecycle_diagram(
    actor_capabilities: Sequence[tuple[str, str]],
    entity_states: Sequence[tuple[str, Sequence[str]]] | None = None,
    *,
    fenced: bool = True,
) -> str:
    """Render an actor→capability + entity data-lifecycle view for the BRD (Track C).

    Two business views in one diagram: actor→capability edges (who does what) and
    per-entity state flows (``Draft → Submitted → Paid``). Reuses :func:`flowchart`.
    Either view may be empty; if BOTH are empty the emitter omits (``""``).

    Args:
        actor_capabilities: ``(actor, capability)`` pairs — each actor is wired to
            a capability it exercises.
        entity_states: optional ``(entity, [state, ...])`` pairs — the business
            entity's lifecycle states, wired in order as a left-to-right chain.
        fenced: wrap in a ```` ```mermaid ```` block (default) or return raw.

    Returns:
        A Mermaid flowchart string, or ``""`` when both inputs are empty.
    """
    states = entity_states or []
    if not actor_capabilities and not states:
        return ""
    nodes: dict[str, tuple[str, str]] = {}
    edges: list[tuple[str, str, str]] = []
    for a_idx, (actor, capability) in enumerate(actor_capabilities):
        actor_id = f"actor::{actor}"
        cap_id = f"cap::{capability}"
        nodes.setdefault(actor_id, (actor_id, actor))
        nodes.setdefault(cap_id, (cap_id, capability))
        edges.append((actor_id, cap_id, "performs" if a_idx == 0 else ""))
    for entity, entity_state_list in states:
        state_ids: list[str] = []
        for s_idx, state in enumerate(entity_state_list):
            state_id = f"state::{entity}::{s_idx}"
            label = f"{entity}: {state}" if s_idx == 0 else state
            nodes.setdefault(state_id, (state_id, label))
            state_ids.append(state_id)
        for a, b in pairwise(state_ids):
            edges.append((a, b, ""))
    return flowchart(list(nodes.values()), edges, direction="LR", fenced=fenced)


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
