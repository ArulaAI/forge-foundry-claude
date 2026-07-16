"""Forge Foundry — `forge-validate spec-validate` subcommand (C5).

Was inlined in `forge_validate/cli.py` before #101 / TOOL-2.
"""

from __future__ import annotations

import sys
from pathlib import Path

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


@click.command("spec-validate")
@click.argument("spec_path", type=click.Path(exists=False))
@click.argument("code_path", type=click.Path(exists=False))
@click.option(
    "--test-results",
    "test_results_path",
    type=click.Path(exists=False),
    default=None,
    help="Path to pytest-json-report JSON (Stage D downgrade).",
)
@format_option(default="markdown")
@list_formats_option("primitives.spec_validate.shared.report_renderer")
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
    help="CI gate: exit 4 if any trace is at or above this severity.",
)
@click.option(
    "--skip-ai-quality",
    is_flag=True,
    default=False,
    help="Skip sub-call rule §3.4 (C10 + C11 audit).",
)
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(  # noqa: PLR0913, PLR0915, PLR0912
    spec_path: str,
    code_path: str,
    test_results_path: str | None,
    format_: str,
    output: str | None,
    max_severity: str | None,
    skip_ai_quality: bool,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """Round-trip spec ↔ code traceability validation (C5 /spec-validate).

    For each acceptance criterion in SPEC_PATH, locates the corresponding code
    element in CODE_PATH and classifies: satisfied, partial, unsatisfied, or
    not_applicable. Also detects scope drift (implementation features without
    spec backing).

    Exit codes:
    - 0: all criteria satisfied (or only not_applicable)
    - 2: spec or code path not found
    - 4: ≥1 criterion unsatisfied/partial AND severity ≥ --max-severity
    - 5: RESERVED/INACTIVE in skill-first mode. Historically "C10/C11 sub-call
      returned critical findings (after dedup)", but the ``--continue`` path
      hardcodes ``ai_quality=None``, so ``has_critical_ai_quality`` is always
      false and this code can never fire (#813).
    - 7: awaiting judgments (skill-first mode); prompts file written
    """

    # --- Skill-first flag validation -----------------------------------------
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )

    # --- Lazy imports to keep cold-start cheap (TOOL-2) ----------------------
    from primitives._shared.pipeline_state import (  # noqa: PLC0415
        PipelineState,
        cleanup_state,
        load_intermediate,
        load_state,
        save_intermediate,
        save_state,
    )
    from primitives._shared.protocol import (  # noqa: PLC0415
        PromptEnvelope,
        assert_matching_round,
        read_judgments,
        write_prompts,
    )
    from primitives.spec_validate.shared.acceptance_consumer import (  # noqa: PLC0415
        parse_acceptance,
    )
    from primitives.spec_validate.shared.code_element_searcher import (  # noqa: PLC0415
        _build_codebase_summary,
        apply_element_judgments,
        build_prompt_items_for_criteria,
        enumerate_reviewed_files,
    )
    from primitives.spec_validate.shared.scope_drift_detector import (  # noqa: PLC0415
        apply_drift_judgment,
        build_prompt_item_for_drift,
    )

    resolved_spec = Path(spec_path)
    resolved_code = Path(code_path)
    resolved_output = Path(output) if output else None
    resolved_test_results = Path(test_results_path) if test_results_path else None
    # State directory defaults to spec_path's parent / .state / spec-validate (or --state-dir)
    _default_state = resolved_spec.parent / ".state" / "spec-validate"
    resolved_state_dir = Path(state_dir) if state_dir is not None else _default_state

    # =========================================================================
    # Branch: --continue (resume after in-session judgment)
    # =========================================================================
    if continue_:
        if not resolved_spec.exists():
            click.echo(f"Error: spec path not found: {resolved_spec}", err=True)
            sys.exit(2)
        if not resolved_code.exists():
            click.echo(f"Error: code path not found: {resolved_code}", err=True)
            sys.exit(2)

        pipeline = load_state(resolved_state_dir)

        assert judgments is not None  # guaranteed by validate_skill_first_flags
        judgment_envelope = read_judgments(Path(judgments))
        # Reject a stale prior-round judgments file rather than silently
        # finalizing and dropping the pending round (#1199).
        assert_matching_round(judgment_envelope, pipeline.round, path=Path(judgments))
        judgment_map = {item.id: item.response for item in judgment_envelope.items}

        if pipeline.round == 1:
            # --- After round 1: apply element judgments, emit round-2 drift prompt ---
            reviewed_files_list: list[str] = load_intermediate("reviewed_files", resolved_state_dir)
            reviewed_files = set(reviewed_files_list)
            criteria_raw: list[dict[str, object]] = load_intermediate(
                "criteria", resolved_state_dir
            )
            spec_text: str = load_intermediate("spec_text", resolved_state_dir)

            from primitives._shared.acceptance import AcceptanceCriterion  # noqa: PLC0415

            criteria = [AcceptanceCriterion.model_validate(d) for d in criteria_raw]

            # Cast judgment_map to the expected type for apply_element_judgments
            judgment_map_obj: dict[str, object] = dict(judgment_map)
            traces = apply_element_judgments(
                criteria, judgment_map_obj, reviewed_files, resolved_code
            )

            # Save traces for round 2
            save_intermediate(
                "traces",
                [t.model_dump() for t in traces],
                resolved_state_dir,
            )

            # Build round-2 drift prompt
            drift_item = build_prompt_item_for_drift(spec_text, resolved_code, reviewed_files)
            envelope = PromptEnvelope(
                primitive="spec-validate",
                round=2,
                items=[drift_item],
            )
            prompts_path = resolved_state_dir / "round-2-prompts.json"
            write_prompts(envelope, prompts_path)
            click.echo(f"WROTE {prompts_path}")

            save_state(
                PipelineState(
                    primitive="spec-validate",
                    round=2,
                    completed_stages=["A", "B", "C"],
                    next_stage="E_continue",
                    status="awaiting_judgments",
                ),
                resolved_state_dir,
            )
            sys.exit(7)

        elif pipeline.round == 2:  # noqa: PLR2004
            # --- After round 2: apply drift judgment, render final report ---
            from datetime import UTC, datetime  # noqa: PLC0415

            from primitives.spec_validate.shared.models import (  # noqa: PLC0415
                AcceptanceTrace,
                SpecValidateReport,
            )
            from primitives.spec_validate.shared.report_renderer import (  # noqa: PLC0415
                render_report,
            )

            reviewed_files_list = load_intermediate("reviewed_files", resolved_state_dir)
            reviewed_files = set(reviewed_files_list)
            traces_raw: list[dict[str, object]] = load_intermediate("traces", resolved_state_dir)

            traces = [AcceptanceTrace.model_validate(d) for d in traces_raw]

            # Apply drift judgment
            drift_response = None
            for item in judgment_envelope.items:
                if item.id == "drift:scope":
                    drift_response = item.response
                    break

            scope_drift = apply_drift_judgment(drift_response, reviewed_files)

            # Stage D: optional test results downgrade
            if resolved_test_results is not None:
                from primitives.spec_validate.shared.test_results_reader import (  # noqa: PLC0415
                    downgrade_traces_on_test_failure,
                    read_test_results,
                )

                try:
                    test_results = read_test_results(resolved_test_results)
                    traces = downgrade_traces_on_test_failure(traces, test_results)
                except (FileNotFoundError, ValueError) as exc:
                    print(
                        f"[spec-validate] WARNING: could not read test results: {exc}",
                        file=sys.stderr,
                    )

            report = SpecValidateReport(
                spec_path=str(resolved_spec),
                code_path=str(resolved_code),
                traces=traces,
                scope_drift=scope_drift,
                ai_quality=None,
                generated_at=datetime.now(tz=UTC),
            )
            rendered = render_report(report, format_)

            if resolved_output is not None:
                resolved_output.parent.mkdir(parents=True, exist_ok=True)
                resolved_output.write_text(rendered, encoding="utf-8")
                click.echo(f"WROTE {resolved_output}")
            else:
                emit_report(rendered)

            cleanup_state(resolved_state_dir, explicit=(state_dir is not None))

            # Exit code logic
            if report.has_critical_ai_quality:
                sys.exit(5)

            if max_severity is not None:
                threshold = _SEVERITY_ORDER.get(max_severity, 2)
                if any(
                    _SEVERITY_ORDER.get(t.severity, 0) >= threshold
                    and t.status in ("unsatisfied", "partial")
                    for t in report.traces
                ):
                    sys.exit(4)

            sys.exit(0)

        else:
            click.echo(
                f"Error: unexpected pipeline round {pipeline.round}; expected 1 or 2.",
                err=True,
            )
            sys.exit(1)

    # =========================================================================
    # Common preamble: validate paths, parse spec, enumerate codebase
    # =========================================================================

    if not resolved_spec.exists():
        click.echo(f"Error: spec path not found: {resolved_spec}", err=True)
        sys.exit(2)
    if not resolved_code.exists():
        click.echo(f"Error: code path not found: {resolved_code}", err=True)
        sys.exit(2)

    spec_text = resolved_spec.read_text(encoding="utf-8")
    try:
        acceptance_doc = parse_acceptance(spec_text)
    except ValueError as exc:
        # A spec with no `## Acceptance` section is a malformed input, not an
        # internal error — surface it as a clean exit 2, not an unhandled
        # traceback (exit 1) (#1199).
        click.echo(f"Error: {exc} (spec: {resolved_spec})", err=True)
        sys.exit(2)
    criteria = acceptance_doc.criteria

    reviewed_files = enumerate_reviewed_files(resolved_code)
    codebase_summary = _build_codebase_summary(resolved_code, reviewed_files)

    # =========================================================================
    # Branch: --extract-only (round 1 — emit element: prompts)
    # =========================================================================
    if extract_only:
        # Save intermediates
        from primitives._shared.acceptance import AcceptanceCriterion  # noqa: PLC0415

        save_intermediate("spec_text", spec_text, resolved_state_dir)
        save_intermediate("reviewed_files", sorted(reviewed_files), resolved_state_dir)
        save_intermediate(
            "criteria",
            [{"id": c.id, "text": c.text} for c in criteria],
            resolved_state_dir,
        )

        # Build element: prompt items (one per criterion)
        prompt_items = build_prompt_items_for_criteria(criteria, codebase_summary)

        envelope = PromptEnvelope(
            primitive="spec-validate",
            round=1,
            items=prompt_items,
        )
        prompts_path = resolved_state_dir / "round-1-prompts.json"
        write_prompts(envelope, prompts_path)
        click.echo(f"WROTE {prompts_path}")

        save_state(
            PipelineState(
                primitive="spec-validate",
                round=1,
                completed_stages=["A"],
                next_stage="B_continue",
                status="awaiting_judgments",
            ),
            resolved_state_dir,
        )
        sys.exit(7)

    # =========================================================================
    # No skill-first flag: the LLM trace stage has no in-CLI path anymore.
    # =========================================================================
    # The spec↔code trace judgments are produced in-session via the skill-first
    # protocol; a bare invocation has nothing to run. Direct the caller to
    # --extract-only.
    raise click.UsageError(
        "spec-validate requires the skill-first protocol: run with "
        "--extract-only to emit the trace prompts, then "
        "--continue --judgments <path> to render the report."
    )
