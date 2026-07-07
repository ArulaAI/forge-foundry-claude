"""Forge Foundry — `forge-validate architecture-review` subcommand (C8).

Was inlined in `forge_validate/cli.py` before #101 / TOOL-2.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import click

from forge_validate._cli_common import (
    _SEVERITY_ORDER,
    continue_option,
    emit_report,
    extract_only_option,
    format_option,
    judgments_option,
    list_formats_option,
    state_dir_option,
    validate_skill_first_flags,
)

if TYPE_CHECKING:
    from primitives.architecture_review.shared.models import (
        CouplingMetric,
        CyclicDependency,
        DepGraph,
        LayerViolation,
        SubjectiveIssue,
    )
    from primitives.architecture_review.shared.prioritizer import AnyIssue
    from primitives.architecture_review.shared.runtime_context import RuntimeContext


@click.command("architecture-review")
@click.argument("code_path", type=click.Path(exists=False))
@click.option(
    "--spec",
    "spec_path",
    type=click.Path(exists=True),
    default=None,
    help="Optional spec file for Stage E subjective review context.",
)
@click.option(
    "--arch-yaml",
    type=click.Path(exists=True),
    default=None,
    help="Path to .foundry/architecture.yaml (auto-detected if omitted).",
)
@click.option(
    "--module-pattern",
    type=str,
    default=None,
    help="Glob pattern to scope review (e.g. '**/*.py').",
)
@click.option(
    "--language",
    type=click.Choice(["python", "java", "ts", "auto"]),
    default="auto",
    help="Source language (default: auto-detect).",
)
@format_option(default="markdown")
@list_formats_option("primitives.architecture_review.shared.report_renderer")
@click.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Write output to file (default: stdout).",
)
@click.option(
    "--max-severity",
    type=click.Choice(["high", "critical"]),
    default=None,
    help="CI gate: exit 4 if any issue is at or above this severity.",
)
@click.option(
    "--skip-ai-quality",
    is_flag=True,
    default=False,
    help="Skip sub-call rule §3.4 (C10 + C11 audit).",
)
@click.option(
    "--skip-subjective",
    is_flag=True,
    default=False,
    help="Skip Stage E LLM subjective review (pure-static review).",
)
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(  # noqa: PLR0913
    code_path: str,
    spec_path: str | None,
    arch_yaml: str | None,
    module_pattern: str | None,
    language: str,
    format_: str,
    output: str | None,
    max_severity: str | None,
    skip_ai_quality: bool,
    skip_subjective: bool,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """Review the system-shape of a codebase (C8 architecture-review)."""

    # --- Validate skill-first flags -----------------------------------------
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )

    resolved_code = Path(code_path)
    if not resolved_code.exists():
        click.echo(f"Error: code path not found: {resolved_code}", err=True)
        sys.exit(2)

    resolved_spec = Path(spec_path) if spec_path else None
    resolved_arch = Path(arch_yaml) if arch_yaml else None
    resolved_output = Path(output) if output else None

    # --- Resolve state directory --------------------------------------------
    # Default to code_path/.state so it lives near the reviewed source.
    _state_base = resolved_output.parent if resolved_output else resolved_code
    resolved_state_dir = Path(state_dir) if state_dir else _state_base / ".state"

    # =========================================================================
    # Branch: --continue (resume after in-session judgment)
    # =========================================================================
    if continue_:
        _run_continue(
            resolved_code=resolved_code,
            resolved_spec=resolved_spec,
            resolved_arch=resolved_arch,
            resolved_output=resolved_output,
            module_pattern=module_pattern,
            language=language,
            format_=format_,
            max_severity=max_severity,
            resolved_state_dir=resolved_state_dir,
            judgments=judgments,  # guaranteed non-None by validate_skill_first_flags
            skip_ai_quality=skip_ai_quality,
        )
        return

    # =========================================================================
    # Branch: --extract-only (run deterministic stages, emit prompts, exit 7)
    # =========================================================================
    if extract_only:
        _run_extract_only(
            resolved_code=resolved_code,
            resolved_spec=resolved_spec,
            resolved_arch=resolved_arch,
            module_pattern=module_pattern,
            language=language,
            resolved_state_dir=resolved_state_dir,
            skip_subjective=skip_subjective,
            resolved_output=resolved_output,
            format_=format_,
            max_severity=max_severity,
            skip_ai_quality=skip_ai_quality,
        )
        return

    # No skill-first flag supplied: the CLI never calls an LLM, so the only
    # execution paths are --extract-only and --continue --judgments.
    raise click.UsageError(
        "architecture-review requires --extract-only (emit prompts) or "
        "--continue --judgments <path> (apply judgments). The CLI does not call an LLM."
    )


# ---------------------------------------------------------------------------
# Skill-first helpers
# ---------------------------------------------------------------------------


def _run_static_stages(
    resolved_code: Path,
    resolved_spec: Path | None,
    resolved_arch: Path | None,
    module_pattern: str | None,
    language: str,
) -> tuple[
    DepGraph,
    list[LayerViolation],
    list[CyclicDependency],
    RuntimeContext,
    str | None,
    dict[str, CouplingMetric],
]:
    """Run Stages A-D deterministically.

    Returns (dep_graph, layer_violations, cycles, runtime_ctx, spec_text, coupling).
    """
    from typing import cast as _cast  # noqa: PLC0415

    from primitives.architecture_review.shared.coupling_metrics import (  # noqa: PLC0415
        compute_coupling_metrics,
    )
    from primitives.architecture_review.shared.cycle_detector import detect_cycles  # noqa: PLC0415
    from primitives.architecture_review.shared.dep_graph import build_dep_graph  # noqa: PLC0415
    from primitives.architecture_review.shared.layer_checker import (  # noqa: PLC0415
        detect_layer_violations,
    )
    from primitives.architecture_review.shared.runtime_context import (  # noqa: PLC0415
        load_runtime_context,
    )

    click.echo("[architecture-review] Stage A: building dep graph...", err=True)
    dep_graph = build_dep_graph(
        resolved_code,
        language=_cast("Literal['python', 'java', 'ts', 'auto']", language),
        module_pattern=module_pattern,
    )
    click.echo(
        f"[architecture-review] Stage A: {len(dep_graph.modules)} modules, "
        f"{len(dep_graph.edges)} edges",
        err=True,
    )

    runtime_ctx = load_runtime_context(resolved_code)

    click.echo("[architecture-review] Stage B: detecting layer violations...", err=True)
    layer_violations = detect_layer_violations(
        dep_graph,
        arch_yaml_path=resolved_arch,
        code_path=resolved_code if resolved_arch is None else None,
        runtime_ctx=runtime_ctx,
    )

    click.echo("[architecture-review] Stage C: computing coupling metrics...", err=True)
    coupling = compute_coupling_metrics(dep_graph)

    click.echo("[architecture-review] Stage D: detecting cycles...", err=True)
    cycles = detect_cycles(dep_graph)

    spec_text: str | None = None
    if resolved_spec is not None and resolved_spec.exists():
        spec_text = resolved_spec.read_text(encoding="utf-8")

    return dep_graph, layer_violations, cycles, runtime_ctx, spec_text, coupling


def _run_extract_only(  # noqa: PLR0913
    *,
    resolved_code: Path,
    resolved_spec: Path | None,
    resolved_arch: Path | None,
    module_pattern: str | None,
    language: str,
    resolved_state_dir: Path,
    skip_subjective: bool,
    resolved_output: Path | None,
    format_: str,
    max_severity: str | None,
    skip_ai_quality: bool,
) -> None:
    """Execute the --extract-only branch (Stage A-D deterministic + emit prompts)."""
    from primitives._shared.pipeline_state import (  # noqa: PLC0415
        PipelineState,
        save_intermediate,
        save_state,
    )
    from primitives._shared.protocol import (  # noqa: PLC0415
        PromptEnvelope,
        write_prompts,
    )
    from primitives.architecture_review.shared.llm_subjective import (  # noqa: PLC0415
        build_prompt_item_for_subjective,
    )

    dep_graph, layer_violations, cycles, runtime_ctx, spec_text, coupling = _run_static_stages(
        resolved_code,
        resolved_spec,
        resolved_arch,
        module_pattern,
        language,
    )

    # --- Persist dep graph for --continue ------------------------------------
    from primitives.architecture_review.shared.models import (  # noqa: PLC0415
        LayerViolation,
    )

    save_intermediate(
        "dep_graph",
        {
            "modules": [m.model_dump() for m in dep_graph.modules],
            "edges": list(dep_graph.edges),
        },
        resolved_state_dir,
    )
    save_intermediate(
        "layer_violations",
        [v.model_dump() for v in layer_violations if isinstance(v, LayerViolation)],
        resolved_state_dir,
    )
    save_intermediate(
        "cycles",
        [c.model_dump() for c in cycles],
        resolved_state_dir,
    )
    save_intermediate("spec_text", spec_text, resolved_state_dir)
    save_intermediate(
        "coupling",
        {name: m.model_dump() for name, m in coupling.items()},
        resolved_state_dir,
    )

    if skip_subjective:
        # No model step needed — the pipeline is fully deterministic, so render
        # the report directly (there is nothing to judge in-session).
        click.echo(
            "[architecture-review] --skip-subjective set; rendering deterministic "
            "report (no prompts needed).",
            err=True,
        )
        _render_and_exit(
            all_issues=[],
            layer_violations=[v for v in layer_violations if isinstance(v, LayerViolation)],
            cycles=cycles,
            subjective=[],
            coupling={name: m.model_dump() for name, m in coupling.items()},
            resolved_code=resolved_code,
            resolved_output=resolved_output,
            format_=format_,
            max_severity=max_severity,
            resolved_state_dir=resolved_state_dir,
            skip_ai_quality=skip_ai_quality,
        )
        return

    # --- Build round-1 prompts -----------------------------------------------
    item = build_prompt_item_for_subjective(dep_graph, spec_text, runtime_ctx)

    envelope = PromptEnvelope(
        primitive="architecture-review",
        round=1,
        items=[item],
    )
    prompts_path = resolved_state_dir / "round-1-prompts.json"
    write_prompts(envelope, prompts_path)
    click.echo(f"WROTE {prompts_path}")

    save_state(
        PipelineState(
            primitive="architecture-review",
            round=1,
            completed_stages=["A", "B", "C", "D"],
            next_stage="E_continue",
            status="awaiting_judgments",
        ),
        resolved_state_dir,
    )

    sys.exit(7)


def _run_continue(  # noqa: PLR0913
    *,
    resolved_code: Path,
    resolved_spec: Path | None,
    resolved_arch: Path | None,
    resolved_output: Path | None,
    module_pattern: str | None,
    language: str,
    format_: str,
    max_severity: str | None,
    resolved_state_dir: Path,
    judgments: str | None,
    skip_ai_quality: bool,
) -> None:
    """Execute the --continue branch (apply judgments, emit round-2 prompts or render)."""
    from primitives._shared.pipeline_state import (  # noqa: PLC0415
        PipelineState,
        load_intermediate,
        load_state,
        save_intermediate,
        save_state,
    )
    from primitives._shared.protocol import (  # noqa: PLC0415
        PromptEnvelope,
        read_judgments,
        write_prompts,
    )
    from primitives.architecture_review.shared.llm_subjective import (  # noqa: PLC0415
        apply_subjective_judgment,
    )
    from primitives.architecture_review.shared.models import (  # noqa: PLC0415
        CyclicDependency,
        DepGraph,
        LayerViolation,
        Module,
        SubjectiveIssue,
    )
    from primitives.architecture_review.shared.prioritizer import (  # noqa: PLC0415
        apply_ranking_judgment,
        build_prompt_item_for_ranking,
    )

    pipeline = load_state(resolved_state_dir)

    assert judgments is not None  # guaranteed by validate_skill_first_flags
    judgment_envelope = read_judgments(Path(judgments))
    judgment_map = judgment_envelope.by_id()

    # ------------------------------------------------------------------
    # After round 1: apply subjective judgments
    # ------------------------------------------------------------------
    if pipeline.round == 1:
        # Load intermediates
        dep_graph_raw = load_intermediate("dep_graph", resolved_state_dir)
        modules = [Module.model_validate(m) for m in dep_graph_raw["modules"]]
        dep_graph = DepGraph(modules=modules, edges=[tuple(e) for e in dep_graph_raw["edges"]])

        layer_violations_raw = load_intermediate("layer_violations", resolved_state_dir)
        layer_violations = [LayerViolation.model_validate(v) for v in layer_violations_raw]

        cycles_raw = load_intermediate("cycles", resolved_state_dir)
        cycles = [CyclicDependency.model_validate(c) for c in cycles_raw]

        coupling_raw: dict[str, object] = load_intermediate("coupling", resolved_state_dir)

        # Apply subjective judgment
        subjective_item = judgment_map.get("review:subjective")
        subjective: list[SubjectiveIssue] = []
        if subjective_item is not None:
            response_val = subjective_item.response
            if isinstance(response_val, list):
                subjective = apply_subjective_judgment(response_val, dep_graph)
            else:
                click.echo(
                    "[architecture-review] WARNING: review:subjective judgment response "
                    "is not a list; skipping.",
                    err=True,
                )

        # Combine all issues for ranking

        all_issues: list[AnyIssue] = list(layer_violations) + list(cycles) + list(subjective)

        # Decide whether to emit round-2 (ranking) prompts
        _MIN_ISSUES_FOR_RANKING = 2
        if len(all_issues) >= _MIN_ISSUES_FOR_RANKING:
            # Build round-2 ranking prompt
            rank_item = build_prompt_item_for_ranking(all_issues)
            envelope = PromptEnvelope(
                primitive="architecture-review",
                round=2,
                items=[rank_item],
            )
            prompts_path = resolved_state_dir / "round-2-prompts.json"
            write_prompts(envelope, prompts_path)
            click.echo(f"WROTE {prompts_path}")

            # Save intermediates for round-2 continue
            save_intermediate(
                "all_issues",
                _serialize_all_issues(all_issues),
                resolved_state_dir,
            )
            # Re-persist coupling so round-2 continue can load it.
            save_intermediate("coupling", coupling_raw, resolved_state_dir)
            save_state(
                PipelineState(
                    primitive="architecture-review",
                    round=2,
                    completed_stages=["A", "B", "C", "D", "E"],
                    next_stage="F_continue",
                    status="awaiting_judgments",
                ),
                resolved_state_dir,
            )
            sys.exit(7)
        else:
            # Not enough issues to rank — render directly
            _render_and_exit(
                all_issues=all_issues,
                layer_violations=layer_violations,
                cycles=cycles,
                subjective=subjective,
                coupling=coupling_raw,
                resolved_code=resolved_code,
                resolved_output=resolved_output,
                format_=format_,
                max_severity=max_severity,
                resolved_state_dir=resolved_state_dir,
                skip_ai_quality=skip_ai_quality,
            )
            return

    # ------------------------------------------------------------------
    # After round 2: apply ranking judgment, render final report
    # ------------------------------------------------------------------
    _ROUND_2 = 2
    if pipeline.round == _ROUND_2:
        from primitives.architecture_review.shared.models import (  # noqa: PLC0415
            CyclicDependency,
            LayerViolation,
            SubjectiveIssue,
        )

        all_issues_raw = load_intermediate("all_issues", resolved_state_dir)
        all_issues = _deserialize_all_issues(all_issues_raw)

        coupling_raw_r2: dict[str, object] = load_intermediate("coupling", resolved_state_dir)

        ranking_item = judgment_map.get("rank:issues")
        if ranking_item is not None:
            response_val = ranking_item.response
            if isinstance(response_val, list):
                all_issues = apply_ranking_judgment(response_val, all_issues)
            else:
                click.echo(
                    "[architecture-review] WARNING: rank:issues judgment response "
                    "is not a list; using static sort.",
                    err=True,
                )

        # Separate back into typed lists for the report
        layer_violations = [i for i in all_issues if isinstance(i, LayerViolation)]
        cycles = [i for i in all_issues if isinstance(i, CyclicDependency)]
        subjective = [i for i in all_issues if isinstance(i, SubjectiveIssue)]

        _render_and_exit(
            all_issues=all_issues,
            layer_violations=layer_violations,
            cycles=cycles,
            subjective=subjective,
            coupling=coupling_raw_r2,
            resolved_code=resolved_code,
            resolved_output=resolved_output,
            format_=format_,
            max_severity=max_severity,
            resolved_state_dir=resolved_state_dir,
            skip_ai_quality=skip_ai_quality,
        )


def _render_and_exit(  # noqa: PLR0913
    *,
    all_issues: list[AnyIssue],
    layer_violations: list[LayerViolation],
    cycles: list[CyclicDependency],
    subjective: list[SubjectiveIssue],
    coupling: dict[str, object],
    resolved_code: Path,
    resolved_output: Path | None,
    format_: str,
    max_severity: str | None,
    resolved_state_dir: Path,
    skip_ai_quality: bool,
) -> None:
    """Assemble the ArchitectureReport and render it, then apply exit-code logic."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from primitives._shared.pipeline_state import cleanup_state  # noqa: PLC0415
    from primitives.architecture_review.shared.models import (  # noqa: PLC0415
        ArchitectureReport,
        CouplingMetric,
    )
    from primitives.architecture_review.shared.report_renderer import render_report  # noqa: PLC0415

    # Restore coupling metrics from the persisted intermediate.
    restored_coupling = {name: CouplingMetric.model_validate(m) for name, m in coupling.items()}
    report = ArchitectureReport(
        code_path=str(resolved_code),
        layer_violations=layer_violations,
        coupling=restored_coupling,
        cycles=cycles,
        subjective=subjective,
        ai_quality=None,  # sub-call skipped in skill-first mode
        generated_at=datetime.now(tz=UTC),
    )

    rendered = render_report(report, format_)

    if resolved_output is not None:
        resolved_output.parent.mkdir(parents=True, exist_ok=True)
        resolved_output.write_text(rendered, encoding="utf-8")
        click.echo(f"WROTE {resolved_output}")
    else:
        emit_report(rendered)

    cleanup_state(resolved_state_dir)

    # Exit 4: any issue at or above --max-severity
    if max_severity is not None:
        from primitives.architecture_review.shared.models import (  # noqa: PLC0415
            LayerViolation,
        )

        threshold = _SEVERITY_ORDER.get(max_severity, 2)

        def _sev(v: object) -> int:
            if isinstance(v, LayerViolation):
                return _SEVERITY_ORDER.get(v.severity, 0)
            return _SEVERITY_ORDER["high"]

        from primitives.architecture_review.shared.models import SubjectiveIssue  # noqa: PLC0415

        subj_sev = [
            _SEVERITY_ORDER.get(s.severity, 0) for s in subjective if isinstance(s, SubjectiveIssue)
        ]
        all_sev = (
            [_sev(v) for v in layer_violations]
            + [_SEVERITY_ORDER["high"] for _ in cycles]
            + subj_sev
        )
        if any(s >= threshold for s in all_sev):
            sys.exit(4)


# ---------------------------------------------------------------------------
# Serialization helpers for all_issues across pipeline rounds
# ---------------------------------------------------------------------------


def _serialize_all_issues(all_issues: list[AnyIssue]) -> list[dict[str, object]]:
    """Serialize a mixed list of architecture issues for intermediate storage."""
    from primitives.architecture_review.shared.models import (  # noqa: PLC0415
        CyclicDependency,
        LayerViolation,
        SubjectiveIssue,
    )

    result: list[dict[str, object]] = []
    for issue in all_issues:
        if isinstance(issue, LayerViolation):
            result.append({"_type": "LayerViolation", **issue.model_dump()})
        elif isinstance(issue, CyclicDependency):
            result.append({"_type": "CyclicDependency", **issue.model_dump()})
        elif isinstance(issue, SubjectiveIssue):
            result.append({"_type": "SubjectiveIssue", **issue.model_dump()})
        else:
            raise TypeError(f"Unknown issue type: {type(issue)}")
    return result


def _deserialize_all_issues(raw: list[dict[str, object]]) -> list[AnyIssue]:
    """Deserialize a mixed list of architecture issues from intermediate storage."""
    from primitives.architecture_review.shared.models import (  # noqa: PLC0415
        CyclicDependency,
        LayerViolation,
        SubjectiveIssue,
    )

    result: list[LayerViolation | CyclicDependency | SubjectiveIssue] = []
    for raw_item in raw:
        item = dict(raw_item)
        type_ = item.pop("_type", None)
        if type_ == "LayerViolation":
            result.append(LayerViolation.model_validate(item))
        elif type_ == "CyclicDependency":
            result.append(CyclicDependency.model_validate(item))
        elif type_ == "SubjectiveIssue":
            result.append(SubjectiveIssue.model_validate(item))
        else:
            raise ValueError(f"Unknown _type in serialized issue: {type_!r}")
    return result
