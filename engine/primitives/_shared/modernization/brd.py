"""Business Requirements Document (BRD) renderer (Layer 1, Track C, #1429).

:func:`render_brd` produces a **per-context, non-technical, visual** Business
Requirements Document beside the technical spec :func:`render_spec` emits. Both
consume the SAME slice and share the SAME ``R-`` / ``AC-`` / ``FR-`` ids — a
business reader and an engineer trace the *same* requirement. The BRD is framed
for a business audience: capabilities, stakeholders, business processes, business
rules as *policy* (with Track-D real values), functional requirements in plain
language cross-referenced to the spec's ids, the real data & content (Track-D
option/filter values), KPIs, glossary, assumptions, risks, and scope — plus four
business-audience Mermaid diagrams (:mod:`~primitives._shared.modernization.render`
``business_process_diagram`` / ``screen_flow_diagram`` / ``business_capability_map``
/ ``actor_lifecycle_diagram``).

**No fabrication.** A section or diagram whose backing data is absent omits — a
thin slice yields a sparser (honest) BRD, never invented content. Real values
inlined from the slice keep their ``file:line`` provenance.

**QUAL-2 layering.** Like :mod:`~primitives._shared.modernization.scope`, this
Layer-1 renderer imports NO Layer-2 primitive model: it reads the ``SpecJudgment``
(``spec``), the functional-map slice, and the system-map model-agnostically (by
attribute-or-key), so a duck-typed object (the pipeline path) and a plain dict
(tests / callers) render identically.

Pure and deterministic: no LLM, no clock, no filesystem. ``generated_at`` is
injected by the caller; nothing here reads the wall clock.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from primitives._shared.modernization.render import (
    actor_lifecycle_diagram,
    business_capability_map,
    business_process_diagram,
    heading,
    md_table,
    screen_flow_diagram,
)

if TYPE_CHECKING:
    from primitives._shared.modernization.models import SystemMap

# Journey↔entry-point name-token match: a token shorter than this is too generic to
# be a reliable join (avoids matching on short filler words).
_MIN_JOIN_TOKEN_LEN = 4
# A business-entity lifecycle needs at least this many states to be worth drawing.
_MIN_LIFECYCLE_STATES = 2

# The nested-model fields each list element carries, with default-empty values, so a
# plain dict element reads identically to a duck-typed pydantic object (the same
# model-agnostic discipline scope.py uses — QUAL-2 layering).
_REQ_DEFAULTS = {"id": "", "statement": "", "source": "", "priority": ""}
_NFR_DEFAULTS = {"id": "", "category": "", "requirement": "", "source": ""}


def _get(obj: Any, key: str, default: Any) -> Any:
    """Read *key* from a dict (by key) or an object (by attribute), else *default*."""
    val = obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)
    return default if val is None else val


def _view(element: Any, defaults: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**{k: _get(element, k, d) for k, d in defaults.items()})


def _views(items: Any, defaults: dict[str, Any]) -> list[SimpleNamespace]:
    return [_view(it, defaults) for it in (items or ())]


class _BrdSpec:
    """A model-agnostic view over a SpecJudgment (duck-typed object or dict)."""

    def __init__(self, item: Any) -> None:
        self.title: str = _get(item, "title", "Specification") or "Specification"
        self.description: str = _get(item, "description", "")
        self.context: str = _get(item, "context", "")
        self.business_narrative: str = _get(item, "business_narrative", "")
        self.business_process_diagram: str = _get(item, "business_process_diagram", "")
        self.capability_map: str = _get(item, "capability_map", "")
        self.kpis: list[str] = list(_get(item, "kpis", ()) or ())
        self.business_glossary: list[str] = list(_get(item, "business_glossary", ()) or ())
        self.assumptions: list[str] = list(_get(item, "assumptions", ()) or ())
        self.business_risks: list[str] = list(_get(item, "business_risks", ()) or ())
        self.requirements = _views(_get(item, "requirements", ()), _REQ_DEFAULTS)
        self.nfrs = _views(_get(item, "nfrs", ()), _NFR_DEFAULTS)


# ---------------------------------------------------------------------------
# Section helpers (each returns a list of lines, empty when nothing to render).
# ---------------------------------------------------------------------------


def _section(title: str, body: list[str]) -> list[str]:
    return [heading(title, level=2), "", *body, ""]


def _mermaid_block(source: str) -> list[str]:
    """Fence an already-fenced-or-raw Mermaid *source* string.

    The render.py business emitters return a fenced ```` ```mermaid ```` block;
    a harness-authored override is a raw string. Normalize by stripping any
    existing fence and re-fencing so the BRD never double-fences.
    """
    stripped = source.strip()
    if stripped.startswith("```"):
        return [stripped, ""]
    return ["```mermaid", stripped, "```", ""]


# ---------------------------------------------------------------------------
# Slice accessors (duck-typed; None-safe).
# ---------------------------------------------------------------------------


def _digest(slice_report: Any | None) -> Any:
    return getattr(slice_report, "digest", None) if slice_report is not None else None


def _is_web_slice(slice_report: Any | None) -> bool:
    """True when *slice_report* is a web/UI context (has endpoints or nav edges).

    Used to decide whether a diagram whose backing data is absent should render an
    honest ``⚠️ requires`` stub (#1456) rather than silently omit. A pure Data/DB
    or otherwise non-web context has no endpoints/nav → the diagrams omit exactly as
    before (no spurious stubs on a context that legitimately has no web content).
    """
    digest = _digest(slice_report)
    if digest is None:
        return False
    return bool(getattr(digest, "endpoints", ()) or getattr(digest, "nav_edges", ()))


def _diagram_stub(what: str, requires: str) -> list[str]:
    """An honest ``⚠️ requires`` line for a web diagram whose backing data is absent.

    Consistent with the §8/§9 ``⚠️ requires S40 / M1`` discipline elsewhere — a
    partial BRD reads as visibly partial, not silently complete (#1456)."""
    return [f"**{what}:**", "", f"> ⚠️ requires {requires}.", ""]


def _roles(slice_report: Any | None) -> list[str]:
    digest = _digest(slice_report)
    if digest is None:
        return []
    return sorted({r.name for r in getattr(digest, "roles", ()) if getattr(r, "name", "")})


def _capabilities_section(spec: _BrdSpec, system_map: SystemMap | None) -> list[str]:
    body: list[str] = []
    if spec.business_narrative:
        body.append(spec.business_narrative)
        body.append("")
    elif spec.description:
        body.append(spec.description)
        body.append("")
    contexts = _context_labels(system_map)
    if contexts:
        body.append("**Bounded capabilities:** " + ", ".join(contexts) + ".")
    elif spec.title:
        body.append(f"**Bounded capability:** {spec.title}.")
    # The capability / context map (deterministic emitter; harness override wins).
    diagram = spec.capability_map or business_capability_map(
        contexts or [spec.title], [], fenced=True
    )
    if diagram:
        body.append("")
        body.append("**Capability map:**")
        body.append("")
        body.extend(_mermaid_block(diagram))
    if not body:
        return []
    return _section("Business Capabilities", body)


def _context_labels(system_map: SystemMap | None) -> list[str]:
    if system_map is None:
        return []
    labels: list[str] = []
    for node in getattr(system_map, "nodes", []):
        labels.extend(getattr(node, "domains", []) or [])
    return sorted(dict.fromkeys(labels))


def _stakeholders_section(slice_report: Any | None) -> list[str]:
    roles = _roles(slice_report)
    if not roles:
        return []
    body = ["The people and systems that interact with this capability:", ""]
    body.extend(f"- **{role}**" for role in roles)
    return _section("Stakeholders & Actors", body)


def _entry_point_map(system_map: SystemMap | None) -> dict[str, str]:
    """Journey-label → entry-point label, so each journey starts at its trigger."""
    if system_map is None:
        return {}
    out: dict[str, str] = {}
    for ep in getattr(system_map, "entry_points", []):
        name = getattr(ep, "name", "")
        trigger = getattr(ep, "trigger", "")
        if name:
            out[name] = f"{name} ({trigger})" if trigger else name
    return out


def _flow_journeys(slice_report: Any | None) -> list[tuple[str, list[str]]]:
    """Business journeys as ``(name, [step description, ...])`` from the slice flows."""
    if slice_report is None:
        return []
    journeys: list[tuple[str, list[str]]] = []
    for flow in getattr(slice_report, "flows", ()):
        name = getattr(flow, "name", "") or getattr(flow, "id", "") or "Flow"
        steps = [
            getattr(step, "description", "") or f"step {getattr(step, 'order', '')}"
            for step in getattr(flow, "steps", ())
        ]
        journeys.append((name, steps))
    return journeys


def _business_processes_section(
    slice_report: Any | None, system_map: SystemMap | None, spec: _BrdSpec
) -> list[str]:
    journeys = _flow_journeys(slice_report)
    if not journeys and not spec.business_process_diagram:
        # Honest stub for a web context that has no captured flows (#1456), rather
        # than silently omitting the business-process / user-journey diagram.
        if _is_web_slice(slice_report):
            return _section(
                "Business Processes",
                _diagram_stub(
                    "Process flow",
                    "captured user flows from `/functional-map` (this context has "
                    "endpoints but no flows were synthesized)",
                ),
            )
        return []
    body: list[str] = []
    for name, steps in journeys:
        body.append(f"**{name}**")
        if steps:
            body.extend(f"1. {step}" for step in steps)
        body.append("")
    # The business-process / user-journey diagram — each journey starts at its
    # entry point (Track B). The harness override (if any) wins.
    ep_map = _map_journey_entry_points(journeys, _entry_point_map(system_map))
    diagram = spec.business_process_diagram or business_process_diagram(
        [(name, steps) for name, steps in journeys], ep_map, fenced=True
    )
    if diagram:
        body.append("**Process flow:**")
        body.append("")
        body.extend(_mermaid_block(diagram))
    if not body:
        return []
    return _section("Business Processes", body)


def _map_journey_entry_points(
    journeys: list[tuple[str, list[str]]], ep_labels: dict[str, str]
) -> dict[str, str]:
    """Best-effort join a journey to an entry point by name containment.

    An entry point whose name appears in (or contains a token of) a journey's name
    is that journey's start — the business framing "the request starts here". Purely
    structural (no fabrication): a journey with no matching entry point simply has
    none, and its process flow starts at its first step.
    """
    out: dict[str, str] = {}
    for name, _steps in journeys:
        low = name.lower()
        for ep_name, ep_label in ep_labels.items():
            token = ep_name.lower()
            tokens = [t for t in token.split() if len(t) >= _MIN_JOIN_TOKEN_LEN]
            if token and (token in low or any(t in low for t in tokens)):
                out[name] = ep_label
                break
    return out


def _business_rules_section(slice_report: Any | None) -> list[str]:
    """Business rules as *policy* (plain language) with Track-D real values + source."""
    digest = _digest(slice_report)
    if slice_report is None or digest is None:
        return []
    rows: list[list[str]] = []
    for br in getattr(slice_report, "business_rules", ()):
        rows.append(
            [
                getattr(br, "id", "") or "—",
                getattr(br, "rule", ""),
                f"`{getattr(br, 'enforced_at', '')}`" if getattr(br, "enforced_at", "") else "—",
            ]
        )
    for vr in getattr(digest, "validation_rules", ()):
        message = getattr(vr, "message", "") or (
            f"{getattr(vr, 'field', '')}: {getattr(vr, 'constraint', '')}"
        )
        rows.append([getattr(vr, "entity", "") or "—", message, "—"])
    if not rows:
        return []
    body = [
        "The policies this capability enforces, in plain language:",
        "",
        md_table(["Applies to", "Policy", "Enforced at"], rows),
    ]
    return _section("Business Rules (Policy)", body)


def _functional_requirements_section(spec: _BrdSpec, slice_report: Any | None) -> list[str]:
    """FRs in business terms, cross-referenced to the spec's ``R-`` / ``FR-`` ids.

    The BRD's ids are the SAME as the spec's (``R-`` from the requirements the spec
    renders, ``FR-`` from the slice's functional-requirement statements) so a
    business reader and an engineer trace the same requirement.
    """
    rows: list[list[str]] = []
    for req in spec.requirements:
        if req.id or req.statement:
            rows.append([req.id or "—", req.statement, "R-"])
    if slice_report is not None:
        for fr in getattr(slice_report, "fr_statements", ()):
            fid = getattr(fr, "id", "")
            statement = getattr(fr, "statement", "")
            if fid or statement:
                rows.append([fid or "—", statement, "FR-"])
    if not rows:
        return []
    body = [
        "What the business needs the capability to do — each traceable to the "
        "technical specification by the same id:",
        "",
        md_table(["Id", "Requirement (business terms)", "Class"], rows),
    ]
    return _section("Functional Requirements", body)


def _data_content_section(slice_report: Any | None) -> list[str]:
    """Data & content — the real per-view data models + Track-D option/filter values.

    Carries the screen / page-flow diagram (Track D). When the context is a web
    slice (has endpoints/nav) but neither the field table nor the screen-flow has
    backing data, an honest ``⚠️ requires`` stub renders for the screen-flow (#1456)
    rather than silently omitting the whole section.
    """
    digest = _digest(slice_report)
    if digest is None:
        return []
    form_fields = list(getattr(digest, "form_fields", ()))
    body: list[str] = []
    # Track D: inline the real select/dropdown option list, business-framed, keeping
    # the field's file:line provenance from its endpoint.
    if form_fields:
        ep_source = {
            getattr(ep, "id", ""): f"{getattr(ep, 'file', '')}:{getattr(ep, 'line', '')}"
            for ep in getattr(digest, "endpoints", ())
            if getattr(ep, "file", "")
        }
        rows: list[list[str]] = []
        for ff in form_fields:
            options = getattr(ff, "options", ()) or ()
            rows.append(
                [
                    getattr(ff, "label", "") or getattr(ff, "name", ""),
                    getattr(ff, "field_type", ""),
                    "required" if getattr(ff, "required", False) else "optional",
                    ", ".join(options) if options else "—",
                    f"`{ep_source[getattr(ff, 'endpoint_id', '')]}`"
                    if ep_source.get(getattr(ff, "endpoint_id", ""))
                    else "—",
                ]
            )
        body = [
            "The information this capability captures and shows — with the real values "
            "each field offers:",
            "",
            md_table(["Field", "Type", "Requirement", "Offered values", "Source"], rows),
        ]
    # Screen / page-flow + navigation map with per-page data (Track D).
    nav_edges = [
        (getattr(ne, "from_screen", ""), getattr(ne, "to_screen", ""), getattr(ne, "kind", ""))
        for ne in getattr(digest, "nav_edges", ())
    ]
    diagram = screen_flow_diagram(nav_edges, _page_data(digest), fenced=True) if nav_edges else ""
    if diagram:
        if body:
            body.append("")
        body.append("**Screen & page flow:**")
        body.append("")
        body.extend(_mermaid_block(diagram))
    elif _is_web_slice(slice_report):
        # A web context with no nav edges → honest stub for the screen-flow diagram.
        if body:
            body.append("")
        body.extend(
            _diagram_stub(
                "Screen & page flow",
                "captured navigation edges from `/functional-map` (this context has "
                "endpoints but no screen-flow was extracted)",
            )
        )
    if not body:
        return []
    return _section("Data & Content", body)


def _page_data(digest: Any) -> dict[str, list[str]]:
    """Per-screen field lists from the slice's form fields (best-effort, business-framed).

    All extracted form-field labels feed a single "form" data note; a richer
    per-view mapping is harness-authored. Deterministic + no fabrication (an empty
    field set yields no annotation).
    """
    labels = [
        getattr(ff, "label", "") or getattr(ff, "name", "")
        for ff in getattr(digest, "form_fields", ())
    ]
    labels = [lbl for lbl in labels if lbl]
    if not labels:
        return {}
    # Annotate the destination screens of nav edges with the captured fields.
    screens = {getattr(ne, "to_screen", "") for ne in getattr(digest, "nav_edges", ())}
    return {screen: labels for screen in screens if screen}


def _kpi_section(spec: _BrdSpec) -> list[str]:
    body: list[str] = []
    body.extend(f"- {kpi}" for kpi in spec.kpis)
    # NFRs read as measurable success targets in business terms.
    for nfr in spec.nfrs:
        if nfr.requirement:
            body.append(f"- **{nfr.category or 'target'}**: {nfr.requirement}")
    if not body:
        return []
    return _section("Success Metrics (KPIs)", body)


def _glossary_section(spec: _BrdSpec, slice_report: Any | None) -> list[str]:
    terms: list[str] = list(spec.business_glossary)
    digest = _digest(slice_report)
    if digest is not None:
        seen = {t.split(":", 1)[0].strip().lower() for t in terms}
        for vr in getattr(digest, "validation_rules", ()):
            entity = getattr(vr, "entity", "")
            if entity and entity.lower() not in seen:
                seen.add(entity.lower())
                terms.append(f"{entity}: a business entity governed by field-level policy.")
    if not terms:
        return []
    body = [f"- {term}" for term in terms]
    return _section("Glossary", body)


def _bullet_section(title: str, items: list[str]) -> list[str]:
    if not items:
        return []
    return _section(title, [f"- {item}" for item in items])


def _scope_section(spec: _BrdSpec, system_map: SystemMap | None) -> list[str]:
    contexts = _context_labels(system_map)
    in_scope = contexts or ([spec.title] if spec.title else [])
    if not in_scope:
        return []
    body = ["**In scope:**", ""]
    body.extend(f"- {c}" for c in in_scope)
    body.append("")
    body.append("**Out of scope:** capabilities not listed above (see the roadmap for sequencing).")
    return _section("Scope", body)


def _actor_lifecycle_section(slice_report: Any | None, system_map: SystemMap | None) -> list[str]:
    """The actor→capability + entity data-lifecycle diagram (4th business diagram)."""
    roles = _roles(slice_report)
    contexts = _context_labels(system_map) or []
    capability = contexts[0] if contexts else ""
    actor_caps = [(role, capability or "this capability") for role in roles]
    entity_states = _entity_states(slice_report)
    if not actor_caps and not entity_states:
        # Honest stub for a web context with no captured actors/entity states (#1456)
        # — e.g. petclinic has no role annotations — rather than silently omitting.
        if _is_web_slice(slice_report):
            return _section(
                "Actors & Data Lifecycle",
                _diagram_stub(
                    "Actors & lifecycle",
                    "captured roles/guards or entity view-states from "
                    "`/functional-map` (this context has endpoints but no actor or "
                    "lifecycle data was extracted)",
                ),
            )
        return []
    diagram = actor_lifecycle_diagram(actor_caps, entity_states, fenced=True)
    if not diagram:
        return []
    body = [
        "Who does what, and how the key business entities move through their lifecycle:",
        "",
        *_mermaid_block(diagram),
    ]
    return _section("Actors & Data Lifecycle", body)


def _entity_states(slice_report: Any | None) -> list[tuple[str, list[str]]]:
    """Business-entity state flows, from any webflow view-states in the slice.

    Deterministic + no fabrication: only real extracted webflow view-states become
    a lifecycle; no webflow → no entity-state view.
    """
    digest = _digest(slice_report)
    if digest is None:
        return []
    states: list[tuple[str, list[str]]] = []
    for wf in getattr(digest, "webflow_flows", ()):
        view_states = list(getattr(wf, "view_states", ()) or ())
        if len(view_states) >= _MIN_LIFECYCLE_STATES:
            states.append((getattr(wf, "flow_id", "") or "Entity", view_states))
    return states


def _runtime_screens_section(runtime_screens: list[dict[str, Any]] | None) -> list[str]:
    """§ Runtime Screens (observed) — embed the route screenshots captured by driving
    the running app (``/mod-runtime-explore``), as visual evidence in the BRD.

    Deterministic (the pipeline computed each ``image_path`` relative to the BRD): a
    screen with an ``image_path`` renders an inline image; one without renders a plain
    row. Absent/empty ``runtime_screens`` → no section (byte-identical BRD).
    """
    if not runtime_screens:
        return []
    body: list[str] = [
        heading("Runtime Screens (observed)", level=2),
        "",
        "Screens captured by driving the running application — visual evidence of the "
        "real UI, complementing the synthesized business narrative:",
        "",
    ]
    for scr in runtime_screens:
        title = str(scr.get("title") or scr.get("url") or scr.get("id") or "screen")
        url = str(scr.get("url") or "")
        img = str(scr.get("image_path") or "")
        caption = f"**{title}**" + (f" — `{url}`" if url else "")
        body.append(caption)
        body.append("")
        if img:
            body.append(f"![{title}]({img})")
            body.append("")
    return body


# ---------------------------------------------------------------------------
# Public: render_brd
# ---------------------------------------------------------------------------


def render_brd(
    spec: Any,
    *,
    slice_report: Any | None = None,
    system_map: SystemMap | None = None,
    target: Any | None = None,  # noqa: ARG001 — accepted for signature parity with render_spec
    generated_at: Any | None = None,  # noqa: ARG001 — reserved; the BRD carries no timestamp row
    runtime_screens: list[dict[str, Any]] | None = None,
) -> str:
    """Render a per-context Business Requirements Document (BRD.md).

    Produces a non-technical, visual BRD beside the technical spec, sharing the
    spec's ``R-`` / ``AC-`` / ``FR-`` traceability ids. Sections whose backing data
    is absent are omitted (a thin slice yields a sparser BRD — no fabrication).

    Args:
        spec: a ``SpecJudgment``-shaped object or dict (read model-agnostically).
            Its ``requirements`` supply the ``R-`` ids; its Track-C BRD fields
            (``business_narrative`` / ``kpis`` / ``business_glossary`` /
            ``assumptions`` / ``business_risks`` / harness diagram overrides)
            supply the business framing.
        slice_report: the per-context functional-map slice — its flows, roles,
            business rules, validation rules, form fields (with Track-D real
            option values), nav edges, and ``fr_statements`` (the ``FR-`` ids)
            populate the business sections + diagrams.
        system_map: the correlated system-map — its node domains give the bounded
            capabilities, and its Track-B ``entry_points`` let each business journey
            start at its inbound trigger.
        target: accepted for signature parity with :func:`render_spec` (unused).
        generated_at: reserved (the BRD carries no timestamp row); accepted so the
            pipeline can pass it uniformly.

    Returns:
        A Markdown string (no trailing newline).
    """
    s = _BrdSpec(spec)
    lines: list[str] = [heading(f"{s.title} — Business Requirements", level=1), ""]
    if s.context:
        lines.append(f"> {s.context}")
        lines.append("")

    lines += _capabilities_section(s, system_map)
    lines += _stakeholders_section(slice_report)
    lines += _business_processes_section(slice_report, system_map, s)
    lines += _runtime_screens_section(runtime_screens)
    lines += _business_rules_section(slice_report)
    lines += _functional_requirements_section(s, slice_report)
    lines += _data_content_section(slice_report)
    lines += _actor_lifecycle_section(slice_report, system_map)
    lines += _kpi_section(s)
    lines += _glossary_section(s, slice_report)
    lines += _bullet_section("Assumptions", s.assumptions)
    lines += _bullet_section("Risks & Business Impact", s.business_risks)
    lines += _scope_section(s, system_map)

    return "\n".join(lines).rstrip("\n")


__all__ = ["render_brd"]
