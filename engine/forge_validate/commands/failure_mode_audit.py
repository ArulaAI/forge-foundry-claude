"""Forge Foundry — `forge-validate failure-mode-audit` subcommand.

Was inlined in `forge_validate/cli.py` before #101 / TOOL-2.

Skill-first mode (--extract-only / --continue) adds the extract→judge→continue
protocol (LLM judgments are opt-in via config); the protocol is:

- --extract-only: runs FM1-FM5 deterministic regex scan, collects FM3/FM4
  candidates (when config enables LLM mode), writes round-1-prompts.json,
  exits 7. If LLM mode is not enabled or no candidates exist, exits 0.
- --continue: loads intermediates, applies FM3/FM4 judgments, merges with
  deterministic findings, renders report, exits per --max-severity logic.
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
from primitives.failure_mode_audit import audit_path as run_audit

if TYPE_CHECKING:
    from primitives.failure_mode_audit import Severity
    from primitives.failure_mode_audit.shared.findings import Finding
from primitives.failure_mode_audit.shared.output.json_writer import render_json
from primitives.failure_mode_audit.shared.output.markdown import render_markdown
from primitives.failure_mode_audit.shared.output.sarif import render_sarif


@click.command("failure-mode-audit")
@click.argument("path", type=click.Path(exists=True))
@format_option(default="markdown")
@list_formats_option("primitives.failure_mode_audit.shared.report_renderer")
@click.option(
    "--output",
    type=click.Path(),
    default=None,
    help="Write output to file (required for --format sarif).",
)
@click.option(
    "--max-severity",
    type=click.Choice(["low", "medium", "high", "critical"]),
    default=None,
    help="Exit nonzero if any finding is at or above this severity.",
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
    """Audit a path for AI failure modes (FM1-FM5)."""
    from primitives.failure_mode_audit.shared.report_renderer import (  # noqa: PLC0415
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
        click.echo("Error: --output FILE is required when --format sarif", err=True)
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

    # Resolve state directory (relative to target directory)
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
            format_=format_,
            output=output,
            max_severity=max_severity,
            judgments_path=Path(judgments),  # type: ignore[arg-type]
            state_dir=resolved_state_dir,
            explicit=(state_dir is not None),
        )
        return

    # =========================================================================
    # Branch: --extract-only (run deterministic stages, emit prompts)
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
    # Branch: normal (deterministic-only) mode
    # =========================================================================
    findings = run_audit(target, config=config)
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
    """Run FM1-FM5 deterministic scan; emit prompts for FM3/FM4 LLM candidates.

    If FM3/FM4 LLM candidates exist, writes round-1-prompts.json and exits 7.
    Otherwise there is nothing for the caller to judge, so the deterministic
    FM1/FM2/FM5 findings are rendered directly (exit per the --max-severity
    gate) instead of a silent exit-0 that hides them (#1192).
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
    from primitives.failure_mode_audit.shared.fm2 import audit_python_fm2  # noqa: PLC0415
    from primitives.failure_mode_audit.shared.llm_judge import (  # noqa: PLC0415
        build_prompt_items_for_fm3_candidates,
        build_prompt_items_for_fm4_functions,
        serialize_finding,
    )
    from primitives.failure_mode_audit.shared.regex_rules import (  # noqa: PLC0415
        audit_regex_mode,
    )
    from primitives.failure_mode_audit.shared.stacks.java import audit_java_fm1  # noqa: PLC0415
    from primitives.failure_mode_audit.shared.stacks.nodejs import (  # noqa: PLC0415
        audit_nodejs_fm1,
    )
    from primitives.failure_mode_audit.shared.stacks.python import (  # noqa: PLC0415
        audit_python_fm1,
    )

    # Run all deterministic stages (FM1 + FM2 + FM5 regex)
    # audit_path() normally calls all FMs; here we call individually to
    # separate candidates from confirmed findings.
    project_root = target if target.is_dir() else target.parent
    deterministic_findings = []

    if config.fm1_hallucinated_deps.enabled:
        deterministic_findings.extend(
            audit_python_fm1(target, project_root=project_root, config=config)
        )
        deterministic_findings.extend(
            audit_nodejs_fm1(target, project_root=project_root, config=config)
        )
        deterministic_findings.extend(
            audit_java_fm1(target, project_root=project_root, config=config)
        )

    if config.fm2_context_window.enabled:
        deterministic_findings.extend(
            audit_python_fm2(target, project_root=project_root, config=config)
        )

    if config.fm5_security_propagation.enabled:
        deterministic_findings.extend(audit_regex_mode(target, mode="fm5", config=config))

    # Collect FM3/FM4 regex candidates (they are candidates, not final findings)
    fm3_candidates = []
    fm4_candidates: list[tuple[str, int, int, str, object]] = []

    fm3_mode = config.fm3_training_bias.mode
    fm4_mode = config.fm4_overconfident.mode

    if config.fm3_training_bias.enabled and fm3_mode in ("llm", "both"):
        # Regex-detected FM3 findings become LLM candidates
        fm3_candidates = audit_regex_mode(target, mode="fm3", config=config)
    elif config.fm3_training_bias.enabled:
        # regex-only: treat as confirmed findings
        deterministic_findings.extend(audit_regex_mode(target, mode="fm3", config=config))

    if config.fm4_overconfident.enabled and fm4_mode in ("llm", "both"):
        # Regex-detected FM4 findings become LLM candidates (no function-body extraction yet)
        # We treat each FM4 regex finding as a "function candidate" with the snippet as body
        for f in audit_regex_mode(target, mode="fm4", config=config):
            fm4_candidates.append((f.file, f.line, f.line, f.snippet, f))
    elif config.fm4_overconfident.enabled:
        deterministic_findings.extend(audit_regex_mode(target, mode="fm4", config=config))

    # Apply budget: only include candidates that fit within the budget
    max_calls = config.llm.max_calls_per_audit
    budgeted_fm3 = fm3_candidates[:max_calls]
    remaining_budget = max(0, max_calls - len(budgeted_fm3))
    budgeted_fm4 = fm4_candidates[:remaining_budget]

    # Build prompt items
    prompt_items = []
    prompt_items.extend(build_prompt_items_for_fm3_candidates(budgeted_fm3, "unknown", "unknown"))
    from primitives.failure_mode_audit.shared.findings import Finding  # noqa: PLC0415

    prompt_items.extend(
        build_prompt_items_for_fm4_functions(
            [
                (f, sl, el, body, rf if isinstance(rf, Finding) else None)
                for f, sl, el, body, rf in budgeted_fm4
            ]
        )
    )

    # Save intermediates
    save_intermediate(
        "deterministic_findings",
        [serialize_finding(f) for f in deterministic_findings],
        state_dir,
    )
    save_intermediate(
        "fm3_candidates",
        [serialize_finding(f) for f in budgeted_fm3],
        state_dir,
    )
    save_intermediate(
        "fm4_candidates",
        [
            {
                "file": f,
                "start_line": sl,
                "end_line": el,
                "function_body": body,
                "regex_finding": serialize_finding(rf) if isinstance(rf, Finding) else None,
            }
            for f, sl, el, body, rf in budgeted_fm4
        ],
        state_dir,
    )

    if not prompt_items:
        # No LLM candidates — nothing to judge. Render the deterministic
        # findings directly (and apply the severity gate) rather than exit 0
        # silently, which would strand real findings behind a --continue that
        # has no prompts to continue (#1192).
        click.echo(
            "[failure-mode-audit] --extract-only: no FM3/FM4 LLM candidates; "
            "rendering deterministic FM1-FM5 findings.",
            err=True,
        )
        _render_and_exit(
            deterministic_findings, format_=format_, output=output, max_severity=max_severity
        )
        return

    # Write prompts envelope
    envelope = PromptEnvelope(
        primitive="failure-mode-audit",
        round=1,
        items=prompt_items,
    )
    prompts_path = state_dir / "round-1-prompts.json"
    write_prompts(envelope, prompts_path)
    click.echo(f"WROTE {prompts_path}")

    save_state(
        PipelineState(
            primitive="failure-mode-audit",
            round=1,
            completed_stages=["FM1", "FM2", "FM3_regex", "FM4_regex", "FM5"],
            next_stage="FM3_FM4_continue",
            status="awaiting_judgments",
        ),
        state_dir,
    )

    sys.exit(7)


def _do_continue(
    *,
    format_: str,
    output: str | None,
    max_severity: str | None,
    judgments_path: Path,
    state_dir: Path,
    explicit: bool = False,
) -> None:
    """Apply FM3/FM4 judgments, merge with deterministic findings, render report."""
    from primitives._shared.pipeline_state import (  # noqa: PLC0415
        cleanup_state,
        load_intermediate,
    )
    from primitives._shared.protocol import (  # noqa: PLC0415
        assert_matching_round,
        read_judgments,
    )
    from primitives.failure_mode_audit.shared.llm_judge import (  # noqa: PLC0415
        apply_fm3_judgment,
        apply_fm4_judgment,
        deserialize_finding,
    )

    # Load saved intermediates
    deterministic_raw = load_intermediate("deterministic_findings", state_dir)
    fm3_raw = load_intermediate("fm3_candidates", state_dir)
    fm4_raw = load_intermediate("fm4_candidates", state_dir)

    deterministic_findings = [deserialize_finding(d) for d in deterministic_raw]
    fm3_candidates = [deserialize_finding(d) for d in fm3_raw]
    fm4_candidates_data = fm4_raw  # list of dicts

    # Read and apply judgments. C10 is single-round: reject a wrong-round judgments
    # file loudly rather than silently dropping the pending round (#1304).
    judgment_envelope = read_judgments(judgments_path)
    assert_matching_round(judgment_envelope, 1, path=judgments_path)
    judgment_map = {item.id: item.response for item in judgment_envelope.items}

    llm_findings = []

    # Apply FM3 judgments
    for idx, candidate in enumerate(fm3_candidates):
        item_id = f"judge:fm3:{idx}"
        response = judgment_map.get(item_id)
        if response is not None and isinstance(response, dict):
            result = apply_fm3_judgment(candidate, response)
            if result is not None:
                llm_findings.append(result)

    # Apply FM4 judgments
    for idx, fm4_data in enumerate(fm4_candidates_data):
        item_id = f"judge:fm4:{idx}"
        response = judgment_map.get(item_id)
        if response is not None and isinstance(response, dict):
            regex_finding_data = fm4_data.get("regex_finding")
            regex_finding = (
                deserialize_finding(regex_finding_data) if regex_finding_data is not None else None
            )
            result = apply_fm4_judgment(
                file=fm4_data["file"],
                start_line=fm4_data["start_line"],
                end_line=fm4_data["end_line"],
                function_body=fm4_data["function_body"],
                regex_finding=regex_finding,
                response=response,
            )
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
        # Stamp the C10 discriminator so C6 confidence-gate can classify even an
        # empty-findings report (#1195 4c).
        out_text = render_json(findings, report_type="c10_failure_mode_audit")
    elif format_ == "sarif":
        out_text = render_sarif(findings, tool_version="0.2.0")
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
