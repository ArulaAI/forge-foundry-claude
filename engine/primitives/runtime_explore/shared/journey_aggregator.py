"""Phase-C journey aggregator + reconciliation for runtime_explore (#1475).

Deterministically renders the merged crawl graph + agent-authored journeys into the
final ``runtime-journeys.json`` machine artifact and a business-audience
``RUNTIME-DISCOVERY.md`` (screen inventory, a Mermaid nav map, reachability summary).
When a static ``functional-map.json`` is supplied it also reconciles the runtime
observation against it — confirmed / runtime-only / static-only — via the shared
``http_iface`` join (path-param-agnostic). Pure Python: no LLM, no browser.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from primitives._shared.modernization.iface import http_iface
from primitives._shared.modernization.render import flowchart, heading, md_table
from primitives.runtime_explore.shared.models import (
    CrawlGraph,
    Journey,
    NavEdge,
    RuntimeJourneys,
    Screen,
)
from primitives.runtime_explore.shared.screenshots import templatize

#: Max nodes rendered in the RUNTIME-DISCOVERY.md Mermaid nav map. Beyond this the
#: graph is unrenderable in most viewers, so we cap the diagram (the complete graph
#: always lives in runtime-journeys.json) and note how many screens were omitted.
_FLOWCHART_MAX_NODES = 60


@dataclass(frozen=True)
class Reconciliation:
    """Runtime↔static reconciliation buckets (each a sorted list of ``http_iface``s)."""

    confirmed: list[str] = field(default_factory=list)
    runtime_only: list[str] = field(default_factory=list)
    static_only: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ControlReconciliation:
    """Additive control-vs-capability reconciliation buckets (#1602).

    ``confirmed`` — controls that matched a functional-map capability or FR via the
    kind+label-token heuristic.  Each entry is ``"<control-id> (<kind>: <label>)"``.
    ``runtime_only`` — controls with no static-map match (same string shape).
    Both lists are sorted deterministically.
    """

    confirmed: list[str] = field(default_factory=list)
    runtime_only: list[str] = field(default_factory=list)


#: View-technology suffixes that server-rendered apps (JSP/Thymeleaf) carry on URLs but
#: that the static functional-map's extension-less routes do not (``/owners/1.html`` ↔
#: ``/owners/{}``). Stripped to produce a suffix-free join candidate.
_VIEW_SUFFIXES = (".html", ".htm")


def _context_path(base_url: str) -> str:
    """The servlet/context path prefix of ``base_url`` (e.g. ``/petclinic``), or ``""``.

    The crawler records absolute URLs (``http://host:8080/petclinic/owners/find.html``)
    while the static map is app-relative (``/owners/find``). Stripping the base_url's
    origin *and* path prefix app-relativizes the runtime path so the two join.
    """
    path = urlsplit(base_url).path.rstrip("/")
    return path


def _app_relative_path(url: str, context_path: str) -> str:
    """App-relativize a crawler URL: drop origin + servlet context path.

    ``url`` may be absolute (``http://host/petclinic/owners/find.html``) or already
    app-relative (``/owners/find`` — the shape the crawler emits for a root-context app
    and the shape unit fixtures use). In both cases the returned path is app-relative,
    leading-slashed, query/fragment-free.
    """
    path = urlsplit(url).path or url  # bare relative paths land in .path
    path = path.split("?", 1)[0].split("#", 1)[0]
    if context_path and (path == context_path or path.startswith(context_path + "/")):
        path = path[len(context_path) :] or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def route_name_from_url(url: str, context_path: str) -> str:
    """Derive a human-readable feature-name label from a screen URL (#1607).

    Algorithm:
    1. App-relativize the URL (strip origin + context path).
    2. Drop the query string / fragment.
    3. Split on ``/`` and discard empty segments and the root ``/``.
    4. Replace each purely-numeric segment with ``{}`` (templatize concrete ids).
    5. Title-case each remaining segment and join with a space.

    Examples::

        /owners/find        → "Owners Find"
        /owners/123         → "Owners {}"
        /vets               → "Vets"
        /                   → ""    (no segments → empty → omit-when-empty)
        http://host/petclinic/vets.html → "Vets"   (context-path stripped, .html kept)

    The returned string is EMPTY when no meaningful path segments remain (e.g. the
    bare root ``/``).  The caller stores the empty string; ``Screen._ser`` then omits
    the key so existing single-screen output stays byte-identical.
    """
    path = _app_relative_path(url, context_path)
    # Strip any query string that survived _app_relative_path (shouldn't, but be safe).
    path = path.split("?", 1)[0].split("#", 1)[0]
    segments = [s for s in path.split("/") if s]
    if not segments:
        return ""
    # Templatize purely-numeric segments (concrete record ids like "123").
    named = ["{}" if s.isdigit() else s.title() for s in segments]
    return " ".join(named)


def _screen_ifaces(screen: Screen, context_path: str) -> set[str]:
    """The set of join-candidate ``http_iface``s a runtime screen can match on.

    Robust to context-path and view-suffix mismatch: emits both the suffix-stripped
    candidate (``/owners/1.html`` → ``/owners/{}``, matching an extension-less static
    route) and the raw templatized candidate (so an app that DOES expose ``.html`` in
    its static map still joins). Concrete id segments (``/owners/123``) collapse to
    ``{}`` via ``templatize`` so they meet the static map's ``/owners/{ownerId}``.
    """
    path = _app_relative_path(screen.url, context_path)
    candidates = {path}
    for suffix in _VIEW_SUFFIXES:
        if path.endswith(suffix):
            candidates.add(path[: -len(suffix)])
    return {http_iface("GET", templatize(c)) for c in candidates}


def merge_role_graphs(role_graphs: dict[str, CrawlGraph]) -> tuple[CrawlGraph, dict[str, int]]:
    """Merge N per-role crawl graphs into one role-tagged graph (#1563).

    Each role's SEPARATE crawl produces its own :class:`CrawlGraph`; this unions them:

    * **Screens** are keyed by URL. A screen reached by several roles appears once, its
      ``roles`` tuple carrying the sorted set of every role that reached it. The first
      role (iteration order) to reach a URL owns the screen's id / title / forms /
      screenshot; later roles only contribute their role tag (deterministic).
    * **Nav edges** are unioned and de-duplicated by ``(from, to, trigger, kind, label)``.
    * **External edges** are unioned and de-duplicated by ``(from, to_url)``.

    Returns ``(merged_graph, role_coverage)`` where ``role_coverage`` maps each role to
    the number of screens ITS crawl reached (its own graph's screen count), so a role
    that reached fewer screens is visible even after the union.
    """
    merged_by_url: dict[str, Screen] = {}
    roles_by_url: dict[str, set[str]] = {}
    order: list[str] = []  # URL insertion order for deterministic output
    role_coverage: dict[str, int] = {}

    for role in sorted(role_graphs):
        graph = role_graphs[role]
        role_coverage[role] = len(graph.screens)
        for screen in graph.screens:
            if screen.url not in merged_by_url:
                merged_by_url[screen.url] = screen
                roles_by_url[screen.url] = set()
                order.append(screen.url)
            roles_by_url[screen.url].add(role)

    merged_screens = tuple(
        merged_by_url[url].model_copy(update={"roles": tuple(sorted(roles_by_url[url]))})
        for url in order
    )

    seen_nav: set[tuple[str, str, str, str, str]] = set()
    nav_edges: list[NavEdge] = []
    seen_ext: set[tuple[str, str]] = set()
    external_edges = []
    truncated = False
    for role in sorted(role_graphs):
        graph = role_graphs[role]
        truncated = truncated or graph.truncated
        for e in graph.nav_edges:
            key = (e.frm, e.to, e.trigger, e.kind, e.label)
            if key not in seen_nav:
                seen_nav.add(key)
                nav_edges.append(e)
        for x in graph.external_edges:
            xkey = (x.frm, x.to_url)
            if xkey not in seen_ext:
                seen_ext.add(xkey)
                external_edges.append(x)

    merged = CrawlGraph(
        screens=merged_screens,
        nav_edges=tuple(nav_edges),
        external_edges=tuple(external_edges),
        budget_visited=len(merged_screens),
        truncated=truncated,
    )
    return merged, role_coverage


def aggregate(
    crawl_graph: CrawlGraph,
    journeys_judgment: dict[str, Any],
    *,
    base_url: str,
    generated_at: str,
    functional_map: dict[str, Any] | None = None,
    role_coverage: dict[str, int] | None = None,
) -> tuple[RuntimeJourneys, str]:
    """Merge the crawl graph + authored journeys → ``(RuntimeJourneys, discovery_md)``.

    ``role_coverage`` (a role→reachable-screen-count map from :func:`merge_role_graphs`)
    is threaded through for a multi-role run so the artifact + the "Role Coverage" MD
    section render; ``None``/empty ⇒ today's single-perspective output (byte-identical).
    """
    journeys = tuple(
        Journey.model_validate(j)
        for j in journeys_judgment.get("journeys", [])
        if isinstance(j, dict)
    )
    # Derive a human-readable route_name for each screen from its URL (#1607).
    # Omit-when-empty: _route_name_from_url returns "" for root/bare URLs, and
    # Screen._ser drops the key when empty — so existing single-screen output stays
    # byte-identical when all screens have no meaningful path segments.
    context_path = _context_path(base_url)
    screens_with_route_names = tuple(
        s.model_copy(update={"route_name": route_name_from_url(s.url, context_path)})
        for s in crawl_graph.screens
    )
    rj = RuntimeJourneys(
        base_url=base_url,
        generated_at=generated_at,
        screens=screens_with_route_names,
        nav_edges=crawl_graph.nav_edges,
        journeys=journeys,
        external_edges=crawl_graph.external_edges,
        budget={"pages_visited": crawl_graph.budget_visited, "truncated": crawl_graph.truncated},
        role_coverage=role_coverage or {},
    )
    rec = reconcile(rj, functional_map, base_url=base_url) if functional_map is not None else None
    ctrl_rec = reconcile_controls(rj, functional_map) if functional_map is not None else None
    return rj, _render_md(rj, rec, ctrl_rec)


def reconcile(
    runtime: RuntimeJourneys,
    functional_map_json: dict[str, Any],
    *,
    base_url: str = "",
) -> Reconciliation:
    """Bucket runtime screens vs static functional-map endpoints by ``http_iface``.

    ``base_url`` supplies the origin + servlet context path so an absolute crawler URL
    is app-relativized before the join (``/petclinic/owners/find.html`` → ``/owners/{}``
    vs the static ``/owners/find``). Each runtime screen contributes a *candidate set*
    (suffix-stripped and raw), so a screen is ``confirmed`` if any candidate matches a
    static iface. ``runtime_only`` reports one representative candidate per unmatched
    screen; ``static_only`` is the static ifaces no candidate hit.
    """
    context_path = _context_path(base_url)
    static: set[str] = set()
    for ep in functional_map_json.get("endpoints", []):
        if isinstance(ep, dict) and ep.get("path"):
            static.add(http_iface(str(ep.get("method") or "GET"), str(ep["path"])))

    matched_static: set[str] = set()  # static ifaces a runtime candidate confirmed
    confirmed: set[str] = set()
    runtime_only: set[str] = set()
    for screen in runtime.screens:
        candidates = _screen_ifaces(screen, context_path)
        hits = candidates & static
        if hits:
            confirmed |= hits
            matched_static |= hits
        else:
            # No static route matched — report the primary (suffix-stripped) candidate.
            runtime_only.add(sorted(candidates)[0])
    static_only = static - matched_static
    return Reconciliation(
        confirmed=sorted(confirmed),
        runtime_only=sorted(runtime_only),
        static_only=sorted(static_only),
    )


#: Minimum token length for label/action overlap matching — filters out stop-words
#: ("by", "in", "an", "the") which are all 2 chars or fewer.
_MIN_TOKEN_LEN = 3


def _normalise_tokens(text: str) -> set[str]:
    """Lower-case alphabetic tokens from a capability action, FR statement, or control label.

    Strips punctuation and short stop-words so ``"Sort by name"`` → ``{"sort", "name"}``
    and ``"filter"`` → ``{"filter"}``.  Only pure-alpha tokens of length ≥ _MIN_TOKEN_LEN
    are kept so incidental words ("by", "the", "an", "in") do not create false matches.
    """
    return {t for t in re.split(r"[^a-z]+", text.lower()) if len(t) >= _MIN_TOKEN_LEN}


#: ``Control.kind`` → set of canonical tokens to match against capability ``action`` strings.
#: A control whose kind expansion overlaps with an action-token set is "confirmed" via
#: signal 1/2.
_KIND_ACTION_MAP: dict[str, set[str]] = {
    "sort": {"sort"},
    "filter": {"filter", "search", "find"},
    "paginate": {"paginate", "pagination"},
    "dropdown": {"select", "choose", "pick", "dropdown"},
    "checkbox": {"select", "toggle", "check"},
    "radio": {"select", "choose"},
    "modal": {"modal", "dialog", "popup", "overlay"},
    "button": {"submit", "create", "add", "delete", "remove", "save", "cancel"},
    "edit": {"edit", "update", "change"},
    "other": set(),
}

#: ``Control.kind`` → set of ``fr_type`` values that conceptually cover it (signal 3).
#: The control-kind vocabulary uses ``"paginate"``; the FR schema spells the same concept
#: as ``"pagination"`` — both are mapped here so the join is kind-to-fr_type, not string
#: equality.  Only close semantic equivalences are listed — "functional" is intentionally
#: omitted to avoid false-positives (it is too broad; nearly every control could match).
_KIND_FR_TYPE_MAP: dict[str, set[str]] = {
    "sort": {"sort"},
    "filter": {"filter"},
    "paginate": {"paginate", "pagination"},
    "edit": {"edit"},
    "checkbox": {"checkbox"},
    "radio": {"radio"},
    "dropdown": {"dropdown"},
    "modal": {"modal"},
    "button": {"button"},
    "other": set(),
}


def reconcile_controls(
    runtime: RuntimeJourneys,
    functional_map_json: dict[str, Any],
) -> ControlReconciliation | None:
    """Additive control-vs-capability/FR reconciliation (#1602).

    Returns ``None`` when either ``functional_map_json`` is not supplied or no screen
    carries any controls — so the caller is guaranteed byte-identical output in both cases
    without any extra conditional logic.

    **Match heuristic (conservative):** a control is ``confirmed`` when at least one of:

    1. ``control.kind`` is in the canonical expansion of a capability ``action`` token
       (e.g. kind ``"filter"`` matches ``action="filter"`` or ``action="search"``).
    2. The lower-cased label-tokens of the control (≥3-char alpha tokens) overlap with the
       lower-cased tokens of a capability ``action`` string.
    3. ``control.kind`` matches the ``fr_type`` of an ``fr_statements`` entry.

    Only these three signals are used.  Selector contents, screenshot paths, and
    ``data_state_change`` prose are deliberately NOT matched to avoid false positives.
    """
    # Collect all controls across all screens.
    all_controls = [c for s in runtime.screens for c in s.controls]
    if not all_controls:
        return None

    # Build candidate-match sets from the static map.
    # capability actions: lowercased strings + their token sets.
    cap_actions: list[str] = []
    for cap in functional_map_json.get("capabilities", []):
        if isinstance(cap, dict) and cap.get("action"):
            cap_actions.append(str(cap["action"]).lower())

    # FR types: lowercased fr_type values from fr_statements.
    fr_types: set[str] = set()
    for fr in functional_map_json.get("fr_statements", []):
        if isinstance(fr, dict) and fr.get("fr_type"):
            fr_types.add(str(fr["fr_type"]).lower())

    # No static signals at all → every control is runtime-only; still return a result
    # (the caller treats runtime-only controls as a finding in the MD).

    def _matches(kind: str, label: str) -> bool:
        """Return True iff the control is confirmed against the static map."""
        kind = kind.lower()
        label_tokens = _normalise_tokens(label)

        # Signal 1: kind-exact expansion vs each capability action.
        kind_expansions = _KIND_ACTION_MAP.get(kind, {kind})
        for action in cap_actions:
            action_tokens = _normalise_tokens(action)
            # Kind expansion overlaps with action tokens.
            if kind_expansions & action_tokens:
                return True
            # Label-token overlap with action tokens (signal 2).
            if label_tokens and label_tokens & action_tokens:
                return True

        # Signal 3: kind expands to an fr_type that the static map declares.
        return bool(_KIND_FR_TYPE_MAP.get(kind, {kind}) & fr_types)

    confirmed: list[str] = []
    runtime_only: list[str] = []
    for ctrl in all_controls:
        if ctrl.label:
            desc = f"{ctrl.id} ({ctrl.kind}: {ctrl.label})"
        else:
            desc = f"{ctrl.id} ({ctrl.kind})"
        if _matches(ctrl.kind, ctrl.label):
            confirmed.append(desc)
        else:
            runtime_only.append(desc)

    return ControlReconciliation(
        confirmed=sorted(confirmed),
        runtime_only=sorted(runtime_only),
    )


def _render_functional_controls(parts: list[str], rj: RuntimeJourneys) -> None:
    """Append the "Functional Controls" section for runs that discovered controls (#1601).

    Renders ONLY when at least one screen has controls.  For a run with NO controls it
    is a complete no-op — keeping RUNTIME-DISCOVERY.md byte-identical to the pre-#1601
    output.  Mirrors the same conditional-section pattern as ``_render_role_coverage``.

    Structure: one ``## Functional Controls`` top-level section; under it, a
    ``### <Screen-id> — <title>`` subsection per screen that HAS controls, with a
    table (kind, label, data-state change, screenshot).
    """
    screens_with_controls = [s for s in rj.screens if s.controls]
    if not screens_with_controls:
        return

    parts.append(heading("Functional Controls", level=2))
    parts.append("")

    for screen in screens_with_controls:
        section_title = f"{screen.id} — {screen.title}".strip(" —")
        parts.append(heading(section_title, level=3))
        parts.append("")
        parts.append(
            md_table(
                ["Id", "Kind", "Label", "Data / state change", "Screenshot"],
                [
                    [
                        c.id,
                        c.kind,
                        c.label or "—",
                        c.data_state_change or "—",
                        f"[view]({c.screenshot})" if c.screenshot else "—",
                    ]
                    for c in screen.controls
                ],
            )
        )
        parts.append("")


def _render_role_coverage(parts: list[str], rj: RuntimeJourneys) -> None:
    """Append the "Role Coverage" section for a multi-role run (#1563).

    Renders ONLY when the run carries role tags (``role_coverage`` set OR any screen
    tagged with a role). For a single-perspective run it is a no-op — keeping the
    RUNTIME-DISCOVERY.md byte-identical to the pre-#1563 output.

    Shows: per-role reachable-screen count, a screen-by-role table (which role(s) reached
    each screen), and the role-gated delta (N screens reachable ONLY by <role>).
    """
    tagged = tuple(s for s in rj.screens if s.roles)
    all_roles = sorted({r for s in rj.screens for r in s.roles} | set(rj.role_coverage))
    if not all_roles and not tagged:
        return

    parts.append(heading("Role Coverage", level=2))
    parts.append("")

    # Per-role reachable count. Prefer the explicit role_coverage (each role's OWN crawl
    # count); fall back to counting tagged screens when it was not supplied.
    counts = {
        r: rj.role_coverage.get(r, sum(1 for s in rj.screens if r in s.roles)) for r in all_roles
    }
    parts.append(
        md_table(
            ["Role", "Reachable screens"],
            [[r, str(counts[r])] for r in all_roles],
        )
    )
    parts.append("")

    # Screen-by-role matrix: which role(s) reached each screen.
    parts.append(
        md_table(
            ["Screen", "URL", *all_roles],
            [[s.id, s.url, *["✓" if r in s.roles else "—" for r in all_roles]] for s in rj.screens],
        )
    )
    parts.append("")

    # Role-gated delta: screens reachable by exactly ONE role.
    for role in all_roles:
        only = [s for s in rj.screens if s.roles == (role,)]
        if only:
            ids = ", ".join(s.id for s in only)
            parts.append(f"- {len(only)} screen(s) reachable only by **{role}**: {ids}")
    parts.append("")


def _render_control_reconciliation(
    parts: list[str], ctrl_rec: ControlReconciliation | None
) -> None:
    """Append the "Control Reconciliation" subsection for runs with a functional map and
    controls (#1602).

    Renders ONLY when ``ctrl_rec`` is not ``None`` (i.e. a functional-map was supplied AND
    at least one screen has controls).  Otherwise it is a complete no-op — output is
    byte-identical to the pre-#1602 output.  This function must be called AFTER the
    parent ``## Reconciliation`` section header so it naturally appears as a subsection.
    """
    if ctrl_rec is None:
        return

    parts.append(heading("Control Reconciliation", level=3))
    parts.append("")
    parts.append(
        "Discovered controls matched against functional-map capabilities / FR types "
        "(kind + label-token overlap, conservative):"
    )
    parts.append("")
    parts.append(
        md_table(
            ["Bucket", "Count", "Controls"],
            [
                [
                    "Confirmed (matched capability / FR type)",
                    str(len(ctrl_rec.confirmed)),
                    ", ".join(ctrl_rec.confirmed) or "—",
                ],
                [
                    "Runtime-only (no static match)",
                    str(len(ctrl_rec.runtime_only)),
                    ", ".join(ctrl_rec.runtime_only) or "—",
                ],
            ],
        )
    )
    parts.append("")


def _render_md(
    rj: RuntimeJourneys,
    rec: Reconciliation | None,
    ctrl_rec: ControlReconciliation | None = None,
) -> str:
    parts: list[str] = [heading(f"Runtime Discovery — {rj.base_url}", level=1), ""]

    parts.append(heading("Screen Inventory", level=2))
    parts.append("")
    parts.append(
        md_table(
            ["Id", "URL", "Title", "Reached via", "Forms", "Screenshot"],
            [
                [
                    s.id,
                    s.url,
                    s.title,
                    s.reached_via,
                    str(len(s.forms)),
                    f"[view]({s.screenshot})" if s.screenshot else "—",
                ]
                for s in rj.screens
            ],
        )
    )
    parts.append("")

    if rj.nav_edges:
        parts.append(heading("Navigation Map", level=2))
        parts.append("")
        node_labels = {s.id: f"{s.id} {s.title}".strip() for s in rj.screens}
        all_screen_ids = {s.id for s in rj.screens}
        capped = len(rj.screens) > _FLOWCHART_MAX_NODES
        if capped:
            # A large crawl's flowchart is unrenderable in most viewers, so cap the
            # visited-screen nodes to the first N. The full graph is always in
            # runtime-journeys.json. We drop only edges to a *visited* screen that fell
            # outside the window; edges to a dangling frontier target (a raw URL that is
            # not a screen id — an unvisited/blocked/budget-cut target) are KEPT so the
            # diagram still shows the reachability boundaries, exactly as before. Below
            # the cap the output is byte-identical to the pre-cap behavior.
            shown = rj.screens[:_FLOWCHART_MAX_NODES]
            shown_ids = {s.id for s in shown}
            fc_nodes = [(s.id, node_labels.get(s.id, s.id)) for s in shown]
            fc_edges = [
                (e.frm, e.to, e.label or e.trigger)
                for e in rj.nav_edges
                if e.frm in shown_ids and (e.to in shown_ids or e.to not in all_screen_ids)
            ]
        else:
            fc_nodes = [(s.id, node_labels.get(s.id, s.id)) for s in rj.screens]
            fc_edges = [(e.frm, e.to, e.label or e.trigger) for e in rj.nav_edges]
        parts.append(flowchart(fc_nodes, fc_edges, direction="LR"))
        if capped:
            omitted = len(rj.screens) - _FLOWCHART_MAX_NODES
            parts.append("")
            parts.append(
                f"_+{omitted} more screen(s) omitted from the diagram "
                f"(showing the first {_FLOWCHART_MAX_NODES}); see `runtime-journeys.json` "
                f"for the complete graph._"
            )
        parts.append("")

    if rj.journeys:
        parts.append(heading("User Journeys", level=2))
        parts.append("")
        for j in rj.journeys:
            parts.append(f"**{j.id} — {j.persona}: {j.goal}**")
            parts.append("")
            parts.append(" → ".join(j.path))
            if j.narrative:
                parts.append("")
                parts.append(j.narrative)
            parts.append("")

    parts.append(heading("Reachability", level=2))
    parts.append("")
    n_crawl = sum(1 for s in rj.screens if s.reached_via == "crawl")
    n_agent = sum(1 for s in rj.screens if s.reached_via == "agent")
    parts.append(
        f"- {len(rj.screens)} screens reached ({n_crawl} by breadth crawl, {n_agent} agent-driven)."
    )
    parts.append(f"- {len(rj.external_edges)} external edge(s) recorded (not followed).")
    if rj.budget.get("truncated"):
        parts.append("- ⚠️ crawl was truncated by the page budget — coverage is partial.")
    parts.append("")

    _render_functional_controls(parts, rj)
    _render_role_coverage(parts, rj)

    if rec is not None:
        parts.append(heading("Reconciliation", level=2))
        parts.append("")
        parts.append(
            "Runtime observation vs the static functional-map (joined by canonical route):"
        )
        parts.append("")
        parts.append(
            md_table(
                ["Bucket", "Count", "Interfaces"],
                [
                    [
                        "Confirmed (static + runtime)",
                        str(len(rec.confirmed)),
                        ", ".join(rec.confirmed) or "—",
                    ],
                    [
                        "Runtime-only (static missed)",
                        str(len(rec.runtime_only)),
                        ", ".join(rec.runtime_only) or "—",
                    ],
                    [
                        "Static-only (unreachable?)",
                        str(len(rec.static_only)),
                        ", ".join(rec.static_only) or "—",
                    ],
                ],
            )
        )
        parts.append("")
        _render_control_reconciliation(parts, ctrl_rec)

    return "\n".join(parts).rstrip("\n") + "\n"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="runtime-explore-aggregate")
    # Single-perspective: one --crawl-graph. Multi-role: repeat --role-graph name=path
    # (one per role's separate crawl); the graphs are merged + role-tagged. Exactly one
    # of the two modes is used (--role-graph wins when present).
    p.add_argument("--crawl-graph", type=Path, default=None)
    p.add_argument(
        "--role-graph",
        action="append",
        default=[],
        metavar="ROLE=PATH",
        help="a role's crawl-graph.json as <role>=<path>; repeat for a multi-role merge",
    )
    p.add_argument("--journeys", required=True, type=Path)
    p.add_argument("--functional-map", type=Path, default=None)
    p.add_argument("--base-url", required=True)
    p.add_argument("--generated-at", default="")
    p.add_argument("--out", required=True, type=Path)
    return p


def _parse_role_graphs(specs: list[str]) -> tuple[dict[str, CrawlGraph] | None, str | None]:
    """Parse ``--role-graph <role>=<path>`` specs → ``({role: CrawlGraph}, None)`` or an
    error string. Each path must exist and hold a valid crawl-graph."""
    graphs: dict[str, CrawlGraph] = {}
    for spec in specs:
        role, sep, raw = spec.partition("=")
        if not sep or not role.strip() or not raw.strip():
            return None, f"--role-graph must be <role>=<path> (got {spec!r})"
        role = role.strip()
        path = Path(raw.strip())
        if not path.is_file():
            return None, f"role '{role}' crawl-graph not found: {path}"
        try:
            graphs[role] = CrawlGraph.model_validate(_load_json(path))
        except ValueError as exc:
            return None, f"role '{role}' crawl-graph failed validation: {exc}"
    return graphs, None


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.journeys.is_file():
        print(f"ERROR: journeys not found: {args.journeys}")
        return 2
    fm: dict[str, Any] | None = None
    if args.functional_map is not None:
        if not args.functional_map.is_file():
            print(f"ERROR: functional-map not found: {args.functional_map}")
            return 2
        fm = _load_json(args.functional_map)

    # Multi-role merge (--role-graph, repeatable) OR single-perspective (--crawl-graph).
    role_coverage: dict[str, int] | None = None
    if args.role_graph:
        role_graphs, err = _parse_role_graphs(args.role_graph)
        if err is not None:
            print(f"ERROR: {err}")
            return 2 if "not found" in err else 1
        assert role_graphs is not None
        graph, role_coverage = merge_role_graphs(role_graphs)
    else:
        if args.crawl_graph is None:
            print("ERROR: pass --crawl-graph <path> or one or more --role-graph <role>=<path>")
            return 2
        if not args.crawl_graph.is_file():
            print(f"ERROR: crawl-graph not found: {args.crawl_graph}")
            return 2
        try:
            graph = CrawlGraph.model_validate(_load_json(args.crawl_graph))
        except ValueError as exc:
            print(f"ERROR: crawl-graph failed validation: {exc}")
            return 1

    rj, md = aggregate(
        graph,
        _load_json(args.journeys),
        base_url=args.base_url,
        generated_at=args.generated_at,
        functional_map=fm,
        role_coverage=role_coverage,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "runtime-journeys.json").write_text(
        json.dumps(rj.model_dump(mode="json", by_alias=True), indent=2) + "\n", encoding="utf-8"
    )
    (args.out / "RUNTIME-DISCOVERY.md").write_text(md, encoding="utf-8")
    print(f"WROTE {args.out / 'runtime-journeys.json'}")
    print(f"WROTE {args.out / 'RUNTIME-DISCOVERY.md'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "ControlReconciliation",
    "Reconciliation",
    "aggregate",
    "main",
    "merge_role_graphs",
    "reconcile",
    "reconcile_controls",
    "route_name_from_url",
]
