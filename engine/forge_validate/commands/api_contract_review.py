"""Forge Foundry — `forge-validate api-contract-review` subcommand (S40).

Was inlined in `forge_validate/cli.py` before #101 / TOOL-2.

Two dispatch modes:
  - Skill-first ``--extract-only``: detects format, loads contract(s), builds
    ``contract:<file>`` prompt items (one per file, batched mode), writes
    ``round-1-prompts.json``, exits 7.
  - Skill-first ``--continue``: loads intermediates + judgments, applies findings,
    runs confidence filter, renders report, exits 0/4/5.

Degraded-output mode (``--degraded-on-missing``):
  - When the contract path is absent AND ``--degraded-on-missing`` is set, writes a
    well-formed empty ``api-contract.json`` at ``--degraded-output`` and exits 0.
  - Malformed-but-present contracts still exit 3 (not degraded).
  - The degraded artifact is SP2-adapter-parseable by ``adapters/api.py::to_endpoints``
    and ``to_digest`` (empty operations list → empty interfaces / endpoints).

The AI-quality sub-call (C10+C11, §3.4) is skipped in skill-first mode — a
subprocess call from within the skill body would violate the protocol
boundary. A stderr note is printed instead.
"""

from __future__ import annotations

import json
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
    from primitives.api_contract_review.shared.models import (
        APILens,
        Confidence,
        ContractFormat,
    )

# SEC-8 (#172): the baseline ref is interpolated into `"{ref}:{path}"` and handed
# to git. The shared ``validate_git_ref`` (forge_validate/_cli_common.py) is the
# CLI-side mirror — first-char class excludes `-` so a ref can't be parsed as a
# flag; covers HEAD~1, HEAD^, @{upstream}, release/1.2, etc.
from forge_validate._cli_common import validate_git_ref


@click.command("api-contract-review")
@click.argument("contract_path", type=click.Path(exists=False))
@click.option(
    "--baseline",
    "baseline_ref",
    default=None,
    callback=validate_git_ref,
    help="Git ref of baseline contract for backward-compat diff (e.g., HEAD~1, main).",
)
@click.option(
    "--rules-yaml",
    "rules_yaml_path",
    type=click.Path(exists=True),
    default=None,
    help="Path to .foundry/api-rules.yaml (default: auto-detect).",
)
@click.option(
    "--format-hint",
    type=click.Choice(
        ["openapi-3.0", "openapi-3.1", "asyncapi-2", "asyncapi-3", "graphql", "auto"]
    ),
    default="auto",
    help="Contract format hint (default: auto-detect).",
)
@click.option(
    "--lens",
    "lenses",
    type=click.Choice(
        [
            "versioning",
            "error_shapes",
            "auth_spec",
            "idempotency",
            "backward_compatibility",
            "pagination",
            "rate_limiting",
        ]
    ),
    multiple=True,
    default=[],
    help="Lenses to apply (repeatable; default: all 7).",
)
@format_option(default="markdown")
@list_formats_option("primitives.api_contract_review.shared.report_renderer")
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
    "--degraded-on-missing",
    is_flag=True,
    default=False,
    help=(
        "Graceful-skip mode: when the contract path is absent, emit an empty-but-valid "
        "api-contract.json and exit 0. Malformed-but-present contracts still exit 3. "
        "Use in the app-mod chain when the API surface may be thin or absent (#1404)."
    ),
)
@click.option(
    "--degraded-output",
    type=click.Path(),
    default=None,
    help=(
        "Path to write the degraded api-contract.json when --degraded-on-missing fires. "
        "Defaults to api/api-contract.json relative to the contract path's parent directory."
    ),
)
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(  # noqa: PLR0913
    contract_path: str,
    baseline_ref: str | None,
    rules_yaml_path: str | None,
    format_hint: str,
    lenses: tuple[str, ...],
    format_: str,
    output: str | None,
    confidence_threshold: str,
    max_severity: str | None,
    skip_ai_quality: bool,  # accepted for CLI compat; sub-call skipped in skill-first mode
    degraded_on_missing: bool,
    degraded_output: str | None,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """API contract review across 7 lenses (S40 /api-contract-review).

    CONTRACT_PATH must be a contract DOCUMENT — an OpenAPI 3.0/3.1 YAML/JSON
    file, an AsyncAPI 2.x/3.x YAML file, or a GraphQL SDL (.graphql/.gql).
    It is NOT a directory of route source files.  If your framework generates
    the spec at runtime, export it first (e.g. `app.openapi()` for FastAPI).

    Reviews OpenAPI 3.0/3.1, AsyncAPI 2.x/3.x, and GraphQL SDL contracts for
    versioning, error_shapes (RFC 7807), auth_spec, idempotency, backward_compatibility,
    pagination, and rate_limiting issues.

    Skill-first mode (--extract-only / --continue --judgments) runs Stage B
    in-session and defers contract review findings to the SKILL.md body for
    in-session judgment.

    Exit codes:
    - 0: success (or degraded-on-missing clean skip)
    - 2: contract path not found (checked before --extract-only/--continue precondition;
         suppressed when --degraded-on-missing is set)
    - 3: format unresolvable
    - 4: findings exceed --max-severity threshold
    - 5: C10/C11 sub-call returned critical findings (after dedup)
    - 7: awaiting judgments (skill-first mode — prompts file written)
    """
    from typing import cast as _cast  # noqa: PLC0415

    # --- Validate contract path existence BEFORE the precondition check (#890) ---
    # Exit 2 when the contract path does not exist, UNLESS --degraded-on-missing is set
    # (in which case we emit a well-formed empty artifact and exit 0 — #1404).
    # The --list-formats flag is an eager option (forge_validate._cli_common.list_formats_option,
    # #885) that prints + exits 0 *before* this body runs and before Click requires the
    # positional, so it still works without a real path.  This existence check
    # runs before validate_skill_first_flags so a typo'd path yields "contract
    # path not found" rather than the skill-first precondition message.
    resolved_contract = Path(contract_path)
    if not resolved_contract.exists():
        if degraded_on_missing:
            _emit_degraded_artifact(
                contract_path=resolved_contract,
                degraded_output=Path(degraded_output) if degraded_output else None,
            )
            return  # _emit_degraded_artifact calls sys.exit(0)
        click.echo(f"Error: contract path not found: {resolved_contract}", err=True)
        sys.exit(2)

    # --- Validate skill-first flags (--continue requires --judgments) ---
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )

    resolved_rules_yaml = Path(rules_yaml_path) if rules_yaml_path else None
    resolved_output_file = Path(output) if output else None
    selected_lenses: list[APILens] | None = _cast("list[APILens]", list(lenses)) if lenses else None
    resolved_format_hint: ContractFormat | None = (
        None if format_hint == "auto" else _cast("ContractFormat", format_hint)
    )

    # --- Determine state directory (default: beside contract_path / .state) ---
    resolved_state_dir = (
        Path(state_dir) if state_dir is not None else resolved_contract.parent / ".state"
    )

    # =========================================================================
    # Branch: --continue (resume after in-session judgment)
    # =========================================================================
    if continue_:
        _run_continue(
            resolved_contract=resolved_contract,
            resolved_state_dir=resolved_state_dir,
            judgments_path=Path(judgments),  # type: ignore[arg-type]  # guaranteed by validate_skill_first_flags
            confidence_threshold=_cast("Confidence", confidence_threshold),
            max_severity=_cast("Literal['high', 'critical'] | None", max_severity),
            format_=format_,
            resolved_output_file=resolved_output_file,
            explicit=(state_dir is not None),
        )
        return  # _run_continue calls sys.exit

    # =========================================================================
    # Branch: --extract-only (emit prompts, exit 7)
    # =========================================================================
    if extract_only:
        _run_extract_only(
            resolved_contract=resolved_contract,
            resolved_rules_yaml=resolved_rules_yaml,
            resolved_format_hint=resolved_format_hint,
            selected_lenses=selected_lenses,
            baseline_ref=baseline_ref,
            resolved_state_dir=resolved_state_dir,
        )
        return  # _run_extract_only calls sys.exit

    # No skill-first flag supplied: the CLI never calls an LLM, so the only
    # execution paths are --extract-only and --continue --judgments.
    raise click.UsageError(
        "api-contract-review requires --extract-only (emit prompts) or "
        "--continue --judgments <path> (apply judgments). The CLI does not call an LLM."
    )


# ---------------------------------------------------------------------------
# Degraded-output helper (#1404)
# ---------------------------------------------------------------------------

#: The well-formed empty artifact emitted by --degraded-on-missing.
#: Shape matches what ``primitives._shared.modernization.adapters.api.to_endpoints``
#: and ``to_digest`` expect — all optional fields, empty collections.
_DEGRADED_ARTIFACT: dict[str, object] = {
    "status": "degraded",
    "reason": "no_contract_document",
    "contract_path": "",
    "format": None,
    "operations": [],
    "schemas": [],
    "flavor": "rest",
    "debt": [],
    "security_schemes": [],
}


def _emit_degraded_artifact(
    *,
    contract_path: Path,
    degraded_output: Path | None,
) -> None:
    """Emit an empty-but-valid api-contract.json and exit 0.

    Called when ``--degraded-on-missing`` is set and the contract path is absent.
    The artifact shape is SP2-adapter-parseable: ``to_endpoints`` and ``to_digest``
    both safe-default every missing key, so an empty ``operations`` list yields an
    empty endpoint list / empty digest without raising.

    The output path defaults to ``api/api-contract.json`` adjacent to the
    contract_path's parent directory.  When ``degraded_output`` is supplied
    explicitly, it takes precedence.
    """
    artifact = dict(_DEGRADED_ARTIFACT)
    artifact["contract_path"] = str(contract_path)

    if degraded_output is not None:
        out_path = degraded_output
    else:
        # Default: place beside where the contract would have lived —
        # i.e. <contract_path.parent>/api/api-contract.json.
        out_path = contract_path.parent / "api" / "api-contract.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")

    click.echo(
        f"[api-contract-review] degraded: no contract document at {contract_path}; "
        f"emitted empty api-contract.json at {out_path} (status=degraded, "
        "reason=no_contract_document). API surface treated as absent.",
        err=True,
    )
    sys.exit(0)


# ---------------------------------------------------------------------------
# Skill-first helpers
# ---------------------------------------------------------------------------


def _run_extract_only(
    *,
    resolved_contract: Path,
    resolved_rules_yaml: Path | None,
    resolved_format_hint: ContractFormat | None,
    selected_lenses: list[APILens] | None,
    baseline_ref: str | None,
    resolved_state_dir: Path,
) -> None:
    """--extract-only: detect format, load contract, emit prompt items, exit 7."""
    from primitives._shared.pipeline_state import (  # noqa: PLC0415
        PipelineState,
        save_intermediate,
        save_state,
    )
    from primitives._shared.protocol import PromptEnvelope, write_prompts  # noqa: PLC0415
    from primitives.api_contract_review.shared.format_detector import (  # noqa: PLC0415
        ContractFormatUnresolvableError,
        parse_contract,
    )
    from primitives.api_contract_review.shared.lens_reviewer import (  # noqa: PLC0415
        build_prompt_item_for_contract,
    )
    from primitives.api_contract_review.shared.rules_loader import (  # noqa: PLC0415
        load_api_rules,
    )

    _ALL_LENSES: list[APILens] = [
        "versioning",
        "error_shapes",
        "auth_spec",
        "idempotency",
        "backward_compatibility",
        "pagination",
        "rate_limiting",
    ]

    if not resolved_contract.exists():
        click.echo(f"Error: contract path not found: {resolved_contract}", err=True)
        sys.exit(2)

    # --- Stage A.1: Load rules ---
    rules_config = load_api_rules(resolved_contract, resolved_rules_yaml)

    # --- Stage A.2: Detect format and parse contract ---
    if resolved_format_hint is not None:
        contract_format = resolved_format_hint
        try:
            endpoints, _ = parse_contract(resolved_contract)
        except ContractFormatUnresolvableError as exc:
            click.echo(
                f"Error: --format-hint '{resolved_format_hint}' supplied but contract"
                f" could not be parsed: {exc}",
                err=True,
            )
            sys.exit(3)
        except OSError as exc:
            click.echo(f"Error: cannot read contract: {exc}", err=True)
            sys.exit(2)
    else:
        try:
            endpoints, contract_format = parse_contract(resolved_contract)
        except ContractFormatUnresolvableError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(3)
        except OSError as exc:
            click.echo(f"Error: cannot read contract: {exc}", err=True)
            sys.exit(2)

    # --- Select and filter lenses ---
    active_lenses_all: list[APILens] = selected_lenses if selected_lenses else _ALL_LENSES
    active_lenses: list[APILens] = [
        ln for ln in active_lenses_all if not rules_config.is_lens_disabled(ln)
    ]

    # --- Read contract content ---
    try:
        contract_content = resolved_contract.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        click.echo(f"Error: cannot read contract: {exc}", err=True)
        sys.exit(2)

    # --- Build baseline context (for backward_compatibility lens) ---
    context: str | None = None
    if baseline_ref is not None and "backward_compatibility" in active_lenses:
        try:
            from primitives.api_contract_review.shared.baseline_differ import (  # noqa: PLC0415
                diff_baseline,
            )

            baseline_diff = diff_baseline(resolved_contract, baseline_ref)
            if baseline_diff.has_breaking_changes:
                context = (
                    f"Baseline diff (ref={baseline_ref!r}):\n"
                    f"  Removed: {baseline_diff.removed}\n"
                    f"  Type narrowed: {baseline_diff.type_narrowed}\n"
                    f"  New required fields: {baseline_diff.new_required_fields}"
                )
        except Exception as exc:
            click.echo(
                f"[api-contract-review] WARNING: baseline diff failed: {exc}",
                err=True,
            )

    # Build reviewed_file_set for this file (anti-hallucination 1 at continue time)
    reviewed_file_set: set[str] = {str(resolved_contract), resolved_contract.name}

    # --- Build prompt item ---
    prompt_item = build_prompt_item_for_contract(
        file=resolved_contract,
        contract_content=contract_content,
        lenses=active_lenses,
        context=context,
        endpoints=endpoints,
        reviewed_file_set=reviewed_file_set,
    )

    envelope = PromptEnvelope(
        primitive="api-contract-review",
        round=1,
        items=[prompt_item],
    )
    prompts_path = resolved_state_dir / "round-1-prompts.json"
    write_prompts(envelope, prompts_path)
    click.echo(f"WROTE {prompts_path}")

    # --- Save intermediates for --continue ---
    save_intermediate(
        "contract_meta",
        {
            "contract_path": str(resolved_contract),
            "contract_format": contract_format,
            "active_lenses": active_lenses,
            "reviewed_file_set": sorted(reviewed_file_set),
            "endpoints": [ep.model_dump() for ep in endpoints],
        },
        resolved_state_dir,
    )

    save_state(
        PipelineState(
            primitive="api-contract-review",
            round=1,
            completed_stages=["A", "B_pending"],
            next_stage="B_continue",
            status="awaiting_judgments",
        ),
        resolved_state_dir,
    )

    sys.exit(7)


def _run_continue(
    *,
    resolved_contract: Path,
    resolved_state_dir: Path,
    judgments_path: Path,
    confidence_threshold: Confidence,
    max_severity: Literal["high", "critical"] | None,
    format_: str,
    resolved_output_file: Path | None,
    explicit: bool = False,
) -> None:
    """--continue: apply judgments, run confidence filter, render, exit 0/4/5."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from primitives._shared.pipeline_state import (  # noqa: PLC0415
        cleanup_state,
        load_intermediate,
    )
    from primitives._shared.protocol import read_judgments  # noqa: PLC0415
    from primitives.api_contract_review.shared.confidence_filter import (  # noqa: PLC0415
        filter_by_confidence,
    )
    from primitives.api_contract_review.shared.lens_reviewer import (  # noqa: PLC0415
        _build_endpoint_set,
        apply_lens_judgments_for_contract,
    )
    from primitives.api_contract_review.shared.models import (  # noqa: PLC0415
        APIContractReviewReport,
        Endpoint,
    )
    from primitives.api_contract_review.shared.report_renderer import (  # noqa: PLC0415
        render_report,
    )

    # Load intermediates
    meta = load_intermediate("contract_meta", resolved_state_dir)
    contract_path = Path(meta["contract_path"])
    contract_format = meta["contract_format"]
    active_lenses = meta["active_lenses"]
    reviewed_file_set: set[str] = set(meta["reviewed_file_set"])
    raw_endpoints = meta.get("endpoints", [])
    endpoints = [Endpoint.model_validate(ep) for ep in raw_endpoints]
    endpoint_set = _build_endpoint_set(endpoints)

    # Read and apply judgments
    judgment_envelope = read_judgments(judgments_path)
    judgment_map = {item.id: item.response for item in judgment_envelope.items}

    item_id = f"contract:{contract_path.name}"
    raw_response = judgment_map.get(item_id, [])

    # Ensure raw_response is a list (model might return dict on error)
    if not isinstance(raw_response, list):
        click.echo(
            f"[api-contract-review] WARNING: judgment for {item_id!r} is not a list; "
            "treating as empty",
            err=True,
        )
        raw_response = []

    all_raw_findings = apply_lens_judgments_for_contract(
        response_array=raw_response,
        file=contract_path,
        reviewed_file_set=reviewed_file_set,
        endpoint_set=endpoint_set,
    )

    # --- Stage C: Confidence filter ---
    findings = filter_by_confidence(all_raw_findings, confidence_threshold)
    click.echo(
        f"[api-contract-review] Stage C: {len(findings)} finding(s) after "
        f"confidence_threshold={confidence_threshold!r}.",
        err=True,
    )

    # AI quality sub-call is skipped in skill-first mode
    click.echo(
        "[api-contract-review] NOTE: AI quality sub-call (C10+C11 §3.4) is skipped in "
        "skill-first mode.",
        err=True,
    )

    # --- Build report ---
    report = APIContractReviewReport(
        contract_path=str(contract_path),
        format=contract_format,
        lenses=active_lenses,
        endpoints=endpoints,
        findings=findings,
        baseline_diff=None,
        ai_quality=None,
        generated_at=datetime.now(tz=UTC),
    )

    # --- Render ---
    rendered = render_report(report, format_)

    # --- Write or print output ---
    if resolved_output_file is not None:
        resolved_output_file.parent.mkdir(parents=True, exist_ok=True)
        resolved_output_file.write_text(rendered, encoding="utf-8")
        click.echo(f"[api-contract-review] Wrote report to {resolved_output_file}", err=True)
    else:
        emit_report(rendered)

    # --- Clean up state ---
    cleanup_state(resolved_state_dir, explicit=explicit)

    # --- Exit code logic ---
    # Exit 4: any finding at or above --max-severity
    if max_severity is not None:
        threshold = _SEVERITY_ORDER.get(max_severity, 2)
        if any(_SEVERITY_ORDER.get(f.severity, 0) >= threshold for f in findings):
            sys.exit(4)

    sys.exit(0)
