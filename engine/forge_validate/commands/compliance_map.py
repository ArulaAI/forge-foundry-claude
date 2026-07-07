"""Forge Foundry — `forge-validate compliance-map` subcommand (S70).

Was inlined in `forge_validate/cli.py` before #101 / TOOL-2.

Skill-first (--extract-only / --continue) dispatch:

  --extract-only (round 1):
    1. Load spec, select frameworks, load catalogs.
    2. Build ``controls:<fw>/<family>`` prompt items (one per family group).
    3. Write ``round-1-prompts.json``, save intermediates, exit 7.

  --continue after round 1 (pipeline.round == 1):
    1. Load state + intermediates; apply controls judgments -> list[ControlMatch].
    2. If ``--code-path`` given: run D.1 regex pre-scan, build ``evidence:`` items,
       write ``round-2-prompts.json``, update state (round=2), exit 7.
    3. Else: run Stage E (gap analysis), render, cleanup, exit 0/4.

  --continue after round 2 (pipeline.round == 2):
    1. Load state + intermediates; apply evidence judgments -> updated ControlMatch list.
    2. Run Stage E, render, cleanup, exit 0/4.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

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
from primitives.compliance_map.shared.framework_selector import FrameworkResolutionError

_ROUND_2 = 2


@click.command("compliance-map")
@click.argument("spec_path", type=click.Path(exists=False))
@click.option(
    "--code-path",
    type=click.Path(exists=False),
    default=None,
    help="Code path for Stage D evidence finding + sub-call rule §3.4 (C10 + C11).",
)
@click.option(
    "--framework",
    "frameworks",
    multiple=True,
    type=click.Choice(["pci-dss-4.0", "gdpr", "sox-404", "nist-800-53-rev5", "epa-environmental"]),
    default=[],
    help="Framework(s) to analyse (repeatable). Default: all from .foundry/compliance.yaml.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Custom catalog YAML merged into shipped catalog.",
)
@format_option(default="markdown")
@list_formats_option("primitives.compliance_map.shared.report_renderer")
@click.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Write output to file (default: stdout).",
)
@click.option(
    "--git-diff",
    type=str,
    default=None,
    help="PR-delta mode: git diff range (e.g. HEAD~1...HEAD).",
)
@click.option(
    "--max-severity",
    type=click.Choice(["high", "critical"]),
    default=None,
    help="CI gate: exit 4 if any gap is at or above this severity.",
)
@click.option(
    "--skip-ai-quality",
    is_flag=True,
    default=False,
    help="Skip sub-call rule §3.4 (C10 + C11 audit) even when --code-path is given.",
)
@click.option(
    "--include-vendored",
    is_flag=True,
    default=False,
    help="Include emitted/vendored harness trees (.foundry, .claude, .github/skills, …) "
    "in the evidence scan. Dependency/build dirs stay excluded.",
)
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(  # noqa: PLR0913, PLR0915
    spec_path: str,
    code_path: str | None,
    frameworks: tuple[str, ...],
    config_path: str | None,
    format_: str,
    output: str | None,
    git_diff: str | None,
    max_severity: str | None,
    skip_ai_quality: bool,
    include_vendored: bool,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """Map a feature/PR to compliance controls (S70 compliance-map)."""

    # --- Validate skill-first flags (--continue requires --judgments) ---
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )

    resolved_spec = Path(spec_path)
    resolved_config = Path(config_path) if config_path else None
    resolved_output = Path(output) if output else None
    fw_list = list(frameworks) if frameworks else []

    # Default state directory sits alongside the spec file.
    resolved_state_dir = (
        Path(state_dir) if state_dir is not None else resolved_spec.parent / ".compliance-map-state"
    )

    # =========================================================================
    # Branch: --extract-only (round 1) -- emit controls prompts, exit 7
    # =========================================================================
    if extract_only:
        # Lazy imports for cold-start budget (TOOL-2).
        from primitives._shared.pipeline_state import (  # noqa: PLC0415
            PipelineState,
            save_intermediate,
            save_state,
        )
        from primitives._shared.protocol import (  # noqa: PLC0415
            PromptEnvelope,
            PromptItem,
            ResponseFormat,
            write_prompts,
        )
        from primitives.compliance_map.shared.catalog_loader import (  # noqa: PLC0415
            load_catalog,
        )
        from primitives.compliance_map.shared.framework_selector import (  # noqa: PLC0415
            select_frameworks,
        )
        from primitives.compliance_map.shared.touched_controls import (  # noqa: PLC0415
            build_prompt_items_for_controls,
        )

        if not resolved_spec.exists():
            click.echo(f"Error: spec file not found: {resolved_spec}", err=True)
            sys.exit(2)

        spec_text = resolved_spec.read_text(encoding="utf-8")

        # Select frameworks.
        try:
            resolved_frameworks = select_frameworks(
                cli_frameworks=fw_list if fw_list else None,
                config_path=resolved_config,
                project_root=resolved_spec.parent,
            )
        except FrameworkResolutionError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(3)

        # Load catalogs + build prompt items.
        all_prompt_items: list[PromptItem] = []
        for fw in resolved_frameworks:
            catalog = load_catalog(fw, extra_yaml=resolved_config)
            items = build_prompt_items_for_controls(catalog, spec_text)
            all_prompt_items.extend(items)

        if not all_prompt_items:
            click.echo(
                "[compliance-map] No controls found in catalogs -- nothing to assess.",
                err=True,
            )
            sys.exit(0)

        envelope = PromptEnvelope(
            primitive="compliance-map",
            round=1,
            items=all_prompt_items,
        )
        prompts_path = resolved_state_dir / "round-1-prompts.json"
        write_prompts(envelope, prompts_path)
        click.echo(f"WROTE {prompts_path}")

        # Save intermediates.
        resolved_code = Path(code_path) if code_path else None
        save_intermediate("spec_text", spec_text, resolved_state_dir)
        save_intermediate("frameworks", resolved_frameworks, resolved_state_dir)
        save_intermediate(
            "code_path",
            str(resolved_code) if resolved_code else None,
            resolved_state_dir,
        )
        save_intermediate("format_", format_, resolved_state_dir)
        save_intermediate(
            "output",
            str(resolved_output) if resolved_output else None,
            resolved_state_dir,
        )
        save_intermediate("max_severity", max_severity, resolved_state_dir)

        save_state(
            PipelineState(
                primitive="compliance-map",
                round=1,
                completed_stages=["A", "B", "C_extract"],
                next_stage="C_continue",
                status="awaiting_judgments",
            ),
            resolved_state_dir,
        )
        sys.exit(7)

    # =========================================================================
    # Branch: --continue (resume after in-session judgment)
    # =========================================================================
    if continue_:
        # Lazy imports.
        from primitives._shared.pipeline_state import (  # noqa: PLC0415
            PipelineState,
            load_intermediate,
            load_state,
            save_intermediate,
            save_state,
        )
        from primitives._shared.protocol import (  # noqa: PLC0415
            PromptEnvelope,
            PromptItem,
            ResponseFormat,
            read_judgments,
            write_prompts,
        )
        from primitives.compliance_map.shared.catalog_loader import (  # noqa: PLC0415
            load_catalog,
        )
        from primitives.compliance_map.shared.models import ControlMatch  # noqa: PLC0415
        from primitives.compliance_map.shared.touched_controls import (  # noqa: PLC0415
            apply_controls_judgments,
        )

        pipeline = load_state(resolved_state_dir)

        # Load shared intermediates.
        spec_text = load_intermediate("spec_text", resolved_state_dir)
        resolved_frameworks = load_intermediate("frameworks", resolved_state_dir)
        stored_code_path_str = load_intermediate("code_path", resolved_state_dir)
        stored_format_ = load_intermediate("format_", resolved_state_dir)
        stored_output_str = load_intermediate("output", resolved_state_dir)
        stored_max_severity = load_intermediate("max_severity", resolved_state_dir)

        # CLI flags passed to --continue override stored values
        # (the code-path might not have been available at extract-only time).
        eff_code_path = (
            Path(code_path)
            if code_path is not None
            else (Path(stored_code_path_str) if stored_code_path_str else None)
        )
        eff_output = (
            Path(output)
            if output is not None
            else (Path(stored_output_str) if stored_output_str else None)
        )
        eff_max_severity = max_severity if max_severity is not None else stored_max_severity

        assert judgments is not None  # guaranteed by validate_skill_first_flags
        judgment_envelope = read_judgments(Path(judgments))

        # -------------------------------------------------------------------------
        # Sub-branch: round 1 -- apply controls, maybe emit evidence prompts
        # -------------------------------------------------------------------------
        if pipeline.round == 1:
            judgment_map: dict[str, object] = {
                item.id: item.response for item in judgment_envelope.items
            }

            all_matches: list[ControlMatch] = []
            for fw in resolved_frameworks:
                catalog = load_catalog(fw, extra_yaml=resolved_config)
                matches = apply_controls_judgments(catalog, judgment_map)
                all_matches.extend(matches)

            # If code-path available and touched controls exist -> emit evidence prompts.
            if eff_code_path is not None and all_matches:
                from primitives.compliance_map.shared.evidence_finder import (  # noqa: PLC0415
                    _EVIDENCE_RESPONSE_SCHEMA,
                    _build_verification_user_message,
                    build_prompt_items_for_evidence,
                )
                from primitives.compliance_map.shared.prompts import (  # noqa: PLC0415
                    EVIDENCE_VERIFICATION_SYSTEM_PROMPT,
                )

                triples = build_prompt_items_for_evidence(
                    all_matches, eff_code_path, include_vendored=include_vendored
                )

                if triples:
                    evidence_items = [
                        PromptItem(
                            id=item_id,
                            system=EVIDENCE_VERIFICATION_SYSTEM_PROMPT,
                            user=_build_verification_user_message(hit, ctrl, eff_code_path),
                            response_format=ResponseFormat(
                                type="json",
                                schema=_EVIDENCE_RESPONSE_SCHEMA,
                            ),
                        )
                        for item_id, hit, ctrl in triples
                    ]

                    envelope = PromptEnvelope(
                        primitive="compliance-map",
                        round=2,
                        items=evidence_items,
                    )
                    prompts_path = resolved_state_dir / "round-2-prompts.json"
                    write_prompts(envelope, prompts_path)
                    click.echo(f"WROTE {prompts_path}")

                    save_intermediate(
                        "matches",
                        [m.model_dump(by_alias=True) for m in all_matches],
                        resolved_state_dir,
                    )
                    save_state(
                        PipelineState(
                            primitive="compliance-map",
                            round=2,
                            completed_stages=["A", "B", "C", "D_extract"],
                            next_stage="D_continue",
                            status="awaiting_judgments",
                        ),
                        resolved_state_dir,
                    )
                    sys.exit(7)

            # No evidence round needed -- proceed straight to gap analysis.
            _run_gap_and_render(
                all_matches=all_matches,
                resolved_frameworks=resolved_frameworks,
                spec_text=spec_text,
                format_=stored_format_,
                output=eff_output,
                max_severity=eff_max_severity,
                state_dir=resolved_state_dir,
            )
            return

        # -------------------------------------------------------------------------
        # Sub-branch: round 2 -- apply evidence, gap analysis, render
        # -------------------------------------------------------------------------
        if pipeline.round == _ROUND_2:
            from primitives.compliance_map.shared.evidence_finder import (  # noqa: PLC0415
                apply_evidence_judgments,
            )

            evidence_judgment_map: dict[str, object] = {
                item.id: item.response for item in judgment_envelope.items
            }

            raw_matches = load_intermediate("matches", resolved_state_dir)
            pre_matches = [ControlMatch.model_validate(m) for m in raw_matches]

            updated_matches = apply_evidence_judgments(
                pre_matches,
                evidence_judgment_map,
                eff_code_path if eff_code_path is not None else Path(),
                include_vendored=include_vendored,
            )

            _run_gap_and_render(
                all_matches=updated_matches,
                resolved_frameworks=resolved_frameworks,
                spec_text=spec_text,
                format_=stored_format_,
                output=eff_output,
                max_severity=eff_max_severity,
                state_dir=resolved_state_dir,
            )
            return

        click.echo(
            f"Error: unexpected pipeline round {pipeline.round} in state; "
            "delete .compliance-map-state and re-run.",
            err=True,
        )
        sys.exit(1)

    # Neither --extract-only nor --continue: the Python CLI never calls an LLM, so
    # there is no autonomous run path. The skill body drives the extract→judge→
    # continue protocol in-session.
    raise click.UsageError(
        "compliance-map requires --extract-only (round 1) or --continue --judgments "
        "(resume). The agent session runs the skill body to produce judgments."
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run_gap_and_render(
    *,
    all_matches: list[Any],
    resolved_frameworks: list[Any],
    spec_text: str,
    format_: str,
    output: Path | None,
    max_severity: str | None,
    state_dir: Path,
) -> None:
    """Run Stage E gap analysis + render + exit-code logic (both --continue rounds)."""
    from primitives._shared.pipeline_state import cleanup_state  # noqa: PLC0415
    from primitives.compliance_map.shared.gap_analyzer import analyze_gaps  # noqa: PLC0415
    from primitives.compliance_map.shared.models import ComplianceReport  # noqa: PLC0415
    from primitives.compliance_map.shared.orchestrator import _extract_title  # noqa: PLC0415
    from primitives.compliance_map.shared.report_renderer import render_report  # noqa: PLC0415

    spec_title = _extract_title(spec_text)
    updated_matches, gaps = analyze_gaps(
        matches=all_matches,
        ai_quality=None,  # AI quality sub-call not available in skill-first mode.
        selected_frameworks=resolved_frameworks,
    )

    report = ComplianceReport(
        spec_title=spec_title,
        frameworks=resolved_frameworks,
        matches=updated_matches,
        gaps=gaps,
        ai_quality=None,
    )

    rendered = render_report(report, format_)

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        click.echo(f"WROTE {output}")
    else:
        emit_report(rendered)

    cleanup_state(state_dir)

    if max_severity is not None and gaps:
        threshold = _SEVERITY_ORDER.get(max_severity, 2)
        exceeded = [g for g in gaps if _SEVERITY_ORDER.get(g.severity, 0) >= threshold]
        if exceeded:
            sys.exit(4)
