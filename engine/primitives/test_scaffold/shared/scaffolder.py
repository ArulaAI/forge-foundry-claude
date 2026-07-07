"""Drive a scaffold run: plan -> per-layer renderers -> write sink -> report (#1031).

The deterministic spine. Given a :class:`~architecture.ScaffoldPlan`, it asks the
registry for the renderer of each ``(stack, layer)`` pair, runs it, and writes
the result through the idempotent containment-guarded sink. Layers with no
registered renderer are recorded as ``unsupported`` (the keystone ships zero
renderers; #1033-#1038 register them); a renderer that raises is recorded as
``errored`` (#1057) - neither ever crashes the run or leaves a half-scaffold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from primitives.test_scaffold.shared.registry import (
    ScaffoldContext,
    get_renderer,
    write_scaffold,
)

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.test_scaffold.shared.architecture import ScaffoldPlan
    from primitives.test_scaffold.shared.registry import WriteOutcome


@dataclass(frozen=True)
class LayerOutcome:
    """The result of scaffolding one layer."""

    layer: str
    #: ``"scaffolded"`` (a renderer ran), ``"unsupported"`` (no renderer for this
    #: stack+layer), ``"no-stack"`` (the stack itself was unrecognised), or
    #: ``"errored"`` (the renderer or its write raised - recorded, not propagated,
    #: so a faulty third-party renderer degrades gracefully without aborting the
    #: run or leaving a half-scaffold; #1057).
    status: str
    #: The cone emphasis for this layer (1-3) — how many samples a renderer is
    #: asked to emit, derived from the cone shape (#1032). Surfaced so the
    #: proportional weighting is observable in the report.
    emphasis: int = 1
    writes: tuple[WriteOutcome, ...] = ()
    note: str = ""
    #: The first line of the S1 ``test-architecture.md`` tooling pick for this layer,
    #: if the architecture named it (#1064). Carried as a structured field (not
    #: spliced into ``note``) so the report can't confuse it with a renderer's own
    #: note text. Empty when S1 named no pick for this layer.
    s1_tooling: str = ""


@dataclass(frozen=True)
class ScaffoldRun:
    """The full outcome of a scaffold run, for the report + exit-code mapping."""

    stack: str | None
    cone_shape: str
    layers: tuple[LayerOutcome, ...]
    warnings: tuple[str, ...] = ()
    #: Per-layer model fill-ins consumed (echoed for transparency).
    fillins: dict[str, str] = field(default_factory=dict)

    def any_written(self) -> bool:
        """True when at least one file was newly written this run."""
        return any(w.status == "written" for lo in self.layers for w in lo.writes)


def _emphasis_for(layer: str, cone_shape: str) -> int:
    """How many sample tests/jobs this layer emits, per the cone shape (#1032).

    Delegates to the cone engine's proportional profile (:func:`cone.layer_emphasis`):
    the inverted snow cone emphasises the wide middle (integration / container /
    contract / API e2e), the pyramid emphasises unit.
    """
    from primitives.test_scaffold.shared.cone import layer_emphasis  # noqa: PLC0415

    return layer_emphasis(layer, cone_shape)


def run_scaffold(plan: ScaffoldPlan, project_root: Path, fillins: dict[str, str]) -> ScaffoldRun:
    """Execute *plan* against *project_root*, returning a :class:`ScaffoldRun`.

    For each planned layer: resolve the ``(stack, layer)`` renderer, run it with a
    :class:`ScaffoldContext`, and write its files idempotently. No renderer ->
    ``unsupported``; no detected stack -> every layer is ``no-stack`` (the CLI
    maps that to exit 2).
    """
    outcomes: list[LayerOutcome] = []
    for layer in plan.layers:
        emphasis = _emphasis_for(layer, plan.cone_shape)
        if plan.stack is None:
            outcomes.append(LayerOutcome(layer=layer, status="no-stack", emphasis=emphasis))
            continue
        renderer = get_renderer(plan.stack, layer)
        if renderer is None:
            outcomes.append(LayerOutcome(layer=layer, status="unsupported", emphasis=emphasis))
            continue
        context = ScaffoldContext(
            stack=plan.stack,
            layer=layer,
            project_root=project_root,
            emphasis=emphasis,
            fillins=dict(fillins),
            # The S1 tooling pick for this layer, if the architecture named it (#1064).
            tooling=plan.tooling.get(layer, ""),
        )
        # The register_scaffold seam means renderers can be third-party and CAN
        # raise. Guard the renderer + its write so a failure degrades to an
        # ``errored`` layer (recorded, not propagated) rather than aborting the run
        # and leaving earlier layers' files as an inconsistent half-scaffold (#1057).
        try:
            result = renderer(context)
            writes = tuple(write_scaffold(project_root, result.files))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(
                LayerOutcome(
                    layer=layer,
                    status="errored",
                    emphasis=emphasis,
                    note=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        # Carry the S1 tooling pick for this layer as a structured field so the
        # engineer sees what S1 recommended vs what was scaffolded (#1064). First
        # line only (the picks section is a one-line-per-layer summary).
        s1_pick = plan.tooling.get(layer, "").strip()
        s1_first_line = s1_pick.splitlines()[0].strip() if s1_pick else ""
        outcomes.append(
            LayerOutcome(
                layer=layer,
                status="scaffolded",
                emphasis=emphasis,
                writes=writes,
                note=result.note,
                s1_tooling=s1_first_line,
            )
        )
    return ScaffoldRun(
        stack=plan.stack,
        cone_shape=plan.cone_shape,
        layers=tuple(outcomes),
        warnings=plan.warnings,
        fillins=dict(fillins),
    )


def render_report(run: ScaffoldRun) -> str:
    """Render a deterministic Markdown report of a scaffold run."""
    lines: list[str] = ["# Test scaffold report", ""]
    lines.append(f"- **Stack:** {run.stack or '(unrecognised)'}")
    lines.append(f"- **Cone shape:** {run.cone_shape}")
    lines.append("")
    if run.warnings:
        lines.append("## Warnings")
        lines.extend(f"- {w}" for w in run.warnings)
        lines.append("")
    lines.append("## Layers")
    lines.append("")
    lines.append("| Layer | Emphasis | Status | Files written | Files skipped |")
    lines.append("|-------|----------|--------|---------------|---------------|")
    for lo in run.layers:
        written = sum(1 for w in lo.writes if w.status == "written")
        skipped = sum(1 for w in lo.writes if w.status == "skipped")
        lines.append(f"| {lo.layer} | {lo.emphasis} | {lo.status} | {written} | {skipped} |")
    lines.append("")
    # Surface errored layers (a renderer raised) with their message so a faulty
    # third-party renderer is visible, not silently swallowed (#1057).
    errored = [lo for lo in run.layers if lo.status == "errored"]
    if errored:
        lines.append("## Errored layers")
        lines.append("")
        lines.extend(f"- **{lo.layer}**: {lo.note}" for lo in errored)
        lines.append("")
    # Surface the S1 tooling picks the architecture named per layer (#1064), so the
    # engineer sees what S1 recommended vs the deterministic scaffold that landed.
    # Read the structured s1_tooling field (never spliced from note text).
    s1_picks = [lo for lo in run.layers if lo.s1_tooling]
    if s1_picks:
        lines.append("## S1 tooling picks")
        lines.append("")
        lines.extend(f"- **{lo.layer}**: {lo.s1_tooling}" for lo in s1_picks)
        lines.append("")
    if not run.any_written():
        lines.append(
            "_No files were written - no layer renderers are registered for this stack yet, "
            "or every target already existed (idempotent)._"
        )
    return "\n".join(lines) + "\n"
