"""M3 plan writer — render ``modernization-plan.md`` from the decomposition.

:func:`render_plan_md` assembles the human-readable modernization plan from the
deterministic :class:`DecompositionPlan`, the per-context narratives (the single
LLM stage, :mod:`narrative_synthesizer`), and an optional C8 sub-call result
(:mod:`sub_call`). Every line is produced by the Layer-1 render helpers
(:func:`heading` / :func:`md_table`) — Markdown text is Python-rendered, not
model-emitted (token-offload). Mermaid diagrams live in separate ``.mmd`` files
(:mod:`diagram_renderer`); this document references them but embeds none inline.

Pure and deterministic: no LLM, no clock, no filesystem.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives._shared.modernization import heading, md_table

if TYPE_CHECKING:
    from primitives.modernization_strategist.shared.models import DecompositionPlan
    from primitives.modernization_strategist.shared.sub_call import SubCallResult


def _context_section(plan: DecompositionPlan, narratives: dict[str, str]) -> list[str]:
    owned = set(plan.ownership.owned)
    shared = set(plan.ownership.shared)
    parts: list[str] = [heading("Bounded contexts", 2)]
    for ctx in plan.contexts:
        parts.append(heading(ctx.name, 3))
        narrative = narratives.get(ctx.name, "").strip()
        if narrative:
            parts.append(narrative)
        if ctx.rationale:
            parts.append(f"_Cohesion signal:_ {ctx.rationale}")
        rows = [
            [t, "shared" if t in shared else ("owned" if t in owned else "—")] for t in ctx.tables
        ]
        parts.append(md_table(["Table", "Ownership"], rows))
        ctx_tables = set(ctx.tables)
        ctx_aps = [ap for ap in plan.antipatterns if ctx_tables & set(ap.evidence)]
        if ctx_aps:
            ap_rows = [[ap.kind, ap.detail] for ap in ctx_aps]
            parts.append(md_table(["Antipattern", "Detail"], ap_rows))
    return parts


def _sequencing_section(plan: DecompositionPlan) -> list[str]:
    parts: list[str] = [heading("Sequencing roadmap", 2)]
    waves = sorted(plan.roadmap.waves, key=lambda w: w.wave)
    if waves:
        rows = [[str(w.wave), ", ".join(item.name for item in w.items) or "—"] for w in waves]
        parts.append(md_table(["Wave", "Contexts"], rows))
    if plan.roadmap.deferred:
        parts.append(heading("Deferred", 3))
        parts.append(
            md_table(
                ["Context", "Rationale"],
                [[i.name, i.rationale or "—"] for i in plan.roadmap.deferred],
            )
        )
    if plan.roadmap.never_extract:
        parts.append(heading("Never extract", 3))
        parts.append(
            md_table(
                ["Context", "Rationale"],
                [[i.name, i.rationale or "—"] for i in plan.roadmap.never_extract],
            )
        )
    return parts


def _risks_section(plan: DecompositionPlan, sub_call: SubCallResult | None) -> list[str]:
    if sub_call is None:
        # No C8 architecture-review fold ran. The "Risks per C8 review" heading
        # must be **absent** unless a real C8 pass produced findings (#946) — so
        # surface M3's own Stage-D antipatterns under their own honest heading
        # instead of mislabeling them as C8-reviewed risks. (The M0 roll-up keys
        # off "## Risks per C8 review"; renaming here stops it inheriting
        # phantom C8 findings.)
        parts: list[str] = [heading("Detected antipatterns", 2)]
        if not plan.antipatterns:
            parts.append("No decomposition antipatterns detected.")
            return parts
        parts.append(
            md_table(
                ["Antipattern", "Detail", "Evidence"],
                [[ap.kind, ap.detail, ", ".join(ap.evidence)] for ap in plan.antipatterns],
            )
        )
        return parts

    # A real C8 fold ran — this is the authoritative, C8-reviewed risk section.
    parts = [heading("Risks per C8 review", 2)]
    if sub_call.reviewed:
        rows = [
            [
                r.antipattern.kind,
                r.antipattern.detail,
                "yes" if r.reviewed_by_c8 else "no",
                r.c8_note or "—",
            ]
            for r in sub_call.reviewed
        ]
        parts.append(md_table(["Antipattern", "Detail", "C8-reviewed", "C8 note"], rows))
    else:
        parts.append("No decomposition antipatterns detected.")
    parts.append(f"_C8 overlap count:_ {sub_call.overlap_count}")
    if sub_call.c8_only:
        parts.append(heading("Risks C8 found that decomposition did not", 3))
        parts.append(
            md_table(
                ["Severity", "Affected", "Message"],
                [
                    [
                        str(f.get("severity", "—")),
                        str(f.get("affected", "—")),
                        str(f.get("message", "—")),
                    ]
                    for f in sub_call.c8_only
                ],
            )
        )
    return parts


def render_plan_md(
    plan: DecompositionPlan,
    narratives: dict[str, str],
    *,
    sub_call: SubCallResult | None = None,
) -> str:
    """Render the ``modernization-plan.md`` body as Markdown text.

    Sections: a per-context block (narrative + ownership table + that context's
    antipatterns), the sequencing roadmap, and a final risk section. When
    *sub_call* is given (a real C8 architecture-review fold ran), that section is
    "Risks per C8 review" with the dedup'd C8 findings; otherwise it is the
    honest "Detected antipatterns" roll-up of M3's own Stage-D antipatterns — the
    "Risks per C8 review" heading is absent unless C8 actually ran (#946).
    """
    parts: list[str] = [
        heading("Modernization plan", 1),
        f"{len(plan.contexts)} bounded context(s); "
        f"{len(plan.roadmap.waves)} sequencing wave(s); "
        f"{len(plan.antipatterns)} antipattern(s).",
    ]
    parts += _context_section(plan, narratives)
    parts += _sequencing_section(plan)
    parts += _risks_section(plan, sub_call)
    parts.append("See `diagrams/` for the decomposition, sequencing, and ownership Mermaid views.")
    return "\n\n".join(parts) + "\n"


__all__ = ["render_plan_md"]
