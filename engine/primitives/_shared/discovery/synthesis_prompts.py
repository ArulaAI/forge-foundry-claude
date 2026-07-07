"""Discovery engine — deterministic bounded source selection for synthesis prompts.

Implements the token-efficiency core of the M5 harness synthesis round
(design spec §4 "Bounded grounding"). Python, not the model, selects a
small bounded set of source files per synthesis section and assembles a
``synthesis_prompts.json``-conformant dict.

The five synthesis-driven sections (§1 Executive Summary, §2 System
Architecture, §8 Business Rules Catalog, §14 Technical Debt Inventory,
§15 Modernization Analysis) each receive a different slice of the
:class:`~primitives._shared.discovery.models.AnalysisDigest`:

- **§8 / §14 / §15** — the top-N highest-signal execution units (ranked
  by LoC, then complexity, then fan-in / ``called_by`` length), mirroring
  ``playbook.py::_TOP_N_MODERNIZATION = 5``.
- **§1 / §2** — all entry points + data stores from the correlated system
  map (the "entry points + correlated system map" specified in §4).

Source-file selection is **deterministic** (stable sort, no set-ordering
leakage) and **bounded** (never the whole tree; total unique paths capped
at ``_MAX_TOTAL_SOURCE_FILES``). Paths are repo-relative and are
filtered to those that actually exist under ``code_root`` before being
included.

Design spec:
    ``docs/superpowers/specs/2026-06-19-m5-playbook-harness-synthesis-design.md``
    §4 (bounded grounding) and §5 (schema contracts).

Schema reference:
    ``primitives/playbook_discovery/skill/references/schemas/synthesis_prompts.json``

Issue: `#863 <https://github.com/arula-ai/forge-foundry-primitives/issues/863>`_.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from primitives._shared.discovery.models import AnalysisDigest, ExecutionUnit

# ---------------------------------------------------------------------------
# Constants (mirrors playbook.py)
# ---------------------------------------------------------------------------

#: Number of highest-signal execution units surfaced for §8 / §14 / §15,
#: matching ``playbook.py::_TOP_N_MODERNIZATION``.
_TOP_N_MODERNIZATION: int = 5

#: Hard cap on the total unique source files emitted across all sections.
#: Prevents accidental whole-tree dumps when a codebase has many small units.
_MAX_TOTAL_SOURCE_FILES: int = 20

#: Section titles matching the ``_SECTIONS`` spine in ``playbook.py``.
_SECTION_TITLES: dict[int, str] = {
    1: "Executive Summary",
    2: "System Architecture",
    8: "Business Rules Catalog",
    14: "Technical Debt Inventory",
    15: "Modernization Analysis",
}

#: Soft word budget per section (per spec §4: "soft word budget ~250-350 words").
_SECTION_WORD_BUDGETS: dict[int, int] = {
    1: 300,
    2: 300,
    8: 300,
    14: 300,
    15: 300,
}

#: Citation instruction injected into every section prompt.
_CITATION_RULE: str = "Every non-obvious claim carries a `path:line` citation present in code_root."


# ---------------------------------------------------------------------------
# Ranking helpers — deterministic, mirrors playbook.py §15 ranking
# ---------------------------------------------------------------------------


def _rank_execution_units(units: list[ExecutionUnit]) -> list[ExecutionUnit]:
    """Return ``units`` ranked highest-signal first, stable-sorted.

    Ranking key (descending priority):

    1. **LoC** (highest first) — the primary signal, matches ``playbook.py``
       ``_section_15``'s ``sorted(units, key=lambda u: (-u.loc, u.name))``.
    2. **complexity** (highest first, ``None`` treated as 0) — secondary
       signal; the spec §4 mentions "LoC / fan-in / debt-signal the digest
       already computes".
    3. **fan-in** (``len(called_by)``, highest first) — tertiary signal.
    4. **name** (ascending) — tie-breaker for byte-stable determinism.

    The sort is stable and uses only fields present on every
    :class:`~primitives._shared.discovery.models.ExecutionUnit` instance.
    """
    return sorted(
        units,
        key=lambda u: (
            -u.loc,
            -(u.complexity if u.complexity is not None else 0),
            -len(u.called_by),
            u.name,
        ),
    )


# ---------------------------------------------------------------------------
# Source-file resolution helpers
# ---------------------------------------------------------------------------


def _resolve_source_files(
    paths: list[str],
    code_root: str,
) -> list[str]:
    """Return the subset of ``paths`` that resolve under ``code_root``.

    Each path is treated as a repo-relative POSIX path. A path is
    **included** when ``code_root / path`` resolves to an existing file.
    The returned list preserves the input order (after filtering) so
    the caller's deterministic ranking is not disrupted.

    Absolute paths, paths containing ``..``, and paths that do not resolve
    to existing files are skipped silently — Phase D will surface any broken
    citations in the synthesis body.
    """
    root = Path(code_root)
    seen: set[str] = set()
    result: list[str] = []
    for rel in paths:
        if not rel or ".." in rel.split("/") or Path(rel).is_absolute():
            continue
        if rel in seen:
            continue
        if (root / rel).exists():
            seen.add(rel)
            result.append(rel)
    return result


def _collect_unit_paths(units: list[ExecutionUnit]) -> list[str]:
    """Return de-duplicated ``source_path`` values from ``units``, order-preserving."""
    seen: set[str] = set()
    result: list[str] = []
    for u in units:
        if u.source_path and u.source_path not in seen:
            seen.add(u.source_path)
            result.append(u.source_path)
    return result


# ---------------------------------------------------------------------------
# Per-section digest slicers
# ---------------------------------------------------------------------------


def _slice_for_sections_8_14_15(
    top_units: list[ExecutionUnit],
) -> dict[str, Any]:
    """Return the ``digest_slice`` for §8 / §14 / §15.

    Contains only the top-N execution-unit records relevant to business
    rules, technical debt, and modernization analysis.
    """
    return {
        "execution_units": [
            {
                "name": u.name,
                "kind": u.kind,
                "source_path": u.source_path,
                "loc": u.loc,
                "complexity": u.complexity,
                "called_by": list(u.called_by),
                "calls_to": list(u.calls_to),
                "shared_component_deps": list(u.shared_component_deps),
            }
            for u in top_units
        ]
    }


def _slice_for_sections_1_2(analysis: AnalysisDigest) -> dict[str, Any]:
    """Return the ``digest_slice`` for §1 / §2.

    Contains entry points (the system's public interface — "what it exposes")
    and data stores (the correlated system map — "what it persists") per
    spec §4 "entry points + the correlated system map".
    """
    return {
        "entry_points": [
            {
                "name": ep.name,
                "kind": ep.kind,
                "target_unit": ep.target_unit,
                "security_requirements": list(ep.security_requirements),
            }
            for ep in sorted(analysis.entry_points, key=lambda e: e.name)
        ],
        "data_stores": [
            {
                "name": ds.name,
                "kind": ds.kind,
            }
            for ds in sorted(analysis.data_stores, key=lambda d: d.name)
        ],
        "execution_unit_count": len(analysis.execution_units),
        "shared_component_count": len(analysis.shared_components),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_synthesis_prompts(
    analysis: AnalysisDigest,
    *,
    run_id: str,
    word_budget: int = 300,
) -> dict[str, Any]:
    """Build a ``synthesis_prompts.json``-conformant dict from an :class:`AnalysisDigest`.

    Python (not the model) deterministically selects a bounded set of
    source files per section, applying the ranking logic from
    ``playbook.py::_TOP_N_MODERNIZATION``.

    Selection strategy per section:

    - **§8 Business Rules / §14 Technical Debt / §15 Modernization** — the
      top-N highest-LoC/complexity/fan-in execution units (at most
      ``_TOP_N_MODERNIZATION = 5``).
    - **§1 Executive Summary / §2 System Architecture** — source files from
      entry-point target execution units (fallback: top-N ranked units when
      no entry-point targets resolve to known units) to provide grounding
      for the architecture narrative.

    The emitted ``source_files`` are repo-relative, deduplicated across all
    sections, and capped at ``_MAX_TOTAL_SOURCE_FILES`` total unique paths.
    Only paths that exist under ``analysis.code_root`` are included.

    Args:
        analysis: Phase B :class:`AnalysisDigest` (the six abstract lists).
        run_id: Opaque run identifier passed through to the artifact; matches
            the ``--run-id`` CLI flag and the state-dir subdirectory name.
        word_budget: Soft word-count target per section body. Defaults to
            300 (the spec §4 midpoint of "~250-350 words"). Individual
            sections use the per-section value from ``_SECTION_WORD_BUDGETS``
            when it differs; ``word_budget`` overrides the default only for
            sections not listed in ``_SECTION_WORD_BUDGETS``.

    Returns:
        A ``dict`` conforming to the ``synthesis_prompts.json`` schema:
        ``{"run_id": …, "code_root": …, "sections": […]}``.

    Note:
        This function is **pure** except for filesystem-existence checks
        (to guard source-file paths against non-existent files). No LLM,
        no network, no subprocess, no filesystem writes.
    """
    # Rank all execution units once, descending by signal strength.
    all_units = list(analysis.execution_units)
    ranked_units = _rank_execution_units(all_units)
    top_units = ranked_units[:_TOP_N_MODERNIZATION]

    # §8 / §14 / §15 source files — top-N units' source paths.
    # Bounded at _TOP_N_MODERNIZATION paths (one per ranked unit).
    high_signal_paths = _collect_unit_paths(top_units)
    capped_high_signal = _resolve_source_files(high_signal_paths, analysis.code_root)

    # §1 / §2 source files — entry-point target units' paths, then fallback
    # to the highest-ranked units when entry points carry no path info.
    entry_target_names: set[str] = {
        ep.target_unit for ep in analysis.entry_points if ep.target_unit
    }
    entry_units = [u for u in ranked_units if u.name in entry_target_names]
    # Fallback: if no entry-point targets resolve to known units, use top ranked.
    summary_units = entry_units if entry_units else top_units
    summary_paths = _collect_unit_paths(summary_units)
    capped_summary = _resolve_source_files(summary_paths, analysis.code_root)

    # Enforce the total-unique-paths cap on the union of all sections combined.
    # Each section keeps its own independently-selected list; this check guards
    # against pathological cases where a future refactor widens the selection.
    # With _TOP_N_MODERNIZATION=5 the natural union is at most 10 unique paths
    # (well under _MAX_TOTAL_SOURCE_FILES=20).
    _union = set(capped_high_signal) | set(capped_summary)
    if len(_union) > _MAX_TOTAL_SOURCE_FILES:  # pragma: no cover — defensive
        # Trim the lower-priority (summary) list to bring the union in range.
        allowed = _MAX_TOTAL_SOURCE_FILES - len(set(capped_high_signal))
        capped_summary = capped_summary[: max(0, allowed)]

    # Digest slices — strict subsets of the digest.
    slice_8_14_15 = _slice_for_sections_8_14_15(top_units)
    slice_1_2 = _slice_for_sections_1_2(analysis)

    sections: list[dict[str, Any]] = [
        {
            "number": 1,
            "title": _SECTION_TITLES[1],
            "word_budget": _SECTION_WORD_BUDGETS.get(1, word_budget),
            "digest_slice": slice_1_2,
            "source_files": capped_summary,
            "citation_rule": _CITATION_RULE,
        },
        {
            "number": 2,
            "title": _SECTION_TITLES[2],
            "word_budget": _SECTION_WORD_BUDGETS.get(2, word_budget),
            "digest_slice": slice_1_2,
            "source_files": capped_summary,
            "citation_rule": _CITATION_RULE,
        },
        {
            "number": 8,
            "title": _SECTION_TITLES[8],
            "word_budget": _SECTION_WORD_BUDGETS.get(8, word_budget),
            "digest_slice": slice_8_14_15,
            "source_files": capped_high_signal,
            "citation_rule": _CITATION_RULE,
        },
        {
            "number": 14,
            "title": _SECTION_TITLES[14],
            "word_budget": _SECTION_WORD_BUDGETS.get(14, word_budget),
            "digest_slice": slice_8_14_15,
            "source_files": capped_high_signal,
            "citation_rule": _CITATION_RULE,
        },
        {
            "number": 15,
            "title": _SECTION_TITLES[15],
            "word_budget": _SECTION_WORD_BUDGETS.get(15, word_budget),
            "digest_slice": slice_8_14_15,
            "source_files": capped_high_signal,
            "citation_rule": _CITATION_RULE,
        },
    ]

    return {
        "run_id": run_id,
        "code_root": analysis.code_root,
        "sections": sections,
    }


__all__ = [
    "build_synthesis_prompts",
]
