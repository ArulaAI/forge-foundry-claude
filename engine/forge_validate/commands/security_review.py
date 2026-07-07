"""Forge Foundry — `forge-validate security-review` subcommand (S11).

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
    from primitives.security_review.shared.models import Confidence


@click.command("security-review")
@click.argument("code_path", type=click.Path(exists=False))
@click.option(
    "--spec",
    "spec_path",
    type=click.Path(exists=True),
    default=None,
    help="Optional spec file for LLM context.",
)
@click.option(
    "--security-yaml",
    "security_yaml_path",
    type=click.Path(exists=True),
    default=None,
    help="Path to .foundry/security.yaml (default: auto-detect).",
)
@click.option(
    "--compliance-yaml",
    "compliance_yaml_path",
    type=click.Path(exists=True),
    default=None,
    help="Path to .foundry/compliance.yaml for compliance_refs annotation.",
)
@click.option(
    "--git-diff",
    type=str,
    default=None,
    help="Git diff range (e.g. HEAD~1...HEAD) to narrow scope to changed files.",
)
@click.option(
    "--file-pattern",
    type=str,
    default=None,
    help="Glob pattern to filter files (e.g. 'src/*.py').",
)
@click.option(
    "--language",
    type=click.Choice(["python", "java", "ts", "auto"]),
    default="auto",
    help="Source language (default: auto-detect).",
)
@click.option(
    "--lens",
    "lenses",
    type=click.Choice(
        [
            "authn",
            "authz",
            "crypto",
            "input_handling",
            "data_exposure",
            "secrets_mgmt",
            "owasp_top10",
        ]
    ),
    multiple=True,
    default=[],
    help="Security lenses to apply (repeatable; default: all 7).",
)
@format_option(default="markdown")
@list_formats_option("primitives.security_review.shared.report_renderer")
@click.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Write output to file (default: stdout).",
)
@click.option(
    "--confidence-threshold",
    type=click.Choice(["low", "medium", "high"]),
    default="medium",
    help="Minimum confidence for surfaced findings (default: medium).",
)
@click.option(
    "--max-severity",
    type=click.Choice(["high", "critical"]),
    default=None,
    help="CI gate: exit 4 if any finding is at or above this severity.",
)
@click.option(
    "--skip-ai-quality",
    is_flag=True,
    default=False,
    help="Skip sub-call rule §3.4 (C10 + C11 audit).",
)
@click.option(
    "--concurrency",
    type=click.IntRange(min=1, max=32),
    default=5,
    help="Max files reviewed in parallel (PERF-2; default 5).",
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
def cli(  # noqa: PLR0912, PLR0913, PLR0915
    code_path: str,
    spec_path: str | None,
    security_yaml_path: str | None,
    compliance_yaml_path: str | None,
    git_diff: str | None,
    file_pattern: str | None,
    language: str,
    lenses: tuple[str, ...],
    format_: str,
    output: str | None,
    confidence_threshold: str,
    max_severity: str | None,
    skip_ai_quality: bool,
    concurrency: int,  # retained CLI flag; the deleted CI path consumed it.
    include_vendored: bool,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """Deep security review with OWASP/NIST-grounded lenses (S11 /security-review).

    Applies 7 security lenses (authn, authz, crypto, input_handling, data_exposure,
    secrets_mgmt, owasp_top10) and optionally invokes C10+C11 sub-calls with
    dedup discipline (sub-call rule §3.4).

    Exit codes:
    - 0: success (no findings exceed severity gate)
    - 2: code path not found
    - 4: findings exceed --max-severity threshold
    - 5: C10/C11 sub-call returned critical findings (after dedup)
    - 7: awaiting judgments (skill-first mode)
    """
    # --- Validate skill-first flags -----------------------------------------
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )

    from typing import cast as _cast  # noqa: PLC0415

    resolved_code = Path(code_path)
    if not resolved_code.exists():
        click.echo(f"Error: code path not found: {resolved_code}", err=True)
        sys.exit(2)

    resolved_spec = Path(spec_path) if spec_path else None
    resolved_sec_yaml = Path(security_yaml_path) if security_yaml_path else None
    resolved_comp_yaml = Path(compliance_yaml_path) if compliance_yaml_path else None
    resolved_output = Path(output) if output else None
    resolved_state_dir = Path(state_dir) if state_dir is not None else resolved_code / ".state"

    # =========================================================================
    # Branch: --extract-only (enumerate files, emit per-file review prompts)
    # =========================================================================
    if extract_only:
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
        from primitives.security_review.shared.compliance_annotator import (  # noqa: PLC0415
            load_compliance_refs,
        )
        from primitives.security_review.shared.file_enumerator import (  # noqa: PLC0415
            enumerate_files,
        )
        from primitives.security_review.shared.lens_reviewer import (  # noqa: PLC0415
            build_prompt_item_for_file,
        )
        from primitives.security_review.shared.models import (  # noqa: PLC0415
            SecurityLens as _SecurityLens,
        )
        from primitives.security_review.shared.pr_diff_filter import (  # noqa: PLC0415
            get_changed_files,
        )
        from primitives.security_review.shared.security_yaml_loader import (  # noqa: PLC0415
            load_security_yaml,
        )
        from primitives.security_review.shared.sensitive_area import (  # noqa: PLC0415
            detect_sensitive_areas,
        )

        _ALL_LENSES: list[_SecurityLens] = [
            "authn",
            "authz",
            "crypto",
            "input_handling",
            "data_exposure",
            "secrets_mgmt",
            "owasp_top10",
        ]
        selected_lenses: list[_SecurityLens] = (
            _cast("list[_SecurityLens]", list(lenses)) if lenses else _ALL_LENSES
        )

        # --- Load spec text ---
        spec_text: str | None = None
        if resolved_spec is not None and resolved_spec.exists():
            spec_text = resolved_spec.read_text(encoding="utf-8")

        # --- Load security.yaml ---
        effective_sec_yaml = resolved_sec_yaml
        if effective_sec_yaml is None:
            default_sec_yaml = resolved_code / ".foundry" / "security.yaml"
            effective_sec_yaml = default_sec_yaml if default_sec_yaml.exists() else None
        load_security_yaml(effective_sec_yaml)  # run for side-effects / validation

        # --- Load compliance refs for annotation ---
        effective_comp_yaml = resolved_comp_yaml
        if effective_comp_yaml is None:
            default_comp_yaml = resolved_code / ".foundry" / "compliance.yaml"
            effective_comp_yaml = default_comp_yaml if default_comp_yaml.exists() else None
        valid_compliance_refs = load_compliance_refs(effective_comp_yaml)

        # --- Stage A.1: detect sensitive areas ---
        from primitives.security_review.shared.security_yaml_loader import (  # noqa: PLC0415
            load_security_yaml as _lsy,
        )

        security_config = _lsy(effective_sec_yaml)
        sensitive_areas = detect_sensitive_areas(
            resolved_code, security_config, include_vendored=include_vendored
        )
        click.echo(
            f"[security-review] --extract-only: {len(sensitive_areas)} sensitive area(s) detected.",
            err=True,
        )

        # --- Stage A.2: enumerate files ---
        allowed_files = None
        if git_diff is not None:
            try:
                allowed_files = get_changed_files(
                    git_diff, repo_root=resolved_code, primitive_name="security-review"
                )
            except Exception as exc:
                click.echo(
                    f"Error: --git-diff '{git_diff}' could not be resolved: {exc}",
                    err=True,
                )
                sys.exit(2)

        files = enumerate_files(
            code_path=resolved_code,
            language=_cast("Literal['python', 'java', 'ts', 'auto']", language),
            file_pattern=file_pattern,
            allowed_files=allowed_files,
            primitive_name="security-review",
            include_vendored=include_vendored,
        )
        click.echo(f"[security-review] --extract-only: {len(files)} file(s) to review.", err=True)

        # --- Build one prompt item per file (batched mode) ---
        prompt_items: list[PromptItem] = []
        file_paths_serialized: list[str] = []
        for file in files:
            try:
                source_code = file.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                click.echo(f"[security-review] WARNING: could not read {file}: {exc}", err=True)
                continue

            item_dict = build_prompt_item_for_file(
                file=file,
                source_code=source_code,
                lenses=selected_lenses,
                spec_text=spec_text,
                valid_compliance_refs=valid_compliance_refs,
                code_path=resolved_code,
            )
            prompt_items.append(
                PromptItem(
                    id=item_dict["id"],
                    system=item_dict["system"],
                    user=item_dict["user"],
                    response_format=ResponseFormat(**item_dict["response_format"]),
                )
            )
            file_paths_serialized.append(str(file))

        if not prompt_items:
            click.echo("[security-review] --extract-only: no files to review; exiting 0.", err=True)
            sys.exit(0)

        # --- Save intermediates ---
        save_intermediate("files", file_paths_serialized, resolved_state_dir)
        save_intermediate(
            "config",
            {
                "lenses": selected_lenses,
                "confidence_threshold": confidence_threshold,
                "max_severity": max_severity,
                "format_": format_,
                "output": str(resolved_output) if resolved_output else None,
                "language": language,
                "code_path": str(resolved_code),
                "skip_ai_quality": skip_ai_quality,
                "sensitive_areas": [sa.model_dump() for sa in sensitive_areas],
            },
            resolved_state_dir,
        )

        # --- Write prompts envelope ---
        envelope = PromptEnvelope(
            primitive="security-review",
            round=1,
            items=prompt_items,
        )
        prompts_path = resolved_state_dir / "round-1-prompts.json"
        write_prompts(envelope, prompts_path)
        click.echo(f"WROTE {prompts_path}")

        # --- Save pipeline state ---
        save_state(
            PipelineState(
                primitive="security-review",
                round=1,
                completed_stages=["A.1", "A.2"],
                next_stage="B_continue",
                status="awaiting_judgments",
            ),
            resolved_state_dir,
        )

        sys.exit(7)

    # =========================================================================
    # Branch: --continue --judgments (apply per-file judgments, render report)
    # =========================================================================
    if continue_:
        assert judgments is not None  # guaranteed by validate_skill_first_flags

        from datetime import UTC, datetime  # noqa: PLC0415, I001
        from primitives._shared.pipeline_state import (  # noqa: PLC0415
            cleanup_state,
            load_intermediate,
        )
        from primitives._shared.protocol import read_judgments  # noqa: PLC0415
        from primitives.security_review.shared.compliance_annotator import (  # noqa: PLC0415
            annotate_compliance_refs,
            load_compliance_refs,
        )
        from primitives.security_review.shared.confidence_filter import (  # noqa: PLC0415
            filter_by_confidence,
        )
        from primitives.security_review.shared.lens_reviewer import (  # noqa: PLC0415
            apply_lens_judgments_for_file,
        )
        from primitives.security_review.shared.models import (  # noqa: PLC0415
            SecurityLens as _SecurityLens,
            SecurityReviewReport,
            SensitiveArea,
        )
        from primitives.security_review.shared.report_renderer import (  # noqa: PLC0415
            render_report,
        )

        # Load intermediates
        file_paths = load_intermediate("files", resolved_state_dir)
        config = load_intermediate("config", resolved_state_dir)

        # Reconstruct sensitive_areas
        sensitive_areas_raw = config.get("sensitive_areas", [])
        sensitive_areas = [SensitiveArea.model_validate(sa) for sa in sensitive_areas_raw]

        # Read judgments
        judgment_envelope = read_judgments(Path(judgments))
        judgment_map = {item.id: item.response for item in judgment_envelope.items}

        # Reload compliance refs for annotation
        resolved_code_from_config = Path(config.get("code_path", str(resolved_code)))
        effective_comp_yaml = resolved_comp_yaml
        if effective_comp_yaml is None:
            default_comp_yaml = resolved_code_from_config / ".foundry" / "compliance.yaml"
            effective_comp_yaml = default_comp_yaml if default_comp_yaml.exists() else None
        valid_compliance_refs = load_compliance_refs(effective_comp_yaml)

        # Apply judgments per file
        all_raw_findings = []
        for file_str in file_paths:
            file = Path(file_str)
            # Build the item id the same way --extract-only did
            try:
                rel = file.relative_to(resolved_code_from_config)
                item_id = f"security:{rel}"
            except ValueError:
                item_id = f"security:{file}"

            response = judgment_map.get(item_id)
            if response is None:
                click.echo(
                    f"[security-review] WARNING: no judgment for {item_id}; skipping",
                    err=True,
                )
                continue

            if not isinstance(response, list):
                click.echo(
                    f"[security-review] WARNING: judgment for {item_id} is not a list; skipping",
                    err=True,
                )
                continue

            findings = apply_lens_judgments_for_file(
                response_array=response,
                file=file,
                valid_compliance_refs=valid_compliance_refs,
            )
            all_raw_findings.extend(findings)

        click.echo(
            f"[security-review] --continue: {len(all_raw_findings)} raw finding(s) from judgments.",
            err=True,
        )

        # Stage C: confidence filter
        saved_threshold = _cast(
            "Confidence", config.get("confidence_threshold", confidence_threshold)
        )
        findings = filter_by_confidence(all_raw_findings, saved_threshold)
        click.echo(
            f"[security-review] --continue: {len(findings)} finding(s) after confidence filter.",
            err=True,
        )

        # Stage D: compliance annotation
        findings = annotate_compliance_refs(findings, valid_refs=valid_compliance_refs)

        # AI quality sub-call skipped in skill-first mode
        saved_skip = config.get("skip_ai_quality", True)
        if not saved_skip:
            click.echo(
                "[security-review] NOTE: AI quality sub-call (§3.4) is unavailable in "
                "skill-first mode and has been skipped.",
                err=True,
            )

        # Build report
        saved_lenses = _cast("list[_SecurityLens]", config.get("lenses", []))
        report = SecurityReviewReport(
            code_path=str(resolved_code_from_config),
            lenses=saved_lenses,
            sensitive_areas=sensitive_areas,
            findings=findings,
            ai_quality=None,
            generated_at=datetime.now(tz=UTC),
        )

        # Render — the CONTINUE invocation's --format/--output control rendering,
        # NOT the values saved at --extract-only time. Reading config here made a
        # continue-time `--format json` silently render markdown (#1195 4a);
        # architecture-review already threads the invocation values, so mirror it.
        rendered = render_report(report, format_)

        # Write output — likewise the invocation's --output wins.
        effective_output = resolved_output
        if effective_output is not None:
            effective_output.parent.mkdir(parents=True, exist_ok=True)
            effective_output.write_text(rendered, encoding="utf-8")
            click.echo(f"[security-review] Wrote report to {effective_output}", err=True)
        else:
            emit_report(rendered)

        # Cleanup state
        cleanup_state(resolved_state_dir)

        # Exit code logic
        saved_max_severity = config.get("max_severity", max_severity)
        if saved_max_severity is not None:
            threshold = _SEVERITY_ORDER.get(saved_max_severity, 2)
            if any(_SEVERITY_ORDER.get(f.severity, 0) >= threshold for f in report.findings):
                sys.exit(4)

        sys.exit(0)

    # No execution mode selected: --extract-only and --continue are the only paths.
    raise click.UsageError(
        "security-review requires --extract-only (to emit prompts) or "
        "--continue --judgments (to apply judgments)."
    )
