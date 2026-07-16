from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from primitives._shared.discovery.flavor import FLAVOR_PACKS
from primitives.functional_map.shared.models import (
    BusinessContext,
    Capability,
    Endpoint,
    FormField,
    FunctionalDigest,
    Guard,
    I18nEntry,
    NavEdge,
    Role,
    ValidationRule,
    WebflowDefinition,
)

if TYPE_CHECKING:
    from primitives.functional_map.shared.recon import ReconReport

# ---------------------------------------------------------------------------
# Suffix → sub-extractor routing table
# O(1) dispatch: each suffix maps to the list of sub-extractor modules to try.
# The flavor pack's parse() is always tried first (it handles the broad case).
# Sub-extractors are imported lazily at call time to avoid circular imports.
# ---------------------------------------------------------------------------

_SUFFIX_MODULES: dict[str, list[str]] = {
    # Java / JVM
    ".java": [
        "primitives.functional_map.shared.sub_extractors.spring_mvc",
        "primitives.functional_map.shared.sub_extractors.validation_constraints",
        "primitives.functional_map.shared.sub_extractors.jpa_entities",
        "primitives.functional_map.shared.sub_extractors.servlet_controllers",
        "primitives.functional_map.shared.sub_extractors.rest_api",
        "primitives.functional_map.shared.sub_extractors.spring_security",
        "primitives.functional_map.shared.sub_extractors.spring_config",
    ],
    ".kt": [
        "primitives.functional_map.shared.sub_extractors.spring_mvc",
        "primitives.functional_map.shared.sub_extractors.rest_api",
    ],
    # JSP / JSPX
    ".jsp": [
        "primitives.functional_map.shared.sub_extractors.jsp_forms",
        "primitives.functional_map.shared.sub_extractors.spring_security",
    ],
    ".jspx": [
        "primitives.functional_map.shared.sub_extractors.jsp_forms",
        "primitives.functional_map.shared.sub_extractors.spring_security",
    ],
    # Thymeleaf / FreeMarker templates + Angular template-driven forms
    ".html": [
        "primitives.functional_map.shared.sub_extractors.thymeleaf_forms",
        "primitives.functional_map.shared.sub_extractors.freemarker_forms",
        "primitives.functional_map.shared.sub_extractors.angular_forms",
    ],
    ".ftl": ["primitives.functional_map.shared.sub_extractors.freemarker_forms"],
    ".ftlh": ["primitives.functional_map.shared.sub_extractors.freemarker_forms"],
    # Spring Webflow / Struts XML / Spring Integration / WSDL / Spring Security + servlet web.xml
    ".xml": [
        "primitives.functional_map.shared.sub_extractors.spring_webflow",
        "primitives.functional_map.shared.sub_extractors.struts_actions",
        "primitives.functional_map.shared.sub_extractors.spring_integration",
        "primitives.functional_map.shared.sub_extractors.soap_wsdl",
        "primitives.functional_map.shared.sub_extractors.spring_security",
        "primitives.functional_map.shared.sub_extractors.servlet_controllers",
    ],
    # JSF / Facelets
    ".xhtml": [
        "primitives.functional_map.shared.sub_extractors.jsf_beans",
        "primitives.functional_map.shared.sub_extractors.thymeleaf_forms",
    ],
    # i18n properties + Spring config feature flags
    ".properties": [
        "primitives.functional_map.shared.sub_extractors.i18n_messages",
        "primitives.functional_map.shared.sub_extractors.spring_config",
    ],
    # React / TypeScript
    ".tsx": [
        "primitives.functional_map.shared.sub_extractors.react_routes",
        "primitives.functional_map.shared.sub_extractors.react_forms",
    ],
    ".jsx": [
        "primitives.functional_map.shared.sub_extractors.react_routes",
        "primitives.functional_map.shared.sub_extractors.react_forms",
    ],
    # JavaScript (Express, Vue single-file < .vue, React CJS)
    ".js": [
        "primitives.functional_map.shared.sub_extractors.react_routes",
        "primitives.functional_map.shared.sub_extractors.react_forms",
        "primitives.functional_map.shared.sub_extractors.vue_routes",
        "primitives.functional_map.shared.sub_extractors.rest_api",
    ],
    # TypeScript (Angular, Vue SFC script, generic TS)
    ".ts": [
        "primitives.functional_map.shared.sub_extractors.react_routes",
        "primitives.functional_map.shared.sub_extractors.react_forms",
        "primitives.functional_map.shared.sub_extractors.vue_routes",
        "primitives.functional_map.shared.sub_extractors.angular_routes",
        "primitives.functional_map.shared.sub_extractors.angular_forms",
        "primitives.functional_map.shared.sub_extractors.rest_api",
    ],
    # Vue single-file components
    ".vue": ["primitives.functional_map.shared.sub_extractors.vue_routes"],
    # Python (Flask, FastAPI, Django)
    ".py": [
        "primitives.functional_map.shared.sub_extractors.rest_api",
        "primitives.functional_map.shared.sub_extractors.i18n_messages",
    ],
    # GraphQL
    ".graphql": ["primitives.functional_map.shared.sub_extractors.graphql_schema"],
    ".gql": ["primitives.functional_map.shared.sub_extractors.graphql_schema"],
    ".graphqls": ["primitives.functional_map.shared.sub_extractors.graphql_schema"],
    # gRPC proto
    ".proto": ["primitives.functional_map.shared.sub_extractors.grpc_proto"],
    # OpenAPI / API Gateway
    ".yaml": [
        "primitives.functional_map.shared.sub_extractors.api_gateway",
        "primitives.functional_map.shared.sub_extractors.i18n_messages",
        "primitives.functional_map.shared.sub_extractors.spring_config",
    ],
    ".yml": [
        "primitives.functional_map.shared.sub_extractors.api_gateway",
        "primitives.functional_map.shared.sub_extractors.i18n_messages",
        "primitives.functional_map.shared.sub_extractors.spring_config",
    ],
    ".json": [
        "primitives.functional_map.shared.sub_extractors.api_gateway",
        "primitives.functional_map.shared.sub_extractors.i18n_messages",
    ],
    # C# ASP.NET
    ".cs": ["primitives.functional_map.shared.sub_extractors.rest_api"],
    # Ruby (Rails routes)
    ".rb": ["primitives.functional_map.shared.sub_extractors.rest_api"],
    # PHP (Laravel)
    ".php": ["primitives.functional_map.shared.sub_extractors.rest_api"],
    # WSDL (also .wsdl extension)
    ".wsdl": ["primitives.functional_map.shared.sub_extractors.soap_wsdl"],
}


def _load_module(name: str) -> Any:
    return importlib.import_module(name)


def _run_sub_extractors(file_path: Path, content: str) -> dict[str, list[Any]]:
    """Run all sub-extractors for this file's suffix and merge results."""
    suffix = file_path.suffix.lower()
    modules = _SUFFIX_MODULES.get(suffix, [])
    merged: dict[str, list[Any]] = {}
    for mod_name in modules:
        try:
            mod = _load_module(mod_name)
            out = mod.parse(file_path, content)
        except (
            NotImplementedError,
            OSError,
            re.error,
            ValueError,
            KeyError,
            AttributeError,
            TypeError,
        ):
            continue
        for key, val in out.items():
            merged.setdefault(key, []).extend(val)
    return merged


_ENTITY_SUFFIXES = ("Command", "Form", "Request", "Dto", "Input", "Payload", "Body", "Model")


def _entity_base(entity: str) -> str:
    for suffix in _ENTITY_SUFFIXES:
        if entity.endswith(suffix) and len(entity) > len(suffix):
            return entity[: -len(suffix)]
    return entity


def _resolve_form_endpoint(
    raw: dict[str, Any],
    path_to_ep: dict[str, str],
    view_to_ep: dict[str, str],
) -> str:
    existing = str(raw.get("endpoint_id", ""))
    if existing:
        return existing
    action = str(raw.get("form_action", ""))
    if action and action in path_to_ep:
        return path_to_ep[action]
    view = str(raw.get("view_name", ""))
    if view and view in view_to_ep:
        return view_to_ep[view]
    return ""


def _dedup_endpoints(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedup endpoints by (path, method), keeping first occurrence."""
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for ep in raw:
        key = (str(ep["path"]), str(ep["method"]))
        if key not in seen:
            seen.add(key)
            result.append(ep)
    return result


_GuardKind = Literal["auth_required", "role_required", "policy", "public"]
_VALID_GUARD_KINDS: frozenset[str] = frozenset(
    {"auth_required", "role_required", "policy", "public"}
)
_GUARD_KIND_MAPPING: dict[str, _GuardKind] = {
    "requiresAuth": "auth_required",
    "canActivate": "role_required",
    "openapi_security": "auth_required",
    "graphql_auth": "auth_required",
    "secured": "auth_required",
    "intersect_role": "role_required",
}


def _guard_kind_normalize(kind: str) -> _GuardKind:
    if kind in _VALID_GUARD_KINDS:
        return kind  # type: ignore[return-value]
    return _GUARD_KIND_MAPPING.get(kind, "auth_required")


def _build_endpoints(
    raw_eps: list[dict[str, Any]],
) -> tuple[list[Endpoint], dict[tuple[str, str], str]]:
    raw_eps = _dedup_endpoints(raw_eps)
    endpoints: list[Endpoint] = []
    key_to_id: dict[tuple[str, str], str] = {}
    for i, e in enumerate(raw_eps, start=1):
        ep_id = f"EP-{i:03d}"
        key = (str(e["path"]), str(e["method"]))
        key_to_id[key] = ep_id
        endpoints.append(
            Endpoint(
                id=ep_id,
                path=str(e["path"]),
                method=e["method"],
                handler=str(e.get("handler", "")),
                file=str(e.get("file", "")),
                line=int(e.get("line", 0)),
            )
        )
    return endpoints, key_to_id


def _resolve_guard_endpoint(
    raw_g: dict[str, Any],
    key_to_id: dict[tuple[str, str], str],
    path_to_ep_id: dict[str, str],
) -> str:
    # 1. path/method key (existing behaviour)
    ep_key = (str(raw_g.get("path", "")), str(raw_g.get("method", "")))
    from_key = key_to_id.get(ep_key)
    if from_key:
        return from_key
    # 2. explicit endpoint_id already set
    explicit = str(raw_g.get("endpoint_id", ""))
    if explicit:
        return explicit
    # 3. url_pattern exact match
    url_pattern = str(raw_g.get("url_pattern", ""))
    if url_pattern and url_pattern in path_to_ep_id:
        return path_to_ep_id[url_pattern]
    return "EP-000"


def _glob_matched_ep_ids(url_pattern: str, path_to_ep_id: dict[str, str]) -> list[str]:
    """Return all endpoint ids whose path matches the glob prefix."""
    prefix = url_pattern[:-1]  # "/admin/*" → "/admin/"
    return [ep_id for ep_path, ep_id in path_to_ep_id.items() if ep_path.startswith(prefix)]


def _build_guards(
    raw_guards: list[dict[str, Any]],
    key_to_id: dict[tuple[str, str], str],
    endpoints: list[Endpoint] | None = None,
) -> list[Guard]:
    path_to_ep_id: dict[str, str] = {ep.path: ep.id for ep in (endpoints or [])}
    guard_objs: list[Guard] = []
    for raw_g in raw_guards:
        url_pattern = str(raw_g.get("url_pattern", ""))
        explicit_ep_id = str(raw_g.get("endpoint_id", ""))
        # Fan-out: glob url_pattern with no explicit endpoint_id → one guard per matched endpoint
        is_glob = url_pattern.endswith("/*") or ("*" in url_pattern and not explicit_ep_id)
        if is_glob and not explicit_ep_id:
            matched = _glob_matched_ep_ids(url_pattern, path_to_ep_id)
            if len(matched) >= 1:
                kind = _guard_kind_normalize(str(raw_g.get("kind", "auth_required")))
                roles = tuple(str(r) for r in raw_g.get("roles", []))
                file_ = str(raw_g.get("file", ""))
                line_ = int(raw_g.get("line", 0))
                for ep_id in matched:
                    guard_objs.append(
                        Guard(endpoint_id=ep_id, kind=kind, roles=roles, file=file_, line=line_)
                    )
                continue
            # 0 matches — fall through to single-guard path below
        ep_id = _resolve_guard_endpoint(raw_g, key_to_id, path_to_ep_id)
        guard_objs.append(
            Guard(
                endpoint_id=str(ep_id),
                kind=_guard_kind_normalize(str(raw_g.get("kind", "auth_required"))),
                roles=tuple(str(r) for r in raw_g.get("roles", [])),
                file=str(raw_g.get("file", "")),
                line=int(raw_g.get("line", 0)),
            )
        )
    seen: set[tuple[str, str, frozenset[str]]] = set()
    deduped: list[Guard] = []
    for guard in guard_objs:
        gkey = (guard.endpoint_id, guard.kind, frozenset(guard.roles))
        if gkey not in seen:
            seen.add(gkey)
            deduped.append(guard)
    return deduped


def _build_roles(
    raw_roles: list[dict[str, Any]],
    guard_objs: list[Guard],
) -> tuple[Role, ...]:
    role_names = sorted({str(r["name"]) for r in raw_roles if isinstance(r, dict) and "name" in r})
    for gd in guard_objs:
        for r in gd.roles:
            if r not in role_names:
                role_names.append(r)
    role_names = sorted(set(role_names))
    return tuple(Role(name=n, source="code") for n in role_names)


_VALID_FORM_SOURCES: frozenset[str] = frozenset(
    {
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
    }
)


def _build_form_fields(
    raw_form_fields: list[dict[str, Any]],
    endpoints: list[Endpoint],
    raw_business_contexts: list[dict[str, Any]],
) -> list[FormField]:
    path_to_ep: dict[str, str] = {ep.path: ep.id for ep in endpoints}
    view_to_ep: dict[str, str] = {
        str(bc.get("view_name", "")): str(bc.get("endpoint_id", ""))
        for bc in raw_business_contexts
        if bc.get("view_name")
    }
    form_fields_objs: list[FormField] = []
    seen_fields: set[tuple[str, str]] = set()
    for f in raw_form_fields:
        resolved = _resolve_form_endpoint(f, path_to_ep, view_to_ep)
        key = (resolved, str(f.get("name", "")))
        if key in seen_fields:
            continue
        seen_fields.add(key)
        src = str(f.get("source", "annotation"))
        if src not in _VALID_FORM_SOURCES:
            src = "annotation"
        form_fields_objs.append(
            FormField(
                endpoint_id=resolved,
                name=str(f.get("name", "")),
                field_type=str(f.get("field_type", "text")),
                required=bool(f.get("required", False)),
                constraints=tuple(str(c) for c in f.get("constraints", [])),
                label=str(f.get("label", "")),
                conditional_role=str(f.get("conditional_role", "")),
                conditional_on=str(f.get("conditional_on", "")),
                conditional_value=str(f.get("conditional_value", "")),
                default_value=str(f.get("default_value", "")),
                read_only=bool(f.get("read_only", False)),
                display_order=int(f.get("display_order", 0)),
                options=tuple(str(o) for o in f.get("options", ())),
                source=src,  # type: ignore[arg-type]
            )
        )
    return form_fields_objs


def _build_validation_rules(
    raw_validation_rules: list[dict[str, Any]],
) -> list[ValidationRule]:
    objs: list[ValidationRule] = []
    seen_rules: set[tuple[str, str, str]] = set()
    for raw_vr in raw_validation_rules:
        vr_key = (
            str(raw_vr.get("entity", "")),
            str(raw_vr.get("field", "")),
            str(raw_vr.get("constraint", "")),
        )
        if vr_key in seen_rules:
            continue
        seen_rules.add(vr_key)
        objs.append(
            ValidationRule(
                entity=str(raw_vr.get("entity", "")),
                field=str(raw_vr.get("field", "")),
                constraint=str(raw_vr.get("constraint", "")),
                params={str(k): str(v) for k, v in raw_vr.get("params", {}).items()},
                message=str(raw_vr.get("message", "")),
                error_i18n_key=str(raw_vr.get("error_i18n_key", "")),
                cross_field=bool(raw_vr.get("cross_field", False)),
            )
        )
    return objs


def _build_i18n(raw_i18n: list[dict[str, Any]]) -> list[I18nEntry]:
    objs: list[I18nEntry] = []
    seen: set[tuple[str, str]] = set()
    for entry in raw_i18n:
        key = (str(entry.get("key", "")), str(entry.get("locale", "default")))
        if key in seen:
            continue
        seen.add(key)
        objs.append(
            I18nEntry(
                key=str(entry.get("key", "")),
                value=str(entry.get("value", "")),
                locale=str(entry.get("locale", "default")),
            )
        )
    return objs


_WEBFLOW_TRANSITION_ARITY = 3


def _build_webflows(raw_webflow: list[dict[str, Any]]) -> list[WebflowDefinition]:
    objs: list[WebflowDefinition] = []
    seen: set[str] = set()
    for wf in raw_webflow:
        flow_id = str(wf.get("flow_id", ""))
        if flow_id in seen:
            continue
        seen.add(flow_id)
        objs.append(
            WebflowDefinition(
                flow_id=flow_id,
                view_states=tuple(str(v) for v in wf.get("view_states", [])),
                transitions=tuple(
                    (str(t[0]), str(t[1]), str(t[2]))
                    for t in wf.get("transitions", [])
                    if len(t) >= _WEBFLOW_TRANSITION_ARITY
                ),
            )
        )
    return objs


_VALID_ENDPOINT_KINDS: frozenset[str] = frozenset(
    {"mvc", "rest", "ws", "graphql", "grpc", "system", "soap", "integration"}
)
_VALID_BC_SOURCES: frozenset[str] = frozenset(
    {"annotation", "xml", "webflow", "gateway", "openapi", "wsdl", "proto", "struts", "jsf"}
)


def _bc_endpoint_kind(raw_bc: dict[str, Any], method: str) -> str:
    kind = str(raw_bc.get("endpoint_kind", "rest"))
    if kind not in _VALID_ENDPOINT_KINDS:
        kind = "rest"
    if method in ("QUERY", "MUTATION", "SUBSCRIPTION"):
        return "graphql"
    if method == "RPC":
        return "grpc"
    if method == "SYSTEM":
        return "system"
    return kind


def _build_one_business_context(
    ep: Endpoint,
    raw_bc: dict[str, Any],
    cond_bc: dict[str, Any],
    guard_roles_by_ep: dict[str, list[str]],
    form_fields_by_ep: dict[str, list[FormField]],
    validation_rules_objs: list[ValidationRule],
) -> BusinessContext:
    kind = _bc_endpoint_kind(raw_bc, ep.method)
    src = str(raw_bc.get("source", "annotation"))
    if src not in _VALID_BC_SOURCES:
        src = "annotation"
    conditional = bool(raw_bc.get("conditional", False) or cond_bc.get("conditional", False))
    conditional_flag = str(
        raw_bc.get("conditional_flag", "") or cond_bc.get("conditional_flag", "")
    )
    return BusinessContext(
        endpoint_id=ep.id,
        http_security_roles=tuple(guard_roles_by_ep.get(ep.id, [])),
        form_fields=tuple(form_fields_by_ep.get(ep.id, [])),
        validation_rules=tuple(
            vr for vr in validation_rules_objs if _entity_base(vr.entity) in ep.handler
        ),
        model_attributes=tuple(raw_bc.get("model_attributes", [])),
        session_reads=tuple(raw_bc.get("session_reads", [])),
        session_writes=tuple(raw_bc.get("session_writes", [])),
        error_flows=tuple(raw_bc.get("error_flows", [])),
        conditional=conditional,
        conditional_flag=conditional_flag,
        async_endpoint=bool(raw_bc.get("async_endpoint", False)),
        endpoint_kind=kind,  # type: ignore[arg-type]
        source=src,  # type: ignore[arg-type]
        view_name=str(raw_bc.get("view_name", "")),
        api_description=str(raw_bc.get("api_description", "")),
        notification_hints=tuple(raw_bc.get("notification_hints", [])),
        pagination=bool(raw_bc.get("pagination", False)),
        file_upload=bool(raw_bc.get("file_upload", False)),
    )


def _build_business_contexts(
    raw_business_contexts: list[dict[str, Any]],
    endpoints: list[Endpoint],
    guard_objs: list[Guard],
    form_fields_objs: list[FormField],
    validation_rules_objs: list[ValidationRule],
) -> list[BusinessContext]:
    bc_by_ep: dict[str, dict[str, Any]] = {}
    bc_by_handler: dict[str, dict[str, Any]] = {}
    # Key: (path, method) → BC carrying api_description or other annotation-only data
    bc_by_path_method: dict[tuple[str, str], dict[str, Any]] = {}
    for bc in raw_business_contexts:
        ep_id = str(bc.get("endpoint_id", ""))
        if ep_id:
            bc_by_ep[ep_id] = bc
        handler_hint = str(bc.get("handler", ""))
        if not ep_id and handler_hint and handler_hint != "handler":
            bc_by_handler.setdefault(handler_hint, bc)
        # Index annotation-only BCs by path+method so api_description can be merged
        bc_path = str(bc.get("path", ""))
        bc_method = str(bc.get("method", ""))
        if not ep_id and bc_path:
            key_pm = (bc_path, bc_method)
            existing = bc_by_path_method.get(key_pm)
            if existing is None:
                bc_by_path_method[key_pm] = bc
            elif bc.get("api_description") and not existing.get("api_description"):
                # Prefer the BC that carries api_description
                bc_by_path_method[key_pm] = bc

    guard_roles_set: dict[str, set[str]] = {}
    for gd in guard_objs:
        guard_roles_set.setdefault(gd.endpoint_id, set()).update(gd.roles)
    guard_roles_by_ep: dict[str, list[str]] = {
        ep_id: sorted(roles) for ep_id, roles in guard_roles_set.items()
    }

    form_fields_by_ep: dict[str, list[FormField]] = {}
    for ff in form_fields_objs:
        form_fields_by_ep.setdefault(ff.endpoint_id, []).append(ff)

    bc_objs: list[BusinessContext] = []
    for ep in endpoints:
        raw_bc = (
            bc_by_ep.get(ep.id)
            or bc_by_handler.get(ep.handler)
            or bc_by_handler.get(Path(ep.file).stem)
            or {}
        )
        cond_bc = bc_by_handler.get(Path(ep.file).stem, {})
        # Merge api_description from a path/method-keyed BC when the primary raw_bc lacks it
        if not raw_bc.get("api_description"):
            pm_bc = bc_by_path_method.get((ep.path, ep.method)) or bc_by_path_method.get(
                (ep.path, "")
            )
            if pm_bc and pm_bc.get("api_description"):
                # Shallow-merge: don't replace the primary BC, just patch the field
                raw_bc = dict(raw_bc, api_description=str(pm_bc["api_description"]))
        bc_objs.append(
            _build_one_business_context(
                ep,
                raw_bc,
                cond_bc,
                guard_roles_by_ep,
                form_fields_by_ep,
                validation_rules_objs,
            )
        )
    return bc_objs


_VALID_NAV_KINDS: frozenset[str] = frozenset({"link", "redirect", "router", "ajax"})


def _build_nav_edges(raw_nav: list[dict[str, Any]]) -> list[NavEdge]:
    objs: list[NavEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for n in raw_nav:
        nkey = (
            str(n.get("from_screen", "")),
            str(n.get("to_screen", "")),
            str(n.get("kind", "link")),
        )
        if nkey in seen:
            continue
        seen.add(nkey)
        kind = str(n.get("kind", "link"))
        if kind not in _VALID_NAV_KINDS:
            kind = "link"
        objs.append(
            NavEdge(
                from_screen=str(n["from_screen"]),
                to_screen=str(n["to_screen"]),
                kind=kind,  # type: ignore[arg-type]
                file=str(n.get("file", "")),
                line=int(n.get("line", 0)),
            )
        )
    return objs


def _build_capabilities(
    endpoints: list[Endpoint],
    guard_objs: list[Guard],
) -> tuple[Capability, ...]:
    guard_roles_union: dict[str, set[str]] = {}
    for gd in guard_objs:
        if gd.roles:
            guard_roles_union.setdefault(gd.endpoint_id, set()).update(gd.roles)
    guard_roles_frozen: dict[str, tuple[str, ...]] = {
        ep_id: tuple(sorted(roles)) for ep_id, roles in guard_roles_union.items()
    }
    return tuple(
        Capability(
            id=f"FR-{i:03d}",
            action=f"{ep.method} {ep.path}",
            endpoint_id=ep.id,
            roles=guard_roles_frozen.get(ep.id, ()),
        )
        for i, ep in enumerate(endpoints, start=1)
    )


def extract(recon: ReconReport) -> FunctionalDigest:
    pack = FLAVOR_PACKS.get(recon.flavor)

    raw_eps: list[dict[str, Any]] = []
    raw_guards: list[dict[str, Any]] = []
    raw_roles: list[dict[str, Any]] = []
    raw_nav: list[dict[str, Any]] = []
    raw_form_fields: list[dict[str, Any]] = []
    raw_validation_rules: list[dict[str, Any]] = []
    raw_business_contexts: list[dict[str, Any]] = []
    raw_i18n: list[dict[str, Any]] = []
    raw_webflow: list[dict[str, Any]] = []

    def _merge(out: dict[str, list[Any]]) -> None:
        raw_eps.extend(out.get("endpoints", []))
        raw_guards.extend(out.get("guards", []))
        raw_roles.extend(out.get("roles", []))
        raw_nav.extend(out.get("nav_edges", []))
        raw_form_fields.extend(out.get("form_fields", []))
        raw_validation_rules.extend(out.get("validation_rules", []))
        raw_business_contexts.extend(out.get("business_contexts", []))
        raw_i18n.extend(out.get("i18n_entries", []))
        raw_webflow.extend(out.get("webflow_flows", []))

    for path in recon.source_files:
        try:
            content = path.read_text(errors="ignore")
        except OSError:
            continue

        # Flavor pack first (may short-circuit or provide coarse extraction)
        if pack is not None:
            try:
                out = pack.parse(path, content)
                _merge(out)
            except (
                NotImplementedError,
                OSError,
                re.error,
                ValueError,
                KeyError,
                AttributeError,
                TypeError,
            ):
                pass

        # Sub-extractor layer (O(1) suffix dispatch — provides fine-grained data)
        _merge(_run_sub_extractors(path, content))

    # ---- Endpoints ----
    endpoints, key_to_id = _build_endpoints(raw_eps)

    # ---- Guards ----
    guard_objs = _build_guards(raw_guards, key_to_id, endpoints)

    # ---- Roles ----
    roles = _build_roles(raw_roles, guard_objs)

    # ---- FormFields ----
    form_fields_objs = _build_form_fields(raw_form_fields, endpoints, raw_business_contexts)

    # ---- ValidationRules ----
    validation_rules_objs = _build_validation_rules(raw_validation_rules)

    # ---- I18n entries ----
    i18n_objs = _build_i18n(raw_i18n)

    # ---- WebflowDefinitions ----
    webflow_objs = _build_webflows(raw_webflow)

    # ---- BusinessContexts ----
    bc_objs = _build_business_contexts(
        raw_business_contexts,
        endpoints,
        guard_objs,
        form_fields_objs,
        validation_rules_objs,
    )

    # ---- NavEdges ----
    nav_edges = _build_nav_edges(raw_nav)

    # ---- Capabilities (FR statements from endpoints) ----
    capabilities = _build_capabilities(endpoints, guard_objs)

    return FunctionalDigest(
        endpoints=tuple(endpoints),
        guards=tuple(guard_objs),
        roles=roles,
        capabilities=capabilities,
        nav_edges=tuple(nav_edges),
        form_fields=tuple(form_fields_objs),
        validation_rules=tuple(validation_rules_objs),
        business_contexts=tuple(bc_objs),
        i18n=tuple(i18n_objs),
        webflow_flows=tuple(webflow_objs),
    )
