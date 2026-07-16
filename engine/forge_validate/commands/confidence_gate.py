"""Forge Foundry — `forge-validate confidence-gate` subcommand (C6).

Was inlined in `forge_validate/cli.py` before #101 / TOOL-2.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from forge_validate._cli_common import (
    _RECOMMENDATION_ORDER,
    continue_option,
    emit_report,
    extract_only_option,
    format_option,
    judgments_option,
    list_formats_option,
    state_dir_option,
    validate_skill_first_flags,
)


@click.command("confidence-gate")
@click.argument("report_paths", nargs=-1, required=True, type=click.Path())
@click.option(
    "--policy",
    "policy_path",
    type=click.Path(),
    default=None,
    help="Path to gate-policy.yaml (default: .foundry/gate-policy.yaml).",
)
@format_option(default="markdown")
@list_formats_option("primitives.confidence_gate.shared.report_renderer")
@click.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Write output to file.",
)
@click.option(
    "--max-recommendation",
    type=click.Choice(["pass", "review", "fail"]),
    default=None,
    help="Exit 4 if recommendation exceeds this level.",
)
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(  # noqa: PLR0915
    report_paths: tuple[str, ...],
    policy_path: str | None,
    format_: str,
    output: str | None,
    max_recommendation: str | None,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """Risk-modeled aggregator gate for upstream review reports (C6 /confidence-gate).

    Reads already-produced JSON reports from upstream primitives (C4, C5, C8,
    S10, S11, S20, S30, S40, S60, S70, C10, C11), computes a deterministic
    risk score, and synthesizes a PASS/REVIEW/FAIL recommendation.

    Skill-first mode (--extract-only / --continue --judgments) bypasses the
    LLM harness -- Stages A-B run deterministically and defer the rationale
    to the SKILL.md body for in-session judgment.

    Exit codes:
    - 0: success (recommendation within --max-recommendation)
    - 1: generic error
    - 2: report path not found or malformed JSON
    - 3: policy file malformed
    - 4: recommendation exceeds --max-recommendation
    - 7: --extract-only complete, prompts file written (skill-first mode)
    """

    # --- Validate skill-first flags (--continue requires --judgments) ---
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )

    # --- Resolve state directory ---
    resolved_output_path = Path(output) if output else None
    resolved_state_dir = (
        Path(state_dir)
        if state_dir is not None
        else (resolved_output_path.parent if resolved_output_path else Path()) / ".state"
    )

    # =========================================================================
    # Branch: --continue --judgments (resume after in-session judgment)
    # =========================================================================
    if continue_:
        from primitives._shared.pipeline_state import (  # noqa: PLC0415
            cleanup_state,
            load_intermediate,
        )
        from primitives._shared.protocol import (  # noqa: PLC0415
            assert_matching_round,
            read_judgments,
        )
        from primitives.confidence_gate.shared.models import (  # noqa: PLC0415
            GateScore,
            NormalizedFinding,
            PrimitiveRiskSummary,
        )
        from primitives.confidence_gate.shared.rationale_drafter import (  # noqa: PLC0415
            apply_rationale_judgment,
        )
        from primitives.confidence_gate.shared.report_renderer import (  # noqa: PLC0415
            render_report,
        )

        # Load intermediates saved during --extract-only
        summaries_raw = load_intermediate("summaries", resolved_state_dir)
        gate_score_raw = load_intermediate("gate_score", resolved_state_dir)
        findings_raw = load_intermediate("findings", resolved_state_dir)

        primitive_summaries = [PrimitiveRiskSummary.model_validate(d) for d in summaries_raw]
        gate_score = GateScore.model_validate(gate_score_raw)
        all_findings = [NormalizedFinding.model_validate(d) for d in findings_raw]

        # Read judgments; look up "narrative:confidence-gate" response (dict)
        assert judgments is not None  # guaranteed by validate_skill_first_flags
        judgment_envelope = read_judgments(Path(judgments))
        # confidence-gate is single-round: reject a wrong-round judgments file loudly
        # rather than silently dropping the pending round (#1304).
        assert_matching_round(judgment_envelope, 1, path=Path(judgments))
        judgment_map = {item.id: item.response for item in judgment_envelope.items}

        raw_response = judgment_map.get("narrative:confidence-gate", {})
        if not isinstance(raw_response, dict):
            click.echo(
                "Error: judgment response for 'narrative:confidence-gate' must be a dict",
                err=True,
            )
            sys.exit(1)

        rationale_result = apply_rationale_judgment(raw_response, all_findings)

        # Build the report
        from datetime import UTC, datetime  # noqa: PLC0415

        from primitives.confidence_gate.shared.models import (  # noqa: PLC0415
            ConfidenceGateReport,
        )

        report = ConfidenceGateReport(
            report_paths=list(report_paths),
            primitive_summaries=primitive_summaries,
            gate_score=gate_score,
            rationale=rationale_result.rationale,
            top_findings=rationale_result.top_findings,
            generated_at=datetime.now(UTC),
        )

        # Stage D: render
        # findings_by_report is not needed for render_report in the continue path;
        # pass empty dict (the renderer uses it only for the detailed breakdown section
        # which can fall back gracefully)
        rendered = render_report(report, format_=format_, findings_by_report={})

        if resolved_output_path is not None:
            resolved_output_path.write_text(rendered, encoding="utf-8")
        else:
            emit_report(rendered)

        # Clean up state
        cleanup_state(resolved_state_dir, explicit=(state_dir is not None))

        # Exit 4: recommendation exceeds --max-recommendation
        if max_recommendation is not None:
            actual_order = _RECOMMENDATION_ORDER.get(gate_score.recommendation, 0)
            max_order = _RECOMMENDATION_ORDER.get(max_recommendation, 0)
            if actual_order > max_order:
                sys.exit(4)

        sys.exit(0)

    # =========================================================================
    # Common preamble: validate report paths and policy
    # (shared by --extract-only and --continue paths)
    # =========================================================================

    resolved_paths = [Path(p) for p in report_paths]

    # Exit 2: any report path not found
    for rp in resolved_paths:
        if not rp.exists():
            click.echo(f"Error: report path not found: {rp}", err=True)
            sys.exit(2)

    resolved_policy: Path | None = None
    if policy_path:
        resolved_policy = Path(policy_path)
        if not resolved_policy.exists():
            click.echo(f"Error: policy file not found: {resolved_policy}", err=True)
            sys.exit(3)
    else:
        # Auto-detect .foundry/gate-policy.yaml in CWD
        default_policy = Path(".foundry/gate-policy.yaml")
        if default_policy.exists():
            resolved_policy = default_policy

    # =========================================================================
    # Branch: --extract-only (Stages A-B, save intermediates, emit prompts)
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
        from primitives.confidence_gate.shared.orchestrator import (  # noqa: PLC0415
            run_pre_llm,
        )
        from primitives.confidence_gate.shared.rationale_drafter import (  # noqa: PLC0415
            build_prompt_item_for_rationale,
        )

        try:
            primitive_summaries, gate_score, all_findings = run_pre_llm(
                report_paths=resolved_paths,
                policy_path=resolved_policy,
            )
        except FileNotFoundError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(2)
        except ValueError as exc:
            msg = str(exc)
            if "gate-policy" in msg.lower() or "policy" in msg.lower():
                click.echo(f"Error: {exc}", err=True)
                sys.exit(3)
            click.echo(f"Error: {exc}", err=True)
            sys.exit(2)

        # Save intermediates for --continue
        save_intermediate(
            "summaries",
            [s.model_dump(by_alias=True) for s in primitive_summaries],
            resolved_state_dir,
        )
        save_intermediate(
            "gate_score",
            gate_score.model_dump(by_alias=True),
            resolved_state_dir,
        )
        save_intermediate(
            "findings",
            [f.model_dump(by_alias=True) for f in all_findings],
            resolved_state_dir,
        )

        # Build one prompt item for the rationale
        prompt_item = build_prompt_item_for_rationale(primitive_summaries, gate_score, all_findings)

        envelope = PromptEnvelope(
            primitive="confidence-gate",
            round=1,
            items=[prompt_item],
        )
        prompts_path = resolved_state_dir / "round-1-prompts.json"
        write_prompts(envelope, prompts_path)
        click.echo(f"WROTE {prompts_path}")

        # Save pipeline state
        save_state(
            PipelineState(
                primitive="confidence-gate",
                round=1,
                completed_stages=["A", "B"],
                next_stage="C_continue",
                status="awaiting_judgments",
            ),
            resolved_state_dir,
        )

        sys.exit(7)

    # =========================================================================
    # No skill-first flag: the rationale has no in-CLI LLM path anymore.
    # =========================================================================
    # The narrative rationale is produced in-session via the skill-first
    # protocol; a bare invocation has nothing to synthesize. Direct the caller
    # to --extract-only.
    raise click.UsageError(
        "confidence-gate requires the skill-first protocol: run with "
        "--extract-only to emit the rationale prompt, then "
        "--continue --judgments <path> to render the report."
    )
