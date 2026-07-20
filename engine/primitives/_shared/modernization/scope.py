"""Role-complete spec and ADR renderers (Layer 1).

:func:`render_spec` produces a self-contained, benchmark-depth page/domain
specification from a :class:`~primitives.modernization_scope.shared.models.SpecJudgment`
(the SP-B rich schema) and optional context: a
:class:`~primitives._shared.modernization.models.SystemMap` and a per-context
functional-map slice (SP-A). The spec is readable by any role — frontend,
backend, SDET, designer — and includes all four stable id classes (``R-``,
``AC-``, ``T-``, ``ADR-``), a **closed traceability matrix**, and a Definition
of Done.

**SP-C (#1383) — ~20 role-complete sections.** The renderer no longer echoes a
single synthesized AC/T per requirement (the old 1:1:1 lock). It renders the
ACTUAL multi-AC / multi-T structure the harness authored: it iterates the
top-level ``acceptance_criteria`` / ``test_scenarios`` lists and joins them to
requirements by their ``covers`` (``R-`` ids). The **Traceability Matrix** is a
forcing function — every ``R-`` shows its covering ``AC-`` and ``T-`` ids, and
an uncovered ``R-`` renders a visible ``⚠️ uncovered`` marker rather than being
hidden.

**No fabrication.** A section whose backing SP-B list is empty renders an honest
``> ⚠️`` stub (§8 Current API Surface → "no API surface detected" when neither
S40 nor the functional-map controller inventory is available; §9 Data-Model
Mapping → M1/SP-E) or is omitted — never invented content. A thin synthesis
still produces a valid (sparser) spec.

**SP3 (#1411) — Current vs. Target API.** §8 "Current API Surface" merges the
S40 ``api_endpoints`` (kind ``current``/unset) with the slice's functional-map
controller endpoint inventory (``slice_report.digest.endpoints``, the implicit
contract) — so §8 populates even for a server-rendered MVC app with no OpenAPI
doc. §8b "Target REST API (<backend label>)" renders **only** when a
:class:`TargetStack` is supplied AND the synthesis carries ``kind="target"``
endpoints — the resource-per-aggregate REST design the harness synthesized in the
target backend's idiom, each row tracing its originating ``from_controller``. No
target → §8b is absent entirely (byte-identical backward compatibility).

:func:`render_adr` produces a standalone ADR document (``ADR-NNNN-*.md`` shape)
for a single architecture decision. It cites the ``R-`` ids the decision affects
but does **not** enter a traceability matrix (ADRs are decision records, not
requirement contracts).

Both functions are pure and deterministic: no LLM, no clock, no filesystem.
They reuse :func:`~primitives._shared.modernization.render.assign_ids`,
:func:`~primitives._shared.modernization.render.heading`, and
:func:`~primitives._shared.modernization.render.md_table` from ``render.py`` —
no id-assignment logic is re-implemented here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from primitives._shared.modernization.render import (
    assign_ids,
    component_diagram,
    er_diagram,
    flowchart,
    heading,
    md_table,
)

if TYPE_CHECKING:
    from primitives._shared.modernization.models import SystemMap
    from primitives._shared.modernization.target_stack import TargetDimension, TargetStack

# Analyzer-attribution stubs for sections whose backing data is populated by a
# downstream sub-project (honest "requires X" markers — never fabricated content).
# §8 (Current API Surface) no longer hard-depends on S40 (SP3, #1411): it merges
# S40's api_endpoints with the functional-map controller inventory, so the honest
# stub is "no API surface detected" — an absence of both sources, not a missing S40.
_STUB_API = (
    "> ⚠️ no API surface detected — neither S40 api-contract-review nor a "
    "controller endpoint inventory was available."
)
_STUB_DATA = "> ⚠️ requires M1 mod-db-schema-analyze (SP-E) — no data-model mapping extracted yet."

# Coarse effort-band thresholds (requirement count → S/M/L), deterministic.
_EFFORT_L = 8
_EFFORT_M = 4

# The SpecJudgment fields render_spec reads, with their default-empty values.  The
# renderer is model-agnostic by design: it lives in the shared Layer-1 engine and
# must not import the ``modernization_scope`` primitive's ``SpecJudgment`` model
# (that would be a layering inversion + a cross-primitive ``shared`` import, QUAL-2).
# It accepts either a duck-typed ``SpecJudgment`` object (the pipeline path) or a
# plain dict (tests / callers), reading each field by attribute-or-key with the
# default below when absent.
_SPEC_DEFAULTS: dict[str, Any] = {
    "title": "Specification",
    "description": "",
    "context": "",
    "ui_mockup": "",  # SP5 #1413 — harness-authored target UI mockup (Mermaid)
    "requirements": (),
    "acceptance_criteria": (),
    "test_scenarios": (),
    "architecture_decisions": (),
    "data_field_mappings": (),
    "api_endpoints": (),
    "user_flows": (),
    "nfrs": (),
    "dod_extra": (),
}

# The nested-model fields each list element carries, with default-empty values, so a
# plain dict element reads identically to a duck-typed pydantic object.
_REQ_DEFAULTS = {
    "id": "",
    "statement": "",
    "scope": "",
    "constraint": "",
    "source": "",
    "rationale": "",
    "priority": "",
}
_AC_DEFAULTS = {
    "id": "",
    "given": "",
    "when": "",
    "then": "",
    "covers": (),
    "verified_by": (),
    "is_parity": False,
    "runtime_observed": False,  # runtime-explore — grounded in an observed live journey
}
_TS_DEFAULTS = {
    "id": "",
    "type": "",
    "scenario": "",
    "preconditions": "",
    "input": "",
    "expected": "",
    "covers": (),
    "is_parity_oracle": False,
}
_ADR_DEFAULTS = {
    "id": "",
    "title": "",
    "context": "",
    "decision": "",
    "consequences": (),
    "alternatives": (),
    "affects": (),
}
_MAP_DEFAULTS = {"legacy": "", "target": "", "type": "", "migration_notes": ""}
_EP_DEFAULTS = {
    "verb": "",
    "path": "",
    "request": "",
    "response": "",
    "status_codes": (),
    "validation": (),
    "security": (),
    "kind": "",  # "current" | "target"  (unset reads as current) — SP3 #1411
    "from_controller": "",  # target → originating controller form-flow — SP3 #1411
}
# The functional-map controller-endpoint view (§8 implicit contract, SP3 #1411).
# Mirrors the fields ``_current_api`` reads off a functional-map ``Endpoint``.
_CTRL_EP_DEFAULTS = {"method": "", "path": "", "handler": ""}
_FLOW_DEFAULTS = {
    "id": "",
    "name": "",
    "steps": (),
    "variant": "",
    "sequence_diagram": "",  # SP5 #1413 — harness-authored target request-path sequence
    "runtime_observed": False,  # runtime-explore — steps seen driving the running app
}
_NFR_DEFAULTS = {"id": "", "category": "", "requirement": "", "source": ""}


def _get(obj: Any, key: str, default: Any) -> Any:
    """Read *key* from a dict (by key) or an object (by attribute), else *default*."""
    val = obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)
    return default if val is None else val


def _view(element: Any, defaults: dict[str, Any]) -> SimpleNamespace:
    """A uniform attribute view over a nested list element (dict or pydantic model)."""
    return SimpleNamespace(**{k: _get(element, k, d) for k, d in defaults.items()})


def _views(items: Any, defaults: dict[str, Any]) -> list[SimpleNamespace]:
    return [_view(it, defaults) for it in (items or ())]


class _Spec:
    """A model-agnostic view over a SpecJudgment (duck-typed object or dict).

    Exposes exactly the fields :func:`render_spec` renders, each normalized to a
    uniform attribute shape so the section helpers never branch on dict-vs-model.
    """

    def __init__(self, item: Any) -> None:
        self.title: str = _get(item, "title", _SPEC_DEFAULTS["title"]) or "Specification"
        self.description: str = _get(item, "description", "")
        self.context: str = _get(item, "context", "")
        self.ui_mockup: str = _get(item, "ui_mockup", "")
        self.requirements = _views(_get(item, "requirements", ()), _REQ_DEFAULTS)
        self.acceptance_criteria = _views(_get(item, "acceptance_criteria", ()), _AC_DEFAULTS)
        self.test_scenarios = _views(_get(item, "test_scenarios", ()), _TS_DEFAULTS)
        self.architecture_decisions = _views(
            _get(item, "architecture_decisions", ()), _ADR_DEFAULTS
        )
        self.data_field_mappings = _views(_get(item, "data_field_mappings", ()), _MAP_DEFAULTS)
        self.api_endpoints = _views(_get(item, "api_endpoints", ()), _EP_DEFAULTS)
        self.user_flows = _views(_get(item, "user_flows", ()), _FLOW_DEFAULTS)
        self.nfrs = _views(_get(item, "nfrs", ()), _NFR_DEFAULTS)
        self.dod_extra: list[str] = list(_get(item, "dod_extra", ()) or ())


def _as_judgment(item: Any) -> _Spec:
    """Coerce *item* (a duck-typed SpecJudgment or a dict) into a :class:`_Spec` view."""
    return _Spec(item)


# ---------------------------------------------------------------------------
# Section helpers (each returns a list of lines, empty when nothing to render).
# ---------------------------------------------------------------------------


def _section(title: str, body: list[str]) -> list[str]:
    """A ``## title`` heading followed by *body* and a trailing blank line."""
    return [heading(title, level=2), "", *body, ""]


def _stub_section(title: str, stub: str) -> list[str]:
    """A section whose backing data is absent → an honest analyzer-attribution stub."""
    return _section(title, [stub])


def _mermaid_block(source: str) -> list[str]:
    """Wrap a harness-authored Mermaid *source* string in a fenced code block.

    Deterministic render (SP5, #1413): the AUTHORING is the harness's job; this
    only fences the already-authored string. The stripped source is embedded
    verbatim so the diagram round-trips exactly as authored.
    """
    return ["```mermaid", source.strip("\n"), "```", ""]


def _summary(j: _Spec) -> list[str]:
    body = [j.description] if j.description else [f"Role-complete specification for **{j.title}**."]
    return _section("Summary & Purpose", body)


def _business_context(j: _Spec, slice_report: Any | None) -> list[str]:
    body: list[str] = []
    if j.context:
        body.append(j.context)
        body.append("")
    roles: list[str] = []
    if slice_report is not None:
        roles = [r.name for r in slice_report.digest.roles]
    if roles:
        body.append("**Users / roles:** " + ", ".join(sorted(set(roles))) + ".")
    if not body:
        return []
    return _section("Business Context & Users", body)


def _glossary(slice_report: Any | None) -> list[str]:
    """Domain terms drawn from the slice's validation-rule entities (§3)."""
    if slice_report is None:
        return []
    terms: dict[str, str] = {}
    for vr in slice_report.digest.validation_rules:
        if vr.entity and vr.entity not in terms:
            terms[vr.entity] = f"Entity governed by field-level validation ({vr.field})."
    if not terms:
        return []
    rows = [[term, defn] for term, defn in sorted(terms.items())]
    return _section("Glossary", [md_table(["Term", "Definition"], rows)])


def _business_logic(j: _Spec) -> list[str]:
    """Numbered requirement statements with their ``file:line`` sources (§4)."""
    if not j.requirements:
        return []
    body: list[str] = []
    for n, req in enumerate(j.requirements, start=1):
        rid = req.id or f"R-{n:03d}"
        src = f" (`{req.source}`)" if req.source else ""
        body.append(f"{n}. **{rid}** {req.statement or '(no statement)'}{src}")
    return _section("Business Logic", body)


def _business_rules(j: _Spec, slice_report: Any | None) -> list[str]:
    """§5 — table: R- | rule | scope | constraint | source | rationale.

    Rows come from the authored requirements (the ``R-`` contract) enriched with
    the slice's extracted validation rules (SP-A carry-forward). Empty ⇒ omitted
    (no fabrication).
    """
    rows: list[list[str]] = []
    for req in j.requirements:
        rows.append(
            [
                req.id,
                req.statement,
                req.scope,
                req.constraint,
                f"`{req.source}`" if req.source else "—",
                req.rationale,
            ]
        )
    # Carry-forward the slice's extracted business rules (SP-E, #1385): each now
    # carries a stable ``BR-NNN`` id, an ``enforced_at`` (``file:line``) derived from
    # its ``step_ref``, and an optional harness-authored ``rationale`` — so §5 cites
    # each grounded rule by source rather than fabricating one.
    if slice_report is not None:
        for br in slice_report.business_rules:
            rows.append(
                [
                    br.id or "—",
                    br.rule,
                    "—",
                    "—",
                    f"`{br.enforced_at}`" if br.enforced_at else "—",
                    br.rationale or "extracted (functional-map)",
                ]
            )
    # Carry-forward the slice's extracted validation rules as additional grounded
    # rows (they have no R- id of their own — mark the cell "—" so they read as
    # supporting evidence, not a fabricated requirement).
    if slice_report is not None:
        for vr in slice_report.digest.validation_rules:
            rows.append(
                [
                    "—",
                    vr.message or f"{vr.field}: {vr.constraint}",
                    vr.entity,
                    vr.constraint,
                    "—",
                    "extracted (functional-map)",
                ]
            )
    if not rows:
        return []
    header = ["R-", "Rule", "Scope", "Constraint", "Source", "Rationale"]
    return _section("Business Rules & Validation", [md_table(header, rows)])


def _user_flows(j: _Spec, slice_report: Any | None, target: Any | None = None) -> list[str]:
    """§6 — user & navigation flows (prose from ``user_flows`` + slice nav-edges).

    When *target* is set AND a flow carries a harness-authored ``sequence_diagram``
    (SP5, #1413), the target request-path sequence renders after that flow's prose
    as a fenced Mermaid ``sequenceDiagram`` block. No target / empty string → the
    sequence is omitted (no fabrication); the prose flow renders regardless.
    """
    body: list[str] = []
    for flow in j.user_flows:
        name = flow.name or flow.id or "Flow"
        variant = f" _({flow.variant})_" if flow.variant else ""
        observed = " _(runtime-observed)_" if flow.runtime_observed else ""
        body.append(f"**{name}**{variant}{observed}")
        for step in flow.steps:
            body.append(f"1. {step}")
        body.append("")
        if target is not None and flow.sequence_diagram:
            body.append("_Target request path:_")
            body.append("")
            body.extend(_mermaid_block(flow.sequence_diagram))
    if slice_report is not None and slice_report.digest.nav_edges:
        nav_edges = slice_report.digest.nav_edges
        body.append("**Navigation edges (extracted):**")
        body.append("")
        for edge in nav_edges:
            body.append(f"- `{edge.from_screen}` → `{edge.to_screen}` ({edge.kind})")
        body.append("")
        # SP4 (#1412): the per-slice navigation graph, from this slice's nav edges,
        # via the shared render.flowchart emitter (no re-implementation). Nodes are
        # the screens (label = the real path); edges carry the nav kind.
        nav_nodes = {
            screen: (screen, screen)
            for edge in nav_edges
            for screen in (edge.from_screen, edge.to_screen)
        }
        nav_arrows = [(edge.from_screen, edge.to_screen, edge.kind) for edge in nav_edges]
        body.append("**Navigation graph:**")
        body.append("")
        body.append(flowchart(list(nav_nodes.values()), nav_arrows, direction="LR"))
        body.append("")
    if not body:
        return []
    return _section("User & Navigation Flows", body)


def _ux_notes(j: _Spec, slice_report: Any | None, target: Any | None = None) -> list[str]:
    """§7 — UX/UI design notes: form fields, states, a11y/i18n (from the slice).

    When *target* is set AND the harness authored a ``ui_mockup`` (SP5, #1413),
    the target UI screen renders first as a fenced Mermaid block (designed in the
    target frontend's idiom), followed by the structured field-layout inventory.
    No target / empty ``ui_mockup`` → the mockup is omitted (no fabrication); the
    field inventory is structural and renders regardless.
    """
    body: list[str] = []
    if target is not None and j.ui_mockup:
        label = _frontend_label(target)
        heading_text = f"Target UI Mockup ({label})" if label else "Target UI Mockup"
        body.append(f"**{heading_text}**")
        body.append("")
        body.extend(_mermaid_block(j.ui_mockup))
    if slice_report is not None and slice_report.digest.form_fields:
        # Track D (#1426): inline the real select/dropdown option list AND retain
        # the field's `file:line` provenance (from its endpoint) — the reference
        # stays, the content is added. A field with no options renders `—` (no
        # fabrication).
        ep_source = {
            ep.id: f"{ep.file}:{ep.line}" for ep in slice_report.digest.endpoints if ep.file
        }
        rows = [
            [
                ff.name,
                ff.field_type,
                "yes" if ff.required else "no",
                ", ".join(ff.constraints) if ff.constraints else "—",
                ", ".join(ff.options) if ff.options else "—",
                ff.label or "—",
                f"`{ep_source[ff.endpoint_id]}`" if ep_source.get(ff.endpoint_id) else "—",
            ]
            for ff in slice_report.digest.form_fields
        ]
        body.append(
            md_table(
                ["Field", "Type", "Required", "Constraints", "Options", "Label", "Source"],
                rows,
            )
        )
        body.append("")
    if slice_report is not None and slice_report.digest.i18n:
        locales = sorted({e.locale for e in slice_report.digest.i18n})
        body.append(
            f"**Internationalisation:** {len(slice_report.digest.i18n)} message key(s) "
            f"across locale(s): {', '.join(locales)}."
        )
        body.append("")
    # NFRs authored on the spec (a11y / i18n categories).
    ux_nfrs = [n for n in j.nfrs if n.category in {"a11y", "i18n"}]
    body.extend(f"- **{nfr.category}**: {nfr.requirement}" for nfr in ux_nfrs)
    if not body:
        return []
    return _section("UX/UI Design Notes", body)


def _controller_endpoints(slice_report: Any | None) -> list[SimpleNamespace]:
    """The functional-map controller endpoint inventory for §8 (SP3, #1411).

    Reads ``slice_report.digest.endpoints`` — the implicit current API contract
    a server-rendered MVC app exposes even with no OpenAPI document. Returns an
    empty list when no slice or no endpoints are present.
    """
    if slice_report is None:
        return []
    digest = getattr(slice_report, "digest", None)
    endpoints = getattr(digest, "endpoints", ()) if digest is not None else ()
    return _views(endpoints, _CTRL_EP_DEFAULTS)


def _current_api(j: _Spec, slice_report: Any | None) -> list[str]:
    """§8 — Current API Surface (SP3, #1411).

    Merges two sources of the app's *existing* contract:

    * the S40 ``api-contract-review`` extraction (``j.api_endpoints`` whose
      ``kind`` is ``"current"`` or unset), and
    * the functional-map controller endpoint inventory
      (``slice_report.digest.endpoints`` — the implicit contract).

    So §8 populates even for a server-rendered MVC app with no OpenAPI doc (the
    #1404 relief — no hard S40 dependency). Empty BOTH → an honest
    ``⚠️ no API surface detected`` stub.
    """
    # (a) S40 endpoints — anything NOT explicitly kind="target" is a current row.
    rows: list[list[str]] = [
        [
            ep.verb,
            ep.path,
            ep.from_controller or "—",
            ep.request or "—",
            ep.response or "—",
            ", ".join(ep.status_codes) if ep.status_codes else "—",
            ", ".join(ep.validation) if ep.validation else "—",
            ", ".join(ep.security) if ep.security else "—",
        ]
        for ep in j.api_endpoints
        if ep.kind != "target"
    ]
    # (b) functional-map controller inventory — the implicit contract.
    rows += [
        [
            ep.method,
            ep.path,
            ep.handler or "—",
            "—",
            "—",
            "—",
            "—",
            "—",
        ]
        for ep in _controller_endpoints(slice_report)
    ]
    if not rows:
        return _stub_section("Current API Surface", _STUB_API)
    header = [
        "Verb",
        "Path",
        "From (controller)",
        "Request",
        "Response",
        "Status",
        "Validation R-",
        "Security",
    ]
    return _section("Current API Surface", [md_table(header, rows)])


def _backend_label(target: Any) -> str:
    """The render label for the target's backend dimension (SP3, #1411).

    Returns the ``label`` of the ``backend`` dimension (e.g. ``"Spring Boot 3.3"``),
    falling back to ``"<key>=<value>"`` when the registry supplied no label, or an
    empty string when the target declares no backend dimension.
    """
    for dim in getattr(target, "dimensions", ()) or ():
        if getattr(dim, "key", "") == "backend":
            return dim.label or f"{dim.key}={dim.value}"
    return ""


def _frontend_label(target: Any) -> str:
    """The render label for the target's frontend dimension (SP5, #1413).

    Returns the ``label`` of the ``frontend`` dimension (e.g. ``"Angular 18"``),
    falling back to ``"<key>=<value>"`` when the registry supplied no label, or an
    empty string when the target declares no frontend dimension. Used to title the
    §7 target UI mockup in the target frontend's idiom.
    """
    for dim in getattr(target, "dimensions", ()) or ():
        if getattr(dim, "key", "") == "frontend":
            return dim.label or f"{dim.key}={dim.value}"
    return ""


def _target_api(j: _Spec, target: Any | None) -> list[str]:
    """§8b — Target REST API, synthesized under the target stack (SP3, #1411).

    Renders **only** when *target* is present AND the synthesis carries
    ``kind="target"`` endpoints — the resource-per-aggregate REST design the
    harness synthesized in the target backend's idiom. Absent either, returns
    ``[]`` so §8b is omitted entirely (no target → byte-identical behavior; a
    target with no synthesized endpoints → no fabrication).
    """
    if target is None:
        return []
    target_eps = [ep for ep in j.api_endpoints if ep.kind == "target"]
    if not target_eps:
        return []
    rows = [
        [
            ep.verb,
            ep.path,
            ep.from_controller or "—",
            ep.request or "—",
            ep.response or "—",
            ", ".join(ep.status_codes) if ep.status_codes else "—",
            ", ".join(ep.validation) if ep.validation else "—",
            ", ".join(ep.security) if ep.security else "—",
        ]
        for ep in target_eps
    ]
    header = [
        "Verb",
        "Path",
        "From (controller)",
        "Request",
        "Response",
        "Status",
        "Validation R-",
        "Security",
    ]
    label = _backend_label(target)
    title = f"Target REST API ({label})" if label else "Target REST API"
    return _section(title, [md_table(header, rows)])


def _er_entities_relationships(
    mappings: list[SimpleNamespace],
) -> tuple[list[tuple[str, list[tuple[str, str]]]], list[tuple[str, str, str, str]]]:
    """Derive ER entities + FK relationships from the §9 legacy-column mappings.

    Each ``DataFieldMapping.legacy`` is ``entity.column`` (SP3, #1411): group the
    mappings by their legacy entity (table), and give each entity its columns as
    ``(type, column)`` attribute pairs (mermaid ER's ``type name`` order). A
    relationship is inferred when a table carries a column named ``<other>_id``
    (or ``<other>id``) whose ``<other>`` matches another entity's name — the
    classic FK shape — rendered as a ``}o--||`` (many-to-one) edge. Deterministic:
    entities and relationships are emitted in first-seen order.

    Returns ``([], [])`` when no mapping carries an ``entity.column`` legacy value
    (so §9 omits the ER block rather than fabricating one).
    """
    entities: dict[str, list[tuple[str, str]]] = {}
    for m in mappings:
        table, sep, column = str(m.legacy).partition(".")
        if not sep or not table or not column:
            continue  # not an entity.column shape — nothing to model
        entities.setdefault(table, [])
        entities[table].append((m.type or "column", column))

    entity_list: list[tuple[str, list[tuple[str, str]]]] = [
        (t, cols) for t, cols in entities.items()
    ]

    relationships: list[tuple[str, str, str, str]] = []
    seen_rel: set[tuple[str, str]] = set()
    for table, cols in entities.items():
        for _type, column in cols:
            base = (
                column[:-3]
                if column.endswith("_id")
                else (column[:-2] if column.endswith("id") else "")
            )
            if not base or base == table:
                continue
            # Match the referenced entity by exact or trivially-pluralized name.
            for other in entities:
                if other == table:
                    continue
                if other in {base, f"{base}s", f"{base}es"} and (table, other) not in seen_rel:
                    relationships.append((table, "}o--||", other, "references"))
                    seen_rel.add((table, other))
                    break

    return entity_list, relationships


def _data_model(j: _Spec) -> list[str]:
    """§9 — data-model mapping (from ``data_field_mappings``; SP-E stub when empty).

    When the mappings resolve to ``entity.column`` shapes, an ``erDiagram`` (SP4,
    #1412) is appended under the table — entities + inferred FK relationships from
    :func:`_er_entities_relationships`. No ``entity.column`` mappings → no ER block
    (omit, no fabrication).
    """
    if not j.data_field_mappings:
        return _stub_section("Data-Model Mapping", _STUB_DATA)
    rows = [
        [m.legacy, m.target, m.type or "—", m.migration_notes or "—"] for m in j.data_field_mappings
    ]
    header = ["Legacy Column", "Target Field", "Type", "Migration Notes"]
    body: list[str] = [md_table(header, rows)]

    entities, relationships = _er_entities_relationships(j.data_field_mappings)
    if entities:
        body.append("")
        body.append("**Entity-relationship diagram:**")
        body.append("")
        body.append(er_diagram(entities, relationships))
    return _section("Data-Model Mapping", body)


def _contracts(j: _Spec) -> list[str]:
    """§10 — Contracts (DTOs): the request/response shapes named by the API section."""
    if not j.api_endpoints:
        return []
    shapes: dict[str, None] = {}
    for ep in j.api_endpoints:
        for shape in (ep.request, ep.response):
            if shape:
                shapes.setdefault(shape, None)
    if not shapes:
        return []
    body = [f"- `{shape}`" for shape in shapes]
    return _section("Contracts (DTOs)", body)


def _test_spec(j: _Spec) -> list[str]:
    """§11 — test specification table (T- | type | scenario | pre | expected | covers R-)."""
    if not j.test_scenarios:
        return []
    rows: list[list[str]] = []
    for n, ts in enumerate(j.test_scenarios, start=1):
        tid = ts.id or f"T-{n:03d}"
        scenario = ts.scenario
        if ts.is_parity_oracle:
            scenario = f"{scenario} _(parity oracle)_"
        rows.append(
            [
                tid,
                ts.type or "—",
                scenario,
                ts.preconditions or "—",
                ts.expected or "—",
                ", ".join(ts.covers) if ts.covers else "⚠️ covers none",
            ]
        )
    header = ["T-", "Type", "Scenario", "Preconditions", "Expected", "Covers R-"]
    return _section("Test Specification", [md_table(header, rows)])


def _acceptance_criteria(j: _Spec) -> list[str]:
    """§12 — acceptance criteria in Given/When/Then, each citing its R- and T- ids.

    Iterates the top-level ``acceptance_criteria`` list (multi-AC per R- is the
    whole point — no positional 1:1 pairing).
    """
    if not j.acceptance_criteria:
        return []
    body: list[str] = []
    for n, ac in enumerate(j.acceptance_criteria, start=1):
        acid = ac.id or f"AC-{n:03d}"
        covers = ", ".join(ac.covers) if ac.covers else "⚠️ covers no R-"
        verified = ", ".join(ac.verified_by) if ac.verified_by else "⚠️ no T-"
        parity = " _(parity)_" if ac.is_parity else ""
        observed = " _(runtime-observed)_" if ac.runtime_observed else ""
        gwt = f"Given {ac.given}, when {ac.when}, then {ac.then}."
        body.append(f"**{acid}**{parity}{observed} {gwt} Covers {covers}. Verified by {verified}.")
        body.append("")
    return _section("Acceptance Criteria", body)


def _definition_of_done(j: _Spec, r_ids: list[str], t_ids: list[str]) -> list[str]:
    body = [
        f"- All {len(r_ids)} requirement(s) ({', '.join(r_ids) or '—'}) have "
        "≥1 acceptance criterion and ≥1 passing test.",
        f"- All {len(t_ids)} test(s) ({', '.join(t_ids) or '—'}) pass in CI.",
        "- No open P0/P1 defects.",
        "- Code reviewed and approved.",
        "- Documentation updated.",
    ]
    body.extend(f"- {extra}" for extra in j.dod_extra)
    return _section("Definition of Done", body)


def _uat_criteria(j: _Spec) -> list[str]:
    """§14 — UAT criteria: the parity-flagged acceptance criteria (user-facing behaviour)."""
    parity_acs = [ac for ac in j.acceptance_criteria if ac.is_parity]
    if not parity_acs:
        return []
    body = [
        f"- **{ac.id or 'AC'}**: a user confirms — given {ac.given}, when {ac.when}, "
        f"then {ac.then}."
        for ac in parity_acs
    ]
    return _section("UAT Criteria", body)


def _traceability_matrix(j: _Spec, r_ids: list[str], slice_report: Any | None = None) -> list[str]:
    """§15 — the forcing function: every R- shows covering AC- and T- ids.

    An R- with no covering AC or no covering T renders a visible ``⚠️ uncovered``
    marker in the empty cell — the gap is surfaced, never hidden.

    When *slice_report* carries ``fr_statements``, a second block of FR- rows is
    appended after the R- block (additive — existing R-/AC-/T- rows are unchanged).
    Each FR- row shows the covering functional-map AC ids (from
    ``slice_report.acceptance_criteria`` keyed on ``fr_id``) in the AC column and
    ``—`` in the Tests column (FR- ids are not tracked at test-scenario level).
    A slice with no ``fr_statements`` adds no FR- rows — no fabrication.
    """
    # Build R- → covering AC- / T- ids by joining on the `covers` lists.
    ac_by_r: dict[str, list[str]] = {rid: [] for rid in r_ids}
    for ac in j.acceptance_criteria:
        for rid in ac.covers:
            ac_by_r.setdefault(rid, []).append(ac.id or "AC-?")
    t_by_r: dict[str, list[str]] = {rid: [] for rid in r_ids}
    for ts in j.test_scenarios:
        for rid in ts.covers:
            t_by_r.setdefault(rid, []).append(ts.id or "T-?")

    rows: list[list[str]] = []
    for rid in r_ids:
        acs = ac_by_r.get(rid) or []
        tests = t_by_r.get(rid) or []
        ac_cell = ", ".join(acs) if acs else "⚠️ uncovered"
        t_cell = ", ".join(tests) if tests else "⚠️ uncovered"
        rows.append([rid, ac_cell, t_cell])

    # FR- rows — additive block surfacing functional-map FR ids (BRD/spec parity,
    # #1436). Uses the slice's acceptance_criteria keyed on fr_id so the same
    # FR-→AC linkage the functional-map produced appears here.
    fr_statements = list(getattr(slice_report, "fr_statements", ()) or ())
    if fr_statements:
        # Build FR- id → covering functional-map AC ids.
        ac_by_fr: dict[str, list[str]] = {}
        for ac in getattr(slice_report, "acceptance_criteria", ()) or ():
            frid = getattr(ac, "fr_id", None) or (ac.get("fr_id") if isinstance(ac, dict) else None)
            acid = getattr(ac, "id", None) or (ac.get("id") if isinstance(ac, dict) else None)
            if frid and acid:
                ac_by_fr.setdefault(frid, []).append(acid)
        for fr in fr_statements:
            frid = getattr(fr, "id", None) or (fr.get("id") if isinstance(fr, dict) else None)
            if not frid:
                continue
            fr_acs = ac_by_fr.get(frid) or []
            ac_cell = ", ".join(fr_acs) if fr_acs else "—"
            rows.append([frid, ac_cell, "—"])

    header = ["Requirement", "Acceptance Criteria", "Tests"]
    return _section("Traceability Matrix", [md_table(header, rows)])


def _architecture_decisions(j: _Spec) -> list[str]:
    """§16 — inline structured ADRs (from ``architecture_decisions``; affects R-)."""
    if not j.architecture_decisions:
        return []
    body: list[str] = []
    for adr in j.architecture_decisions:
        title = adr.title or adr.id or "Decision"
        body.append(heading(f"{adr.id}: {title}" if adr.id else title, level=3))
        body.append("")
        if adr.affects:
            body.append(f"**Affects:** {', '.join(adr.affects)}")
            body.append("")
        if adr.context:
            body.append(f"**Context:** {adr.context}")
            body.append("")
        if adr.decision:
            body.append(f"**Decision:** {adr.decision}")
            body.append("")
        if adr.alternatives:
            body.append("**Alternatives:** " + "; ".join(adr.alternatives))
            body.append("")
        if adr.consequences:
            body.append("**Consequences:** " + "; ".join(adr.consequences))
            body.append("")
    return _section("Architecture Decisions", body)


def _nfr_section(j: _Spec) -> list[str]:
    """Non-functional requirements not already surfaced in the UX section."""
    other = [n for n in j.nfrs if n.category not in {"a11y", "i18n"}]
    if not other:
        return []
    rows = [
        [n.id or "—", n.category, n.requirement, f"`{n.source}`" if n.source else "—"]
        for n in other
    ]
    return _section(
        "Non-Functional Requirements",
        [md_table(["Id", "Category", "Requirement", "Source"], rows)],
    )


#: Max distinct labeled edges rendered per (src, dst) node pair in the C4 diagram
#: before collapsing the remainder into a single "+N more" edge — keeps a spec whose
#: app node reads 20 tables from becoming an unreadable wall of parallel edges (#1661).
_MAX_EDGES_PER_PAIR = 6


def _component_edges(edges: Any) -> list[tuple[str, str, str]]:
    """Build labeled, de-duplicated C4 edges from the system-map edges (#1661).

    Each edge is labeled by its ``evidence`` (the ``data:<table>`` / interface join
    key) rather than the generic ``kind`` (which is ``"interface"`` for EVERY data
    edge), so a spec's component diagram shows one **named** edge per table instead
    of N identical unlabeled ``interface`` lines. Identical (src, dst, label) tuples
    are de-duplicated; when a single (src, dst) pair carries more than
    :data:`_MAX_EDGES_PER_PAIR` distinct edges, the surplus collapses into one
    ``"+N more"`` edge to keep the diagram legible.
    """
    by_pair: dict[tuple[str, str], list[str]] = {}
    for edge in edges:
        label = (getattr(edge, "evidence", "") or edge.kind or "depends on").strip()
        labels = by_pair.setdefault((edge.src, edge.dst), [])
        if label not in labels:
            labels.append(label)
    out: list[tuple[str, str, str]] = []
    for (src, dst), labels in by_pair.items():
        shown = labels[:_MAX_EDGES_PER_PAIR]
        out.extend((src, dst, lbl) for lbl in shown)
        if len(labels) > _MAX_EDGES_PER_PAIR:
            out.append((src, dst, f"+{len(labels) - _MAX_EDGES_PER_PAIR} more"))
    return out


def _dependencies(system_map: SystemMap | None, target: Any | None = None) -> list[str]:
    """§17 — dependencies & sequencing: the system-map components this spec touches.

    Appends a C4-style component diagram (SP4, #1412) from the system-map's nodes
    (as bounded contexts) + edges (dependencies) via
    :func:`~primitives._shared.modernization.render.component_diagram`. When a
    :class:`TargetStack` is supplied, its frontend/backend/database dimensions add
    target-component nodes (e.g. ``Angular SPA`` → ``Spring Boot Service`` →
    ``Oracle Database``). No system-map → the whole section (diagram included) omits.
    """
    if system_map is None or not system_map.nodes:
        return []
    rows = [
        [node.id, node.kind or "—", ", ".join(node.domains) if node.domains else "—"]
        for node in system_map.nodes
    ]
    body = [
        "The following components correlate to this bounded context:",
        "",
        md_table(["Component", "Kind", "Domain"], rows),
    ]

    contexts = [
        (node.id, f"{node.id} ({node.kind})" if node.kind else node.id) for node in system_map.nodes
    ]
    edges = _component_edges(system_map.edges)
    # Track B (#1427): thread the typed inbound entry points into the C4 diagram
    # as a distinct inbound tier. Empty → no tier (byte-identical to prior output).
    ep_tuples = [(ep.name, ep.kind, ep.target) for ep in getattr(system_map, "entry_points", [])]
    body.append("")
    body.append("**Component diagram (C4):**")
    body.append("")
    body.append(component_diagram(contexts, edges, target, entry_points=ep_tuples))

    return _section("Dependencies & Sequencing", body)


def _has_web_surface(system_map: SystemMap | None) -> bool:
    """True when *system_map* has a node that fronts a **real** inbound web surface.

    The scope pipeline ALWAYS threads a ``#ui`` node (from the functional-map unit)
    and a ``#api`` node (from the api-contract unit) — even for a pure batch/DB
    system whose functional-map is empty — so their mere presence is NOT a web
    signal. A genuine web tier is one where the ``#ui``/``#api`` node **formed a
    bounded context**: it carries a non-empty ``domains`` (the synthetic ``Web``
    label the functional-map adapter adds only when there are real endpoints, #1455,
    or a role/guard business domain). A batch/DB/CLI system's ``#ui`` node has empty
    ``domains`` → this returns ``False`` and the empty Entry Points section stays
    omitted, byte-identically to prior behavior.
    """
    if system_map is None:
        return False
    for node in getattr(system_map, "nodes", []):
        nid = str(getattr(node, "id", "")).lower()
        if (nid.endswith("#ui") or nid.endswith("#api")) and getattr(node, "domains", None):
            return True
    return False


def _entry_points_section(system_map: SystemMap | None) -> list[str]:
    """§ Entry Points (Track B, #1427/#1455) — the typed inbound triggers.

    Renders a table (``kind | name | trigger | target | file:line``) of every
    captured entry point (HTTP route, scheduled/cron job, CLI command, message
    consumer, integration/webhook/SOAP endpoint) with its real trigger value.
    Plus a per-entry-point **sequence-start** line — the batch job / message
    consumer / HTTP request is the initiating actor of its flow.

    When there are **no** captured entry points but the system's web tier FORMED a
    bounded context (a ``.#ui`` / ``.#api`` node with a non-empty ``domains`` — see
    :func:`_has_web_surface`), an honest ``⚠️ requires`` stub renders instead of
    silently omitting (#1455 — the silent-degradation the issue calls out; a partial
    deliverable must read as partial). A system with no real web surface and no entry
    points (incl. a batch/DB system whose structural ``#ui``/``#api`` nodes carry
    empty ``domains``) omits the section entirely (byte-identical to prior behavior).
    No system-map → omit.
    """
    if system_map is None:
        return []
    eps = list(getattr(system_map, "entry_points", []))
    if not eps:
        if _has_web_surface(system_map):
            return _section(
                "Entry Points",
                [
                    "⚠️ requires captured inbound triggers — this system has a web/HTTP "
                    "surface but no entry points were threaded onto the system-map "
                    "(functional-map endpoints or discovery entry points). Re-run "
                    "`/functional-map` (or the app-analysis discovery leg) so the inbound "
                    "routes are captured, then re-run `/mod-modernization-scope`.",
                ],
            )
        return []
    eps = sorted(eps, key=lambda e: (e.kind, e.name))
    rows = [
        [ep.kind or "—", ep.name, ep.trigger or "—", ep.target or "—", ep.source or "—"]
        for ep in eps
    ]
    body = [
        "The system's inbound triggers — every entry point across all classes "
        "(http/rest, batch/scheduled, cli, messaging, integration/webhook/soap):",
        "",
        md_table(["Kind", "Name", "Trigger", "Target", "Source"], rows),
        "",
        "Each entry point is the initiating actor of its flow (the sequence-start):",
    ]
    body.extend(
        f"- **{ep.name}** ({ep.kind or 'entry point'}) → `{ep.target or 'system'}`"
        + (f" — trigger `{ep.trigger}`" if ep.trigger else "")
        for ep in eps
    )
    return _section("Entry Points", body)


def _complexity(j: _Spec) -> list[str]:
    """§18 — complexity & effort: a coarse count-based estimate (deterministic)."""
    n_r = len(j.requirements)
    n_ac = len(j.acceptance_criteria)
    n_t = len(j.test_scenarios)
    if not (n_r or n_ac or n_t):
        return []
    # Coarse S/M/L from the requirement count (deterministic, no wall-clock).
    band = "L" if n_r >= _EFFORT_L else "M" if n_r >= _EFFORT_M else "S"
    body = [
        f"- Requirements: {n_r}",
        f"- Acceptance criteria: {n_ac}",
        f"- Test scenarios: {n_t}",
        f"- Coarse effort band: **{band}**",
    ]
    return _section("Complexity & Effort", body)


def _migration_notes(j: _Spec, r_ids: list[str]) -> list[str]:
    """§19 — migration notes & open questions.

    Surfaces the honest gaps: an R- with no covering AC/T, and the absent
    downstream analyzers (API/data-mapping) whose sections are still stubs.
    """
    body: list[str] = []
    ac_covered = {rid for ac in j.acceptance_criteria for rid in ac.covers}
    t_covered = {rid for ts in j.test_scenarios for rid in ts.covers}
    uncovered = [rid for rid in r_ids if rid not in ac_covered or rid not in t_covered]
    if uncovered:
        body.append(
            "> ⚠️ Open: the following requirement(s) lack full AC/T coverage — "
            f"{', '.join(uncovered)}."
        )
        body.append("")
    # SP3 (#1411): §8 Current API Surface no longer hard-depends on S40 (it merges
    # the functional-map controller inventory), so its absence is surfaced by §8's
    # own honest "no API surface detected" stub, not a migration-notes open item.
    if not j.data_field_mappings:
        body.append("- Data-model mapping pending M1 mod-db-schema-analyze (SP-E).")
    if not body:
        return []
    return _section("Migration Notes & Open Questions", body)


# ---------------------------------------------------------------------------
# Public: render_spec
# ---------------------------------------------------------------------------


def render_spec(
    item: Any,
    system_map: SystemMap | None = None,
    *,
    slice_report: Any | None = None,
    target: Any | None = None,
    id_width: int = 3,  # noqa: ARG001 — retained for signature stability; ids come from item
    adr_start: int = 1,  # noqa: ARG001 — retained for signature stability
) -> str:
    """Render a benchmark-depth, role-complete page/domain specification.

    Produces the ~20-section role-complete spec set from a
    ``SpecJudgment``-shaped input (the SP-B rich schema), with optional
    *system_map* and per-context functional-map *slice_report* (SP-A) grounding
    the business-rule, flow, and UX sections. Sections whose backing data is
    absent are omitted or render an honest ``> ⚠️`` stub — never fabricated.

    ``item`` is read model-agnostically (by attribute or key), so a duck-typed
    ``SpecJudgment`` object (the pipeline path) and a plain dict (tests) render
    identically; the shared Layer-1 renderer never imports the
    ``modernization_scope`` primitive's model (QUAL-2 layering).

    Args:
        item: a ``SpecJudgment``-shaped object or a dict. The
            authored ``R-`` ids come from ``item.requirements``; ``AC-`` / ``T-``
            ids from the top-level ``acceptance_criteria`` / ``test_scenarios``
            lists (multi-AC / multi-T per requirement — no 1:1:1 lock).
        system_map: optional :class:`SystemMap` — its nodes populate the
            §17 Dependencies & Sequencing section.
        slice_report: optional per-context functional-map slice (SP-A) — its
            validation rules, nav-edges, form-fields, and i18n populate
            §3 / §5 / §6 / §7, its ``digest.endpoints`` controller inventory
            merges into §8 Current API Surface (SP3, #1411), and its
            ``fr_statements`` / ``acceptance_criteria`` surface FR- ids as
            additional rows in §15 Traceability Matrix (#1436).
        target: optional :class:`~primitives._shared.modernization.target_stack.TargetStack`
            (SP3, #1411). When present AND the synthesis carries ``kind="target"``
            endpoints, §8b Target REST API renders under the backend label; absent
            it (or with no target endpoints), §8b is omitted entirely — the
            no-target render is byte-identical to prior behavior.
        id_width: retained for signature stability (ids are authored on *item*).
        adr_start: retained for signature stability.

    Returns:
        A Markdown string (no trailing newline).
    """
    j = _as_judgment(item)
    r_ids = [req.id for req in j.requirements if req.id]
    t_ids = [ts.id for ts in j.test_scenarios if ts.id]

    lines: list[str] = [heading(j.title or "Specification", level=1), ""]

    lines += _summary(j)
    lines += _business_context(j, slice_report)
    lines += _glossary(slice_report)
    lines += _business_logic(j)
    lines += _business_rules(j, slice_report)
    lines += _user_flows(j, slice_report, target)
    lines += _ux_notes(j, slice_report, target)
    lines += _current_api(j, slice_report)
    lines += _target_api(j, target)
    lines += _data_model(j)
    lines += _contracts(j)
    lines += _test_spec(j)
    lines += _acceptance_criteria(j)
    lines += _definition_of_done(j, r_ids, t_ids)
    lines += _uat_criteria(j)
    lines += _traceability_matrix(j, r_ids, slice_report)
    lines += _architecture_decisions(j)
    lines += _nfr_section(j)
    lines += _dependencies(system_map, target)
    lines += _entry_points_section(system_map)
    lines += _complexity(j)
    lines += _migration_notes(j, r_ids)

    return "\n".join(lines).rstrip("\n")


# ---------------------------------------------------------------------------
# Public: target_stack_adr_decision (WS1, #1449)
# ---------------------------------------------------------------------------

# Dimension keys rendered in the target-stack ADR title/decision, in this order.
_TARGET_ADR_KEY_ORDER = ("backend", "frontend", "database", "cloud", "messaging", "api_style")


def _target_dim_label(dim: TargetDimension) -> str:
    """The honest render label for one target dimension.

    A registry-matched dimension uses its ``label`` annotated with ``version_source``
    honesty ("explicit" | "latest-known"). A ``registry_matched=False`` dimension has
    no registry label, so its raw ``value`` is carried VERBATIM and flagged
    ``(unverified)`` — consistent with :func:`parse_target`'s warn-not-fail contract:
    nothing is dropped or fabricated.
    """
    if dim.registry_matched:
        base = dim.label or f"{dim.value}{(' ' + dim.version) if dim.version else ''}"
        return f"{base} ({dim.version_source})" if dim.version_source else base
    # Not in the registry — carry the value verbatim, note it is unverified.
    verbatim = f"{dim.value}{('@' + dim.version) if dim.version else ''}"
    return f"{verbatim} (unverified — not in the bundled registry)"


def _ordered_target_dims(target: TargetStack) -> list[TargetDimension]:
    """Target dimensions in a stable, human-first order (backend → frontend → …).

    Keys in :data:`_TARGET_ADR_KEY_ORDER` render first (in that order); any other
    key follows in declaration order. Deterministic — no set iteration.
    """
    known = [d for key in _TARGET_ADR_KEY_ORDER for d in target.dimensions if d.key == key]
    rest = [d for d in target.dimensions if d.key not in _TARGET_ADR_KEY_ORDER]
    return known + rest


def target_stack_adr_decision(target: TargetStack, *, legacy_stack: str = "") -> dict[str, Any]:
    """Build the standalone target-stack ADR ``decision`` dict from a resolved target.

    Records the declared :class:`~primitives._shared.modernization.target_stack.TargetStack`
    as a first-class architectural decision — the whole-system counterpart to the
    per-spec §8b Target REST API. The dict is fed straight to :func:`render_adr`
    (this function does NOT render — it only assembles the recognised ``decision``
    keys: ``title``, ``status``, ``context``, ``decision``, ``consequences``,
    ``alternatives``).

    **Deterministic + honest.** Built purely from the ``TargetStack`` (no wall-clock,
    no LLM — the target is a *fact*). A ``registry_matched=False`` dimension is carried
    verbatim and flagged ``(unverified)``; nothing is dropped or invented — consistent
    with :func:`parse_target`'s warn-not-fail contract.

    Args:
        target: the resolved target stack (must carry ≥1 dimension — guard the call
            site on ``target is not None and target.dimensions``).
        legacy_stack: the legacy stack being strangled (e.g. the app-digest flavor
            label "Spring MVC + JSP on PostgreSQL"). When empty, a generic
            "the current legacy stack" phrasing is used — no fabricated specifics.

    Returns:
        A ``decision`` dict for :func:`render_adr`.
    """
    dims = _ordered_target_dims(target)
    labels = [_target_dim_label(d) for d in dims]
    # The title names the primary dimensions (backend + frontend + database) in a
    # human-first order; a shorter, label-only form (no version_source annotation).
    title_bits = [
        (d.label or f"{d.value}{(' ' + d.version) if d.version else ''}")
        if d.registry_matched
        else f"{d.value}{('@' + d.version) if d.version else ''}"
        for d in dims
    ]
    title = (
        "Adopt the target stack: " + " + ".join(title_bits)
        if title_bits
        else ("Adopt the declared target stack")
    )

    legacy = legacy_stack.strip() or "the current legacy stack"

    context = (
        f"The modernization strangles {legacy} toward a declared target stack. "
        "This decision records the resolved target dimensions so every downstream "
        "spec, ADR, and roadmap wave reads as extraction *toward* that target — not "
        "just descriptive dependency order. The target was resolved offline against "
        "the bundled target registry (never a live lookup); dimensions carried "
        "verbatim (unverified) were not found in the registry and must be confirmed "
        "at implementation time."
    )

    decision = "Adopt the following resolved target dimensions:\n" + "\n".join(
        f"- **{d.key}:** {label}" for d, label in zip(dims, labels, strict=True)
    )

    consequences = [
        "Extraction follows a strangler-fig migration toward the target stack, one "
        "bounded context at a time; the legacy and target stacks run side-by-side "
        "during the transition.",
        "The target REST surface (§8b per spec) replaces the legacy server-rendered "
        "or in-process interfaces as each context is migrated.",
        "Each dimension carried verbatim (unverified) must be confirmed against the "
        "team's supported-technology list before its extraction wave begins.",
        "Roadmap wave sequencing and per-spec target APIs stay in sync with this "
        "recorded target — a target change is a new ADR, not a silent drift.",
    ]

    alternatives = [
        "Retain the legacy stack (no modernization) — rejected: the modernization "
        "goal is to strangle the legacy stack toward the declared target.",
        "Adopt a different target per bounded context — rejected as the default: a "
        "single declared target keeps the roadmap and per-spec target APIs coherent "
        "(a per-context deviation warrants its own ADR).",
    ]

    return {
        "title": title,
        "status": "Proposed",
        "context": context,
        "decision": decision,
        "consequences": consequences,
        "alternatives": alternatives,
    }


# ---------------------------------------------------------------------------
# Public: render_adr
# ---------------------------------------------------------------------------


def render_adr(
    decision: dict[str, Any],
    *,
    adr_number: int = 1,
    id_width: int = 3,
    date: str = "",
) -> str:
    """Render a standalone Architecture Decision Record (ADR).

    The output follows the standard Nygard ADR format extended with an
    **Alternatives** section and explicit ``R-`` id citations.  ADRs are
    standalone decision records and do **not** enter the traceability matrix
    (that is the contract of :func:`render_spec`).

    Args:
        decision: Structured ADR data.  Recognised keys:

            * ``"title"`` (str) — short decision title (default
              ``"Architecture Decision"``).
            * ``"status"`` (str) — e.g. ``"Accepted"`` / ``"Proposed"`` /
              ``"Deprecated"`` (default ``"Proposed"``).
            * ``"context"`` (str) — background that motivated the decision.
            * ``"decision"`` (str) — the decision itself.
            * ``"consequences"`` (list[str] | str) — list of consequence
              bullets, or a plain string.
            * ``"alternatives"`` (list[str] | str) — list of alternatives
              considered, or a plain string.
            * ``"affected_requirements"`` (list[str]) — ``R-`` ids the decision
              addresses (e.g. ``["R-001", "R-002"]``).

        adr_number: The ADR sequence number used to build the ``ADR-NNNN``
            id (default ``1``).
        id_width: Zero-pad width for the ``ADR-`` id (default ``3``).
        date: ISO-8601 date string for the Date field.  When empty or absent
            the Date row is omitted from the metadata table so no placeholder
            literal leaks into the rendered document.

    Returns:
        A Markdown string (no trailing newline).
    """
    # Build the ADR id via assign_ids so we never duplicate that logic.
    adr_id = assign_ids("ADR", 1, width=id_width, start=adr_number)[0]

    title = decision.get("title", "Architecture Decision")
    status = decision.get("status", "Proposed")
    context_text = decision.get("context", "")
    decision_text = decision.get("decision", "")
    consequences_raw = decision.get("consequences", [])
    alternatives_raw = decision.get("alternatives", [])
    affected_reqs: list[str] = list(decision.get("affected_requirements") or [])

    def _to_bullets(value: list[str] | str) -> list[str]:
        if isinstance(value, str):
            return [value] if value else []
        return list(value)

    consequences = _to_bullets(consequences_raw)
    alternatives = _to_bullets(alternatives_raw)

    lines: list[str] = []

    # ── Title ────────────────────────────────────────────────────────────────
    lines.append(heading(f"{adr_id}: {title}", level=1))
    lines.append("")

    # ── Metadata table ───────────────────────────────────────────────────────
    # Omit the Date row when no timestamp is available so no placeholder literal
    # ("YYYY-MM-DD") leaks into the rendered document.
    meta_rows: list[list[str]] = [["Status", status]]
    if date:
        meta_rows.append(["Date", date])
    if affected_reqs:
        meta_rows.append(["Affected Requirements", ", ".join(affected_reqs)])
    lines.append(md_table(["Field", "Value"], meta_rows))
    lines.append("")

    # ── Context ──────────────────────────────────────────────────────────────
    lines.append(heading("Context", level=2))
    lines.append("")
    lines.append(context_text if context_text else "_No context provided._")
    lines.append("")

    # ── Decision ─────────────────────────────────────────────────────────────
    lines.append(heading("Decision", level=2))
    lines.append("")
    lines.append(decision_text if decision_text else "_No decision text provided._")
    lines.append("")

    # ── Consequences ─────────────────────────────────────────────────────────
    lines.append(heading("Consequences", level=2))
    lines.append("")
    if consequences:
        for bullet in consequences:
            lines.append(f"- {bullet}")
    else:
        lines.append("_None listed._")
    lines.append("")

    # ── Alternatives ─────────────────────────────────────────────────────────
    lines.append(heading("Alternatives", level=2))
    lines.append("")
    if alternatives:
        for bullet in alternatives:
            lines.append(f"- {bullet}")
    else:
        lines.append("_None listed._")
    lines.append("")

    return "\n".join(lines).rstrip("\n")
