"""M5 ``/mod-playbook-discovery`` — deterministic pipeline orchestrator.

A thin, **pure-deterministic** orchestrator over the cross-primitive
discovery engine (:mod:`primitives._shared.discovery`). It sequences the
four engine phases into a single run and writes the M5 output artefacts:

* **Phase A — recon.** :func:`~primitives._shared.discovery.recon.run_recon`
  walks ``code_root`` and resolves the platform flavor.
* **Phase B — analyze.**
  :func:`~primitives._shared.discovery.analyze.analyze_to_dir` drives the
  resolved flavor's per-file parse over the tree into an
  :class:`~primitives._shared.discovery.models.AnalysisDigest` and writes
  the Phase B artefacts (``analysis.json`` / ``evidence-ledger.jsonl`` /
  ``analysis-digest.yaml``).
* **Phase C — render.**
  :func:`~primitives._shared.discovery.playbook.render_playbook` /
  :func:`~primitives._shared.discovery.playbook.render_diagrams` emit the
  16-section ``PLAYBOOK.md`` text + the standalone ``.mmd`` diagrams.
* **Phase D — audit.** :func:`~primitives._shared.discovery.audit.audit`
  self-audits the rendered playbook against the digest, and
  :func:`~primitives._shared.discovery.audit.append_validation_report`
  folds the accuracy + findings into a §Validation Report.

The per-file loops are all in the engine; this module never invokes an
LLM.

Three entry paths (design spec §3 of
``docs/superpowers/specs/2026-06-19-m5-playbook-harness-synthesis-design.md``):

``run_pipeline_extract`` (Phase A + B, default)
    Runs recon + analyze, persists the ``AnalysisDigest`` as
    ``analysis.json`` under ``state_dir / run_id``, builds
    ``synthesis_prompts.json`` via
    :func:`~primitives._shared.discovery.synthesis_prompts.build_synthesis_prompts`,
    and returns an :class:`ExtractResult`. The CLI exits 7. The in-session
    model reads the prompts, fills them, and writes ``synthesis.json`` back
    under the same run directory.

``run_pipeline_continue`` (Phase C + D)
    Loads the ``AnalysisDigest`` from the run dir written by the extract
    leg, validates the harness-supplied ``synthesis.json`` via
    :func:`~primitives._shared.discovery.synthesis.load_synthesis`, then
    runs Phase C (render with synthesis) and Phase D (audit with synthesis
    refcheck). Returns a :class:`PipelineResult`.

``run_pipeline`` (A → B → C → D single-shot, ``--no-synthesis``)
    Today's exact single-shot path with ``synthesis=None``. Output is
    BYTE-IDENTICAL to the behaviour before the synthesis round was added.
    Used for ``--no-synthesis``, CI smoke runs, M6 ground-truth
    regeneration, and tests.

Passing a fixed ``generated_at`` makes the run byte-deterministic.

Design spec §M5
(``docs/superpowers/specs/2026-06-03-polyglot-discovery-design.md``)
+ issue [#641](https://github.com/arula-ai/forge-foundry-primitives/issues/641).
Harness synthesis round:
``docs/superpowers/specs/2026-06-19-m5-playbook-harness-synthesis-design.md``
§3 + issue [#864](https://github.com/arula-ai/forge-foundry-primitives/issues/864).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from primitives._shared.discovery.analyze import analyze_to_dir
from primitives._shared.discovery.audit import append_validation_report, audit
from primitives._shared.discovery.playbook import render_diagrams, render_playbook
from primitives._shared.discovery.recon import run_recon

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from primitives._shared.discovery.synthesis import SynthesisJudgments

#: Canonical 16-section spine the renderer + auditor agree on.
_SECTION_COUNT = 16

#: Filename under the run dir where Phase B writes the synthesis prompts
#: artifact (read by the in-session harness).
SYNTHESIS_PROMPTS_FILENAME = "synthesis_prompts.json"

#: Filename under the run dir where the harness writes its synthesis
#: judgments (read by the continue leg). Exported for use in the CLI
#: and tests as a stable constant.
SYNTHESIS_JUDGMENTS_FILENAME = "synthesis.json"


class PipelineResult(BaseModel):
    """The small frozen summary the CLI prints + the skill body reads.

    Every field is derived from the deterministic engine run — no LLM, no
    fabrication. ``accuracy`` is the Phase D self-audit fraction
    (``0.0`` to ``1.0``); ``finding_count`` is the number of Phase D
    :class:`~primitives._shared.discovery.models.AuditFinding` rows.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_dir: str
    flavor: str
    accuracy: float = Field(ge=0.0, le=1.0)
    finding_count: int = Field(ge=0)
    section_count: int = _SECTION_COUNT
    playbook_path: str


class ExtractResult(BaseModel):
    """Frozen summary returned by the extract leg (Phase A + B only).

    The CLI exits 7 after this; the skill body reads ``synthesis_prompts_path``
    to know where to find the prompts artifact. The continue leg reloads the
    persisted ``analysis.json`` from ``run_dir`` using the same ``--state-dir``
    + ``--run-id`` passed to the extract leg.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_dir: str
    flavor: str
    synthesis_prompts_path: str


# ---------------------------------------------------------------------------
# Shared write helper
# ---------------------------------------------------------------------------


def _write_phase_cd_artifacts(
    playbook: str,
    diagrams: dict[str, str],
    report: object,
    run_dir: Path,
) -> str:
    """Write PLAYBOOK.md, diagrams/, and audit.json; return the playbook path string."""
    from primitives._shared.discovery.audit import AuditReport  # noqa: PLC0415

    assert isinstance(report, AuditReport)

    playbook_path = run_dir / "PLAYBOOK.md"
    playbook_path.write_text(playbook, encoding="utf-8")

    diagrams_dir = run_dir / "diagrams"
    diagrams_dir.mkdir(parents=True, exist_ok=True)
    for name, body in diagrams.items():
        (diagrams_dir / name).write_text(body, encoding="utf-8")

    audit_path = run_dir / "audit.json"
    audit_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return str(playbook_path)


def _load_analysis(run_dir: Path) -> object:
    """Load the persisted AnalysisDigest from ``analysis.json`` under ``run_dir``.

    Returns an :class:`~primitives._shared.discovery.models.AnalysisDigest`.

    Raises:
        FileNotFoundError: if ``analysis.json`` does not exist under ``run_dir``
            (the extract leg was not run, or the wrong run dir / run-id was used).
        ValueError: if ``analysis.json`` cannot be parsed or fails validation.
    """
    from primitives._shared.discovery.models import AnalysisDigest  # noqa: PLC0415

    analysis_path = run_dir / "analysis.json"
    if not analysis_path.is_file():
        raise FileNotFoundError(
            f"analysis.json not found in run dir {run_dir!s}; "
            "run the extract leg first (default or --extract-only), "
            "and pass the same --state-dir / --run-id to --continue."
        )
    try:
        raw = analysis_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read analysis.json at {analysis_path}: {exc}") from exc

    from pydantic import ValidationError  # noqa: PLC0415

    try:
        return AnalysisDigest.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"analysis.json at {analysis_path} failed validation: {exc}") from exc


# ---------------------------------------------------------------------------
# Entry path 1 — Extract leg (Phase A + B)
# ---------------------------------------------------------------------------


def run_pipeline_extract(
    code_root: Path,
    state_dir: Path,
    *,
    platform_flavor: str = "auto",
    run_id: str = "run",
    generated_at: datetime | None = None,
) -> ExtractResult:
    """Run the extract leg (Phase A + B) and write the synthesis prompts artifact.

    Runs recon + analyze, writes the Phase B artefacts
    (``analysis.json`` / ``evidence-ledger.jsonl`` / ``analysis-digest.yaml``)
    and ``synthesis_prompts.json`` under ``state_dir / run_id``. The CLI
    exits 7 after calling this function — the in-session harness then reads
    the prompts, fills them, and writes ``synthesis.json`` to the run dir.

    The continue leg (:func:`run_pipeline_continue`) reloads the persisted
    ``analysis.json`` from the **same run dir** via the same ``--state-dir``
    and ``--run-id`` values. The run-dir contract is: both legs share
    ``state_dir / run_id`` as their working directory.

    Args:
        code_root: The source tree to discover (a directory). A missing or
            non-directory path surfaces as ``FileNotFoundError`` /
            ``NotADirectoryError``; the CLI maps those to exit 2.
        state_dir: The ``--state-dir`` root. Artefacts land under
            ``state_dir / run_id``.
        platform_flavor: ``"auto"`` or an explicit flavor name.
        run_id: Per-run subdirectory name under ``state_dir``.
        generated_at: Optional fixed timestamp for byte-deterministic output.

    Returns:
        A frozen :class:`ExtractResult` with the run dir, resolved flavor, and
        the path to ``synthesis_prompts.json``.
    """
    from primitives._shared.discovery.synthesis_prompts import (  # noqa: PLC0415
        build_synthesis_prompts,
    )

    run_dir = state_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # --- Phase A: recon (file-tree walk + flavor resolution) ---------------
    recon = run_recon(code_root, mainframe_flavor=platform_flavor)
    flavor = recon.detected_flavor or "generic"

    # --- Phase B: analyze (per-file parse → AnalysisDigest + artefacts) ----
    # analyze_to_dir already writes analysis.json / evidence-ledger.jsonl /
    # analysis-digest.yaml — no further serialization needed for the digest.
    analysis = analyze_to_dir(
        code_root,
        flavor=flavor,
        state_dir=run_dir,
        recon=recon,
        generated_at=generated_at,
    )

    # --- Build and write synthesis_prompts.json ----------------------------
    prompts = build_synthesis_prompts(analysis, run_id=run_id)
    synthesis_prompts_path = run_dir / SYNTHESIS_PROMPTS_FILENAME
    synthesis_prompts_path.write_text(
        json.dumps(prompts, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return ExtractResult(
        run_dir=str(run_dir),
        flavor=flavor,
        synthesis_prompts_path=str(synthesis_prompts_path),
    )


# ---------------------------------------------------------------------------
# Entry path 2 — Continue leg (Phase C + D)
# ---------------------------------------------------------------------------


def run_pipeline_continue(
    code_root: Path,
    state_dir: Path,
    synthesis_path: Path,
    *,
    run_id: str = "run",
) -> PipelineResult:
    """Run the continue leg (Phase C + D) using a persisted digest + synthesis.

    Loads the :class:`~primitives._shared.discovery.models.AnalysisDigest`
    from ``analysis.json`` in the run dir written by the extract leg (the run
    dir is ``state_dir / run_id`` — must match the extract leg's values),
    validates the harness-supplied ``synthesis.json`` via
    :func:`~primitives._shared.discovery.synthesis.load_synthesis`, then runs
    Phase C (render with synthesis) and Phase D (audit with synthesis refcheck).
    Writes ``PLAYBOOK.md``, ``diagrams/``, and ``audit.json`` under the run dir.

    Args:
        code_root: The source tree root (used for Phase D refcheck of
            ``path:line`` citations in the playbook and synthesis bodies).
        state_dir: The ``--state-dir`` root. The run dir is
            ``state_dir / run_id`` — must match the extract leg.
        synthesis_path: Path to the harness-written ``synthesis.json``.
        run_id: Per-run subdirectory name under ``state_dir``.

    Returns:
        A frozen :class:`PipelineResult`.

    Raises:
        FileNotFoundError: if ``analysis.json`` is missing from the run dir.
        ValueError: if ``analysis.json`` cannot be parsed.
        :class:`~primitives._shared.discovery.synthesis.SynthesisValidationError`:
            if ``synthesis.json`` is missing, malformed, or fails schema
            validation. The CLI maps this to exit 3.
    """
    from primitives._shared.discovery.models import AnalysisDigest  # noqa: PLC0415
    from primitives._shared.discovery.synthesis import load_synthesis  # noqa: PLC0415

    run_dir = state_dir / run_id

    # Load the persisted analysis digest from the extract leg.
    analysis_obj = _load_analysis(run_dir)
    assert isinstance(analysis_obj, AnalysisDigest)

    # Validate + load the harness synthesis judgments. SynthesisValidationError
    # propagates up; the CLI maps it to exit 3 (input rejected).
    synthesis: SynthesisJudgments = load_synthesis(synthesis_path)

    # --- Phase C: render (16-section PLAYBOOK.md + standalone diagrams) ----
    playbook = render_playbook(analysis_obj, synthesis=synthesis)
    diagrams = render_diagrams(analysis_obj)

    # --- Phase D: audit (self-audit + synthesis refcheck → §Validation Report) ---
    report = audit(playbook, analysis=analysis_obj, repo_root=code_root, synthesis=synthesis)
    playbook = append_validation_report(playbook, report)

    # --- Write the M5 output artefacts ------------------------------------
    playbook_path = _write_phase_cd_artifacts(playbook, diagrams, report, run_dir)

    return PipelineResult(
        run_dir=str(run_dir),
        # Report the real resolved flavor persisted by the extract leg, not the
        # closed mainframe_flavor Literal (which defaults to "cics_cobol" whenever the
        # resolved flavor — e.g. "generic"/"java_ee" — is not a member), so the two
        # legs agree (#1650). Older digests without resolved_flavor fall back.
        flavor=analysis_obj.resolved_flavor or analysis_obj.mainframe_flavor,
        accuracy=report.accuracy,
        finding_count=len(report.findings),
        playbook_path=playbook_path,
    )


# ---------------------------------------------------------------------------
# Entry path 3 — No-synthesis single-shot (--no-synthesis)
# ---------------------------------------------------------------------------


def run_pipeline(
    code_root: Path,
    state_dir: Path,
    *,
    platform_flavor: str = "auto",
    run_id: str = "run",
    generated_at: datetime | None = None,
) -> PipelineResult:
    """Run the four-phase discovery pipeline and write the M5 artefacts.

    This is the ``--no-synthesis`` / legacy single-shot path: Phase A recon →
    Phase B analyze → Phase C render (deterministic ``⚠️ UNVERIFIED`` stubs for
    §1/§2/§8/§14/§15) → Phase D audit, all in one call, ``synthesis=None``.
    Output is BYTE-IDENTICAL to pre-synthesis-round behavior (regression-locked
    by test). Use this for ``--no-synthesis``, CI smoke runs, M6 ground-truth
    regeneration, and hermetic test runs.

    Args:
        code_root: The source tree to discover (a directory). A missing or
            non-directory path is surfaced as the engine's
            ``FileNotFoundError`` / ``NotADirectoryError``; the CLI maps
            those to exit 2.
        state_dir: The ``--state-dir`` root. The per-run artefacts land
            under ``state_dir / run_id``.
        platform_flavor: ``"auto"`` (default) runs the flavor-pack
            registry's detect-and-pick; an explicit name short-circuits
            detection. Passed straight through to Phase A recon.
        run_id: Per-run subdirectory name under ``state_dir``.
        generated_at: Optional fixed timestamp for the Phase B digest —
            supply it for byte-deterministic output.

    Returns:
        A frozen :class:`PipelineResult` summarising the run (resolved
        flavor, Phase D accuracy, finding count, and where ``PLAYBOOK.md``
        landed).
    """
    run_dir = state_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # --- Phase A: recon (file-tree walk + flavor resolution) ---------------
    recon = run_recon(code_root, mainframe_flavor=platform_flavor)
    flavor = recon.detected_flavor or "generic"

    # --- Phase B: analyze (per-file parse → AnalysisDigest + artefacts) ----
    analysis = analyze_to_dir(
        code_root,
        flavor=flavor,
        state_dir=run_dir,
        recon=recon,
        generated_at=generated_at,
    )

    # --- Phase C: render (16-section PLAYBOOK.md + standalone diagrams) ----
    playbook = render_playbook(analysis)
    diagrams = render_diagrams(analysis)

    # --- Phase D: audit (self-audit → §Validation Report) ------------------
    report = audit(playbook, analysis=analysis, repo_root=code_root)
    playbook = append_validation_report(playbook, report)

    # --- Write the M5 output artefacts ------------------------------------
    playbook_path = _write_phase_cd_artifacts(playbook, diagrams, report, run_dir)

    return PipelineResult(
        run_dir=str(run_dir),
        flavor=flavor,
        accuracy=report.accuracy,
        finding_count=len(report.findings),
        playbook_path=playbook_path,
    )


__all__ = [
    "SYNTHESIS_JUDGMENTS_FILENAME",
    "SYNTHESIS_PROMPTS_FILENAME",
    "ExtractResult",
    "PipelineResult",
    "run_pipeline",
    "run_pipeline_continue",
    "run_pipeline_extract",
]
