"""Adapter — normalize a functional-map.json (S61 output) into a ModernizationDigest.

The functional-map primitive (S61) emits a JSON artifact whose ``endpoints[]``
inventory and ``nav_edges[]`` navigation graph are the direct input for cross-layer
correlation.  This adapter converts that artifact into a
:class:`~primitives._shared.modernization.models.ModernizationDigest` so that
:func:`~primitives._shared.modernization.correlate.build_system_map` can join a
functional-map unit with an API contract or playbook-discovery unit.

Join compatibility guarantee
----------------------------
Every HTTP interface string emitted here is produced by
:func:`~primitives._shared.modernization.iface.http_iface`, which collapses all
path-parameter spellings (``{ownerId}``, ``:id``, ``*``, …) to a single ``{}``
placeholder.  An ``api_contract`` adapter that also calls ``http_iface`` on the same
route therefore produces *the same* string — the correlation join is name-independent.
"""

from __future__ import annotations

from typing import Any

from primitives._shared.modernization.iface import http_iface
from primitives._shared.modernization.models import (
    FunctionalInventory,
    ModernizationDigest,
    SystemMapEntryPoint,
)

# Maximum list lengths kept in the digest (mirrors digest.compress defaults).
_MAX_EXPOSED = 50
_MAX_CONSUMED = 50
_MAX_ENTITIES = 20
_MAX_DEBT = 10

# The synthetic web-tier bounded-context label (#1455/#1456). A functional-map
# unit (``.#ui``) whose roles+guards yield no domain would otherwise carry
# ``domains: []`` and collapse into ``_unassigned_`` downstream
# (``deconstruct.py``), dropping the named web context, the Entry Points rollup,
# and three of the four business BRD diagrams. When a web app has HTTP endpoints
# but no role/guard signal, we tag it with this single structural tier label so a
# named bounded context materializes. It is a tier label, NOT invented business
# content — deterministic and suppressed the moment a real role/guard domain
# exists (or there are no endpoints at all).
_WEB_DOMAIN = "Web"


def to_digest(functional_map_json: dict[str, Any], *, code_root: str) -> ModernizationDigest:
    """Normalize a ``functional-map.json`` dict into a :class:`ModernizationDigest`.

    Parameters
    ----------
    functional_map_json:
        Parsed contents of a ``functional-map.json`` file produced by the S61
        ``functional_map`` primitive (schema version 2.0).  Unknown top-level keys
        are silently ignored so the adapter remains forward-compatible.
    code_root:
        Absolute path to the analysed source tree.  Passed straight through to
        :attr:`ModernizationDigest.code_root`.

    Returns
    -------
    ModernizationDigest
        A token-budgeted digest ready for
        :func:`~primitives._shared.modernization.correlate.build_system_map`.
    """
    # ------------------------------------------------------------------
    # 1. exposed_interfaces — one canonical string per endpoint
    # ------------------------------------------------------------------
    endpoints: list[dict[str, Any]] = list(functional_map_json.get("endpoints") or [])
    exposed: list[str] = []
    handler_names: list[str] = []
    seen_exposed: set[str] = set()
    for ep in endpoints:
        method: str = ep.get("method") or ""
        path: str = ep.get("path") or ""
        if not path:
            continue
        iface = http_iface(method, path)
        if iface not in seen_exposed:
            seen_exposed.add(iface)
            exposed.append(iface)
        handler: str = ep.get("handler") or ""
        if handler and handler not in handler_names:
            handler_names.append(handler)

    # ------------------------------------------------------------------
    # 2. consumed_interfaces — nav edges + ajax edges + form targets
    # ------------------------------------------------------------------
    nav_edges: list[dict[str, Any]] = list(functional_map_json.get("nav_edges") or [])
    consumed: list[str] = []
    seen_consumed: set[str] = set()
    for edge in nav_edges:
        # Each edge carries a ``to_screen`` (the target path).  When the edge
        # kind is unknown we default to GET (navigation implies a GET).
        to_path: str = edge.get("to_screen") or edge.get("to") or ""
        if not to_path:
            continue
        # kind may be "redirect", "link", "ajax", etc.  Use it as a hint but
        # map only method-bearing kinds; everything else → GET.
        kind: str = (edge.get("kind") or "").lower()
        nav_method = "POST" if kind in {"post", "submit", "form"} else "GET"
        iface = http_iface(nav_method, to_path)
        if iface not in seen_consumed:
            seen_consumed.add(iface)
            consumed.append(iface)

    # ------------------------------------------------------------------
    # 3. flavor — prefer the map's own field, fall back to "web"
    # ------------------------------------------------------------------
    flavor: str = functional_map_json.get("flavor") or "web"

    # ------------------------------------------------------------------
    # 4. migration_state — pass through if present
    # ------------------------------------------------------------------
    migration_state: str | None = functional_map_json.get("migration_state") or None

    # ------------------------------------------------------------------
    # 5. domains — fold roles and guards into domain labels
    # ------------------------------------------------------------------
    domains: list[str] = []
    roles: list[Any] = list(functional_map_json.get("roles") or [])
    for role in roles:
        label: str = role if isinstance(role, str) else role.get("name") or role.get("id") or ""
        if label and label not in domains:
            domains.append(label)

    guards: list[Any] = list(functional_map_json.get("guards") or [])
    for guard in guards:
        label = guard if isinstance(guard, str) else guard.get("name") or guard.get("rule") or ""
        if label and label not in domains:
            domains.append(label)

    # Web-tier fallback (#1455/#1456): a web app with HTTP endpoints but no
    # role/guard signal must still carry a named domain so the ``.#ui`` node
    # forms a bounded context instead of collapsing into ``_unassigned_``.
    if not domains and endpoints:
        domains.append(_WEB_DOMAIN)

    # ------------------------------------------------------------------
    # 6. debt_summary — surface guard/role gaps as lightweight debt hints
    # ------------------------------------------------------------------
    debt_summary: list[str] = []
    if guards:
        debt_summary.append(f"{len(guards)} access guard(s) defined — verify enforcement coverage")
    if roles and any((ep.get("roles") or []) == [] for ep in endpoints):
        debt_summary.append(
            "Some endpoints carry no role annotation — authorization coverage incomplete"
        )

    # ------------------------------------------------------------------
    # 7. key_entities — handler names (most structural entities in a web app)
    # ------------------------------------------------------------------
    key_entities = handler_names[:_MAX_ENTITIES]

    # ------------------------------------------------------------------
    # 8. unit_count — number of distinct endpoint handlers
    # ------------------------------------------------------------------
    unit_count = len(handler_names)

    return ModernizationDigest(
        code_root=code_root,
        flavor=flavor,
        migration_state=migration_state,
        exposed_interfaces=exposed[:_MAX_EXPOSED],
        consumed_interfaces=consumed[:_MAX_CONSUMED],
        domains=domains,
        key_entities=key_entities,
        debt_summary=debt_summary[:_MAX_DEBT],
        unit_count=unit_count,
    )


def to_entry_points(functional_map_json: dict[str, Any]) -> list[SystemMapEntryPoint]:
    """Derive the Track-B (#1427) HTTP entry points from a ``functional-map.json``.

    Each functional-map endpoint is an **inbound trigger** — the outside actor
    invokes an HTTP route which dispatches into a handler. The discovery layer
    already captures batch / scheduled / messaging / CLI entry points for the
    stacks it parses, but a plain HTTP web app (``@GetMapping`` controllers, no
    ``@Scheduled`` / ``@KafkaListener``) surfaces its inbound surface **only** in
    the functional-map's ``endpoints[]``. This adapter converts them so the scope
    pipeline can thread them onto the system-map (via
    :func:`~primitives._shared.modernization.correlate.build_system_map`) and the
    Track-B Entry Points rollup renders instead of silently dropping (#1455).

    Each endpoint becomes a :class:`SystemMapEntryPoint`:

    * ``kind`` — ``"api_route"`` when the endpoint's own ``kind`` is ``rest``
      (a JSON/data route), else ``"web_route"`` (an MVC/HTML route).
    * ``name`` — the :func:`http_iface`-canonical route (join-stable, dedup key).
    * ``trigger`` — the real ``"METHOD path"`` inbound value.
    * ``target`` — the handler name (the unit it dispatches into).
    * ``source`` — the ``file:line`` provenance.

    De-duplicated by ``(kind, name)`` (first occurrence wins) and sorted, so the
    result is deterministic. A ``--no-synthesis`` / thin map with no endpoints
    yields ``[]``.
    """
    endpoints: list[dict[str, Any]] = list(functional_map_json.get("endpoints") or [])
    by_key: dict[tuple[str, str], SystemMapEntryPoint] = {}
    for ep in endpoints:
        method: str = ep.get("method") or "GET"
        path: str = ep.get("path") or ""
        if not path:
            continue
        name = http_iface(method, path)
        kind = "api_route" if (ep.get("kind") or "").lower() == "rest" else "web_route"
        handler: str = ep.get("handler") or ""
        file: str = ep.get("file") or ""
        line = ep.get("line")
        source = f"{file}:{line}" if file and line is not None else ""
        by_key.setdefault(
            (kind, name),
            SystemMapEntryPoint(
                name=name,
                kind=kind,
                trigger=f"{method.upper()} {path}",
                target=handler,
                source=source,
            ),
        )
    return sorted(by_key.values(), key=lambda e: (e.kind, e.name))


def _dict_list(value: Any) -> list[dict[str, Any]]:
    """Coerce a functional-map collection to a list of dicts, dropping non-dicts.

    A missing key (``None``), a non-list value, or malformed non-dict entries all
    degrade to a clean (possibly empty) list rather than raising — a functional-map
    produced with ``--no-synthesis`` legitimately omits these collections.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def functional_map_to_inventory(functional_map_json: dict[str, Any]) -> FunctionalInventory:
    """Extract the synthesized *functional layer* from a ``functional-map.json``.

    Runs **alongside** :func:`to_digest` (which owns the structural endpoints/nav
    graph for cross-layer correlation). This reads the five synthesized collections
    the digest deliberately drops — ``fr_statements``, ``acceptance_criteria``,
    ``business_rules``, ``flows``, ``journeys`` — so the scope synthesizer (#1356/#1357)
    and the app-analysis playbook (#1358) can thread real functional requirements into
    their outputs instead of inventing them (app-mod FR-threading, #1355).

    Missing / malformed collections degrade to empty lists (a ``--no-synthesis`` map
    yields an empty :class:`FunctionalInventory`, never a crash).
    """
    return FunctionalInventory(
        fr_statements=_dict_list(functional_map_json.get("fr_statements")),
        acceptance_criteria=_dict_list(functional_map_json.get("acceptance_criteria")),
        business_rules=_dict_list(functional_map_json.get("business_rules")),
        flows=_dict_list(functional_map_json.get("flows")),
        journeys=_dict_list(functional_map_json.get("journeys")),
    )


__all__ = ["functional_map_to_inventory", "to_digest", "to_entry_points"]
