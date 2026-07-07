"""M2 Stage G — ``report_renderer`` (JSON + markdown summary).

Renders a :class:`StoredProcDigest` to two artifacts on disk:

* ``stored-procs.json`` — machine-readable digest (Pydantic v2
  ``model_dump_json``). Round-trippable through
  ``StoredProcDigest.model_validate_json``.
* ``stored-procs-summary.md`` — human-readable markdown summary.
  Procedures sorted by ``risk_score.total`` descending so the
  highest-conversion-effort SPs surface first — that is the call-out
  M3 / M4 reviewers want when triaging which procs to attack next.

The two renderers are pure functions of the
:class:`StoredProcDigest` + an output ``Path``; they create parent
directories as needed (the CLI in M2-12 wires ``--output-dir`` to
``.foundry/context/db/`` by default). Determinism is preserved by
sorting derived groupings (classification buckets, side-effect
inventory) for byte-stable output run-over-run.

SARIF emission is deliberately deferred from v1 — the issue body
flags it as optional scope and the downstream consumers (M3 / M4 /
S60) all read the JSON digest directly. Adding SARIF before there's
a concrete BFSI consumer asking for it would be premature.

Design spec §M2 Outputs + SPEC.md §"Output".
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.stored_proc_analyze.shared.models import (
        StoredProc,
        StoredProcDigest,
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_UNKNOWN_DIALECT = "unknown"
"""Header substitute when ``source_dialect`` is empty.

The model contract allows any non-empty string (sqlglot owns the
dialect registry per SPEC §"Dialect coverage"), but a zero-length
header would render as ``**Source dialect:** ` ` `` in the markdown
— hard to read in code review. We surface a placeholder so the
header section stays readable even on a malformed digest.
"""

_MARKDOWN_TOP_RISK_LIMIT = 20
"""Top-N procedures shown in the markdown summary's headline table.

Twenty is the BFSI-friendly default: large enough to surface the
high-conversion-effort hub procs in a core-banking dump (typically
5-15 per schema) without overflowing the section on a phone-sized
viewport. The JSON digest always carries the full inventory; only
the headline table is capped.
"""


# ---------------------------------------------------------------------------
# JSON renderer
# ---------------------------------------------------------------------------


def render_json(digest: StoredProcDigest, out_path: Path) -> None:
    """Write the digest as pretty-printed JSON, round-trippable.

    Uses :meth:`StoredProcDigest.model_dump_json` with ``indent=2`` so
    the file is human-diffable in code review and so future Pydantic-v2
    serialiser changes do not silently re-order keys. Round-trip is
    asserted by :func:`test_render_json_is_roundtrippable`.

    Args:
        digest: The :class:`StoredProcDigest` to write.
        out_path: Destination file path. Parent directories are created
            as needed; an existing file is overwritten.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = digest.model_dump_json(indent=2, by_alias=True)
    out_path.write_text(payload, encoding="utf-8")


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def render_markdown(digest: StoredProcDigest, out_path: Path) -> None:
    """Write a human-readable markdown summary of the digest.

    Sections (per SPEC.md §"Output"):

    * Header — dialect, counts (total / parsed / unparseable),
      generated_at.
    * Top-:data:`_MARKDOWN_TOP_RISK_LIMIT` procedures by
      ``risk_score.total`` descending. Ties broken by qualified name
      (schema.name) for determinism — the BFSI invariant is byte-stable
      output run-over-run so a regression in the scorer surfaces in
      diff review immediately.
    * Procedures grouped by ``classification`` (closed-vocab M2
      Classification Literal).
    * Side-effect inventory — count of SPs that carry each
      :data:`SideEffect` literal.
    * Unparseable procedures — those tagged
      ``parse_status="unparseable"`` so a reviewer can spot fixture
      breakage at a glance.

    Args:
        digest: The :class:`StoredProcDigest` to summarise.
        out_path: Destination file path. Parent directories are created
            as needed.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_build_markdown(digest), encoding="utf-8")


def _build_markdown(digest: StoredProcDigest) -> str:
    lines: list[str] = []
    lines += _build_markdown_header(digest)
    lines += _build_markdown_top_risk(digest)
    lines += _build_markdown_classification_groups(digest)
    lines += _build_markdown_side_effects(digest)
    lines += _build_markdown_unparseable(digest)
    # Always end with a newline so POSIX text-file conventions are met.
    return "\n".join(lines) + "\n"


def _build_markdown_header(digest: StoredProcDigest) -> list[str]:
    """Top-of-file metadata block."""
    generated = digest.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    dialect = digest.source_dialect or _UNKNOWN_DIALECT
    parsed = sum(1 for p in digest.procedures if p.parse_status == "parsed")
    unparseable = sum(1 for p in digest.procedures if p.parse_status == "unparseable")
    return [
        "# Stored Procedure Digest",
        "",
        f"**Source dialect:** `{dialect}`  ",
        f"**Schema version:** `{digest.schema_version}`  ",
        f"**Generated:** {generated}",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|---|---|",
        f"| Procedures (total) | {len(digest.procedures)} |",
        f"| Procedures (parsed) | {parsed} |",
        f"| Procedures (unparseable) | {unparseable} |",
        "",
    ]


def _build_markdown_top_risk(digest: StoredProcDigest) -> list[str]:
    """Top-N procedures sorted by ``risk_score.total`` desc.

    The headline section of the report. Tied scores break to the
    qualified ``schema.name`` ascending for determinism — the BFSI
    invariant says identical input → byte-identical output.

    Unparseable SPs carry the sentinel total (:data:`UNPARSEABLE_RISK_SENTINEL`)
    so they sort to the top of the table.  In the rendered table the risk-total
    cell is replaced with ``⚠ unknown`` so readers cannot mistake the sentinel
    number for a scored value (#871).
    """
    if not digest.procedures:
        return [
            "## Top procedures by conversion risk",
            "",
            "_No procedures._",
            "",
        ]
    procs = sorted(
        digest.procedures,
        key=lambda p: (-p.risk_score.total, p.schema_, p.name),
    )
    head_limit = min(len(procs), _MARKDOWN_TOP_RISK_LIMIT)
    out = [
        "## Top procedures by conversion risk",
        "",
        f"Top {head_limit} procedures sorted by `risk_score.total` descending.",
        "",
        "| # | Procedure | Classification | IO shape | Risk total | Cyclomatic | Side effects |",
        "|---|---|---|---|---|---|---|",
    ]
    for idx, proc in enumerate(procs[:head_limit], start=1):
        qn = _qn(proc.schema_, proc.name)
        side_effects = ", ".join(proc.side_effects) if proc.side_effects else "—"
        # Unparseable SPs carry a sentinel total — render as "⚠ unknown" so
        # readers can't confuse it with a scored value (#871).
        if proc.parse_status == "unparseable":
            risk_cell = "⚠ unknown"
        else:
            risk_cell = str(proc.risk_score.total)
        out.append(
            f"| {idx} | `{qn}` | {proc.classification} | {proc.io_shape} | "
            f"{risk_cell} | {proc.cyclomatic} | {side_effects} |"
        )
    out.append("")
    return out


def _build_markdown_classification_groups(digest: StoredProcDigest) -> list[str]:
    """Procedures grouped by ``classification``."""
    if not digest.procedures:
        return ["## Procedures by classification", "", "_No procedures._", ""]
    groups: dict[str, list[StoredProc]] = defaultdict(list)
    for proc in digest.procedures:
        groups[proc.classification].append(proc)

    out = ["## Procedures by classification", ""]
    for classification in sorted(groups):
        members = sorted(groups[classification], key=lambda p: (p.schema_, p.name))
        out.append(f"### {classification} ({len(members)})")
        out.append("")
        out.append("| Procedure | Risk total | IO shape |")
        out.append("|---|---|---|")
        for proc in members:
            qn = _qn(proc.schema_, proc.name)
            out.append(f"| `{qn}` | {proc.risk_score.total} | {proc.io_shape} |")
        out.append("")
    return out


def _build_markdown_side_effects(digest: StoredProcDigest) -> list[str]:
    """Count of SPs that carry each :data:`SideEffect` literal."""
    if not digest.procedures:
        return ["## Side-effect inventory", "", "_No procedures._", ""]
    counter: Counter[str] = Counter()
    for proc in digest.procedures:
        if not proc.side_effects:
            counter["(none)"] += 1
        else:
            for effect in proc.side_effects:
                counter[effect] += 1

    out = [
        "## Side-effect inventory",
        "",
        "| Side effect | Procedures |",
        "|---|---|",
    ]
    # Sort deterministically: empty marker last, otherwise alphabetical.
    for label in sorted(counter, key=lambda e: (e == "(none)", e)):
        out.append(f"| `{label}` | {counter[label]} |")
    out.append("")
    return out


def _build_markdown_unparseable(digest: StoredProcDigest) -> list[str]:
    """Procedures with ``parse_status="unparseable"``, sorted by qualified name.

    Surfaces a visible "⚠ unparseable — risk unknown, review manually" flag
    for each affected SP so reviewers can never mistake a parse failure for a
    safely-low-risk procedure (#871).
    """
    unparseable = sorted(
        (p for p in digest.procedures if p.parse_status == "unparseable"),
        key=lambda p: (p.schema_, p.name),
    )
    if not unparseable:
        return ["## Unparseable procedures", "", "_No unparseable procedures._", ""]
    out = [
        "## Unparseable procedures",
        "",
        f"{len(unparseable)} procedure(s) Stage A could not parse.  "
        "Risk is **unknown** — these procedures have been ranked at the top of the "
        "conversion-risk table and must be reviewed manually before conversion.",
        "",
        "> ⚠ unparseable — risk unknown, review manually",
        "",
        "| Procedure | Language |",
        "|---|---|",
    ]
    for proc in unparseable:
        out.append(f"| `{_qn(proc.schema_, proc.name)}` | {proc.language} |")
    out.append("")
    return out


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _qn(schema: str, name: str) -> str:
    """Return the qualified name ``schema.name`` (or bare ``name`` when no schema).

    Mirrors :func:`primitives.db_schema_analyze.shared.report_renderer._qn`
    so the two M-tier digests render qualified names identically.
    """
    if schema:
        return f"{schema}.{name}"
    return name


__all__ = [
    "render_json",
    "render_markdown",
]
