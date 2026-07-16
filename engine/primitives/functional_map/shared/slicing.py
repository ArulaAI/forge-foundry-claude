"""Per-bounded-context slicing for the functional-map report (SP-A, #1381).

The app-mod chain treats the **bounded-context slice** as the unit of work: the
synthesizer authors one rich spec per context from a dedicated functional-map slice
so traceability holds within each context. This module partitions a whole-app
:class:`FunctionalMapReport` into one sub-report per context, carrying the *full*
functional-map detail (all nine collections) for each context's endpoints — not the
thin :class:`FunctionalInventory` the scope adapter reads today.

Two ways to obtain the ``contexts`` mapping:

* :func:`contexts_from_system_map` — read the app-mod system-map's bounded contexts
  (node ``domains``) and assign endpoints to them by the shared join.
* :func:`fallback_contexts` — when no external contexts are supplied, cluster
  endpoints by the functional-map's own signals (path prefix) so a standalone-but-
  sliced request still produces coherent slices.

Both feed :func:`slice_functional_map`, which resolves every endpoint to its context
using the shared :mod:`primitives._shared.modernization.context_resolver` join (the
same resolver ``distribute_frs`` uses — never a second divergent one) and partitions
each collection accordingly.

Pure and deterministic: no LLM, no clock, no filesystem.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from primitives._shared.modernization.context_resolver import (
    build_member_index,
    resolve_context,
)
from primitives._shared.modernization.iface import http_iface
from primitives.functional_map.shared.models import (
    FRStatement,
    FunctionalDigest,
    FunctionalMapReport,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from primitives.functional_map.shared.models import Endpoint

# Structural web-tier domain labels (the ``.#ui`` node's synthetic label, #1455).
# A domain matching one of these acts as the catch-all bucket for endpoints that
# match no *business* domain token — see :func:`contexts_from_system_map`. Kept in
# sync with ``adapters/functional_map._WEB_DOMAIN`` ("Web").
_WEB_TIER_DOMAIN_LABELS: frozenset[str] = frozenset({"web", "ui", "frontend"})


def _endpoint_keys(ep: Endpoint) -> list[str]:
    """Every identifier an endpoint can be matched on against a context member set.

    An explicit ``--contexts`` map may list endpoints by ``EP-NNN`` id, by canonical
    ``http_iface`` string (``"GET /owners/{}"`` — the app-mod join contract), or by
    raw path — so all three are offered as candidate keys.
    """
    return [ep.id, http_iface(ep.method, ep.path), ep.path]


def resolve_endpoint_contexts(
    endpoints: Sequence[Endpoint],
    contexts: Mapping[str, Sequence[str]],
) -> dict[str, str]:
    """Map each endpoint id → the bounded context that owns it (the shared join).

    Uses :func:`primitives._shared.modernization.context_resolver.resolve_context` —
    the identical resolver ``distribute_frs`` uses — so the endpoint→context join can
    never diverge from the FR→context join. With a single context every endpoint
    falls to it; with multiple contexts an endpoint that matches none is left
    unassigned (its facts are dropped from every slice rather than duplicated — the
    caller can see the count shrink).
    """
    member_index = build_member_index(contexts)
    single_context = next(iter(contexts)) if len(contexts) == 1 else None
    assignment: dict[str, str] = {}
    for ep in endpoints:
        target = resolve_context(_endpoint_keys(ep), member_index, single_context=single_context)
        if target is not None:
            assignment[ep.id] = target
    return assignment


def _slug(text: str) -> str:
    """A filesystem-safe slug for a context label (the per-slice directory name)."""
    out = "".join(c if c.isalnum() else "-" for c in text.lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "context"


def _bucket(contexts_present: Sequence[str]) -> dict[str, list[Any]]:
    """A fresh empty ``{context: []}`` bucket over the present contexts."""
    return {c: [] for c in contexts_present}


def _route_by_endpoint(
    items: Sequence[Any],
    ep_to_context: Mapping[str, str],
    contexts_present: Sequence[str],
) -> dict[str, list[Any]]:
    """Route each item to its ``item.endpoint_id``'s context (drop the unresolved)."""
    out = _bucket(contexts_present)
    for it in items:
        ctx = ep_to_context.get(it.endpoint_id)
        if ctx is not None:
            out[ctx].append(it)
    return out


def _route_validation_rules(
    bc_by_ctx: Mapping[str, list[Any]],
    contexts_present: Sequence[str],
) -> dict[str, list[Any]]:
    """A ValidationRule reaches a context through the business-context that declares it."""
    out = _bucket(contexts_present)
    for ctx in contexts_present:
        seen: set[tuple[str, str, str]] = set()
        for bc in bc_by_ctx[ctx]:
            for vr in bc.validation_rules:
                key = (vr.entity, vr.field, vr.constraint)
                if key not in seen:
                    seen.add(key)
                    out[ctx].append(vr)
    return out


def _route_nav_edges(
    nav_edges: Sequence[Any],
    endpoints: Sequence[Endpoint],
    ep_to_context: Mapping[str, str],
    contexts_present: Sequence[str],
) -> dict[str, list[Any]]:
    """An edge belongs to the context of whichever endpoint it links (either end)."""
    path_to_ctx: dict[str, str] = {}
    for ep in endpoints:
        ctx = ep_to_context.get(ep.id)
        if ctx is not None:
            for key in _endpoint_keys(ep):
                path_to_ctx.setdefault(key, ctx)
    out = _bucket(contexts_present)
    for edge in nav_edges:
        ctx = path_to_ctx.get(edge.from_screen) or path_to_ctx.get(edge.to_screen)
        if ctx is not None:
            out[ctx].append(edge)
    return out


def _route_by_key(
    items: Sequence[Any],
    key_of: Any,
    item_to_ctx: Mapping[str, str],
    contexts_present: Sequence[str],
) -> dict[str, list[Any]]:
    """Route each item to the context its ``key_of(item)`` resolves to."""
    out = _bucket(contexts_present)
    for it in items:
        ctx = item_to_ctx.get(key_of(it))
        if ctx is not None:
            out[ctx].append(it)
    return out


def _route_flows(
    flows: Sequence[Any],
    ep_to_context: Mapping[str, str],
    contexts_present: Sequence[str],
) -> tuple[dict[str, list[Any]], dict[str, str]]:
    """A flow belongs to the context of its first resolvable step; also returns flow→ctx."""
    out = _bucket(contexts_present)
    flow_to_ctx: dict[str, str] = {}
    for flow in flows:
        ctx = next(
            (c for step in flow.steps if (c := ep_to_context.get(step.endpoint_id)) is not None),
            None,
        )
        if ctx is not None:
            out[ctx].append(flow)
            flow_to_ctx[flow.id] = ctx
    return out, flow_to_ctx


def _route_journeys(
    journeys: Sequence[Any],
    flow_to_ctx: Mapping[str, str],
    contexts_present: Sequence[str],
) -> dict[str, list[Any]]:
    """A journey follows the context of its first resolvable flow."""
    out = _bucket(contexts_present)
    for jrn in journeys:
        ctx = next(
            (c for fid in jrn.flow_ids if (c := flow_to_ctx.get(fid)) is not None),
            None,
        )
        if ctx is not None:
            out[ctx].append(jrn)
    return out


def slice_functional_map(
    report: FunctionalMapReport,
    contexts: Mapping[str, Sequence[str]],
) -> tuple[dict[str, FunctionalMapReport], list[FRStatement]]:
    """Partition *report* into one full :class:`FunctionalMapReport` per context.

    Every functional-map fact tied to an endpoint — its ``business_context``,
    ``validation_rules``, ``form_fields``, ``guards``, ``nav_edges``, ``capabilities``,
    ``fr_statements``, ``acceptance_criteria``, ``business_rules``, ``flows``,
    ``journeys`` — is routed into the slice for that endpoint's context. Roles, i18n,
    webflow flows and the role reconciliation are app-wide vocabulary and are
    replicated into every slice unchanged.

    FR routing uses a two-layer precedence (#1595 no-drop invariant):
    1. ``fr.context`` (harness-assigned during synthesis, Layer 1 accuracy) — if set
       and it resolves to a present context (slug-normalised), route there.
    2. ``source_evidence`` → ``ep_to_context`` join (existing behaviour, Layer 2
       completeness backstop).
    3. Single-context fast-path: if only one context exists, every unresolved FR
       falls to it.
    4. **UNROUTED** — the FR is collected in the returned ``unrouted_frs`` list.
       It is *never* silently dropped.

    Returns ``({context-slug: FunctionalMapReport}, unrouted_frs)``.
    No endpoint lands in two slices (each is resolved to exactly one context);
    an endpoint that resolves to no context (multi-context, no match) is simply
    absent from every slice.
    """
    d = report.digest
    ep_to_context = resolve_endpoint_contexts(d.endpoints, contexts)
    contexts_present = sorted(set(ep_to_context.values()))
    if not contexts_present:
        return {}, list(report.fr_statements)

    # Build a slug→context map so harness-assigned context slugs can be resolved
    # against the present contexts (same slug normalisation as _slug()).
    ctx_slug_map: dict[str, str] = {_slug(c): c for c in contexts_present}

    # Endpoint-keyed collections route directly by endpoint_id.
    ep_by_ctx = _route_by_key(d.endpoints, lambda e: e.id, ep_to_context, contexts_present)
    guards_by_ctx = _route_by_endpoint(d.guards, ep_to_context, contexts_present)
    caps_by_ctx = _route_by_endpoint(d.capabilities, ep_to_context, contexts_present)
    form_fields_by_ctx = _route_by_endpoint(d.form_fields, ep_to_context, contexts_present)
    bc_by_ctx = _route_by_endpoint(d.business_contexts, ep_to_context, contexts_present)
    val_by_ctx = _route_validation_rules(bc_by_ctx, contexts_present)
    nav_by_ctx = _route_nav_edges(d.nav_edges, d.endpoints, ep_to_context, contexts_present)

    # FR id → context: two-layer routing with a completeness backstop (#1595).
    #
    # Layer 1 (accuracy): if the harness set ``fr.context`` to a present context slug,
    # use it directly — this is the most reliable signal.
    #
    # Layer 2 (completeness backstop): route by ``source_evidence`` endpoint ids.
    # Each FRStatement carries a tuple of endpoint ids in source_evidence; we pick the
    # first that resolves to a context.  The single-context fast-path assigns every FR
    # that has no resolvable evidence to the sole context.
    #
    # If neither layer resolves, the FR is UNROUTED (collected, never dropped).
    #
    # NOTE: capabilities now carry CAP-NNN ids (distinct from FR-NNN fr_statements ids).
    # Routing FRs through cap.id is therefore no longer valid; source_evidence is the
    # correct link (it holds the EP-NNN endpoint ids the LLM cited as evidence).
    single_context = contexts_present[0] if len(contexts_present) == 1 else None
    fr_to_ctx: dict[str, str] = {}
    unrouted_frs: list[FRStatement] = []
    for fr in report.fr_statements:
        ctx_hit: str | None = None

        # Layer 1: harness-assigned context slug.
        if fr.context:
            # Exact match first; then slug-normalised match.
            if fr.context in contexts_present:
                ctx_hit = fr.context
            else:
                ctx_hit = ctx_slug_map.get(_slug(fr.context))

        # Layer 2: source_evidence → endpoint → context join.
        if ctx_hit is None:
            for ev in fr.source_evidence:
                ctx_hit = ep_to_context.get(ev)
                if ctx_hit is not None:
                    break

        # Single-context fast-path.
        if ctx_hit is None:
            ctx_hit = single_context

        if ctx_hit is not None:
            fr_to_ctx[fr.id] = ctx_hit
        else:
            # UNROUTED: never silently dropped (#1595).
            unrouted_frs.append(fr)

    fr_by_ctx = _route_by_key(report.fr_statements, lambda f: f.id, fr_to_ctx, contexts_present)
    # Acceptance criteria follow their FR: ACs whose fr_id is in the unrouted set are
    # collected alongside the unrouted FRs (they are not injected into any slice).
    unrouted_fr_ids = {fr.id for fr in unrouted_frs}
    ac_by_ctx = _route_by_key(
        report.acceptance_criteria,
        lambda a: a.fr_id,
        {k: v for k, v in fr_to_ctx.items() if k not in unrouted_fr_ids},
        contexts_present,
    )

    # Flows carry their own step endpoints; journeys + business_rules follow the flow.
    flows_by_ctx, flow_to_ctx = _route_flows(report.flows, ep_to_context, contexts_present)
    journeys_by_ctx = _route_journeys(report.journeys, flow_to_ctx, contexts_present)
    br_by_ctx = _route_by_key(
        report.business_rules,
        lambda br: br.step_ref.split(".", 1)[0],
        flow_to_ctx,
        contexts_present,
    )

    slices: dict[str, FunctionalMapReport] = {}
    for ctx in contexts_present:
        slice_digest = FunctionalDigest(
            endpoints=tuple(ep_by_ctx[ctx]),
            guards=tuple(guards_by_ctx[ctx]),
            roles=d.roles,  # app-wide vocabulary — replicated
            capabilities=tuple(caps_by_ctx[ctx]),
            nav_edges=tuple(nav_by_ctx[ctx]),
            form_fields=tuple(form_fields_by_ctx[ctx]),
            validation_rules=tuple(val_by_ctx[ctx]),
            business_contexts=tuple(bc_by_ctx[ctx]),
            i18n=d.i18n,  # app-wide vocabulary — replicated
            webflow_flows=d.webflow_flows,  # app-wide — replicated
        )
        slices[_slug(ctx)] = FunctionalMapReport(
            digest=slice_digest,
            reconciliation=report.reconciliation,
            flows=tuple(flows_by_ctx[ctx]),
            journeys=tuple(journeys_by_ctx[ctx]),
            business_rules=tuple(br_by_ctx[ctx]),
            fr_statements=tuple(fr_by_ctx[ctx]),
            acceptance_criteria=tuple(ac_by_ctx[ctx]),
            synthesized=report.synthesized,
            # Per-slice reports carry no needs_context_assignment — only the
            # whole-app report surfaces unrouted FRs (set by _build_slices).
        )
    return slices, unrouted_frs


def contexts_from_system_map(
    system_map: dict[str, Any],
    endpoints: Sequence[Endpoint],
) -> dict[str, list[str]]:
    """Derive ``{context: [endpoint-http_iface,...]}`` from an app-mod system-map.

    The bounded contexts are the union of every node's ``domains`` (a petclinic app
    node tagged ``owner``/``pet``/``vet``/``visit`` contributes four). An endpoint is
    assigned to the domain whose lowercased token appears in the endpoint's path or
    handler (the same containment heuristic the deterministic layer uses to route a
    fact to a domain). Endpoints matching no domain are left for the caller's
    single-context / fallback handling.
    """
    domains: list[str] = []
    for node in system_map.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if node.get("kind") in {"external", "shared_lib"}:
            continue
        for domain in node.get("domains") or []:
            if domain and domain not in domains:
                domains.append(domain)
    # A structural web-tier domain (the ``.#ui`` node's ``Web`` label synthesized by
    # the functional-map adapter when a web app has no role/guard business domain,
    # #1455/#1456) is the catch-all for any endpoint that matches no *business*
    # domain token. Without it, a petclinic-style app's endpoints (``/owners/{}``,
    # ``/vets``) match no domain, drop from every slice, and the web-context BRD
    # renders 1 of 4 diagrams. When present, unmatched endpoints route here so the
    # web slice carries the flows/nav/roles its diagrams draw from.
    web_domain = next((d for d in domains if d.lower() in _WEB_TIER_DOMAIN_LABELS), None)
    contexts: dict[str, list[str]] = {domain: [] for domain in domains}
    for ep in endpoints:
        haystack = f"{ep.path} {ep.handler}".lower()
        matched = False
        for domain in domains:
            if domain is web_domain:
                continue  # the web tier is the fallback, never a token match
            if domain.lower() in haystack:
                contexts[domain].append(http_iface(ep.method, ep.path))
                matched = True
                break
        if not matched and web_domain is not None:
            contexts[web_domain].append(http_iface(ep.method, ep.path))
    # Drop empty contexts so an unmatched domain does not produce an empty slice.
    return {c: members for c, members in contexts.items() if members}


def fallback_contexts(endpoints: Sequence[Endpoint]) -> dict[str, list[str]]:
    """Cluster endpoints by their own signals when no external contexts exist.

    Groups by first path segment (the resource prefix) — ``/owners/...`` and
    ``/owners/{}/pets/...`` cluster under ``owners`` — so a standalone-but-sliced
    request still yields coherent, non-empty slices. Endpoints with no usable prefix
    fall into a single ``root`` cluster.
    """
    contexts: dict[str, list[str]] = {}
    for ep in endpoints:
        segments = [seg for seg in ep.path.split("/") if seg and not seg.startswith(("{", ":"))]
        prefix = segments[0] if segments else "root"
        contexts.setdefault(prefix, []).append(http_iface(ep.method, ep.path))
    return contexts


__all__ = [
    "contexts_from_system_map",
    "fallback_contexts",
    "resolve_endpoint_contexts",
    "slice_functional_map",
]
