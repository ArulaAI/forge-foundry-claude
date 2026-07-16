from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from primitives.functional_map.shared.models import (
    AcceptanceCriterion,
    BusinessRule,
    FlowStep,
    FRStatement,
    FunctionalDigest,
    Role,
    RoleReconciliation,
    UserFlow,
    UserJourney,
)

# Hard cap on endpoints sent to the LLM per synthesis prompt
_MAX_ENDPOINTS = 200
# Approx token budget for the synthesis round (guidance, not enforced here)
_TOKEN_BUDGET = 80_000


class Synthesis(BaseModel):
    """Output schema the in-session harness must produce in synthesis.json."""

    model_config = ConfigDict(extra="forbid")
    flows: tuple[UserFlow, ...]
    journeys: tuple[UserJourney, ...]
    reconciliation: RoleReconciliation
    business_rules: tuple[BusinessRule, ...] = ()
    fr_statements: tuple[FRStatement, ...] = ()
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()


# DETAIL-tier caps (rich excerpts). INDEX tiers below are UNCAPPED (complete).
_MAX_FORM_FIELDS = 500
_MAX_BC = 200
_MAX_I18N = 200


# --- INDEX tiers: complete, one dense string per entity, nothing omitted ---


def _ep_index(digest: FunctionalDigest) -> list[str]:
    # "{id}|{method}|{path}|{handler}|{file}:{line}" — file:line carries citations
    return [f"{e.id}|{e.method}|{e.path}|{e.handler}|{e.file}:{e.line}" for e in digest.endpoints]


def _form_fields_index(digest: FunctionalDigest) -> list[str]:
    # "{endpoint_id}|{name}|{field_type}|{required}"
    return [f"{f.endpoint_id}|{f.name}|{f.field_type}|{f.required}" for f in digest.form_fields]


def _bc_index(digest: FunctionalDigest) -> list[str]:
    # one line per endpoint_id carrying every boolean/flag signal, so no
    # conditional/security/async/pagination/upload signal is lost past the cap
    return [
        "|".join(
            [
                bc.endpoint_id,
                bc.endpoint_kind,
                f"sec={int(bool(bc.http_security_roles))}",
                f"cond={int(bc.conditional)}",
                f"async={int(bc.async_endpoint)}",
                f"pag={int(bc.pagination)}",
                f"upload={int(bc.file_upload)}",
                f"notif={int(bool(bc.notification_hints))}",
                f"sess={int(bool(bc.session_reads or bc.session_writes))}",
                f"err={int(bool(bc.error_flows))}",
            ]
        )
        for bc in digest.business_contexts
    ]


def _i18n_index(digest: FunctionalDigest) -> list[str]:
    # "{key}|{locale}" — values are capped in the detail tier (can be huge)
    return [f"{e.key}|{e.locale}" for e in digest.i18n]


# --- DETAIL tiers: rich excerpts, relevance/cap-bounded top-N ---


def _ep_excerpt(digest: FunctionalDigest, limit: int = _MAX_ENDPOINTS) -> list[dict[str, Any]]:
    return [
        {
            "id": e.id,
            "path": e.path,
            "method": e.method,
            "handler": e.handler,
            "file": e.file,
            "line": e.line,
        }
        for e in digest.endpoints[:limit]
    ]


def _nav_excerpt(digest: FunctionalDigest) -> list[dict[str, Any]]:
    return [{"from": n.from_screen, "to": n.to_screen, "kind": n.kind} for n in digest.nav_edges]


def _form_fields_excerpt(digest: FunctionalDigest) -> list[dict[str, Any]]:
    return [
        {
            "endpoint_id": f.endpoint_id,
            "name": f.name,
            "field_type": f.field_type,
            "required": f.required,
            "constraints": list(f.constraints),
            "display_order": f.display_order,
        }
        for f in digest.form_fields[:_MAX_FORM_FIELDS]
    ]


def _validation_excerpt(digest: FunctionalDigest) -> list[dict[str, Any]]:
    return [
        {
            "entity": v.entity,
            "field": v.field,
            "constraint": v.constraint,
            "params": v.params,
            "message": v.message,
            "error_i18n_key": v.error_i18n_key,
            "cross_field": v.cross_field,
        }
        for v in digest.validation_rules[:300]
    ]


def _bc_excerpt(digest: FunctionalDigest) -> list[dict[str, Any]]:
    return [
        {
            "endpoint_id": bc.endpoint_id,
            "security_roles": list(bc.http_security_roles),
            "session_reads": list(bc.session_reads),
            "session_writes": list(bc.session_writes),
            "error_flows": list(bc.error_flows),
            "conditional": bc.conditional,
            "endpoint_kind": bc.endpoint_kind,
            "async_endpoint": bc.async_endpoint,
            "view_name": bc.view_name,
            "notification_hints": list(bc.notification_hints),
            "pagination": bc.pagination,
            "file_upload": bc.file_upload,
        }
        for bc in digest.business_contexts[:_MAX_BC]
    ]


def _i18n_excerpt(digest: FunctionalDigest) -> list[dict[str, Any]]:
    return [{"key": e.key, "value": e.value, "locale": e.locale} for e in digest.i18n[:_MAX_I18N]]


def _webflow_excerpt(digest: FunctionalDigest) -> list[dict[str, Any]]:
    return [
        {
            "flow_id": wf.flow_id,
            "view_states": list(wf.view_states),
            "transitions": [list(t) for t in wf.transitions],
        }
        for wf in digest.webflow_flows
    ]


def _truncation_info(total: int, shown: int) -> dict[str, int] | None:
    """Return truncation dict if total exceeds shown, else None."""
    if total > shown:
        return {"shown": shown, "total": total}
    return None


def build_synthesis_prompts(digest: FunctionalDigest) -> dict[str, Any]:
    """Build SP1-SP13 synthesis prompt items for the in-session harness.

    Shared excerpts (endpoints, nav_edges, roles, validations) are emitted
    ONCE in the top-level ``context`` block. Each item carries a ``uses`` list
    naming which context slices apply; it does NOT re-inline them.
    Token budget hint: ~80k tokens across all items.
    """
    # INDEX tiers — complete, uncapped, dense strings. Nothing is lost.
    ep_index = _ep_index(digest)
    ff_index = _form_fields_index(digest)
    bc_index = _bc_index(digest)
    i18n_index = _i18n_index(digest)

    # DETAIL tiers — rich, top-N capped.
    eps = _ep_excerpt(digest)
    nav = _nav_excerpt(digest)
    roles = digest.role_names()
    fields = _form_fields_excerpt(digest)
    validations = _validation_excerpt(digest)
    bcs = _bc_excerpt(digest)
    i18n = _i18n_excerpt(digest)
    webflows = _webflow_excerpt(digest)

    # Truncation markers mark the DETAIL tier only — the INDEX tiers are complete.
    truncated: dict[str, Any] = {}
    ep_info = _truncation_info(len(digest.endpoints), len(eps))
    if ep_info:
        truncated["endpoints_detail"] = ep_info
    ff_info = _truncation_info(len(digest.form_fields), len(fields))
    if ff_info:
        truncated["form_fields_detail"] = ff_info
    bc_info = _truncation_info(len(digest.business_contexts), len(bcs))
    if bc_info:
        truncated["business_contexts_detail"] = bc_info
    i18n_info = _truncation_info(len(digest.i18n), len(i18n))
    if i18n_info:
        truncated["i18n_detail"] = i18n_info
    nav_info = _truncation_info(len(digest.nav_edges), len(nav))
    if nav_info:
        truncated["nav_edges"] = nav_info
    val_info = _truncation_info(len(digest.validation_rules), len(validations))
    if val_info:
        truncated["validations"] = val_info

    # Derive candidate bounded-context slugs from path prefixes (the same signal
    # slice_functional_map uses for fallback clustering). Providing these lets the
    # harness assign a `context` slug to each FR it authors (#1595, Layer 1 accuracy).
    path_prefix_slugs: list[str] = sorted(
        {
            segs[0]
            for ep in digest.endpoints
            for segs in ([s for s in ep.path.split("/") if s and not s.startswith(("{", ":"))],)
            if segs
        }
    )

    # Shared context block — emitted ONCE; items reference via "uses".
    # *_index = COMPLETE map (dense strings); detail excerpts = rich top-N.
    context: dict[str, Any] = {
        "index_semantics": "index=complete (all entities); detail=top-N (see truncated)",
        "endpoints_index": ep_index,
        "form_fields_index": ff_index,
        "business_contexts_index": bc_index,
        "i18n_index": i18n_index,
        "endpoints": eps,
        "nav_edges": nav,
        "roles": roles,
        "validations": validations,
        "fr_context_assignment_instruction": (
            "When authoring any FR statement (fr_statements list), ALSO set the "
            "`context` field to the bounded-context slug that owns this requirement, "
            "IF determinable from the endpoint path, business_context, or domain signal "
            "visible in this prompt. Use one of the slugs in `available_context_slugs` "
            'if any match; leave `context` as empty string "" when uncertain or when '
            "this FR spans multiple contexts. This field is OPTIONAL — leaving it empty "
            "is always valid."
        ),
        "available_context_slugs": path_prefix_slugs,
    }
    if truncated:
        context["truncated"] = truncated

    items: list[dict[str, Any]] = [
        # SP1 — Role reconciliation
        {
            "id": "SP1",
            "kind": "roles",
            "instruction": (
                "Reconcile and name the code-observed roles. "
                "If a BRD role list is provided, flag code_only and brd_only mismatches. "
                "Return: { reconciled: [{name, source}], code_only: [], brd_only: [] }"
            ),
            "uses": ["roles"],
        },
        # SP2 — User flow assembly from nav graph
        {
            "id": "SP2",
            "kind": "flows",
            "instruction": (
                "Order endpoints into named user flows traversing the nav graph. "
                "Name each flow after a user goal (e.g. 'Submit Transfer'). "
                "Every step MUST cite endpoint_id + file:line as evidence. "
                "Include happy-path flows and role-scoped variants."
            ),
            "uses": ["endpoints", "nav_edges"],
        },
        # SP3 — User journeys per persona
        {
            "id": "SP3",
            "kind": "journeys",
            "instruction": (
                "Write goal-oriented user journeys spanning multiple flows, per persona. "
                "Each journey maps a persona's goal to flow_ids. "
                "Return 2-6 journeys."
            ),
            "uses": ["endpoints", "roles"],
        },
        # SP4 — Form field UX requirements
        {
            "id": "SP4",
            "kind": "form_field_requirements",
            "instruction": (
                "For each form (endpoint_id), synthesize the UX requirements: "
                "what fields are visible to each role, which are optional/required, "
                "what field types and order apply, and any conditional display rules. "
                "State as numbered FR statements (must/should/may)."
            ),
            "uses": ["roles"],
            "form_fields": fields,
            "business_contexts": bcs,
        },
        # SP5 — Validation rules as FR statements
        {
            "id": "SP5",
            "kind": "validation_requirements",
            "instruction": (
                "Convert validation constraints into functional requirements. "
                "For each field with constraints, write: "
                "'FR-NNN: The system MUST reject [field] when [constraint violated] "
                "and display [error message].' "
                "Include cross-field validations and i18n error message keys."
            ),
            "uses": ["validations"],
            "i18n_keys": [
                e["key"] for e in i18n if "error" in e["key"].lower() or "valid" in e["key"].lower()
            ],
        },
        # SP6 — Access control requirements
        {
            "id": "SP6",
            "kind": "access_control_requirements",
            "instruction": (
                "Synthesize access control requirements for each endpoint. "
                "State which roles can access each operation, what happens on "
                "unauthorized access, and any row-level security patterns. "
                "Format as FR statements."
            ),
            "uses": ["endpoints", "roles"],
            "business_contexts": [bc for bc in bcs if bc["security_roles"]],
        },
        # SP7 — Session management requirements
        {
            "id": "SP7",
            "kind": "session_requirements",
            "instruction": (
                "Identify session lifecycle requirements: what data is read from "
                "session, what data is written, where session is invalidated, "
                "and any concurrent session constraints. Format as FR statements."
            ),
            "business_contexts": [bc for bc in bcs if bc["session_reads"] or bc["session_writes"]],
        },
        # SP8 — Navigation and redirect requirements
        {
            "id": "SP8",
            "kind": "navigation_requirements",
            "instruction": (
                "Synthesize navigation flow requirements: after each action, "
                "where does the user land? What are the error-path redirects? "
                "What breadcrumbs or back-navigation must be preserved? "
                "Format as FR statements."
            ),
            "uses": ["nav_edges", "endpoints"],
        },
        # SP9 — Notification and async requirements
        {
            "id": "SP9",
            "kind": "notification_requirements",
            "instruction": (
                "Identify notification and async-processing requirements: "
                "what success/error messages are shown, what async operations "
                "trigger background processing, and what polling or push "
                "mechanisms are in use. Format as FR statements."
            ),
            "business_contexts": [
                bc for bc in bcs if bc["notification_hints"] or bc["async_endpoint"]
            ],
        },
        # SP10 — Webflow and wizard requirements
        {
            "id": "SP10",
            "kind": "webflow_requirements",
            "instruction": (
                "If Spring Webflow definitions are present, synthesize the "
                "multi-step wizard requirements: step sequence, back/forward "
                "navigation, decision branches, secured states, and data "
                "persistence between steps. Format as FR statements."
            ),
            "uses": ["endpoints"],
            "webflows": webflows,
        },
        # SP11 — Pagination and file upload requirements
        {
            "id": "SP11",
            "kind": "data_display_requirements",
            "instruction": (
                "Synthesize data display requirements: pagination (page size, "
                "sort, filter), file upload (size limits, types, progress), "
                "and any export/download capabilities. Format as FR statements."
            ),
            "business_contexts": [bc for bc in bcs if bc["pagination"] or bc["file_upload"]],
        },
        # SP12 — Business rules from conditionals and error flows
        {
            "id": "SP12",
            "kind": "business_rules",
            "instruction": (
                "Extract business rules from conditional guards, error flows, "
                "and domain constraints. Each rule should be a concise statement "
                "of what the system enforces (e.g. 'Transfer amount must be > 0 "
                "and ≤ account balance'). Cite step_ref (endpoint_id or field). "
                "Return as list of {step_ref, rule, source} objects."
            ),
            "uses": ["validations"],
            "business_contexts": [bc for bc in bcs if bc["conditional"] or bc["error_flows"]],
        },
        # SP13 — i18n and internationalization requirements
        {
            "id": "SP13",
            "kind": "i18n_requirements",
            "instruction": (
                "Synthesize internationalization requirements: what locales are "
                "supported, which UI strings require translation, what date/number "
                "formats are locale-sensitive, and what fallback locale behavior "
                "is expected. Format as FR statements."
            ),
            "i18n_entries": i18n,
        },
    ]

    # Filter out empty items that have no meaningful data
    filtered_items = [item for item in items if _item_has_data(item, context)]

    return {
        "schema_version": "2.0",
        "primitive": "functional-map",
        "token_budget_hint": _TOKEN_BUDGET,
        "context": context,
        "items": filtered_items,
    }


def _item_has_data(item: dict[str, Any], context: dict[str, Any] | None = None) -> bool:
    """Return True if the item has at least one non-empty data list or non-empty context ref."""
    # "uses" references shared context slices — only counts if the slice is non-empty
    for ref in item.get("uses", []):
        if context and context.get(ref):
            return True
    data_keys = {
        "form_fields",
        "validation_rules",
        "business_contexts",
        "i18n_entries",
        "webflows",
        "i18n_keys",
    }
    for key in data_keys:
        val = item.get(key)
        if val:  # non-empty list
            return True
    return False


def validate_synthesis(obj: dict[str, Any]) -> Synthesis:
    """Validate synthesis.json produced by the in-session harness."""
    try:
        flows = tuple(
            UserFlow(
                id=f["id"],
                name=f["name"],
                steps=tuple(FlowStep(**s) for s in f["steps"]),
                variant=f.get("variant", "happy"),
                variant_label=f.get("variant_label", ""),
            )
            for f in obj.get("flows", [])
        )
        journeys = tuple(UserJourney(**j) for j in obj.get("journeys", []))
        rec = obj.get("reconciliation", {"reconciled": []})
        reconciliation = RoleReconciliation(
            reconciled=tuple(Role(**r) for r in rec.get("reconciled", [])),
            code_only=tuple(rec.get("code_only", [])),
            brd_only=tuple(rec.get("brd_only", [])),
        )
        business_rules = tuple(BusinessRule(**r) for r in obj.get("business_rules", []))
        fr_statements = tuple(FRStatement(**f) for f in obj.get("fr_statements", []))
        acceptance_criteria = tuple(
            AcceptanceCriterion(**ac) for ac in obj.get("acceptance_criteria", [])
        )
    except (KeyError, TypeError, ValidationError) as exc:
        raise ValueError(f"malformed synthesis.json: {exc}") from exc
    return Synthesis(
        flows=flows,
        journeys=journeys,
        reconciliation=reconciliation,
        business_rules=business_rules,
        fr_statements=fr_statements,
        acceptance_criteria=acceptance_criteria,
    )
