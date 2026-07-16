from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from primitives._shared.modernization.iface import http_iface

HttpMethod = Literal[
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "ANY",
    "WS",
    "QUERY",
    "MUTATION",
    "SUBSCRIPTION",
    "RPC",
    "RPC_STREAM",
    "SYSTEM",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Endpoint(_Frozen):
    id: str
    path: str
    method: HttpMethod
    handler: str
    file: str
    line: int
    #: Human-readable page / feature label for this endpoint, derived deterministically
    #: from static signals (Spring MVC view name, named route, or handler label) during
    #: Phase B extraction (#1606). Conservative: populated only when a clean label is
    #: derivable; empty string otherwise. F2 (runtime_explore reconciliation) may
    #: overlay a runtime :attr:`Screen.title` in a later pass — F1 sources only static
    #: signals. Backward-compatible: defaults to ``""`` so existing reports that carry
    #: no view/route signal are byte-additive (the field is present but empty).
    display_name: str = ""


class Guard(_Frozen):
    endpoint_id: str
    kind: Literal["auth_required", "role_required", "policy", "public"]
    roles: tuple[str, ...] = ()
    file: str
    line: int


class Role(_Frozen):
    name: str
    source: Literal["code", "brd", "both"] = "code"


class Capability(_Frozen):
    id: str  # CAP-NNN
    action: str
    endpoint_id: str
    roles: tuple[str, ...] = ()


class NavEdge(_Frozen):
    from_screen: str
    to_screen: str
    kind: Literal["link", "redirect", "router", "ajax"]
    file: str
    line: int


class FormField(_Frozen):
    endpoint_id: str
    name: str
    field_type: str  # "text", "select", "checkbox", "hidden", "file", "date", etc.
    required: bool
    constraints: tuple[str, ...]
    label: str = ""
    conditional_role: str = ""
    conditional_on: str = ""
    conditional_value: str = ""
    default_value: str = ""
    read_only: bool = False
    display_order: int = 0
    # Real select/dropdown/radio option list (Track D, #1426) — the actual
    # literal option values (`<option>`/`<form:option>`) for enumerated fields.
    # Default-empty for backward-compat (functional_map is golden-locked); a
    # non-enumerated field (text/checkbox/…) carries no options.
    options: tuple[str, ...] = ()
    source: Literal[
        "jsp",
        "thymeleaf",
        "freemarker",
        "react",
        "angular",
        "vue",
        "annotation",
        "javascript",
        "hidden",
        "graphql",
        "proto",
    ] = "annotation"


class ValidationRule(_Frozen):
    entity: str
    field: str
    constraint: str
    params: dict[str, str]
    message: str = ""
    error_i18n_key: str = ""
    cross_field: bool = False


class I18nEntry(_Frozen):
    key: str
    value: str
    locale: str = "default"


class WebflowDefinition(_Frozen):
    flow_id: str
    view_states: tuple[str, ...]
    transitions: tuple[tuple[str, str, str], ...]  # (from_state, on_event, to_state)


class BusinessContext(_Frozen):
    endpoint_id: str
    http_security_roles: tuple[str, ...]
    form_fields: tuple[FormField, ...]
    validation_rules: tuple[ValidationRule, ...]
    model_attributes: tuple[str, ...]
    session_reads: tuple[str, ...]
    session_writes: tuple[str, ...]
    error_flows: tuple[str, ...]
    conditional: bool = False
    conditional_flag: str = ""
    async_endpoint: bool = False
    api_description: str = ""
    endpoint_kind: Literal[
        "mvc", "rest", "ws", "graphql", "grpc", "system", "soap", "integration"
    ] = "rest"
    protocol: Literal["http", "ws", "graphql", "grpc", "soap"] = "http"
    source: Literal[
        "annotation",
        "xml",
        "webflow",
        "gateway",
        "openapi",
        "wsdl",
        "proto",
        "struts",
        "jsf",
    ] = "annotation"
    view_name: str = ""
    notification_hints: tuple[str, ...] = ()
    pagination: bool = False
    file_upload: bool = False


class FlowStep(_Frozen):
    order: int
    endpoint_id: str
    description: str
    file: str
    line: int
    data_flow: str = ""
    variant: Literal["happy", "error", "role_scoped"] = "happy"
    variant_label: str = ""


class UserFlow(_Frozen):
    id: str  # FLOW-NNN
    name: str
    steps: tuple[FlowStep, ...]
    variant: Literal["happy", "error", "role_scoped"] = "happy"
    variant_label: str = ""


class UserJourney(_Frozen):
    id: str  # JRN-NNN
    persona: str
    goal: str
    flow_ids: tuple[str, ...]
    narrative: str


class BusinessRule(_Frozen):
    step_ref: str
    rule: str
    source: Literal[
        "validation", "guard", "jpa", "session", "conditional", "error_flow", "inferred"
    ] = "inferred"
    #: Stable ``BR-NNN`` id, assigned deterministically at synthesis-merge time
    #: (:func:`~primitives.functional_map.shared.synthesis_prompts.thread_business_rules`).
    #: Defaults empty so a rule authored without an id — or a ``--no-synthesis`` run —
    #: stays valid and standalone functional-map output remains byte-identical.
    id: str = ""
    #: WHY the rule exists — harness-interpreted in-session (SP-D). The deterministic
    #: extractor usually cannot supply it, so it defaults empty; population is downstream.
    rationale: str = ""
    #: ``file:line`` where the rule fires, derived deterministically from the
    #: ``step_ref``→endpoint mapping at synthesis-merge time
    #: (:func:`~primitives.functional_map.shared.synthesis_prompts.thread_business_rules`).
    #: Empty when the ``step_ref`` resolves to no endpoint (a flow-step ref with no
    #: locatable endpoint, or an unknown id). §5 of the migration spec cites it as source.
    enforced_at: str = ""


class FRStatement(_Frozen):
    id: str  # FR-NNN
    statement: str
    priority: Literal["must", "should", "may"] = "must"
    fr_type: Literal[
        "validation",
        "display_rule",
        "access_control",
        "audit",
        "notification",
        "pagination",
        "file_upload",
        "concurrency",
        "session",
        "async",
        "functional",
    ] = "functional"
    source_evidence: tuple[str, ...] = ()
    #: Bounded-context slug the in-session harness may assign during synthesis (Layer 1
    #: accuracy, #1595). When non-empty the slicer routes this FR to that context
    #: directly, bypassing the source_evidence → endpoint → context join. Leave as ""
    #: when the owning context is not determinable from the whole-app synthesis prompt;
    #: the slicer's backstop then attempts the source_evidence join and, failing that,
    #: collects the FR in needs_context_assignment rather than silently dropping it.
    #: Backward-compatible: defaults to "" so existing fixtures/JSON still validate.
    context: str = ""


class AcceptanceCriterion(_Frozen):
    fr_id: str
    id: str  # {fr_id}-AC{N}
    given: str
    when: str
    then: str


class RoleReconciliation(_Frozen):
    reconciled: tuple[Role, ...]
    code_only: tuple[str, ...] = ()
    brd_only: tuple[str, ...] = ()


class FunctionalDigest(_Frozen):
    endpoints: tuple[Endpoint, ...]
    guards: tuple[Guard, ...]
    roles: tuple[Role, ...]
    capabilities: tuple[Capability, ...]
    nav_edges: tuple[NavEdge, ...]
    form_fields: tuple[FormField, ...] = ()
    validation_rules: tuple[ValidationRule, ...] = ()
    business_contexts: tuple[BusinessContext, ...] = ()
    i18n: tuple[I18nEntry, ...] = ()
    webflow_flows: tuple[WebflowDefinition, ...] = ()

    def role_names(self) -> list[str]:
        return sorted({r.name for r in self.roles})


class FunctionalMapReport(_Frozen):
    digest: FunctionalDigest
    reconciliation: RoleReconciliation
    flows: tuple[UserFlow, ...] = ()
    journeys: tuple[UserJourney, ...] = ()
    business_rules: tuple[BusinessRule, ...] = ()
    fr_statements: tuple[FRStatement, ...] = ()
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    synthesized: bool = False
    #: FRs that could not be routed to any bounded-context slice (#1595 no-drop
    #: invariant). Only populated on the whole-app report when slicing is active
    #: and one or more FRs have neither a harness-assigned context nor source_evidence
    #: that resolves to a context. Empty for per-slice reports (slices carry only the
    #: FRs that DID route to them); empty for whole-app runs with no slicing.
    needs_context_assignment: tuple[FRStatement, ...] = ()


def _enforced_at_for(
    step_ref: str,
    endpoint_loc: dict[str, str],
    flowstep_loc: dict[str, str],
) -> str:
    """Resolve a business rule's ``step_ref`` to a ``file:line`` (empty when unknown).

    The ``step_ref`` a harness authors is one of:

    * an endpoint id (``EP-002``) — resolves to that endpoint's ``file:line``;
    * a canonical ``http_iface`` string or raw path already mapped in
      ``endpoint_loc`` (an endpoint key) — resolves the same way;
    * a flow-step ref (``FLOW-001.1``) — resolves to that flow step's own
      ``file:line`` (the most precise "where the rule fires").

    An unresolvable ref yields ``""`` — never a fabricated location.
    """
    if step_ref in flowstep_loc:
        return flowstep_loc[step_ref]
    return endpoint_loc.get(step_ref, "")


def thread_business_rules(
    business_rules: tuple[BusinessRule, ...],
    endpoints: tuple[Endpoint, ...],
    flows: tuple[UserFlow, ...] = (),
) -> tuple[BusinessRule, ...]:
    """Assign deterministic ``BR-NNN`` ids and derive ``enforced_at`` per rule.

    Rides ALONGSIDE the harness-authored ``step_ref`` / ``rule`` / ``source``
    (and any harness-supplied ``rationale``) — it never overwrites them:

    * ``id`` — a stable, sequential ``BR-001`` / ``BR-002`` … assigned in the
      rules' existing order (mirrors the ``FR-NNN`` / ``EP-NNN`` id conventions).
      A rule that already carries an ``id`` keeps it (idempotent re-threading).
    * ``enforced_at`` — the ``file:line`` the rule's ``step_ref`` resolves to via
      the existing endpoint / flow-step mapping (see :func:`_enforced_at_for`);
      empty when the ref resolves to nothing. A rule that already carries an
      ``enforced_at`` keeps it.
    * ``rationale`` — passed through untouched (harness-interpreted; SP-D).

    Pure and deterministic (no wall-clock / LLM); safe to call on an empty tuple
    (returns ``()``). When ``business_rules`` is empty the output is byte-identical
    to the input, so a ``--no-synthesis`` run stays golden-locked.
    """
    if not business_rules:
        return business_rules

    endpoint_loc: dict[str, str] = {}
    for ep in endpoints:
        loc = f"{ep.file}:{ep.line}"
        # Every identifier the endpoint can be matched on maps to its location.
        for key in (ep.id, http_iface(ep.method, ep.path), ep.path):
            if key and key not in endpoint_loc:
                endpoint_loc[key] = loc

    flowstep_loc: dict[str, str] = {}
    for flow in flows:
        for n, step in enumerate(flow.steps, start=1):
            flowstep_loc[f"{flow.id}.{n}"] = f"{step.file}:{step.line}"

    threaded: list[BusinessRule] = []
    for i, br in enumerate(business_rules, start=1):
        updates: dict[str, str] = {}
        if not br.id:
            updates["id"] = f"BR-{i:03d}"
        if not br.enforced_at:
            enforced = _enforced_at_for(br.step_ref, endpoint_loc, flowstep_loc)
            if enforced:
                updates["enforced_at"] = enforced
        threaded.append(br.model_copy(update=updates) if updates else br)
    return tuple(threaded)
