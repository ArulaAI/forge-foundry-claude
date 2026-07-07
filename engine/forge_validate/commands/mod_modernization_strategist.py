"""Forge Foundry — ``forge-validate mod-modernization-strategist`` subcommand (M3).

Drives the M3 decomposition pipeline over the M1 ``schema.json`` + M2
``stored-procs.json`` digests in ``--context-dir`` and writes the SPEC outputs
under ``--output-dir``:

* ``decomposition.json`` — :class:`DecompositionPlan` (Stages A-D, deterministic).
* ``modernization-plan.md`` — per-context narrative + ownership tables and the
  sequencing roadmap.
* ``diagrams/{01_decomposition,02_sequencing,03_ownership}.mmd`` — the three
  Mermaid strategy views.

Stages A-D are pure Python; the **per-context narrative** (Stage E) is produced
by the SKILL.md body — the Python CLI never calls an LLM. ``--extract-only``
emits the narrative prompts (with an anti-hallucination guard applied to the
returned judgments), and ``--continue --judgments`` applies them and renders the
outputs.

Mirrors M1's :mod:`forge_validate.commands.mod_db_schema_analyze` PLB shape:
thin Click handler, lazy heavy imports (TOOL-2). The ``mod-`` prefix follows the
M-tier invariant.

Exit codes (per SPEC.md §"Exit codes"):

* ``0`` — success
* ``2`` — a required digest (``schema.json`` / ``stored-procs.json``) is
  missing from ``--context-dir``, or the command was run without
  ``--extract-only`` / ``--continue``
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from forge_validate._cli_common import (
    continue_option,
    extract_only_option,
    judgments_option,
    state_dir_option,
    validate_skill_first_flags,
)

if TYPE_CHECKING:
    from primitives.modernization_strategist.shared.models import DecompositionPlan

# The M1/M2 digests live under the `.foundry/context/db/` convention; the
# strategist reads from there by default and writes its decomposition alongside.
_DEFAULT_CONTEXT_DIR = Path(".foundry/context/db")

_SCHEMA_FILENAME = "schema.json"
_PROCS_FILENAME = "stored-procs.json"
_DECOMPOSITION_FILENAME = "decomposition.json"
_PLAN_FILENAME = "modernization-plan.md"
_DIAGRAMS_DIR = "diagrams"


@click.command("mod-modernization-strategist")
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
    help="Where to write decomposition.json + modernization-plan.md + diagrams/ "
    "(default: --context-dir).",
)
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(
    context_dir: str,
    output_dir: str | None,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """Decompose a legacy SQL database into bounded contexts (/mod-modernization-strategist).

    Reads the M1 ``schema.json`` + M2 ``stored-procs.json`` digests, runs the
    deterministic Stages A-D, and renders the Mermaid strategy diagrams.
    Outputs land under ``--output-dir`` (default ``--context-dir``).

    The Python CLI never calls an LLM — run --extract-only to emit the
    per-context narrative prompts, judge them in-session, then
    --continue --judgments to apply the judgments and render the outputs.
    """
    # --- Lazy imports to keep cold-start cheap (TOOL-2). -----------------
    from primitives._shared.pipeline_state import (  # noqa: PLC0415
        PipelineState,
        cleanup_state,
        load_intermediate,
        save_intermediate,
        save_state,
    )
    from primitives._shared.protocol import (  # noqa: PLC0415
        PromptEnvelope,
        read_judgments,
        write_prompts,
    )
    from primitives.db_schema_analyze.shared.models import SchemaDigest  # noqa: PLC0415
    from primitives.modernization_strategist.shared import (  # noqa: PLC0415
        build_decomposition,
    )
    from primitives.modernization_strategist.shared.models import (  # noqa: PLC0415
        DecompositionPlan,
    )
    from primitives.modernization_strategist.shared.narrative_synthesizer import (  # noqa: PLC0415
        apply_narrative_judgments,
        build_prompt_items_for_contexts,
    )
    from primitives.stored_proc_analyze.shared.models import (  # noqa: PLC0415
        StoredProcDigest,
    )

    # --- Validate skill-first flags (mutual exclusivity, --continue requires --judgments) ---
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )

    ctx_dir = Path(context_dir)
    resolved_output = Path(output_dir) if output_dir is not None else ctx_dir
    resolved_state_dir = Path(state_dir) if state_dir is not None else resolved_output / ".state"

    # =========================================================================
    # Branch: --continue --judgments (resume after in-session judgment)
    # =========================================================================
    if continue_:
        # Load the plan intermediate saved during --extract-only
        plan_raw = load_intermediate("plan", resolved_state_dir)
        plan = DecompositionPlan.model_validate(plan_raw)

        # Read the judgments envelope and build the id→response lookup
        assert judgments is not None  # guaranteed by validate_skill_first_flags
        judgment_envelope = read_judgments(Path(judgments))
        narratives_by_id: dict[str, str] = {
            item.id: (item.response if isinstance(item.response, str) else "")
            for item in judgment_envelope.items
        }

        # Apply anti-hallucination guard and map to {context_name: narrative}
        narratives = apply_narrative_judgments(plan, narratives_by_id)

        _render_outputs(plan, narratives, resolved_output)

        # --- Clean up state -------------------------------------------------
        cleanup_state(resolved_state_dir)
        return

    # =========================================================================
    # Common preamble: load M1/M2 digests and run Stages A-D
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

    # --- Stages A-D: deterministic decomposition -------------------------
    plan = build_decomposition(schema, procs)

    # =========================================================================
    # Branch: --extract-only (Stages A-D, save intermediates, emit prompts)
    # =========================================================================
    if extract_only:
        # Save plan intermediate for --continue
        save_intermediate("plan", plan.model_dump(mode="json"), resolved_state_dir)

        # Build prompt items (one per bounded context)
        prompt_items = build_prompt_items_for_contexts(plan)

        # A degenerate zero-context decomposition yields no narrative prompts —
        # there is no Stage E model step to defer. Apply the (empty) narrative
        # judgment map (every context, of which there are none, resolves to an
        # empty narrative), render the deterministic artifacts, and exit 0 — no
        # empty PromptEnvelope, no round. Mirrors M1's _render_digest fast path.
        if not prompt_items:
            narratives = apply_narrative_judgments(plan, {})
            _render_outputs(plan, narratives, resolved_output)
            cleanup_state(resolved_state_dir)
            click.echo(
                "[mod-modernization-strategist] decomposition produced no bounded "
                "contexts; rendered deterministic outputs. No judgments needed.",
                err=True,
            )
            sys.exit(0)

        envelope = PromptEnvelope(
            primitive="mod-modernization-strategist",
            round=1,
            items=prompt_items,
        )
        prompts_path = resolved_state_dir / "round-1-prompts.json"
        write_prompts(envelope, prompts_path)
        click.echo(f"WROTE {prompts_path}")

        # Save pipeline state
        save_state(
            PipelineState(
                primitive="mod-modernization-strategist",
                round=1,
                completed_stages=["A", "B", "C", "D"],
                next_stage="E_continue",
                status="awaiting_judgments",
            ),
            resolved_state_dir,
        )

        sys.exit(7)

    # The Stage E narrative-synthesis LLM phase lived on the deleted harness
    # path; the pipeline now only runs in --extract-only or --continue mode.
    click.echo(
        "Error: run with --extract-only (then --continue --judgments) — "
        "the deterministic stages defer per-context narrative synthesis to "
        "the SKILL.md body.",
        err=True,
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_outputs(
    plan: DecompositionPlan,
    narratives: dict[str, str],
    output_dir: Path,
) -> None:
    """Render the deterministic M3 artifacts (decomposition + plan + diagrams).

    Shared by the ``--continue`` branch (after the per-context narratives are
    guarded) and the ``--extract-only`` fast path that fires when the
    decomposition produced zero bounded contexts (empty prompt set → empty
    narrative map). Mirrors M1's ``_render_digest`` shared-render discipline.
    """
    from primitives.modernization_strategist.shared import (  # noqa: PLC0415
        render_plan_md,
        render_strategy_diagrams,
        write_decomposition,
    )

    # --- Diagrams (deterministic) ----------------------------------------
    diagrams = render_strategy_diagrams(plan)

    # --- Write outputs ---------------------------------------------------
    write_decomposition(plan, output_dir)
    click.echo(f"WROTE {output_dir / _DECOMPOSITION_FILENAME}")

    plan_path = output_dir / _PLAN_FILENAME
    plan_path.write_text(render_plan_md(plan, narratives), encoding="utf-8")
    click.echo(f"WROTE {plan_path}")

    diagrams_dir = output_dir / _DIAGRAMS_DIR
    diagrams_dir.mkdir(parents=True, exist_ok=True)
    for filename, body in diagrams.items():
        (diagrams_dir / filename).write_text(body + "\n", encoding="utf-8")
        click.echo(f"WROTE {diagrams_dir / filename}")
