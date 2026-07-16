"""Adapter — thread a ``runtime-journeys.json`` (mod-runtime-explore output) into the
app-mod scope deliverables ADDITIVELY (#1478).

``runtime_explore`` is an OPTIONAL runtime discovery step: when its
``runtime-journeys.json`` is present under ``.foundry/context/runtime/``,
``/mod-modernization-scope`` surfaces the runtime-observed journeys as an additive
section in ``MODERNIZATION.md``. Absent it, the scope output is **byte-identical** to
today — every function here is tolerant (absent / malformed → a no-op empty result),
so the artifact can never break the deterministic pipeline.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from typing import Any

from primitives._shared.modernization.render import heading, md_table


def discover_runtime_journeys(context_root: Path) -> Path | None:
    """Return ``<context_root>/runtime/runtime-journeys.json`` if it exists, else None.

    Optional artifact (from ``/mod-runtime-explore``). Absent → ``None`` and the runtime
    section is a no-op.
    """
    candidate = context_root / "runtime" / "runtime-journeys.json"
    return candidate if candidate.is_file() else None


def runtime_journeys_to_flows(rj_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract runtime-observed journeys as flow dicts (tolerant of a malformed artifact).

    Each flow: ``{id, persona, goal, steps: [<screen-url>, …], reached_via}``. Screen
    ids in a journey's ``path`` are resolved to their URLs via the ``screens`` table.
    A malformed artifact (missing/!list ``journeys``) degrades to ``[]`` — never raises.
    """
    journeys = rj_json.get("journeys")
    if not isinstance(journeys, list):
        return []
    screens = rj_json.get("screens")
    url_by_id: dict[str, str] = {}
    if isinstance(screens, list):
        for s in screens:
            if isinstance(s, dict) and s.get("id"):
                url_by_id[str(s["id"])] = str(s.get("url") or s["id"])
    flows: list[dict[str, Any]] = []
    for j in journeys:
        if not isinstance(j, dict) or not j.get("id"):
            continue
        raw_path = j.get("path")
        path: list[Any] = raw_path if isinstance(raw_path, list) else []
        steps = [url_by_id.get(str(sid), str(sid)) for sid in path]
        flows.append(
            {
                "id": str(j["id"]),
                "persona": str(j.get("persona") or ""),
                "goal": str(j.get("goal") or ""),
                "steps": steps,
                "reached_via": str(j.get("reached_via") or "agent"),
            }
        )
    return flows


def runtime_journeys_section(rj_json: dict[str, Any] | None) -> str:
    """Render an additive "Runtime-Observed Journeys" MODERNIZATION.md section.

    Empty string when *rj_json* is ``None`` or carries no journeys (so the caller
    appends nothing and the output stays byte-identical). When present, a table of the
    runtime journeys (id / persona / goal / screen path / provenance)."""
    if rj_json is None:
        return ""
    flows = runtime_journeys_to_flows(rj_json)
    if not flows:
        return ""
    rows = [
        [
            f["id"],
            f["persona"] or "—",
            f["goal"] or "—",
            " → ".join(f["steps"]) or "—",
            f["reached_via"],
        ]
        for f in flows
    ]
    lines = [
        heading("Runtime-Observed Journeys", level=2),
        "",
        (
            "Journeys discovered by driving the running app (`/mod-runtime-explore`) — "
            "complementing the static analysis. `agent` provenance = reached via an "
            "authed/deep flow the crawler could not reach alone:"
        ),
        "",
        md_table(["Id", "Persona", "Goal", "Screen path", "Reached via"], rows),
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "discover_runtime_journeys",
    "runtime_journeys_section",
    "runtime_journeys_to_flows",
]
