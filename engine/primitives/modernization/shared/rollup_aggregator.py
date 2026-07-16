"""SP4 ``/mod-modernization`` roll-up renderer — deterministic ``MODERNIZATION-SUMMARY.md``.

Invoked by the skill body after every stage has run (or been skipped/failed)::

    python -m primitives.modernization.shared.rollup_aggregator \\
        --state .foundry/context/modernization/.state/<run-id>/state.json \\
        --output .foundry/context/MODERNIZATION-SUMMARY.md

The orchestrator (`/mod-modernization`) drives five analyzer stages IN ORDER,
persisting each stage's ``pass`` / ``fail`` / ``skipped`` status + durable artifact
path into a resumable state file (``state.json``).  This module is the ONLY Python
component of the primitive (charter §4.1 orchestrator-shape carve-out): it reads that
state file and rolls the per-stage outcomes up into a single top-level summary.

Resumability contract
---------------------
The state file is the resumable ledger.  Each ``StageStatus`` records whether the
stage is ``complete`` (a durable artifact exists on disk), ``failed`` (ran but
errored — the chain halts here and a re-invoke restarts from this stage), or
``skipped`` (legitimately not applicable, e.g. no DB → the schema stage reports
"no schema" and the chain CONTINUES).  On re-invoke, the body reads this file and
skips every stage already marked ``complete`` (or ``skipped``) — only ``failed`` /
``pending`` stages re-run.

The DISCOVERY-PATH invariant (guards the #1291-class silent gap)
---------------------------------------------------------------
``ARTIFACT_DISCOVERY_PATHS`` below MUST stay byte-identical to the paths the
downstream synthesizer (``mod-modernization-scope``) auto-discovers under its
``--context-root`` (``primitives.modernization_scope.shared.pipeline._DISCOVERY``).
If the orchestrator persists an analyzer's artifact anywhere else, ``mod-
modernization-scope`` discovers nothing and the whole chain silently produces an
empty system map.  ``tests/test_modernization_rollup.py`` asserts the two path
tables line up so a drift fails CI loudly.

Anti-hallucination invariant
---------------------------
Every stage name, status, and artifact path in the rendered summary is drawn
exclusively from the on-disk state file.  Nothing is invented; a state file with
no stages renders an explicit "no stages recorded" note rather than fabricating one.

Multi-repo (MR7, #1556)
-----------------------
When the run spanned a repository SET (a ``.foundry/repos.yaml`` / ``--repo`` set),
the state file carries an optional ``repos`` list — one entry per analysed repo
(``{role, name, stages}``). The summary then inserts a per-repo section (each repo's
role-appropriate analyzer outcomes) after the top-level roll-up, which tracks the
SHARED scope synthesis (SP2 correlates all repos into ONE system map). The ``repos``
key is ADDITIVE: absent it, the render is byte-identical to the single-repo output
(the #1408 invariant).

Determinism
-----------
Same state file → byte-identical output.  Stages render in their recorded
(pipeline) order.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Stage / status vocabulary
# ---------------------------------------------------------------------------

#: The five orchestrated stages, in pipeline order.  Each maps a stage id →
#: (human label, the downstream slash command the skill body dispatches).
STAGE_ORDER: tuple[str, ...] = (
    "functional-map",
    "db-schema",
    "api-contract",
    "app-analysis",
    "scope",
)

#: The optional ``--runtime-explore`` pre-stage. It is NOT part of the fixed
#: STAGE_ORDER — it renders in the summary ONLY when present in state.json (i.e. the
#: flag was used), so a run that never used it is byte-identical (backward-compatible).
#: It runs BEFORE the source-analysis stages, so it renders first when present.
_OPTIONAL_PRE_STAGE = "runtime-explore"

_STAGE_LABELS: dict[str, str] = {
    "runtime-explore": "Runtime explore (mod-runtime-explore, --runtime-explore)",
    "functional-map": "Functional map (S61 /functional-map)",
    "db-schema": "DB schema (M1 /mod-db-schema-analyze)",
    "api-contract": "API contract (S40 /api-contract-review)",
    "app-analysis": "App analysis (SP3 /mod-app-analysis)",
    "scope": "Modernization scope (SP2 /mod-modernization-scope)",
}

#: Where the orchestrator persists each analyzer's durable artifact — **relative
#: to the scope synthesizer's ``--context-root`` (default ``.foundry/context``)**.
#: These MUST match ``modernization_scope.shared.pipeline._DISCOVERY`` (guarded by
#: ``tests/test_modernization_rollup.py::test_artifact_paths_match_scope_consumer``).
#: ``{repo}`` is substituted with the analysed repo's basename for the app-digest.
ARTIFACT_DISCOVERY_PATHS: dict[str, str] = {
    "runtime-explore": "runtime/runtime-journeys.json",
    "functional-map": "functional-map/functional-map.json",
    "db-schema": "db/schema.json",
    "api-contract": "api/api-contract.json",
    "app-analysis": "modernization/{repo}/app-digest.json",
    # The scope stage is the synthesizer itself; its primary durable deliverable
    # is MODERNIZATION.md (+ system-map.json, scope/, adr/, ROADMAP.md).
    "scope": "modernization/MODERNIZATION.md",
}

#: Valid per-stage statuses.
_VALID_STATUSES = frozenset({"complete", "failed", "skipped", "pending"})

_STATUS_GLYPH: dict[str, str] = {
    "complete": "✅ complete",
    "failed": "❌ failed",
    "skipped": "⏭️ skipped",
    "pending": "⏳ pending",
}

#: The per-repo analyzer stages, in the order the orchestrator runs them for a
#: multi-repo (``.foundry/repos.yaml`` / ``--repo``) run. A repo runs only the
#: stages its role calls for (MR7): every repo → app-analysis; a ``ui`` repo also
#: → functional-map; an ``api`` repo also → api-contract; a ``db`` repo also →
#: db-schema; an ``iac`` repo → app-analysis only. The multi-repo summary renders
#: only the stages actually recorded for each repo (nothing is invented).
_PER_REPO_STAGE_ORDER: tuple[str, ...] = (
    "functional-map",
    "db-schema",
    "api-contract",
    "app-analysis",
)

_PER_REPO_STAGE_LABELS: dict[str, str] = {
    "functional-map": "Functional map (/functional-map)",
    "db-schema": "DB schema (/mod-db-schema-analyze)",
    "api-contract": "API contract (/api-contract-review)",
    "app-analysis": "App analysis (/mod-app-analysis)",
}


# ---------------------------------------------------------------------------
# Roll-up
# ---------------------------------------------------------------------------


def _load_state(state_path: Path) -> dict[str, object]:
    """Load and shallow-validate the resumable state file."""
    obj = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"{state_path} did not parse to a JSON object")
    return obj


def _stages_map(state: dict[str, object]) -> dict[str, dict[str, object]]:
    """Return the ``stages`` mapping, keeping only well-formed dict entries."""
    raw = state.get("stages")
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _stage_status(stages: dict[str, dict[str, object]], stage_id: str) -> str:
    """The recorded status of *stage_id* (``pending`` when absent/unknown)."""
    entry = stages.get(stage_id)
    if entry is None:
        return "pending"
    status = str(entry.get("status", "pending"))
    return status if status in _VALID_STATUSES else "pending"


def _rendered_stage_order(state: dict[str, object]) -> tuple[str, ...]:
    """The stage ids to render, in order. The optional ``runtime-explore`` pre-stage is
    prepended ONLY when it appears in the state file (the ``--runtime-explore`` flag was
    used); a run without it renders exactly the five source stages (backward-compatible).
    """
    if _OPTIONAL_PRE_STAGE in _stages_map(state):
        return (_OPTIONAL_PRE_STAGE, *STAGE_ORDER)
    return STAGE_ORDER


def _stage_rows(state: dict[str, object]) -> list[tuple[str, str, str, str]]:
    """Return ``(label, status, note, artifact)`` rows in pipeline order.

    Stages absent from the state file render as ``pending`` (not-yet-run) so the
    summary always shows all five rows — a resumed run's completed prefix plus the
    remaining work. The optional ``runtime-explore`` pre-stage row appears only when it
    was recorded (the ``--runtime-explore`` flag was used).
    """
    stages = _stages_map(state)
    rows: list[tuple[str, str, str, str]] = []
    for stage_id in _rendered_stage_order(state):
        label = _STAGE_LABELS[stage_id]
        entry = stages.get(stage_id, {})
        status = _stage_status(stages, stage_id)
        note = str(entry.get("note", "")).strip()
        artifact = str(entry.get("artifact", "")).strip()
        glyph = _STATUS_GLYPH.get(status, status)
        rows.append((label, glyph, note or "—", artifact or "—"))
    return rows


def _overall(state: dict[str, object]) -> str:
    """Derive the roll-up headline from the per-stage statuses.

    * any ``failed`` → ``BLOCKED`` (the chain halted; resume from that stage)
    * every stage ``complete``/``skipped`` (none pending) → ``COMPLETE``
    * otherwise → ``IN PROGRESS``
    """
    stages = _stages_map(state)
    statuses = [_stage_status(stages, stage_id) for stage_id in _rendered_stage_order(state)]
    if "failed" in statuses:
        return "BLOCKED"
    if all(s in {"complete", "skipped"} for s in statuses):
        return "COMPLETE"
    return "IN PROGRESS"


# ---------------------------------------------------------------------------
# Multi-repo (MR7) roll-up — additive; single-repo path is byte-identical
# ---------------------------------------------------------------------------


def _repos_list(state: dict[str, object]) -> list[dict[str, object]]:
    """Return the optional ``repos`` list, keeping only well-formed dict entries.

    A run over a ``.foundry/repos.yaml`` / ``--repo`` set records a ``repos``
    list — one entry per analysed repository, each ``{role, name, stages}``. Its
    ABSENCE is the single-repo default (the whole state renders exactly as before,
    #1408 additive invariant): ``_is_multi_repo`` is false and the multi-repo
    section never renders.
    """
    raw = state.get("repos")
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict)]


def _is_multi_repo(state: dict[str, object]) -> bool:
    """True iff the state records a non-empty ``repos`` list (a multi-repo run)."""
    return bool(_repos_list(state))


def _repo_stage_rows(repo: dict[str, object]) -> list[tuple[str, str, str, str]]:
    """Return ``(label, status_glyph, note, artifact)`` rows for ONE repo's stages.

    Only the stages actually recorded under this repo's ``stages`` map render —
    a ``db`` repo that ran only db-schema + app-analysis shows exactly those two
    rows (nothing is invented; anti-hallucination). Rows render in the fixed
    per-repo pipeline order for the stages present.
    """
    raw_stages = repo.get("stages")
    stages: dict[str, dict[str, object]] = (
        {str(k): v for k, v in raw_stages.items() if isinstance(v, dict)}
        if isinstance(raw_stages, dict)
        else {}
    )
    rows: list[tuple[str, str, str, str]] = []
    for stage_id in _PER_REPO_STAGE_ORDER:
        entry = stages.get(stage_id)
        if entry is None:
            continue
        status = str(entry.get("status", "pending"))
        status = status if status in _VALID_STATUSES else "pending"
        note = str(entry.get("note", "")).strip()
        artifact = str(entry.get("artifact", "")).strip()
        glyph = _STATUS_GLYPH.get(status, status)
        label = _PER_REPO_STAGE_LABELS.get(stage_id, stage_id)
        rows.append((label, glyph, note or "—", artifact or "—"))
    return rows


def _render_multi_repo_section(state: dict[str, object]) -> list[str]:
    """Render the per-repo section of a multi-repo summary (additive)."""
    lines: list[str] = ["## Repositories", ""]
    for repo in _repos_list(state):
        name = str(repo.get("name", "")).strip() or "unknown"
        role = str(repo.get("role", "")).strip() or "—"
        lines.append(f"### `{name}` — role `{role}`")
        lines.append("")
        rows = _repo_stage_rows(repo)
        if rows:
            lines.append("| Stage | Status | Note | Artifact |")
            lines.append("|---|---|---|---|")
            for label, status, note, artifact in rows:
                art = f"`{artifact}`" if artifact != "—" else "—"
                lines.append(f"| {label} | {status} | {note} | {art} |")
        else:
            lines.append("_No analyzer stages recorded for this repo._")
        lines.append("")
    return lines


def render_summary(state: dict[str, object]) -> str:
    """Render the deterministic ``MODERNIZATION-SUMMARY.md`` from state.

    Anti-hallucination: every value comes from *state*; nothing is invented.

    Multi-repo (MR7): when the state records a ``repos`` list (a
    ``.foundry/repos.yaml`` / ``--repo`` run), a per-repo section is inserted
    after the top-level stage roll-up — one sub-table per repo (name + role + its
    analyzer stage outcomes), followed by the SHARED scope-stage outcome already
    in the top-level table. When ``repos`` is ABSENT the render is byte-identical
    to the single-repo output (#1408 additive invariant).
    """
    repo = str(state.get("repo", "")).strip() or "unknown"
    run_id = str(state.get("run_id", "")).strip() or "run"
    overall = _overall(state)
    multi_repo = _is_multi_repo(state)

    lines: list[str] = [
        "# Modernization Summary",
        "",
    ]
    if multi_repo:
        # A multi-repo run names its repository SET, not a single repo. The scope
        # stage correlates all repos into ONE system map, so the top-level roll-up
        # tracks that shared synthesis; the per-repo analyzer outcomes render below.
        repo_names = ", ".join(
            f"`{str(r.get('name', '')).strip() or 'unknown'}`" for r in _repos_list(state)
        )
        lines.append(f"**Repository set:** {repo_names}")
    else:
        lines.append(f"**Repository:** `{repo}`")
    lines += [
        f"**Run id:** `{run_id}`",
        f"**Overall status:** {overall}",
        "",
        "## Stage roll-up",
        "",
        "| Stage | Status | Note | Artifact |",
        "|---|---|---|---|",
    ]
    for label, status, note, artifact in _stage_rows(state):
        art = f"`{artifact}`" if artifact != "—" else "—"
        lines.append(f"| {label} | {status} | {note} | {art} |")
    lines.append("")

    # Multi-repo per-repo analyzer section (additive — absent for single-repo).
    if multi_repo:
        lines += _render_multi_repo_section(state)

    stages = _stages_map(state)

    if overall == "BLOCKED":
        # Name the first failed stage so the operator knows where to resume.
        first_failed = next(
            (s for s in STAGE_ORDER if _stage_status(stages, s) == "failed"),
            None,
        )
        lines.append("## Resume")
        lines.append("")
        if first_failed:
            lines.append(
                f"The chain halted at **{_STAGE_LABELS[first_failed]}**. "
                f"Re-invoke `/mod-modernization` with the same `--run-id {run_id}` "
                "to skip the completed stages and restart from the failed one."
            )
        else:
            lines.append(f"Re-invoke `/mod-modernization --run-id {run_id}` to resume the chain.")
        lines.append("")

    # Skipped-stage callout (graceful skip is a first-class outcome, not an error).
    skipped = [_STAGE_LABELS[s] for s in STAGE_ORDER if _stage_status(stages, s) == "skipped"]
    lines.append("## Skipped stages")
    lines.append("")
    if skipped:
        for s in skipped:
            lines.append(f"- {s} — not applicable (reported and chain continued)")
    else:
        lines.append("_No stages were skipped._")
    lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def aggregate(state_path: Path, output: Path) -> None:
    """Read *state_path*, render the summary, and write it to *output*."""
    state = _load_state(state_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_summary(state), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rollup_aggregator",
        description=(
            "SP4 /mod-modernization roll-up renderer: reads the resumable stage "
            "state file and writes MODERNIZATION-SUMMARY.md."
        ),
    )
    parser.add_argument(
        "--state",
        required=True,
        type=Path,
        metavar="PATH",
        help="Path to the resumable stage state JSON (state.json).",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        metavar="PATH",
        help="Output path for MODERNIZATION-SUMMARY.md.",
    )
    return parser


def main(argv: list[str] | None = None) -> Literal[0, 1, 2]:
    """CLI entry point.  Returns exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    state_path: Path = args.state.resolve()
    output: Path = args.output.resolve()

    if not state_path.is_file():
        print(f"ERROR: --state {state_path} does not exist.", file=sys.stderr)
        return 2

    try:
        aggregate(state_path=state_path, output=output)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"WROTE {output}", file=sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "ARTIFACT_DISCOVERY_PATHS",
    "STAGE_ORDER",
    "aggregate",
    "main",
    "render_summary",
]
