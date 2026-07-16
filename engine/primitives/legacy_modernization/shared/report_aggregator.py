"""M0 Stage-5 roll-up renderer — deterministic ``modernization-report.md`` from on-disk artifacts.

Invoked as::

    python -m primitives.legacy_modernization.shared.report_aggregator \\
        --context-dir .foundry/context/db \\
        --output .foundry/context/db/modernization-report.md

Reads the M1-M4 artifacts written under ``--context-dir``:

* ``schema.json``           — M1 ``SchemaDigest``
* ``schema-summary.md``     — M1 human-readable schema summary (referenced, not re-parsed)
* ``stored-procs.json``     — M2 ``StoredProcDigest``
* ``decomposition.json``    — M3 ``DecompositionPlan``
* ``modernization-plan.md`` — M3 human-readable plan (referenced)
* ``conversions/<sp>/conversion-plan.json`` — M4 ``ConversionPlan``
* ``conversions/<sp>/plan.md``              — M4 human-readable conversion notes (referenced)

Anti-hallucination invariant
-----------------------------
Every name (table, stored procedure, bounded context, conversion target) that
appears in the rendered ``modernization-report.md`` is drawn exclusively from
the on-disk artifacts listed above.  No names are invented; any artifact that
is missing causes a **graceful degradation** (a "stage not run" note rather
than a crash) — but no content from a missing artifact is fabricated.

Determinism
-----------
Same inputs → byte-identical output.  Sort order is explicit everywhere;
``generated_at`` timestamps from upstream artifacts are forwarded verbatim but
the report's own "Rendered at" line is either suppressed or supplied via
``--rendered-at`` so CI round-trips are byte-stable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

from primitives._shared.modernization.render import heading, md_table

# ---------------------------------------------------------------------------
# Artifact file names (canonical names per SPEC.md §"Outputs")
# ---------------------------------------------------------------------------

_SCHEMA_JSON = "schema.json"
_STORED_PROCS_JSON = "stored-procs.json"
_DECOMPOSITION_JSON = "decomposition.json"
_CONVERSIONS_DIR = "conversions"
_CONVERSION_PLAN_JSON = "conversion-plan.json"

# ---------------------------------------------------------------------------
# Section renderers — each returns a list[str] of Markdown lines
# ---------------------------------------------------------------------------


def _render_header(context_dir: Path) -> list[str]:
    """Render the report header.  Pure function of path presence."""
    return [
        heading("Modernization Report", 1),
        "",
        f"**Context directory:** `{context_dir}`",
        "",
    ]


# ---------------------------------------------------------------------------
# M1 — Schema overview
# ---------------------------------------------------------------------------


def _render_schema_section(context_dir: Path) -> list[str]:
    """Render the schema overview from ``schema.json``.

    Anti-hallucination: all names come from the parsed digest.
    Graceful degradation: if the file is absent, emits a "stage not run" note.
    """
    schema_path = context_dir / _SCHEMA_JSON
    parts: list[str] = [heading("Schema overview (M1)", 2), ""]

    if not schema_path.is_file():
        parts.append("_Stage not run — `schema.json` not found._")
        parts.append("")
        return parts

    raw = json.loads(schema_path.read_text(encoding="utf-8"))
    dialect: str = raw.get("source_dialect", "unknown")
    tables: list[dict[str, object]] = raw.get("tables", [])
    views: list[dict[str, object]] = raw.get("views", [])
    sequences: list[dict[str, object]] = raw.get("sequences", [])

    parts.append(f"**Source dialect:** {dialect}")
    parts.append(
        f"**Objects:** {len(tables)} table(s), {len(views)} view(s), {len(sequences)} sequence(s)"
    )
    parts.append("")

    # Table inventory sorted by name (deterministic)
    if tables:
        sorted_tables = sorted(tables, key=lambda t: str(t.get("name", "")))
        rows: list[Sequence[str]] = []
        for t in sorted_tables:
            name = str(t.get("name", ""))
            lob = str(t.get("lob_classification") or "unclassified")
            hotspot = "yes" if t.get("is_hotspot") else "no"
            orphan = "yes" if t.get("is_orphan") else "no"
            rows.append([name, lob, hotspot, orphan])
        parts.append(md_table(["Table", "LoB", "Hotspot", "Orphan"], rows))
        parts.append("")

    # Reference the M1 summary document by relative path
    summary_rel = (
        _SCHEMA_JSON.replace("json", "summary.md")
        .replace(".", "-summary.")
        .replace("json-summary.md", "summary.md")
    )
    # "schema.json" -> "schema-summary.md"
    summary_rel = "schema-summary.md"
    summary_path = context_dir / summary_rel
    if summary_path.is_file():
        parts.append(f"_Full schema summary: [`schema-summary.md`]({summary_rel})_")
        parts.append("")

    return parts


# ---------------------------------------------------------------------------
# M2 — Stored-procedure risk distribution
# ---------------------------------------------------------------------------


def _render_stored_procs_section(context_dir: Path) -> list[str]:
    """Render the stored-procedure risk distribution from ``stored-procs.json``.

    Anti-hallucination: all names come from the parsed digest.
    Graceful degradation: emits a "stage not run" note if file absent.
    """
    sp_path = context_dir / _STORED_PROCS_JSON
    parts: list[str] = [heading("Stored-procedure risk distribution (M2)", 2), ""]

    if not sp_path.is_file():
        parts.append("_Stage not run — `stored-procs.json` not found._")
        parts.append("")
        return parts

    raw = json.loads(sp_path.read_text(encoding="utf-8"))
    procedures: list[dict[str, object]] = raw.get("procedures", [])

    parts.append(f"**Procedures analyzed:** {len(procedures)}")
    parts.append("")

    if procedures:
        # Sort by risk_score.total descending, then name ascending (deterministic)
        def _risk_total(p: dict[str, object]) -> int:
            rs = p.get("risk_score")
            if not isinstance(rs, dict):
                return 0
            total_val = rs.get("total", 0)
            return int(total_val) if isinstance(total_val, int) else 0

        sorted_procs = sorted(
            procedures,
            key=lambda p: (-_risk_total(p), str(p.get("name", ""))),
        )
        rows: list[Sequence[str]] = []
        for proc in sorted_procs:
            name = str(proc.get("name", ""))
            risk_score = proc.get("risk_score", {})
            total = str(risk_score.get("total", "?") if isinstance(risk_score, dict) else "?")
            classification = str(proc.get("classification", "unknown"))
            io_shape = str(proc.get("io_shape", "unknown"))
            rows.append([name, total, classification, io_shape])
        parts.append(md_table(["Procedure", "Risk total", "Classification", "IO shape"], rows))
        parts.append("")

    # Reference the M2 summary document
    sp_summary = "stored-procs-summary.md"
    if (context_dir / sp_summary).is_file():
        parts.append(f"_Full SP summary: [`stored-procs-summary.md`]({sp_summary})_")
        parts.append("")

    return parts


# ---------------------------------------------------------------------------
# M3 — Decomposition plan
# ---------------------------------------------------------------------------


def _render_decomposition_section(context_dir: Path) -> list[str]:
    """Render the decomposition plan from ``decomposition.json``.

    Anti-hallucination: all names come from the parsed digest.
    Graceful degradation: emits a "stage not run" note if file absent.
    """
    decomp_path = context_dir / _DECOMPOSITION_JSON
    parts: list[str] = [heading("Proposed decomposition (M3)", 2), ""]

    if not decomp_path.is_file():
        parts.append("_Stage not run — `decomposition.json` not found._")
        parts.append("")
        return parts

    raw = json.loads(decomp_path.read_text(encoding="utf-8"))
    contexts: list[dict[str, object]] = raw.get("contexts", [])
    antipatterns: list[dict[str, object]] = raw.get("antipatterns", [])
    roadmap: dict[str, object] = raw.get("roadmap", {})

    # Bounded contexts
    parts.append(heading("Bounded contexts", 3))
    if contexts:
        rows: list[Sequence[str]] = []
        for ctx in sorted(contexts, key=lambda c: str(c.get("name", ""))):
            name = str(ctx.get("name", ""))
            tables_raw = ctx.get("tables", [])
            tables_list = list(tables_raw) if isinstance(tables_raw, list) else []
            table_list = ", ".join(sorted(str(t) for t in tables_list)) if tables_list else "—"
            rationale = str(ctx.get("rationale", "")).strip() or "—"
            rows.append([name, table_list, rationale])
        parts.append(md_table(["Context", "Tables", "Rationale"], rows))
    else:
        parts.append("_No bounded contexts detected._")
    parts.append("")

    # Sequencing roadmap
    parts.append(heading("Sequencing roadmap", 3))
    waves_obj = roadmap.get("waves") if isinstance(roadmap, dict) else None
    waves_raw = waves_obj if isinstance(waves_obj, list) else []
    waves: list[dict[str, object]] = [w for w in waves_raw if isinstance(w, dict)]
    if waves:

        def _wave_num(w: dict[str, object]) -> int:
            val = w.get("wave", 0)
            return int(val) if isinstance(val, int) else 0

        sorted_waves = sorted(waves, key=_wave_num)
        wave_rows: list[Sequence[str]] = []
        for w in sorted_waves:
            wave_num = str(w.get("wave", "?"))
            items_raw = w.get("items", [])
            items = list(items_raw) if isinstance(items_raw, list) else []
            item_names = (
                ", ".join(str(i.get("name", i) if isinstance(i, dict) else i) for i in items) or "—"
            )
            wave_rows.append([wave_num, item_names])
        parts.append(md_table(["Wave", "Contexts"], wave_rows))
    else:
        parts.append("_No sequencing waves._")
    parts.append("")

    # Antipatterns
    if antipatterns:
        parts.append(heading("Detected antipatterns", 3))
        ap_rows: list[Sequence[str]] = []
        for ap in sorted(antipatterns, key=lambda a: str(a.get("kind", ""))):
            kind = str(ap.get("kind", "unknown"))
            detail = str(ap.get("detail", "")).strip() or "—"
            evidence_raw = ap.get("evidence", [])
            evidence = list(evidence_raw) if isinstance(evidence_raw, list) else []
            ev_str = ", ".join(sorted(str(e) for e in evidence)) if evidence else "—"
            ap_rows.append([kind, detail, ev_str])
        parts.append(md_table(["Kind", "Detail", "Evidence"], ap_rows))
        parts.append("")

    # Reference the M3 plan document
    plan_md = "modernization-plan.md"
    if (context_dir / plan_md).is_file():
        parts.append(f"_Full modernization plan: [`modernization-plan.md`]({plan_md})_")
        parts.append("")

    # Inherited C8 sub-call findings (surfaced from the plan.md if present)
    parts.extend(_render_c8_findings_section(context_dir))

    return parts


# ---------------------------------------------------------------------------
# Inherited C8 sub-call findings (from M3's optional C8 pass)
# ---------------------------------------------------------------------------


def _render_c8_findings_section(context_dir: Path) -> list[str]:
    """Extract the 'Risks per C8 review' block from ``modernization-plan.md``.

    Per §3.4 orchestrator carve-out: M0 inherits whatever C8 findings the M3
    stage surfaced.  Those findings appear under a '## Risks per C8 review'
    heading inside ``modernization-plan.md``.  We extract that section verbatim
    so the consumer-facing report carries the review surface without M0 inventing
    any content.

    Anti-hallucination: verbatim copy from upstream artifact; no names invented.
    Graceful degradation: if the file is absent or the section does not exist,
    nothing is emitted.
    """
    plan_path = context_dir / "modernization-plan.md"
    if not plan_path.is_file():
        return []

    text = plan_path.read_text(encoding="utf-8")
    # Find the "Risks per C8 review" section
    marker = "## Risks per C8 review"
    idx = text.find(marker)
    if idx == -1:
        return []

    # Extract from that heading to the next same-level heading (or end of file)
    section_start = idx
    next_h2 = text.find("\n## ", idx + 1)
    section_text = text[section_start : next_h2 if next_h2 != -1 else len(text)].strip()

    if not section_text:
        return []

    return [heading("Inherited sub-call findings (C8, from M3)", 2), "", section_text, ""]


# ---------------------------------------------------------------------------
# M4 — Conversion previews
# ---------------------------------------------------------------------------


def _render_conversions_section(context_dir: Path) -> list[str]:
    """Render per-SP conversion previews from ``conversions/<sp>/conversion-plan.json``.

    Anti-hallucination: all SP names, targets, data_deps come from the
    ``ConversionPlan`` JSON artifacts written by M4.
    Graceful degradation: emits a "stage not run" note if the directory is absent.
    """
    conv_dir = context_dir / _CONVERSIONS_DIR
    parts: list[str] = [heading("Conversion previews (M4)", 2), ""]

    if not conv_dir.is_dir():
        parts.append("_Stage not run — `conversions/` directory not found._")
        parts.append("")
        return parts

    # Discover SP conversion plans. M4 writes the plan JSON in one of two layouts
    # (#1345): the NESTED form `conversions/<sp>/conversion-plan.json` (one dir per
    # SP), and the FLAT form `conversions/conversion-plan.json` directly under the
    # conversions dir (when M4 is pointed at a flat `--output-dir`, as the workbench
    # playbook instructs). We must discover both — the flat layout was previously
    # rendered as "No SP conversion artifacts found", silently dropping a completed
    # conversion from the headline report. A conversion "unit" is any directory that
    # carries a `conversion-plan.json`; this includes the conversions dir itself for
    # the flat case. Scaffold-only subdirs (e.g. `<sp>/app/routers/...`) carry no
    # plan JSON and are correctly excluded (#1197 6b).
    plan_units: list[tuple[str, Path, str]] = []  # (plan-link subdir, plan_json, section-slug)
    # Flat: a plan JSON directly under conversions/.
    if (conv_dir / _CONVERSION_PLAN_JSON).is_file():
        plan_units.append(("", conv_dir / _CONVERSION_PLAN_JSON, conv_dir.name))
    # Nested: one plan JSON per SP subdirectory.
    for p in sorted(conv_dir.iterdir()):
        if p.is_dir() and (p / _CONVERSION_PLAN_JSON).is_file():
            plan_units.append((p.name, p / _CONVERSION_PLAN_JSON, p.name))

    if not plan_units:
        parts.append("_No SP conversion artifacts found under `conversions/`._")
        parts.append("")
        return parts

    parts.append(f"**Conversions available:** {len(plan_units)}")
    parts.append("")

    # Summary table across all SPs
    summary_rows: list[Sequence[str]] = []
    sp_details: list[tuple[str, dict[str, object]]] = []

    for link_subdir, plan_json, slug in plan_units:
        raw: dict[str, object] = json.loads(plan_json.read_text(encoding="utf-8"))
        sp_name = str(raw.get("sp_name", slug))
        target = str(raw.get("target", "unknown"))
        effort_raw = raw.get("effort", {})
        effort_size = str(effort_raw.get("size", "?") if isinstance(effort_raw, dict) else "?")
        # plan.md sits beside conversion-plan.json — in the SP subdir (nested) or
        # directly under conversions/ (flat).
        plan_rel = f"conversions/{link_subdir}/plan.md" if link_subdir else "conversions/plan.md"
        plan_link = f"[plan.md]({plan_rel})"
        summary_rows.append([sp_name, target, effort_size, plan_link])
        sp_details.append((slug, raw))

    parts.append(md_table(["Procedure", "Target", "Effort", "Details"], summary_rows))
    parts.append("")

    # Per-SP detail block (data_deps + gotchas — all from the artifact)
    for sp_dir_name, raw in sp_details:
        sp_name = str(raw.get("sp_name", sp_dir_name))
        parts.append(heading(sp_name, 3))
        target = str(raw.get("target", "unknown"))
        parts.append(f"**Target:** {target}")

        data_deps: list[object] = raw.get("data_deps", []) or []  # type: ignore[assignment]
        if data_deps:
            dep_str = ", ".join(sorted(str(x) for x in data_deps))
            parts.append(f"**Data dependencies:** {dep_str}")

        txn_boundaries: list[object] = raw.get("txn_boundaries", []) or []  # type: ignore[assignment]
        if txn_boundaries:
            parts.append(f"**Transaction boundaries:** {len(txn_boundaries)} step(s)")

        gotchas: list[object] = raw.get("gotchas", []) or []  # type: ignore[assignment]
        if gotchas:
            parts.append("**Migration gotchas:**")
            for g in gotchas:
                parts.append(f"- {g}")

        effort_raw = raw.get("effort", {})
        if isinstance(effort_raw, dict) and effort_raw:
            size = str(effort_raw.get("size", "?"))
            rationale = str(effort_raw.get("rationale", "")).strip()
            parts.append(f"**Effort estimate:** {size}" + (f" — {rationale}" if rationale else ""))

        parts.append("")

    return parts


# ---------------------------------------------------------------------------
# Skipped stages
# ---------------------------------------------------------------------------


def _detect_skipped_stages(context_dir: Path) -> list[str]:
    """Infer which optional stages were skipped from absent artifacts.

    Returns a list of human-readable stage descriptions for stages whose
    primary artifact is missing (i.e., the stage was skipped or failed).
    """
    skipped: list[str] = []
    if not (context_dir / _STORED_PROCS_JSON).is_file():
        skipped.append("stored-procs (M2) — `stored-procs.json` not found")
    if not (context_dir / _DECOMPOSITION_JSON).is_file():
        skipped.append("strategy (M3) — `decomposition.json` not found")
    conv_dir = context_dir / _CONVERSIONS_DIR
    if not conv_dir.is_dir() or not any(conv_dir.iterdir()):
        skipped.append("conversions (M4) — `conversions/` directory empty or absent")
    return skipped


def _render_skipped_section(skipped: list[str]) -> list[str]:
    """Render the 'Skipped stages' section."""
    parts: list[str] = [heading("Skipped stages", 2), ""]
    if not skipped:
        parts.append("_No optional stages were skipped._")
    else:
        for stage in skipped:
            parts.append(f"- {stage}")
    parts.append("")
    return parts


# ---------------------------------------------------------------------------
# Reproduction recipe
# ---------------------------------------------------------------------------


def _render_recipe_section(context_dir: Path) -> list[str]:
    """Render the reproduction recipe section.

    The recipe is a deterministic description of how to re-run the pipeline
    to reproduce this report.  It does not contain any LLM-generated content.
    """
    parts: list[str] = [heading("Reproduction recipe", 2), ""]
    parts.append(
        "Re-run the M0 pipeline to reproduce this report.  "
        "All stage artifacts are under the context directory shown above."
    )
    parts.append("")
    parts.append("```bash")
    parts.append("# Run the M1-M4 chain and roll-up (via the SKILL.md / C7 workflow)")
    parts.append("# Then regenerate this report from the on-disk artifacts:")
    parts.append(
        f"python -m primitives.legacy_modernization.shared.report_aggregator "
        f"--context-dir {context_dir} "
        f"--output {context_dir}/modernization-report.md"
    )
    parts.append("```")
    parts.append("")
    return parts


# ---------------------------------------------------------------------------
# Top-level aggregator
# ---------------------------------------------------------------------------


def aggregate(
    context_dir: Path,
    output: Path,
) -> None:
    """Render ``modernization-report.md`` from on-disk M1-M4 artifacts.

    Anti-hallucination: every name in the report traces back to an on-disk
    artifact.  Missing artifacts are noted gracefully; no content is invented.

    Args:
        context_dir: directory holding the M1-M4 artifacts (e.g.
            ``.foundry/context/db/``).
        output: path to write the rendered ``modernization-report.md``.
    """
    sections: list[list[str]] = [
        _render_header(context_dir),
        _render_schema_section(context_dir),
        _render_stored_procs_section(context_dir),
        _render_decomposition_section(context_dir),
        _render_conversions_section(context_dir),
    ]

    # Skipped stages (inferred from absent artifacts)
    skipped = _detect_skipped_stages(context_dir)
    sections.append(_render_skipped_section(skipped))

    # Reproduction recipe
    sections.append(_render_recipe_section(context_dir))

    # Join all sections
    lines: list[str] = []
    for section in sections:
        lines.extend(section)

    # Ensure single trailing newline (byte-stable)
    report = "\n".join(lines).rstrip("\n") + "\n"

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI entry point (python -m primitives.legacy_modernization.shared.report_aggregator)
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="report_aggregator",
        description=(
            "M0 Stage-5 roll-up renderer: reads M1-M4 artifacts from "
            "--context-dir and writes modernization-report.md to --output."
        ),
    )
    parser.add_argument(
        "--context-dir",
        required=True,
        type=Path,
        metavar="DIR",
        help="Directory holding the M1-M4 artifacts (e.g. .foundry/context/db/).",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        metavar="PATH",
        help="Output path for modernization-report.md.",
    )
    return parser


def main(argv: list[str] | None = None) -> Literal[0, 1]:
    """CLI entry point.  Returns exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    context_dir: Path = args.context_dir.resolve()
    output: Path = args.output.resolve()

    if not context_dir.exists():
        print(
            f"ERROR: --context-dir {context_dir} does not exist.",
            file=sys.stderr,
        )
        return 1

    try:
        aggregate(context_dir=context_dir, output=output)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"WROTE {output}", file=sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "aggregate",
    "main",
]
