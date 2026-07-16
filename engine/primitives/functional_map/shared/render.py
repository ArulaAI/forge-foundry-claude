from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from primitives.functional_map.shared.access_matrix import build_matrix

if TYPE_CHECKING:
    from primitives.functional_map.shared.models import (
        AcceptanceCriterion,
        BusinessRule,
        FRStatement,
        FunctionalDigest,
        FunctionalMapReport,
        I18nEntry,
        NavEdge,
        UserFlow,
    )

_STUB = "⚠️ UNVERIFIED — run the synthesis round (omit --no-synthesis) to populate."
_I18N_VALUE_MAX_LEN = 60

_FR_TYPE_ORDER = [
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
]


def _mermaid_id(name: str) -> str:
    """Convert a screen/node name to a Mermaid-safe identifier."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _render_nav_graph(nav_edges: tuple[NavEdge, ...]) -> str:
    if not nav_edges:
        return "_No navigation edges extracted._"
    lines = ["```mermaid", "flowchart LR"]
    for edge in nav_edges:
        from_id = _mermaid_id(edge.from_screen)
        to_id = _mermaid_id(edge.to_screen)
        if edge.kind == "ajax":
            arrow = "-.->"
            label = f'|"{edge.kind}"|'
        else:
            arrow = "-->"
            label = f'|"{edge.kind}"|' if edge.kind != "link" else ""
        lines.append(f"  {from_id} {arrow} {label}{to_id}")
    lines.append("```")
    return "\n".join(lines)


def _render_flows(flows: tuple[UserFlow, ...], *, synthesized: bool) -> str:
    if not synthesized or not flows:
        return _STUB + "\n"
    parts: list[str] = []
    # Group by variant
    by_variant: dict[str, list[Any]] = defaultdict(list)
    for f in flows:
        by_variant[f.variant].append(f)

    variant_headers = {
        "happy": "### Happy Path",
        "error": "### Error Flows",
        "role_scoped": "### Role-Scoped Flows",
    }
    for variant in ("happy", "error", "role_scoped"):
        group = by_variant.get(variant, [])
        if not group:
            continue
        parts.append(variant_headers[variant] + "\n")
        for f in group:
            label = f" ({f.variant_label})" if f.variant_label else ""
            parts.append(f"#### {f.id} — {f.name}{label}")
            for s in f.steps:
                data_flow = f" — _{s.data_flow}_" if s.data_flow else ""
                parts.append(
                    f"{s.order}. {s.description} (`{s.endpoint_id}` @ {s.file}:{s.line}){data_flow}"
                )
            parts.append("")
    return "\n".join(parts)


def _render_fr_statements(fr_statements: tuple[FRStatement, ...]) -> str:
    if not fr_statements:
        return "_No data available._"
    grouped: dict[str, list[Any]] = defaultdict(list)
    for fr in fr_statements:
        grouped[fr.fr_type].append(fr)
    parts: list[str] = []
    for fr_type in _FR_TYPE_ORDER:
        items = grouped.get(fr_type, [])
        if not items:
            continue
        parts.append(f"\n### {fr_type.replace('_', ' ').title()}\n")
        parts.append("| ID | Priority | Statement | Evidence |")
        parts.append("|----|:--------:|-----------|----------|")
        for fr in sorted(items, key=lambda x: {"must": 0, "should": 1, "may": 2}[x.priority]):
            evidence = ", ".join(fr.source_evidence[:5]) if fr.source_evidence else "—"
            parts.append(f"| {fr.id} | **{fr.priority}** | {fr.statement} | {evidence} |")
    return "\n".join(parts)


def _render_acceptance_criteria(
    acceptance_criteria: tuple[AcceptanceCriterion, ...],
    fr_statements: tuple[FRStatement, ...] = (),
) -> str:
    if not acceptance_criteria:
        return "_No data available._"
    # Group by fr_id
    by_fr: dict[str, list[Any]] = defaultdict(list)
    for ac in acceptance_criteria:
        by_fr[ac.fr_id].append(ac)
    parts: list[str] = []
    # Coverage summary — every FR is expected to carry ≥1 AC. Surface any gaps so
    # under-coverage is visible in the map instead of counted by hand (§4 uses the
    # same ⚠️ pattern for ungated capabilities).
    if fr_statements:
        fr_ids = [fr.id for fr in fr_statements]
        uncovered = [fid for fid in fr_ids if fid not in by_fr]
        parts.append(f"**{len(fr_ids) - len(uncovered)} of {len(fr_ids)} FRs covered.**\n")
        if uncovered:
            parts.append(f"> ⚠️ FRs with no acceptance criteria: {', '.join(sorted(uncovered))}\n")
    for fr_id in sorted(by_fr):
        parts.append(f"\n### {fr_id}\n")
        for ac in by_fr[fr_id]:
            parts.append("```gherkin")
            parts.append(f"# {ac.id} ({ac.fr_id})")
            parts.append(f"Given {ac.given}")
            parts.append(f"When {ac.when}")
            parts.append(f"Then {ac.then}")
            parts.append("```\n")
    return "\n".join(parts)


def _render_business_rules(business_rules: tuple[BusinessRule, ...]) -> str:
    if not business_rules:
        return "_No data available._"
    parts: list[str] = []
    parts.append("| Step Ref | Rule | Source |")
    parts.append("|----------|------|--------|")
    for br in business_rules:
        parts.append(f"| {br.step_ref} | {br.rule} | {br.source} |")
    return "\n".join(parts)


def _render_form_fields(digest: FunctionalDigest) -> str:
    if not digest.form_fields:
        return "_No data available._"
    # Group by endpoint_id
    by_ep: dict[str, list[Any]] = defaultdict(list)
    for ff in digest.form_fields:
        by_ep[ff.endpoint_id].append(ff)
    parts: list[str] = []
    for ep_id in sorted(by_ep):
        # Omit the sub-heading when the group key is empty (fields carry no endpoint_id).
        if ep_id:
            parts.append(f"\n### {ep_id}\n")
        parts.append("| Name | Type | Required | Constraints | Conditional |")
        parts.append("|------|------|:--------:|-------------|-------------|")
        for ff in by_ep[ep_id]:
            constraints = "; ".join(ff.constraints) if ff.constraints else "—"
            conditional = ff.conditional_on if ff.conditional_on else "—"
            req = "✓" if ff.required else ""
            parts.append(f"| {ff.name} | {ff.field_type} | {req} | {constraints} | {conditional} |")
    return "\n".join(parts)


def _render_i18n_coverage(i18n: tuple[I18nEntry, ...]) -> str:
    if not i18n:
        return "_No i18n entries extracted._"
    parts: list[str] = []
    parts.append("| Key | Value | Locale |")
    parts.append("|-----|-------|--------|")
    for entry in i18n[:50]:
        value_truncated = (
            entry.value[:_I18N_VALUE_MAX_LEN] + "…"
            if len(entry.value) > _I18N_VALUE_MAX_LEN
            else entry.value
        )
        parts.append(f"| `{entry.key}` | {value_truncated} | {entry.locale} |")
    return "\n".join(parts)


def render_markdown(report: FunctionalMapReport) -> str:
    d = report.digest
    matrix = build_matrix(d)
    out: list[str] = ["# Functional Requirements Map\n"]

    # §1 Executive Summary
    out.append("## 1. Executive Summary\n")
    n_eps = len(d.endpoints)
    n_roles = len(d.roles)
    n_caps = len(d.capabilities)
    out.append(f"**{n_eps} endpoints · {n_roles} roles · {n_caps} capabilities**\n")
    if report.synthesized and report.journeys:
        out.append(report.journeys[0].narrative + "\n")
    else:
        out.append(_STUB + "\n")

    # §2 Endpoint Inventory
    out.append("## 2. Endpoint Inventory\n")
    out.append("| ID | Path | Method | Handler | File:Line | Kind |")
    out.append("|----|------|--------|---------|-----------|------|")
    bc_map = {bc.endpoint_id: bc for bc in d.business_contexts}
    for ep in d.endpoints:
        bc = bc_map.get(ep.id)
        kind = bc.endpoint_kind if bc else "rest"
        out.append(
            f"| {ep.id} | `{ep.path}` | {ep.method} | {ep.handler} | {ep.file}:{ep.line} | {kind} |"
        )
    out.append("")

    # §3 Guard & Access-Control Map
    out.append("## 3. Guard & Access-Control Map\n")
    guards_by_ep: dict[str, list[Any]] = defaultdict(list)
    for g in d.guards:
        guards_by_ep[g.endpoint_id].append(g)
    if guards_by_ep:
        out.append("| Endpoint | Guard Kind | Roles |")
        out.append("|----------|------------|-------|")
        for ep in d.endpoints:
            for g in guards_by_ep.get(ep.id, []):
                roles_str = ", ".join(g.roles) if g.roles else "—"
                out.append(f"| {ep.id} | {g.kind} | {roles_str} |")
    else:
        out.append("_No guards extracted._")
    out.append("")

    # §4 Role x Capability Matrix
    out.append("## 4. Role x Capability Matrix\n")
    header = "| Capability | " + " | ".join(matrix.roles or ["(no roles)"]) + " |"
    out.append(header)
    out.append("|" + "---|" * (len(matrix.roles) + 1))
    for cap_id in matrix.capability_ids:
        cells: list[str] = [matrix.cell(role, cap_id) for role in matrix.roles] or ["—"]
        out.append(f"| {cap_id} | " + " | ".join(cells) + " |")
    if matrix.gaps():
        out.append(f"\n> ⚠️ Ungated capabilities (no guard): {', '.join(matrix.gaps())}")
    out.append("")

    # §5 User Flows
    out.append("## 5. User Flows\n")
    out.append(_render_flows(report.flows, synthesized=report.synthesized))

    # §6 User Journeys
    out.append("## 6. User Journeys\n")
    if report.synthesized and report.journeys:
        for j in report.journeys:
            out.append(f"### {j.id} — {j.persona}: {j.goal}")
            out.append(j.narrative)
            out.append(f"_Flows: {', '.join(j.flow_ids)}_\n")
    else:
        out.append(_STUB + "\n")

    # §7 Role Reconciliation
    out.append("## 7. Role Reconciliation\n")
    for r in report.reconciliation.reconciled:
        out.append(f"- `{r.name}` (source: {r.source})")
    if report.reconciliation.code_only:
        out.append(
            f"\n> Code-only roles (absent from BRD): {', '.join(report.reconciliation.code_only)}"
        )
    if report.reconciliation.brd_only:
        out.append(
            f"\n> BRD-only roles (unobserved in code): {', '.join(report.reconciliation.brd_only)}"
        )
    out.append("")

    # §8 Navigation Graph
    out.append("## 8. Navigation Graph\n")
    out.append(_render_nav_graph(d.nav_edges))
    out.append("")

    # §9 FR Statements
    out.append("## 9. FR Statements\n")
    out.append(_render_fr_statements(report.fr_statements))
    out.append("")

    # §10 Acceptance Criteria
    out.append("## 10. Acceptance Criteria\n")
    out.append(_render_acceptance_criteria(report.acceptance_criteria, report.fr_statements))
    out.append("")

    # §11 Business Rules
    out.append("## 11. Business Rules\n")
    out.append(_render_business_rules(report.business_rules))
    out.append("")

    # §12 i18n Coverage
    out.append("## 12. i18n Coverage\n")
    out.append(_render_i18n_coverage(d.i18n))
    out.append("")

    # §13 Form Field Inventory
    out.append("## 13. Form Field Inventory\n")
    out.append(_render_form_fields(d))
    out.append("")

    return "\n".join(out) + "\n"


def render_json(report: FunctionalMapReport) -> str:
    d = report.digest
    payload = {
        "primitive": "functional-map",
        "schema_version": "2.0",
        "synthesized": report.synthesized,
        "roles": [r.model_dump() for r in report.reconciliation.reconciled],
        "capabilities": [c.model_dump() for c in d.capabilities],
        "endpoints": [e.model_dump() for e in d.endpoints],
        "guards": [g.model_dump() for g in d.guards],
        "nav_edges": [n.model_dump() for n in d.nav_edges],
        "form_fields": [f.model_dump() for f in d.form_fields],
        "validation_rules": [v.model_dump() for v in d.validation_rules],
        "i18n": [i.model_dump() for i in d.i18n],
        "flows": [f.model_dump() for f in report.flows],
        "journeys": [j.model_dump() for j in report.journeys],
        "fr_statements": [f.model_dump() for f in report.fr_statements],
        "acceptance_criteria": [a.model_dump() for a in report.acceptance_criteria],
        "business_rules": [b.model_dump() for b in report.business_rules],
    }
    return json.dumps(payload, indent=2, sort_keys=True)
