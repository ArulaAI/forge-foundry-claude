"""Forge Foundry — `forge-validate data-flow-map` subcommand (S60).

Was inlined in `forge_validate/cli.py` before #101 / TOOL-2.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from primitives.data_flow_map.shared.models import (
        FlowEdge,
        PIISource,
        PurposeIssue,
        RetentionMarker,
        Sink,
    )

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


def _check_severity_gate(report: object, max_severity: str | None) -> None:
    """Exit 4 if any retention/purpose finding meets or exceeds *max_severity*.

    Called from the skill-first ``_render_and_exit`` helper so the gate logic is
    never duplicated.
    """
    if max_severity is None:
        return
    threshold = _SEVERITY_ORDER.get(max_severity, 2)

    # Retention issues — treat missing as medium, expired as high
    retention_sevs: list[int] = []
    for rm in getattr(report, "retention", []):
        if rm.status == "missing":
            retention_sevs.append(_SEVERITY_ORDER["medium"])
        elif rm.status == "expired":
            retention_sevs.append(_SEVERITY_ORDER["high"])

    purpose_sevs = [
        _SEVERITY_ORDER.get(pi.severity, 0) for pi in getattr(report, "purpose_issues", [])
    ]
    all_sevs = retention_sevs + purpose_sevs
    if any(s >= threshold for s in all_sevs):
        sys.exit(4)


@click.command("data-flow-map")
@click.argument("code_path", type=click.Path(exists=False))
@click.option(
    "--classification",
    "classification_path",
    type=click.Path(exists=True),
    default=None,
    help="Custom classification YAML, merged with starter.",
)
@click.option(
    "--spec",
    "spec_path",
    type=click.Path(exists=True),
    default=None,
    help="Optional spec file for Stage E purpose-limitation review.",
)
@click.option(
    "--module-pattern",
    type=str,
    default=None,
    help="Glob pattern to scope scan (e.g. '**/*.py').",
)
@click.option(
    "--language",
    type=click.Choice(["python", "java", "ts", "auto"]),
    default="auto",
    help="Source language (default: auto-detect).",
)
@format_option(default="markdown")
@list_formats_option("primitives.data_flow_map.shared.report_renderer")
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
    help="CI gate: exit 4 if any retention/purpose issue is at or above this severity.",
)
@click.option(
    "--skip-ai-quality",
    is_flag=True,
    default=False,
    help="Skip sub-call rule §3.4 (C10 + C11 audit).",
)
@click.option(
    "--skip-purpose-review",
    is_flag=True,
    default=False,
    help="Skip Stage E LLM purpose-limitation review.",
)
@click.option(
    "--include-vendored",
    is_flag=True,
    default=False,
    help="Include emitted/vendored harness trees (.foundry, .claude, .github/skills, …) "
    "in the source-file walks. Dependency/build dirs stay excluded.",
)
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(  # noqa: PLR0913, PLR0915
    code_path: str,
    classification_path: str | None,
    spec_path: str | None,
    module_pattern: str | None,
    language: str,
    format_: str,
    output: str | None,
    max_severity: str | None,
    skip_ai_quality: bool,
    skip_purpose_review: bool,
    include_vendored: bool,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """Map PII/sensitive-data flows in a codebase (S60 data-flow-map)."""

    # --- Validate skill-first flags ---
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )

    # --- Resolve paths ---
    resolved_code = Path(code_path)
    if not resolved_code.exists():
        click.echo(f"Error: code path not found: {resolved_code}", err=True)
        sys.exit(2)

    resolved_classification = Path(classification_path) if classification_path else None
    resolved_spec = Path(spec_path) if spec_path else None
    resolved_output = Path(output) if output else None

    # State dir defaults to <code_path>/.state/
    resolved_state_dir = Path(state_dir) if state_dir is not None else resolved_code / ".state"

    # =========================================================================
    # Branch: --continue (resume after in-session judgment)
    # =========================================================================
    if continue_:
        from primitives._shared.pipeline_state import (  # noqa: PLC0415
            load_intermediate,
            load_state,
        )
        from primitives._shared.protocol import read_judgments  # noqa: PLC0415

        pipeline = load_state(resolved_state_dir)

        assert judgments is not None  # guaranteed by validate_skill_first_flags
        judgment_envelope = read_judgments(Path(judgments))
        judgment_map = {item.id: item.response for item in judgment_envelope.items}

        if pipeline.round == 1:
            # -----------------------------------------------------------------
            # Round 1 continue: two sub-cases:
            #
            # (a) PII path: ambiguous fields existed in extract-only. Apply PII
            #     judgments, run Stages B-D, then emit round-2-prompts if
            #     --spec given, else render and exit 0.
            #
            # (b) Purpose-only path (skip_pii_round=True): extract-only had no
            #     ambiguous fields but did have a spec. Stages B-D already ran
            #     and all intermediates are present. Apply purpose judgments
            #     directly and render (no further round).
            # -----------------------------------------------------------------
            from primitives._shared.pipeline_state import (  # noqa: PLC0415
                PipelineState,
                save_intermediate,
                save_state,
            )
            from primitives._shared.protocol import (  # noqa: PLC0415
                PromptEnvelope,
                write_prompts,
            )

            # Detect which sub-case we are in.  The flag file is written only by
            # the purpose-only extract-only path; the PII path does not write it.
            _skip_pii_path = resolved_state_dir / "intermediates" / "skip_pii_round.json"
            skip_pii_flag = (
                load_intermediate("skip_pii_round", resolved_state_dir)
                if _skip_pii_path.exists()
                else False
            )

            if skip_pii_flag:
                # Sub-case (b): purpose-only — apply purpose judgments and render.
                from primitives.data_flow_map.shared.models import (  # noqa: PLC0415
                    FlowEdge,
                    PIISource,
                    RetentionMarker,
                    Sink,
                )
                from primitives.data_flow_map.shared.purpose_reviewer import (  # noqa: PLC0415
                    apply_purpose_judgment,
                )

                all_sources_raw = load_intermediate("all_sources", resolved_state_dir)
                flows_raw = load_intermediate("flows", resolved_state_dir)
                sinks_raw = load_intermediate("sinks", resolved_state_dir)
                retention_raw = load_intermediate("retention", resolved_state_dir)

                all_sources = [PIISource.model_validate(d) for d in all_sources_raw]
                flows = [FlowEdge.model_validate(d) for d in flows_raw]
                sinks = [Sink.model_validate(d) for d in sinks_raw]
                retention = [RetentionMarker.model_validate(d) for d in retention_raw]

                known_source_names = {s.name for s in all_sources}
                purpose_response = judgment_map.get("review:purpose-limitation", [])
                purpose_issues = apply_purpose_judgment(purpose_response, known_source_names)

                _render_and_exit(
                    resolved_code=resolved_code,
                    sources=all_sources,
                    flows=flows,
                    sinks=sinks,
                    retention=retention,
                    purpose_issues=purpose_issues,
                    resolved_output=resolved_output,
                    format_=format_,
                    max_severity=max_severity,
                    state_dir=resolved_state_dir,
                )
            else:
                # Sub-case (a): PII path — apply PII judgments, run B-D, then
                # optionally emit round-2-prompts for purpose review.
                from primitives.data_flow_map.shared.apply_helpers import (  # noqa: PLC0415
                    _run_stages_b_to_d,
                )
                from primitives.data_flow_map.shared.classification_loader import (  # noqa: PLC0415
                    load_classification,
                )
                from primitives.data_flow_map.shared.models import PIISource  # noqa: PLC0415
                from primitives.data_flow_map.shared.source_detector import (  # noqa: PLC0415
                    apply_pii_judgments,
                )

                catalog = load_classification(custom_path=resolved_classification)

                # Load saved intermediates from extract-only
                definite_raw = load_intermediate("definite_sources", resolved_state_dir)
                ambiguous_batch = load_intermediate("ambiguous_batch", resolved_state_dir)

                definite_sources = [PIISource.model_validate(d) for d in definite_raw]

                # Apply PII judgments to ambiguous fields
                # ambiguous_batch is list of [field_name, line, file_str, snippet]
                ambiguous_tuples: list[tuple[str, int, str, str]] = [
                    (str(x[0]), int(x[1]), str(x[2]), str(x[3])) for x in ambiguous_batch
                ]
                resolved_llm_sources = apply_pii_judgments(
                    ambiguous_tuples,
                    judgment_map,
                )
                all_sources = definite_sources + resolved_llm_sources

                # Run Stages B-D deterministically
                flows, sinks, retention = _run_stages_b_to_d(
                    code_path=resolved_code,
                    sources=all_sources,
                    catalog=catalog,
                    language=language,
                    include_vendored=include_vendored,
                )

                # Decide whether to go to round 2 (purpose review) or exit 0
                spec_text: str | None = None
                if resolved_spec is not None and resolved_spec.exists():
                    spec_text = resolved_spec.read_text(encoding="utf-8")

                if spec_text is not None and not skip_purpose_review:
                    from primitives.data_flow_map.shared.purpose_reviewer import (  # noqa: PLC0415
                        build_prompt_item_for_purpose_review,
                    )

                    purpose_item = build_prompt_item_for_purpose_review(
                        sources=all_sources,
                        flows=flows,
                        spec_text=spec_text,
                    )
                    envelope = PromptEnvelope(
                        primitive="data-flow-map",
                        round=2,
                        items=[purpose_item],
                    )
                    prompts_path = resolved_state_dir / "round-2-prompts.json"
                    write_prompts(envelope, prompts_path)
                    click.echo(f"WROTE {prompts_path}")

                    # Save intermediates for round 2
                    save_intermediate(
                        "all_sources",
                        [s.model_dump() for s in all_sources],
                        resolved_state_dir,
                    )
                    save_intermediate(
                        "flows",
                        [f.model_dump() for f in flows],
                        resolved_state_dir,
                    )
                    save_intermediate(
                        "sinks",
                        [s.model_dump() for s in sinks],
                        resolved_state_dir,
                    )
                    save_intermediate(
                        "retention",
                        [r.model_dump() for r in retention],
                        resolved_state_dir,
                    )

                    save_state(
                        PipelineState(
                            primitive="data-flow-map",
                            round=2,
                            completed_stages=["A.1", "A.2", "B", "C", "D"],
                            next_stage="E_continue",
                            status="awaiting_judgments",
                        ),
                        resolved_state_dir,
                    )
                    sys.exit(7)
                else:
                    # No spec or purpose review skipped — render and exit 0
                    _render_and_exit(
                        resolved_code=resolved_code,
                        sources=all_sources,
                        flows=flows,
                        sinks=sinks,
                        retention=retention,
                        purpose_issues=[],
                        resolved_output=resolved_output,
                        format_=format_,
                        max_severity=max_severity,
                        state_dir=resolved_state_dir,
                    )

        elif pipeline.round == 2:  # noqa: PLR2004
            # -----------------------------------------------------------------
            # Round 2 continue: apply purpose judgments, render final report.
            # -----------------------------------------------------------------
            from primitives.data_flow_map.shared.models import (  # noqa: PLC0415
                FlowEdge,
                PIISource,
                RetentionMarker,
                Sink,
            )
            from primitives.data_flow_map.shared.purpose_reviewer import (  # noqa: PLC0415
                apply_purpose_judgment,
            )

            all_sources_raw = load_intermediate("all_sources", resolved_state_dir)
            flows_raw = load_intermediate("flows", resolved_state_dir)
            sinks_raw = load_intermediate("sinks", resolved_state_dir)
            retention_raw = load_intermediate("retention", resolved_state_dir)

            all_sources = [PIISource.model_validate(d) for d in all_sources_raw]
            flows = [FlowEdge.model_validate(d) for d in flows_raw]
            sinks = [Sink.model_validate(d) for d in sinks_raw]
            retention = [RetentionMarker.model_validate(d) for d in retention_raw]

            known_source_names = {s.name for s in all_sources}
            purpose_response = judgment_map.get("review:purpose-limitation", [])
            purpose_issues = apply_purpose_judgment(purpose_response, known_source_names)

            _render_and_exit(
                resolved_code=resolved_code,
                sources=all_sources,
                flows=flows,
                sinks=sinks,
                retention=retention,
                purpose_issues=purpose_issues,
                resolved_output=resolved_output,
                format_=format_,
                max_severity=max_severity,
                state_dir=resolved_state_dir,
            )
        else:
            click.echo(
                f"Error: unexpected pipeline round {pipeline.round} in state dir "
                f"{resolved_state_dir}",
                err=True,
            )
            sys.exit(1)

    # =========================================================================
    # Branch: --extract-only (Stage A.1 deterministic scan + emit round-1 prompts)
    # =========================================================================
    if extract_only:
        from primitives._shared.pipeline_state import (  # noqa: PLC0415
            PipelineState,
            save_intermediate,
            save_state,
        )
        from primitives._shared.protocol import (  # noqa: PLC0415
            PromptEnvelope,
            write_prompts,
        )
        from primitives.data_flow_map.shared.classification_loader import (  # noqa: PLC0415
            load_classification,
        )
        from primitives.data_flow_map.shared.source_detector import (  # noqa: PLC0415
            build_prompt_items_for_ambiguous_fields,
            detect_sources_deterministic,
        )

        catalog = load_classification(custom_path=resolved_classification)
        definite_sources, ambiguous_batch = detect_sources_deterministic(
            code_path=resolved_code,
            catalog=catalog,
            language=language,
            include_vendored=include_vendored,
        )

        if not ambiguous_batch:
            # No ambiguous fields — check if we need purpose review
            spec_text = None
            if resolved_spec is not None and resolved_spec.exists():
                spec_text = resolved_spec.read_text(encoding="utf-8")

            if spec_text is not None and not skip_purpose_review:
                # Need to run Stages B-D then emit round-1 purpose prompts
                # (skip the PII round since no ambiguous fields)
                from primitives.data_flow_map.shared.apply_helpers import (  # noqa: PLC0415
                    _run_stages_b_to_d,
                )
                from primitives.data_flow_map.shared.purpose_reviewer import (  # noqa: PLC0415
                    build_prompt_item_for_purpose_review,
                )

                flows, sinks, retention = _run_stages_b_to_d(
                    code_path=resolved_code,
                    sources=definite_sources,
                    catalog=catalog,
                    language=language,
                    include_vendored=include_vendored,
                )

                purpose_item = build_prompt_item_for_purpose_review(
                    sources=definite_sources,
                    flows=flows,
                    spec_text=spec_text,
                )
                # Use round=1: no PII round ran, so this is the first (and only)
                # round the skill body needs to judge. File name and envelope
                # number now agree.
                envelope = PromptEnvelope(
                    primitive="data-flow-map",
                    round=1,
                    items=[purpose_item],
                )
                prompts_path = resolved_state_dir / "round-1-prompts.json"
                write_prompts(envelope, prompts_path)
                click.echo(f"WROTE {prompts_path}")

                save_intermediate(
                    "all_sources",
                    [s.model_dump() for s in definite_sources],
                    resolved_state_dir,
                )
                save_intermediate(
                    "flows",
                    [f.model_dump() for f in flows],
                    resolved_state_dir,
                )
                save_intermediate(
                    "sinks",
                    [s.model_dump() for s in sinks],
                    resolved_state_dir,
                )
                save_intermediate(
                    "retention",
                    [r.model_dump() for r in retention],
                    resolved_state_dir,
                )
                # Flag so --continue round-1 knows PII round was skipped and
                # all_sources/flows/sinks/retention are already available.
                save_intermediate("skip_pii_round", True, resolved_state_dir)  # noqa: FBT003

                save_state(
                    PipelineState(
                        primitive="data-flow-map",
                        round=1,
                        completed_stages=["A.1", "B", "C", "D"],
                        next_stage="E_continue",
                        status="awaiting_judgments",
                    ),
                    resolved_state_dir,
                )
                sys.exit(7)
            else:
                # No ambiguous fields and no spec — run full deterministic pipeline
                from primitives.data_flow_map.shared.apply_helpers import (  # noqa: PLC0415
                    _run_stages_b_to_d,
                )

                flows, sinks, retention = _run_stages_b_to_d(
                    code_path=resolved_code,
                    sources=definite_sources,
                    catalog=catalog,
                    language=language,
                    include_vendored=include_vendored,
                )
                _render_and_exit(
                    resolved_code=resolved_code,
                    sources=definite_sources,
                    flows=flows,
                    sinks=sinks,
                    retention=retention,
                    purpose_issues=[],
                    resolved_output=resolved_output,
                    format_=format_,
                    max_severity=max_severity,
                    state_dir=None,  # nothing to clean up
                )

        # Build prompt items for ambiguous fields
        prompt_items = build_prompt_items_for_ambiguous_fields(ambiguous_batch)

        envelope = PromptEnvelope(
            primitive="data-flow-map",
            round=1,
            items=prompt_items,
        )
        prompts_path = resolved_state_dir / "round-1-prompts.json"
        write_prompts(envelope, prompts_path)
        click.echo(f"WROTE {prompts_path}")

        # Save intermediates for --continue
        save_intermediate(
            "definite_sources",
            [s.model_dump() for s in definite_sources],
            resolved_state_dir,
        )
        save_intermediate(
            "ambiguous_batch",
            list(ambiguous_batch),
            resolved_state_dir,
        )

        save_state(
            PipelineState(
                primitive="data-flow-map",
                round=1,
                completed_stages=["A.1"],
                next_stage="A.2",
                status="awaiting_judgments",
            ),
            resolved_state_dir,
        )
        sys.exit(7)

    # =========================================================================
    # Branch: plain (deterministic-only) — neither --extract-only nor --continue.
    # =========================================================================
    # Without this leg a plain `data-flow-map <path>` (and any `--output`/
    # `--state-dir` on it) fell through and exited 0 with no report and no file
    # written (#1195 4b). Run the full deterministic pipeline over the
    # confidently-detected sources and render the map. Ambiguous fields need an
    # in-session LLM round (via --extract-only), so a plain run reports the
    # deterministic sources only and notes the skipped ambiguous fields.
    from primitives.data_flow_map.shared.apply_helpers import (  # noqa: PLC0415
        _run_stages_b_to_d,
    )
    from primitives.data_flow_map.shared.classification_loader import (  # noqa: PLC0415
        load_classification,
    )
    from primitives.data_flow_map.shared.source_detector import (  # noqa: PLC0415
        detect_sources_deterministic,
    )

    catalog = load_classification(custom_path=resolved_classification)
    definite_sources, ambiguous_batch = detect_sources_deterministic(
        code_path=resolved_code,
        catalog=catalog,
        language=language,
        include_vendored=include_vendored,
    )
    if ambiguous_batch:
        click.echo(
            f"[data-flow-map] NOTE: {len(ambiguous_batch)} ambiguous field(s) skipped in "
            "deterministic-only mode; use --extract-only for in-session LLM classification.",
            err=True,
        )
    flows, sinks, retention = _run_stages_b_to_d(
        code_path=resolved_code,
        sources=definite_sources,
        catalog=catalog,
        language=language,
        include_vendored=include_vendored,
    )
    _render_and_exit(
        resolved_code=resolved_code,
        sources=definite_sources,
        flows=flows,
        sinks=sinks,
        retention=retention,
        purpose_issues=[],
        resolved_output=resolved_output,
        format_=format_,
        max_severity=max_severity,
        state_dir=None,  # nothing to clean up
    )


# ---------------------------------------------------------------------------
# Shared render-and-exit helper (skill-first path)
# ---------------------------------------------------------------------------


def _render_and_exit(
    *,
    resolved_code: Path,
    sources: list[PIISource],
    flows: list[FlowEdge],
    sinks: list[Sink],
    retention: list[RetentionMarker],
    purpose_issues: list[PurposeIssue],
    resolved_output: Path | None,
    format_: str,
    max_severity: str | None,
    state_dir: Path | None,
) -> None:
    """Build DataFlowReport, render it, apply exit gate logic, clean up state.

    Used by the skill-first --continue path after the final LLM round.
    Always calls sys.exit so callers never return normally.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    from primitives._shared.pipeline_state import cleanup_state  # noqa: PLC0415
    from primitives.data_flow_map.shared.models import DataFlowReport  # noqa: PLC0415
    from primitives.data_flow_map.shared.report_renderer import render_report  # noqa: PLC0415

    report = DataFlowReport(
        code_path=str(resolved_code),
        sources=sources,
        flows=flows,
        sinks=sinks,
        retention=retention,
        purpose_issues=purpose_issues,
        ai_quality=None,
        generated_at=datetime.now(tz=UTC),
    )

    rendered = render_report(report, format_)

    if resolved_output is not None:
        resolved_output.parent.mkdir(parents=True, exist_ok=True)
        resolved_output.write_text(rendered, encoding="utf-8")
    else:
        emit_report(rendered)

    if state_dir is not None:
        cleanup_state(state_dir)

    # Exit gate logic.
    _check_severity_gate(report, max_severity)

    sys.exit(0)
