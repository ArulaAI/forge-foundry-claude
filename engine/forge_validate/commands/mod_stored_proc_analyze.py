"""Forge Foundry — ``forge-validate mod-stored-proc-analyze`` subcommand (M2-12).

Drives the M2 pipeline (Stage A parse → F risk score) for a directory
or file of legacy stored-procedure DDL and writes the two SPEC.md
§"Output" artifacts under ``--output-dir``:

* ``stored-procs.json`` — :class:`StoredProcDigest` (Pydantic v2,
  ``extra="forbid"``, schema-versioned at ``1.0``).
* ``stored-procs-summary.md`` — human-readable summary sorted by
  ``risk_score.total`` descending.

Mirrors R1's :mod:`forge_validate.commands.rules_gen` shape (PR #362):
thin Click handler over the per-stage primitive functions, lazy
imports of heavy modules (sqlglot) so cold-start ``forge-validate
--help`` stays cheap (TOOL-2 / #101).

The ``mod-`` slash-command prefix follows the M-tier invariant in
``AGENTS.md`` so the modernization namespace cannot collide with the
compliance-shaped S60 ``data-flow-map`` (charter §3.2.M).

Exit codes (per SPEC.md §"Exit codes"):

* ``0`` — success
* ``1`` — generic error
* ``2`` — ``--ddl-path`` not found or contains no SP source
* ``3`` — sqlglot rejected the requested dialect
* ``5`` — ``--include-pattern`` / ``--exclude-pattern`` reduced the
  candidate set to zero procedures
* ``7`` — ``--extract-only`` succeeded; prompts written, awaiting judgments
"""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import click

from forge_validate._cli_common import (
    continue_option,
    extract_only_option,
    judgments_option,
    state_dir_option,
    validate_skill_first_flags,
)

if TYPE_CHECKING:
    from primitives.stored_proc_analyze.shared.models import IOShape, SideEffect
    from primitives.stored_proc_analyze.shared.sp_parser import ParsedSP
    from primitives.stored_proc_analyze.shared.static_profile import StaticProfile

# Default output directory matches the SPEC §"Output" contract and the
# ``.foundry/context/db/`` convention shipped by the M-tier workbench.
_DEFAULT_OUTPUT_DIR = Path(".foundry/context/db")

_DIGEST_FILENAME = "stored-procs.json"
_SUMMARY_FILENAME = "stored-procs-summary.md"

# The CLI exposes this set so a typo in ``--dialect`` is rejected with a
# clear message instead of bubbling sqlglot's internal error. ``auto``
# defers detection to the parser. Kept in lockstep with the parser's
# accepted dialect tuple — the dedicated unit test asserts equality.
_ALLOWED_DIALECTS: tuple[str, ...] = ("auto", "oracle", "tsql", "mssql", "db2")


@click.command("mod-stored-proc-analyze")
@click.option(
    "--ddl-path",
    "ddl_path",
    type=click.Path(),
    default=None,
    help="Directory or file containing CREATE PROCEDURE/FUNCTION/TRIGGER source.",
)
@click.option(
    "--dialect",
    type=click.Choice(_ALLOWED_DIALECTS),
    default="auto",
    help="Source SQL dialect (default: auto-detect via sqlglot).",
)
@click.option(
    "--include-pattern",
    "include_patterns",
    type=click.STRING,
    multiple=True,
    default=(),
    help="Glob applied to procedure names; repeatable (default: include all).",
)
@click.option(
    "--exclude-pattern",
    "exclude_patterns",
    type=click.STRING,
    multiple=True,
    default=(),
    help="Glob applied to procedure names; repeatable (default: exclude none).",
)
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(),
    default=str(_DEFAULT_OUTPUT_DIR),
    show_default=True,
    help="Where to write stored-procs.json + stored-procs-summary.md.",
)
@click.option(
    "--format",
    "format_",
    type=click.Choice(["both", "json", "markdown"]),
    default="both",
    show_default=True,
    help="Which artifact(s) to emit (default: both).",
)
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(  # noqa: PLR0915
    ddl_path: str | None,
    dialect: str,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    output_dir: str,
    format_: str,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """Run the M2 pipeline on a legacy SP source tree (/mod-stored-proc-analyze).

    Stages: A sqlglot parse → B static profile → C IO classifier →
    D side-effect detector → E business-logic classifier →
    F composite risk score. Outputs land under ``--output-dir`` as
    ``stored-procs.json`` + ``stored-procs-summary.md``.

    The Python CLI never calls an LLM — run --extract-only to emit the Stage E
    classification prompts, judge them in-session, then --continue --judgments
    to apply the judgments and render the outputs.
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
        PromptItem,
        ResponseFormat,
        read_judgments,
        write_prompts,
    )
    from primitives.stored_proc_analyze.shared.bizlogic_classifier import (  # noqa: PLC0415
        SYSTEM_PROMPT,
        build_prompt_items,
        build_user_prompt,
    )
    from primitives.stored_proc_analyze.shared.io_classifier import (  # noqa: PLC0415
        classify_io_shape,
    )
    from primitives.stored_proc_analyze.shared.side_effects import (  # noqa: PLC0415
        detect_side_effects,
    )
    from primitives.stored_proc_analyze.shared.sp_parser import (  # noqa: PLC0415
        parse_stored_procs,
    )
    from primitives.stored_proc_analyze.shared.static_profile import (  # noqa: PLC0415
        compute_static_profile,
    )

    # --- Validate skill-first flags (mutual exclusivity, --continue requires --judgments) ---
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )

    # --- Resolve output and state directories --------------------------------
    resolved_output = Path(output_dir)
    resolved_state_dir = Path(state_dir) if state_dir is not None else resolved_output / ".state"

    # =========================================================================
    # Branch: --continue --judgments (resume after in-session judgment)
    # =========================================================================
    if continue_:
        # Load intermediates saved during --extract-only
        sp_names_raw: list[str] = load_intermediate("sp_names", resolved_state_dir)
        profiles_raw: dict[str, object] = load_intermediate("profiles", resolved_state_dir)
        io_shapes_raw: dict[str, IOShape] = cast(
            "dict[str, IOShape]", load_intermediate("io_shapes", resolved_state_dir)
        )
        side_effects_raw: dict[str, list[SideEffect]] = cast(
            "dict[str, list[SideEffect]]",
            load_intermediate("side_effects", resolved_state_dir),
        )
        parsed_sps_raw: list[dict[str, object]] = load_intermediate(
            "parsed_sps", resolved_state_dir
        )
        source_dialect_saved: str | None = load_intermediate("source_dialect", resolved_state_dir)

        # Reconstruct ParsedSP objects from saved data via from_dict (mirrors
        # Pydantic model_validate — handles Literal narrowing without type: ignore)
        from primitives.stored_proc_analyze.shared.sp_parser import ParsedSP  # noqa: PLC0415
        from primitives.stored_proc_analyze.shared.static_profile import (  # noqa: PLC0415
            StaticProfile,
        )

        parsed_sps_map: dict[str, ParsedSP] = {}
        for raw in parsed_sps_raw:
            sp = ParsedSP.from_dict(raw)
            parsed_sps_map[sp.name] = sp

        profiles_map: dict[str, StaticProfile] = {}
        for sp_name, prof_raw in profiles_raw.items():
            assert isinstance(prof_raw, dict)
            profiles_map[sp_name] = StaticProfile(
                loc=int(prof_raw["loc"]),
                cyclomatic=int(prof_raw["cyclomatic"]),
                touched_tables=tuple(prof_raw.get("touched_tables", [])),
                table_touch_count=int(prof_raw["table_touch_count"]),
                out_calls=int(prof_raw["out_calls"]),
                transaction_statements=int(prof_raw["transaction_statements"]),
            )

        # Read and apply judgments
        assert judgments is not None  # guaranteed by validate_skill_first_flags
        judgment_envelope = read_judgments(Path(judgments))
        judgment_map = {item.id: item.response for item in judgment_envelope.items}

        _render_digest(
            sp_names=sp_names_raw,
            parsed_sps_map=parsed_sps_map,
            profiles_map=profiles_map,
            io_shapes=io_shapes_raw,
            side_effects=side_effects_raw,
            source_dialect=source_dialect_saved,
            judgment_map=judgment_map,
            output_dir=resolved_output,
            format_=format_,
        )

        # --- Clean up state -------------------------------------------------
        cleanup_state(resolved_state_dir, explicit=(state_dir is not None))
        return

    # =========================================================================
    # Common preamble: validate --ddl-path and run Stages A-D
    # (feeds the --extract-only branch)
    # =========================================================================

    # --- --ddl-path is required -----------------------------------------
    if ddl_path is None:
        click.echo(
            "Error: --ddl-path is required.",
            err=True,
        )
        sys.exit(2)

    # --- Validate --ddl-path (exit 2) ------------------------------------
    resolved_path = Path(ddl_path)
    if not resolved_path.exists():
        click.echo(f"Error: --ddl-path not found: {resolved_path}", err=True)
        sys.exit(2)

    # --- Stage A: parse (exit 3 on dialect rejection) --------------------
    try:
        parsed = parse_stored_procs(resolved_path, dialect=dialect)
    except ValueError as exc:
        # parse_stored_procs raises ValueError for unsupported --dialect.
        click.echo(f"Error: unsupported dialect: {exc}", err=True)
        sys.exit(3)

    if not parsed:
        click.echo(
            "Error: --ddl-path contained no CREATE PROCEDURE/FUNCTION/TRIGGER "
            f"source: {resolved_path}",
            err=True,
        )
        sys.exit(2)

    # --- Pattern filtering (exit 5 when the set goes empty) --------------
    candidate = _apply_pattern_filters(
        parsed,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
    )
    if not candidate:
        click.echo(
            "Error: --include-pattern / --exclude-pattern reduced the candidate "
            "set to zero procedures.",
            err=True,
        )
        sys.exit(5)

    # --- Stages B-D: deterministic per-SP work ---------------------------
    # Run parse, profile, IO, side-effects for all candidates.
    # Feeds the --extract-only branch.
    from primitives.stored_proc_analyze.shared.static_profile import (  # noqa: PLC0415
        StaticProfile as _SP,
    )

    profiles_obj: dict[str, _SP] = {}
    profiles_dict: dict[str, object] = {}
    io_shapes_dict: dict[str, IOShape] = {}
    side_effects_dict: dict[str, list[SideEffect]] = {}
    source_dialect_first: str | None = None

    for parsed_sp in candidate:
        profile = compute_static_profile(parsed_sp)
        io_shape = classify_io_shape(parsed_sp)
        sp_side_effects: list[SideEffect] = list(detect_side_effects(parsed_sp))

        profiles_obj[parsed_sp.name] = profile
        profiles_dict[parsed_sp.name] = {
            "loc": profile.loc,
            "cyclomatic": profile.cyclomatic,
            "touched_tables": list(profile.touched_tables),
            "table_touch_count": profile.table_touch_count,
            "out_calls": profile.out_calls,
            "transaction_statements": profile.transaction_statements,
        }
        io_shapes_dict[parsed_sp.name] = io_shape
        side_effects_dict[parsed_sp.name] = sp_side_effects

        if source_dialect_first is None:
            source_dialect_first = parsed_sp.source_dialect

    # =========================================================================
    # Branch: --extract-only (Stages A→D, save intermediates, emit prompts)
    # =========================================================================
    if extract_only:
        # Save intermediates for --continue
        sp_names = [sp.name for sp in candidate]
        save_intermediate("sp_names", sp_names, resolved_state_dir)
        save_intermediate("profiles", profiles_dict, resolved_state_dir)
        save_intermediate("io_shapes", io_shapes_dict, resolved_state_dir)
        save_intermediate("side_effects", side_effects_dict, resolved_state_dir)
        save_intermediate("source_dialect", source_dialect_first, resolved_state_dir)

        # Save enough ParsedSP data to reconstruct in --continue
        parsed_sps_serial = [
            {
                "name": sp.name,
                "schema": sp.schema,
                "kind": sp.kind,
                "source_dialect": sp.source_dialect,
                "raw_body": sp.raw_body,
                "parse_status": sp.parse_status,
            }
            for sp in candidate
        ]
        save_intermediate("parsed_sps", parsed_sps_serial, resolved_state_dir)

        # Build prompt items for Stage E (all non-unparseable SPs)
        prompt_pairs = build_prompt_items(
            candidate, profiles_dict, side_effects_dict, io_shapes_dict
        )

        bizlogic_schema: dict[str, object] = {
            "type": "object",
            "properties": {"classification": {"type": "string"}},
            "required": ["classification"],
        }

        prompt_items = []
        for item_id, sp in prompt_pairs:
            sp_profile = profiles_obj[sp.name]
            prompt_items.append(
                PromptItem(
                    id=item_id,
                    system=SYSTEM_PROMPT,
                    user=build_user_prompt(
                        sp,
                        profile=sp_profile,
                        side_effects=side_effects_dict[sp.name],
                        io_shape=io_shapes_dict[sp.name],
                    ),
                    response_format=ResponseFormat(type="json", schema=bizlogic_schema),
                )
            )

        # Every candidate SP was skipped by build_prompt_items (all unparseable)
        # — there is no Stage E model step to defer. Apply the deterministic
        # fallback classification (an empty judgment map resolves every
        # unparseable SP to "unknown"), render the artifacts, and exit 0 — no
        # empty PromptEnvelope, no round. Mirrors M1's _render_digest fast path.
        if not prompt_items:
            _render_digest(
                sp_names=[sp.name for sp in candidate],
                parsed_sps_map={sp.name: sp for sp in candidate},
                profiles_map=profiles_obj,
                io_shapes=io_shapes_dict,
                side_effects=side_effects_dict,
                source_dialect=source_dialect_first,
                judgment_map={},
                output_dir=resolved_output,
                format_=format_,
            )
            cleanup_state(resolved_state_dir, explicit=(state_dir is not None))
            click.echo(
                "[mod-stored-proc-analyze] no parseable stored procedures to "
                "classify; rendered deterministic fallback. No judgments needed.",
                err=True,
            )
            sys.exit(0)

        envelope = PromptEnvelope(
            primitive="mod-stored-proc-analyze",
            round=1,
            items=prompt_items,
        )
        prompts_path = resolved_state_dir / "round-1-prompts.json"
        write_prompts(envelope, prompts_path)
        click.echo(f"WROTE {prompts_path}")

        # Save pipeline state
        save_state(
            PipelineState(
                primitive="mod-stored-proc-analyze",
                round=1,
                completed_stages=["A", "B", "C", "D"],
                next_stage="E_continue",
                status="awaiting_judgments",
            ),
            resolved_state_dir,
        )

        sys.exit(7)

    # The Stage E LLM phase lived on the deleted harness path; the pipeline
    # now only runs in --extract-only or --continue mode.
    click.echo(
        "Error: run with --extract-only (then --continue --judgments) — "
        "the deterministic stages defer Stage E bizlogic classification to "
        "the SKILL.md body.",
        err=True,
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_digest(
    *,
    sp_names: list[str],
    parsed_sps_map: dict[str, ParsedSP],
    profiles_map: dict[str, StaticProfile],
    io_shapes: dict[str, IOShape],
    side_effects: dict[str, list[SideEffect]],
    source_dialect: str | None,
    judgment_map: dict[str, Any],
    output_dir: Path,
    format_: str,
) -> None:
    """Apply Stage E judgments + Stage F risk score per SP and render the digest.

    Shared by the ``--continue`` branch (after judgments are read) and the
    ``--extract-only`` fast path that fires when every candidate SP is
    unparseable (empty prompt set → empty judgment map, which resolves every
    SP to the deterministic ``"unknown"`` fallback). Mirrors M1's
    :func:`forge_validate.commands.mod_db_schema_analyze._render_digest`.
    """
    from primitives.stored_proc_analyze.shared.anti_hallucination import (  # noqa: PLC0415
        guard_stored_proc,
    )
    from primitives.stored_proc_analyze.shared.bizlogic_classifier import (  # noqa: PLC0415
        apply_judgments,
    )
    from primitives.stored_proc_analyze.shared.models import (  # noqa: PLC0415
        StoredProc,
        StoredProcDigest,
    )
    from primitives.stored_proc_analyze.shared.report_renderer import (  # noqa: PLC0415
        render_json,
        render_markdown,
    )
    from primitives.stored_proc_analyze.shared.risk_scorer import (  # noqa: PLC0415
        score_conversion_risk,
        score_conversion_risk_for_unparseable,
    )
    from primitives.stored_proc_analyze.shared.side_effects import (  # noqa: PLC0415
        audit_write_tables,
    )

    stored_procs: list[StoredProc] = []

    for sp_name in sp_names:
        parsed_sp = parsed_sps_map[sp_name]
        profile = profiles_map[sp_name]
        sp_io_shape = io_shapes[sp_name]
        sp_side_effects = side_effects[sp_name]

        # Stage E: apply judgment (runs anti-hallucination guard internally)
        biz = apply_judgments(
            parsed_sp,
            profile,
            sp_side_effects,
            sp_io_shape,
            judgment_map,
        )

        # Stage F: risk score.  Unparseable SPs receive the sentinel score so
        # they sort to the TOP of the risk-descending digest rather than the
        # bottom — "unknown" is not "safe" (#871).
        if parsed_sp.parse_status == "unparseable":
            risk = score_conversion_risk_for_unparseable()
        else:
            risk = score_conversion_risk(
                profile=profile,
                io_shape=sp_io_shape,
                side_effects=sp_side_effects,
                audit_write_count=len(audit_write_tables(parsed_sp)),
            )

        # Anti-hallucination guard (M2-11) — defence-in-depth
        guarded = guard_stored_proc(
            sp_name=parsed_sp.name,
            classification=biz.classification,
            candidate_tables=list(profile.touched_tables),
            ast_tables=profile.touched_tables,
        )
        for note in guarded.warnings:
            click.echo(f"[mod-stored-proc-analyze] {note}", err=True)

        stored_procs.append(
            StoredProc(
                name=parsed_sp.name,
                schema=parsed_sp.schema or "",
                params=[],
                language=parsed_sp.source_dialect,
                body_loc=profile.loc,
                cyclomatic=profile.cyclomatic,
                io_shape=sp_io_shape,
                side_effects=sp_side_effects,
                classification=guarded.classification,
                risk_score=risk,
                touched_tables=list(guarded.touched_tables),
                parse_status=parsed_sp.parse_status,
            )
        )

    digest = StoredProcDigest(
        source_dialect=source_dialect or "auto",
        procedures=stored_procs,
    )

    # --- Render outputs -----------------------------------------------------
    if format_ in ("json", "both"):
        render_json(digest, output_dir / _DIGEST_FILENAME)
        click.echo(f"WROTE {output_dir / _DIGEST_FILENAME}")
    if format_ in ("markdown", "both"):
        render_markdown(digest, output_dir / _SUMMARY_FILENAME)
        click.echo(f"WROTE {output_dir / _SUMMARY_FILENAME}")


def _apply_pattern_filters(
    parsed: list[ParsedSP],
    *,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
) -> list[ParsedSP]:
    """Apply include/exclude globs to a list of :class:`ParsedSP`.

    ``include_patterns`` is treated as a union (any-match keeps the SP);
    ``exclude_patterns`` is also a union (any-match drops the SP). The
    exclude pass runs after the include pass — that matches the SPEC's
    "set difference" wording and the convention every other foundry CLI
    uses (e.g. ``rules-gen`` profile selection).

    The :class:`ParsedSP` type is imported under ``TYPE_CHECKING`` so
    this helper participates in static typing without dragging the
    sqlglot-heavy ``sp_parser`` module into the CLI cold path.
    """
    if not include_patterns and not exclude_patterns:
        return list(parsed)

    def keep(sp: ParsedSP) -> bool:
        name = sp.name
        if include_patterns and not any(fnmatch.fnmatchcase(name, pat) for pat in include_patterns):
            return False
        return not (
            exclude_patterns and any(fnmatch.fnmatchcase(name, pat) for pat in exclude_patterns)
        )

    return [sp for sp in parsed if keep(sp)]
