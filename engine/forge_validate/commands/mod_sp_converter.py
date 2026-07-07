"""Forge Foundry — ``forge-validate mod-sp-converter`` subcommand (M4).

Converts a single legacy SQL stored procedure into a per-target migration plan +
starter scaffold, reading the M1 ``schema.json`` + M2 ``stored-procs.json``
digests from ``--context-dir`` and writing under ``--output-dir``:

* ``conversion-plan.json`` — the :class:`ConversionPlan` (Stage B + Stage E).
* ``plan.md`` — the deterministic migration narrative (Stage D).
* the per-target skeleton tree (Stage C) — e.g. ``pom.xml`` + ``*.java`` for
  ``spring-batch``, ``pyproject.toml`` + ``app/routers/<sp>.py`` for ``fastapi``;
  ``plan-only`` emits no scaffold.

Stages A / C / D / E are pure Python; the Stage B conversion plan is produced
by the SKILL.md body in-session via the skill-first extract→judge→continue
protocol (``--extract-only`` emits the prompt, ``--continue --judgments`` applies
the judged plan). Mirrors M3's
:mod:`forge_validate.commands.mod_modernization_strategist` PLB shape: thin Click
handler, lazy heavy imports (TOOL-2). The ``mod-`` prefix follows the M-tier
invariant.

Exit codes (per SPEC.md §"Exit codes"):

* ``0`` — success
* ``2`` — input error: ``--sp`` not found in the M2 digest; or a required
  digest (``schema.json`` / ``stored-procs.json``) is missing/unparseable
  from ``--context-dir``; or the ``--continue --judgments`` envelope does
  not contain the expected judgment item
* ``7`` — ``--extract-only``: prompts written, awaiting in-session judgments
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from forge_validate._cli_common import (
    continue_option,
    extract_only_option,
    judgments_option,
    state_dir_option,
    validate_skill_first_flags,
)

# The M1/M2 digests live under the `.foundry/context/db/` convention; the
# converter reads from there by default and writes the conversion alongside.
_DEFAULT_CONTEXT_DIR = Path(".foundry/context/db")

_SCHEMA_FILENAME = "schema.json"
_PROCS_FILENAME = "stored-procs.json"


def _target_choices() -> list[str]:
    """Resolve the registered target names (built-in + FLEX-3 entry-point)."""
    from primitives.sp_converter.shared import registered_targets  # noqa: PLC0415

    return registered_targets()


@click.command("mod-sp-converter")
@click.option(
    "--sp",
    "sp_name",
    required=True,
    help="Stored procedure to convert (must match a StoredProc.name in the M2 digest).",
)
@click.option(
    "--target",
    type=click.Choice(_target_choices()),
    default="plan-only",
    show_default=True,
    help="Conversion target (built-in: plan-only, spring-batch, fastapi; "
    "celery/spring-boot via FLEX-3 entry point).",
)
@click.option(
    "--context-dir",
    "context_dir",
    type=click.Path(),
    default=str(_DEFAULT_CONTEXT_DIR),
    show_default=True,
    help="Directory holding the M1 schema.json + M2 stored-procs.json digests.",
)
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(),
    default=None,
    help="Where to write conversion-plan.json + plan.md + the skeleton tree "
    "(default: --context-dir/conversions/<sp>).",
)
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(
    sp_name: str,
    target: str,
    context_dir: str,
    output_dir: str | None,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """Convert one stored procedure to a target framework (/mod-sp-converter).

    Reads the M1 ``schema.json`` + M2 ``stored-procs.json`` digests, resolves the
    ``--sp`` profile + its tables (Stage A), then renders the skeleton (Stage C) +
    the ``plan.md`` (Stage D) + the effort estimate (Stage E) and writes them
    under ``--output-dir``.

    The Python CLI never calls an LLM — Stage A runs deterministically and the
    per-target conversion plan (Stage B) is produced by the SKILL.md body.
    ``--extract-only`` emits the JSON plan prompt, and ``--continue --judgments``
    applies the judged plan and writes the conversion artifacts.
    """
    # --- Lazy imports to keep cold-start cheap (TOOL-2). -----------------
    from primitives.db_schema_analyze.shared.models import SchemaDigest  # noqa: PLC0415
    from primitives.sp_converter.shared import (  # noqa: PLC0415
        SpNotFoundError,
        write_conversion,
    )
    from primitives.sp_converter.shared.sp_resolver import resolve  # noqa: PLC0415
    from primitives.stored_proc_analyze.shared.models import (  # noqa: PLC0415
        StoredProcDigest,
    )

    # --- Validate skill-first flags (--continue requires --judgments) ---
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )

    ctx_dir = Path(context_dir)
    resolved_output = (
        Path(output_dir) if output_dir is not None else ctx_dir / "conversions" / sp_name
    )
    resolved_state_dir = Path(state_dir) if state_dir is not None else resolved_output / ".state"

    # =========================================================================
    # Branch: --continue --judgments (resume after in-session judgment)
    # =========================================================================
    if continue_:
        from primitives._shared.pipeline_state import (  # noqa: PLC0415
            cleanup_state,
            load_intermediate,
        )
        from primitives._shared.protocol import read_judgments  # noqa: PLC0415
        from primitives.db_schema_analyze.shared.models import Table  # noqa: PLC0415
        from primitives.sp_converter.shared.plan_synthesizer import (  # noqa: PLC0415
            apply_plan_judgment,
        )
        from primitives.sp_converter.shared.sp_resolver import ResolvedSp  # noqa: PLC0415
        from primitives.stored_proc_analyze.shared.models import StoredProc  # noqa: PLC0415

        # Load the resolved_sp intermediate saved during --extract-only
        # (ResolvedSp is a dataclass; reconstruct from serialized dict)
        resolved_sp_raw = load_intermediate("resolved_sp", resolved_state_dir)
        resolved = ResolvedSp(
            proc=StoredProc.model_validate(resolved_sp_raw["proc"]),
            tables=[Table.model_validate(t) for t in resolved_sp_raw["tables"]],
            unresolved_tables=resolved_sp_raw.get("unresolved_tables", []),
        )

        # Read the judgments envelope and look up the SP's response
        assert judgments is not None  # guaranteed by validate_skill_first_flags
        judgment_envelope = read_judgments(Path(judgments))
        item_id = f"narrative:{sp_name}"
        response_obj: dict[str, object] | None = None
        for item in judgment_envelope.items:
            if item.id == item_id:
                raw_response = item.response
                if isinstance(raw_response, dict):
                    response_obj = raw_response
                elif isinstance(raw_response, str):
                    try:
                        parsed = json.loads(raw_response)
                        if isinstance(parsed, dict):
                            response_obj = parsed
                    except json.JSONDecodeError:
                        pass
                break

        if response_obj is None:
            click.echo(
                f"Error: judgment item {item_id!r} not found or not a JSON object in {judgments}.",
                err=True,
            )
            sys.exit(2)

        # Apply anti-hallucination guards and compute effort (Stage E)
        plan = apply_plan_judgment(resolved, target=target, response=response_obj)

        # Stages C/D/E: scaffold + plan.md + effort (write_conversion covers all)
        from primitives.sp_converter.shared.converter import ConversionResult  # noqa: PLC0415
        from primitives.sp_converter.shared.notes_renderer import render_plan_md  # noqa: PLC0415
        from primitives.sp_converter.shared.skeletons import (  # noqa: PLC0415
            render as render_skeleton,
        )

        artifacts = render_skeleton(plan)
        plan_md = render_plan_md(plan)
        result = ConversionResult(plan=plan, artifacts=artifacts, plan_md=plan_md)

        for path in write_conversion(result, resolved_output):
            click.echo(f"WROTE {path}")

        cleanup_state(resolved_state_dir)
        return

    # =========================================================================
    # Common preamble: load M1/M2 digests
    # (feeds the --extract-only branch)
    # =========================================================================

    schema_path = ctx_dir / _SCHEMA_FILENAME
    procs_path = ctx_dir / _PROCS_FILENAME

    # --- Load the M1/M2 digests (exit 2 if absent/unreadable) ------------
    if not schema_path.exists():
        click.echo(
            f"Error: M1 schema digest not found: {schema_path} (run mod-db-schema-analyze first).",
            err=True,
        )
        sys.exit(2)
    if not procs_path.exists():
        click.echo(
            f"Error: M2 stored-proc digest not found: {procs_path} "
            "(run mod-stored-proc-analyze first).",
            err=True,
        )
        sys.exit(2)

    try:
        schema = SchemaDigest.model_validate_json(schema_path.read_text(encoding="utf-8"))
        procs = StoredProcDigest.model_validate_json(procs_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        click.echo(f"Error: could not parse a digest in {ctx_dir}: {exc}", err=True)
        sys.exit(2)

    # =========================================================================
    # Branch: --extract-only (Stage A, save intermediates, emit prompts)
    # =========================================================================
    if extract_only:
        from primitives._shared.pipeline_state import (  # noqa: PLC0415
            PipelineState,
            save_intermediate,
            save_state,
        )
        from primitives._shared.protocol import PromptEnvelope, write_prompts  # noqa: PLC0415
        from primitives.sp_converter.shared.plan_synthesizer import (  # noqa: PLC0415
            build_prompt_item_for_sp,
        )

        # Stage A: resolve the SP against M1/M2 digests
        try:
            resolved = resolve(sp_name, schema=schema, procs=procs)
        except SpNotFoundError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(2)

        # Save intermediate for --continue (ResolvedSp is a dataclass; serialize its fields)
        # Use by_alias=True so StoredProc.schema_ serializes as "schema" and roundtrips cleanly.
        resolved_sp_data = {
            "proc": resolved.proc.model_dump(mode="json", by_alias=True),
            "tables": [t.model_dump(mode="json", by_alias=True) for t in resolved.tables],
            "unresolved_tables": resolved.unresolved_tables,
        }
        save_intermediate("resolved_sp", resolved_sp_data, resolved_state_dir)

        # Build prompt item for the SP (JSON response format)
        prompt_item = build_prompt_item_for_sp(resolved, target=target)

        envelope = PromptEnvelope(
            primitive="mod-sp-converter",
            round=1,
            items=[prompt_item],
        )
        prompts_path = resolved_state_dir / "round-1-prompts.json"
        write_prompts(envelope, prompts_path)
        click.echo(f"WROTE {prompts_path}")

        # Save pipeline state
        save_state(
            PipelineState(
                primitive="mod-sp-converter",
                round=1,
                completed_stages=["A"],
                next_stage="B_continue",
                status="awaiting_judgments",
            ),
            resolved_state_dir,
        )

        sys.exit(7)

    # =========================================================================
    # No skill-first flag: the LLM Stage B has no in-CLI path anymore.
    # =========================================================================
    # The conversion plan is produced in-session via the skill-first protocol;
    # a bare invocation has nothing to run. Direct the caller to --extract-only.
    raise click.UsageError(
        "mod-sp-converter requires the skill-first protocol: run with "
        "--extract-only to emit the conversion prompt, then "
        "--continue --judgments <path> to apply the judged plan."
    )
