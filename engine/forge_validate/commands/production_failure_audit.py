"""Forge Foundry — `forge-validate production-failure-audit` subcommand.

Was inlined in `forge_validate/cli.py` before #101 / TOOL-2.

Skill-first mode (--extract-only / --continue) adds the extract→judge→continue
protocol. C11 LLM dispatch is "NOT yet wired" (per llm_judge.py header); by
default config.requires_llm() is False, so --extract-only exits 0 without
writing any prompts. When config enables LLM mode (FM6/FM7/FM10 mode=llm/both),
the skill-first path collects candidates and emits prompts, then --continue
applies judgments and renders the final report.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from forge_validate._cli_common import (
    continue_option,
    emit_report,
    extract_only_option,
    format_option,
    judgments_option,
    list_formats_option,
    state_dir_option,
    validate_skill_first_flags,
)
from primitives.failure_mode_audit import AuditConfig, filter_by_severity

if TYPE_CHECKING:
    from primitives.failure_mode_audit import Severity
    from primitives.failure_mode_audit.shared.findings import Finding
from primitives.failure_mode_audit.shared.output.json_writer import render_json
from primitives.failure_mode_audit.shared.output.markdown import render_markdown
from primitives.failure_mode_audit.shared.output.sarif import render_sarif
from primitives.production_failure_audit import audit_production_path


@click.command("production-failure-audit")
@click.argument("path", type=click.Path(exists=True))
@format_option(default="markdown")
@list_formats_option("primitives.production_failure_audit.shared.report_renderer")
@click.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Write to file (required for sarif).",
)
@click.option(
    "--max-severity",
    type=click.Choice(["low", "medium", "high", "critical"]),
    default=None,
    help="Exit nonzero if any finding >= threshold.",
)
@click.option(
    "--include-vendored",
    is_flag=True,
    default=False,
    help=(
        "Re-include the vendored harness trees (.foundry, .claude) + emitted "
        "skill/rule subpaths in the scan. Dependency dirs (.venv, node_modules, "
        "…) are always excluded. Default off (#1192)."
    ),
)
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(
    path: str,
    format_: str,
    output: str | None,
    max_severity: str | None,
    include_vendored: bool,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """Audit a path for production failure modes (FM6-FM10)."""
    from primitives.production_failure_audit.shared.report_renderer import (  # noqa: PLC0415
        list_formats as _list_formats,
    )

    # FLEX-3: --format is STRING (plugin-extensible); validate explicitly since the
    # render dispatch below would otherwise fall through to markdown on an unknown
    # value (the click.Choice that previously guarded this is gone, #732).
    if format_ not in _list_formats():
        click.echo(
            f"Error: unknown format {format_!r}; valid: {', '.join(_list_formats())}",
            err=True,
        )
        sys.exit(2)

    if format_ == "sarif" and not output:
        click.echo("Error: --output FILE is required for --format sarif", err=True)
        sys.exit(2)

    # Enforces the --continue requires --judgments check.
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )

    target = Path(path)
    project_root = target if target.is_dir() else target.parent
    config = AuditConfig.from_project(project_root)
    # CLI flag overrides config: scope the scan to first-party source (#1192).
    config.include_vendored = include_vendored

    # Resolve state directory
    resolved_state_dir = (
        Path(state_dir)
        if state_dir is not None
        else (target if target.is_dir() else target.parent) / ".state"
    )

    # =========================================================================
    # Branch: --continue (resume after in-session judgment)
    # =========================================================================
    if continue_:
        _do_continue(
            config=config,
            format_=format_,
            output=output,
            max_severity=max_severity,
            judgments_path=Path(judgments),  # type: ignore[arg-type]
            state_dir=resolved_state_dir,
            explicit=(state_dir is not None),
        )
        return

    # =========================================================================
    # Branch: --extract-only (run deterministic stages, emit prompts if needed)
    # =========================================================================
    if extract_only:
        _do_extract_only(
            target=target,
            config=config,
            state_dir=resolved_state_dir,
            format_=format_,
            output=output,
            max_severity=max_severity,
        )
        return

    # =========================================================================
    # Branch: normal (deterministic) mode
    # =========================================================================
    # LLM-judged FM6/FM7/FM10 run only via the skill-first --extract-only /
    # --continue path; normal mode is the deterministic detector composition.
    findings = audit_production_path(target, config=config)
    _render_and_exit(findings, format_=format_, output=output, max_severity=max_severity)


# ---------------------------------------------------------------------------
# Skill-first helpers
# ---------------------------------------------------------------------------


def _do_extract_only(
    *,
    target: Path,
    config: AuditConfig,
    state_dir: Path,
    format_: str,
    output: str | None,
    max_severity: str | None,
) -> None:
    """Run FM6-FM10 deterministic scan; emit prompts for LLM candidates when configured.

    When LLM mode is configured (FM6/FM7/FM10 mode=llm/both) and candidates
    exist, writes round-1-prompts.json and exits 7. Otherwise there is nothing
    for the caller to judge, so the deterministic FM6-FM10 findings are rendered
    directly (exit per the --max-severity gate) instead of a silent exit-0 that
    hides them (#1192).
    """
    from primitives._shared.pipeline_state import (  # noqa: PLC0415
        PipelineState,
        save_intermediate,
        save_state,
    )
    from primitives._shared.protocol import (  # noqa: PLC0415
        PromptEnvelope,
        write_prompts,
    )
    from primitives.production_failure_audit.shared.fm6_stale_pattern import (  # noqa: PLC0415
        audit_fm6,
    )
    from primitives.production_failure_audit.shared.fm7_overgeneralization import (  # noqa: PLC0415
        audit_fm7,
    )
    from primitives.production_failure_audit.shared.fm8_compliance_blindness import (  # noqa: PLC0415
        audit_fm8,
    )
    from primitives.production_failure_audit.shared.fm9_test_theater import (  # noqa: PLC0415
        audit_fm9,
    )
    from primitives.production_failure_audit.shared.fm10_arch_decay import (  # noqa: PLC0415
        audit_fm10,
    )
    from primitives.production_failure_audit.shared.llm_judge import (  # noqa: PLC0415
        build_prompt_items_for_fm6_candidates,
        build_prompt_items_for_fm7_candidates,
        build_prompt_items_for_fm10_candidates,
        serialize_finding,
    )

    project_root = target if target.is_dir() else target.parent

    # Run all detectors; separate candidates (LLM mode) from confirmed (regex mode)
    deterministic_findings = []
    fm6_candidates = []
    fm7_candidates = []
    fm10_candidates = []

    fm6_mode = config.production.fm6_stale_pattern.mode
    fm7_mode = config.production.fm7_overgeneralization.mode
    fm10_mode = config.production.fm10_arch_decay.mode

    if config.production.fm6_stale_pattern.enabled:
        raw_fm6 = audit_fm6(target, config=config)
        if fm6_mode in ("llm", "both"):
            fm6_candidates = raw_fm6
        else:
            deterministic_findings.extend(raw_fm6)

    if config.production.fm7_overgeneralization.enabled:
        raw_fm7 = audit_fm7(target, config=config)
        if fm7_mode in ("llm", "both"):
            fm7_candidates = raw_fm7
        else:
            deterministic_findings.extend(raw_fm7)

    # FM8 and FM9 are always static (no LLM)
    if config.production.fm8_compliance_blindness.enabled:
        deterministic_findings.extend(audit_fm8(target, config=config))

    if config.production.fm9_test_theater.enabled:
        deterministic_findings.extend(audit_fm9(target, config=config))

    if config.production.fm10_arch_decay.enabled:
        raw_fm10 = audit_fm10(target, project_root=project_root, config=config)
        if fm10_mode in ("llm", "both"):
            fm10_candidates = raw_fm10
        else:
            deterministic_findings.extend(raw_fm10)

    # Build prompt items from LLM candidates
    prompt_items = []
    # Use stack_name/stack_version defaults; real values come from config in v0.6.0
    prompt_items.extend(build_prompt_items_for_fm6_candidates(fm6_candidates, "unknown", "unknown"))
    prompt_items.extend(build_prompt_items_for_fm7_candidates(fm7_candidates, "unknown"))
    prompt_items.extend(build_prompt_items_for_fm10_candidates(fm10_candidates))

    # Save intermediates
    save_intermediate(
        "deterministic_findings",
        [serialize_finding(f) for f in deterministic_findings],
        state_dir,
    )
    save_intermediate(
        "fm6_candidates",
        [serialize_finding(f) for f in fm6_candidates],
        state_dir,
    )
    save_intermediate(
        "fm7_candidates",
        [serialize_finding(f) for f in fm7_candidates],
        state_dir,
    )
    save_intermediate(
        "fm10_candidates",
        [serialize_finding(f) for f in fm10_candidates],
        state_dir,
    )

    if not prompt_items:
        # No LLM candidates — nothing to judge. Render the deterministic
        # findings directly (and apply the severity gate) rather than exit 0
        # silently, which would strand real findings behind a --continue that
        # has no prompts to continue (#1192).
        click.echo(
            "[production-failure-audit] --extract-only: no LLM candidates; "
            "rendering deterministic FM6-FM10 findings.",
            err=True,
        )
        _render_and_exit(
            deterministic_findings, format_=format_, output=output, max_severity=max_severity
        )
        return

    # Write prompts envelope
    envelope = PromptEnvelope(
        primitive="production-failure-audit",
        round=1,
        items=prompt_items,
    )
    prompts_path = state_dir / "round-1-prompts.json"
    write_prompts(envelope, prompts_path)
    click.echo(f"WROTE {prompts_path}")

    save_state(
        PipelineState(
            primitive="production-failure-audit",
            round=1,
            completed_stages=["FM6_regex", "FM7_regex", "FM8", "FM9", "FM10_regex"],
            next_stage="FM6_FM7_FM10_continue",
            status="awaiting_judgments",
        ),
        state_dir,
    )

    sys.exit(7)


def _do_continue(
    *,
    config: AuditConfig,
    format_: str,
    output: str | None,
    max_severity: str | None,
    judgments_path: Path,
    state_dir: Path,
    explicit: bool = False,
) -> None:
    """Apply FM6/FM7/FM10 judgments, merge with deterministic findings, render."""
    from primitives._shared.pipeline_state import (  # noqa: PLC0415
        cleanup_state,
        load_intermediate,
    )
    from primitives._shared.protocol import (  # noqa: PLC0415
        assert_matching_round,
        read_judgments,
    )
    from primitives.production_failure_audit.shared.llm_judge import (  # noqa: PLC0415
        apply_fm6_judgment,
        apply_fm7_judgment,
        apply_fm10_judgment,
        deserialize_finding,
    )

    # Load saved intermediates
    deterministic_raw = load_intermediate("deterministic_findings", state_dir)
    fm6_raw = load_intermediate("fm6_candidates", state_dir)
    fm7_raw = load_intermediate("fm7_candidates", state_dir)
    fm10_raw = load_intermediate("fm10_candidates", state_dir)

    deterministic_findings = [deserialize_finding(d) for d in deterministic_raw]
    fm6_candidates = [deserialize_finding(d) for d in fm6_raw]
    fm7_candidates = [deserialize_finding(d) for d in fm7_raw]
    fm10_candidates = [deserialize_finding(d) for d in fm10_raw]

    # Read and apply judgments. C11 is single-round: reject a wrong-round judgments
    # file loudly rather than silently dropping the pending round (#1304).
    judgment_envelope = read_judgments(judgments_path)
    assert_matching_round(judgment_envelope, 1, path=judgments_path)
    judgment_map = {item.id: item.response for item in judgment_envelope.items}

    llm_findings = []

    for idx, candidate in enumerate(fm6_candidates):
        response = judgment_map.get(f"judge:fm6:{idx}")
        if response is not None and isinstance(response, dict):
            result = apply_fm6_judgment(candidate, response)
            if result is not None:
                llm_findings.append(result)

    for idx, candidate in enumerate(fm7_candidates):
        response = judgment_map.get(f"judge:fm7:{idx}")
        if response is not None and isinstance(response, dict):
            result = apply_fm7_judgment(candidate, response)
            if result is not None:
                llm_findings.append(result)

    for idx, candidate in enumerate(fm10_candidates):
        response = judgment_map.get(f"judge:fm10:{idx}")
        if response is not None and isinstance(response, dict):
            result = apply_fm10_judgment(candidate, response)
            if result is not None:
                llm_findings.append(result)

    all_findings = deterministic_findings + llm_findings
    cleanup_state(state_dir, explicit=explicit)
    _render_and_exit(all_findings, format_=format_, output=output, max_severity=max_severity)


def _render_and_exit(
    findings: list[Finding],
    *,
    format_: str,
    output: str | None,
    max_severity: str | None,
) -> None:
    """Render findings and apply exit gate logic."""
    if format_ == "json":
        # Stamp the C11 discriminator so C6 confidence-gate can classify even an
        # empty-findings report (#1195 4c).
        out_text = render_json(findings, report_type="c11_production_failure_audit")
    elif format_ == "sarif":
        out_text = render_sarif(findings, tool_version="0.5.0")
    else:
        out_text = render_markdown(findings, audit_path="")

    if output:
        Path(output).write_text(out_text, encoding="utf-8")
    else:
        emit_report(out_text)

    if max_severity is not None:
        from typing import cast as _cast  # noqa: PLC0415

        gated = filter_by_severity(findings, _cast("Severity", max_severity))
        if gated:
            sys.exit(1)
