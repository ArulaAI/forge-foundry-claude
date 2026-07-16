"""Forge Foundry — ``forge-validate mod-db-schema-analyze`` subcommand (M1-11).

Drives the M1 pipeline (Stage A parse → E hotspot detect) for a file
or directory of legacy DDL and writes the three SPEC.md §"Outputs"
artifacts under ``--output-dir``:

* ``schema.json`` — :class:`SchemaDigest` (Pydantic v2,
  ``extra="forbid"``, schema-versioned at ``"1.0"``).
* ``schema-summary.md`` — human-readable summary grouped by LoB,
  hotspots / orphans / cycles surfaced.
* ``diagrams/schema-dag.mmd`` — Mermaid flowchart of the top-N
  hotspot tables and their FK neighbours.

Mirrors M2's :mod:`forge_validate.commands.mod_stored_proc_analyze`
shape (PR #382): thin Click handler over the per-stage primitive
functions, lazy imports of heavy modules (sqlglot) so cold-start
``forge-validate --help`` stays cheap (TOOL-2 / #101).

The ``mod-`` slash-command prefix follows the M-tier invariant in
``AGENTS.md`` so the modernization namespace cannot collide with the
compliance-shaped S60 ``data-flow-map`` (charter §3.2.M).

Exit codes (per SPEC.md §"Exit codes"):

* ``0`` — success
* ``1`` — generic error
* ``2`` — ``--ddl-path`` not found or contains no DDL
* ``3`` — sqlglot rejected the requested dialect
* ``5`` — no parseable DDL objects found (the ``--ddl-path`` resolved
  but every statement failed Stage A)
* ``6`` — ``--connect URI`` requested but the ``_db_connector``
  adapter is not installed (deferred to T1.5)
"""

from __future__ import annotations

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

# Default output directory matches the SPEC §"Outputs" contract and the
# ``.foundry/context/db/`` convention shipped by the M-tier workbench.
_DEFAULT_OUTPUT_DIR = Path(".foundry/context/db")

_DIGEST_FILENAME = "schema.json"
_SUMMARY_FILENAME = "schema-summary.md"
_DAG_RELATIVE_PATH = Path("diagrams") / "schema-dag.mmd"

# The CLI exposes this set so a typo in ``--dialect`` is rejected with a
# clear message instead of bubbling sqlglot's internal error. ``auto``
# defers detection to the parser. Kept in lockstep with the parser's
# accepted dialect tuple — the dedicated unit test asserts equality.
_ALLOWED_DIALECTS: tuple[str, ...] = (
    "auto",
    "oracle",
    "mssql",
    "tsql",
    "db2",
    "postgres",
    "mysql",
)

_ALLOWED_FORMATS: tuple[str, ...] = ("both", "markdown", "json")


@click.command("mod-db-schema-analyze")
@click.option(
    "--ddl-path",
    "ddl_path",
    type=click.Path(),
    default=None,
    help="File or directory of .sql DDL exports (required unless --connect is given).",
)
@click.option(
    "--connect",
    "connect",
    type=click.STRING,
    default=None,
    help=(
        "Live DB connection URI. NOT IMPLEMENTED at T1 (exits 6 with a deferral "
        "message). Reserved so the CLI surface stays stable for T1.5."
    ),
)
@click.option(
    "--dialect",
    type=click.Choice(_ALLOWED_DIALECTS),
    default="auto",
    show_default=True,
    help="Source SQL dialect (default: auto via sqlglot's dialect detector).",
)
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(),
    default=str(_DEFAULT_OUTPUT_DIR),
    show_default=True,
    help="Where to write schema.json + schema-summary.md + diagrams/schema-dag.mmd.",
)
@click.option(
    "--hotspot-threshold",
    "hotspot_threshold",
    type=click.INT,
    default=None,
    help="Incoming FK in-degree at or above which a table is flagged hotspot (default: 5).",
)
@click.option(
    "--format",
    "format_",
    type=click.Choice(_ALLOWED_FORMATS),
    default="both",
    show_default=True,
    help="Which artifact(s) to emit (default: both = json + markdown + Mermaid).",
)
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(
    ddl_path: str | None,
    connect: str | None,
    dialect: str,
    output_dir: str,
    hotspot_threshold: int | None,
    format_: str,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """Run the M1 pipeline on a legacy DDL source tree (/mod-db-schema-analyze).

    Stages: A sqlglot parse → B object inventory → C dependency DAG →
    D LoB classification (with anti-hallucination guard) →
    E hotspot/orphan detection. Outputs land under ``--output-dir`` as
    ``schema.json`` + ``schema-summary.md`` + ``diagrams/schema-dag.mmd``.

    The Python CLI never calls an LLM — run --extract-only to emit the Stage D
    classification prompts, judge them in-session, then --continue --judgments
    to apply the judgments and render the outputs.
    """
    # --- Lazy imports to keep cold-start cheap (TOOL-2). -----------------
    from primitives._shared.pipeline_state import (  # noqa: PLC0415
        PipelineState,
        PipelineStateNotFoundError,
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
    from primitives.db_schema_analyze.shared.ddl_parser import (  # noqa: PLC0415
        parse_ddl_files,
    )
    from primitives.db_schema_analyze.shared.lob_classifier import (  # noqa: PLC0415
        SYSTEM_PROMPT,
        apply_judgments,
        build_prompt_items,
        build_user_prompt,
    )
    from primitives.db_schema_analyze.shared.models import (  # noqa: PLC0415
        Sequence,
        Table,
        View,
    )
    from primitives.db_schema_analyze.shared.object_inventory import (  # noqa: PLC0415
        build_inventory,
    )

    # --- Validate skill-first flags (mutual exclusivity, --continue requires --judgments) ---
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )

    # --- Resolve state directory -----------------------------------------
    resolved_output = Path(output_dir)
    resolved_state_dir = Path(state_dir) if state_dir is not None else resolved_output / ".state"

    # =========================================================================
    # Branch: --continue --judgments (resume after in-session judgment)
    # =========================================================================
    if continue_:
        # Load intermediates saved during --extract-only. The state is cleaned up
        # after a successful --continue, so a SECOND --continue on the same state
        # dir must be idempotent (#1197 6d): if the digest was already written the
        # run is finalized (safe no-op, exit 0); if there is no digest either, the
        # run never ran (input error, exit 2) — never the generic exit 1.
        try:
            tables_raw = load_intermediate("tables", resolved_state_dir)
            views_raw = load_intermediate("views", resolved_state_dir)
            sequences_raw = load_intermediate("sequences", resolved_state_dir)
        except PipelineStateNotFoundError:
            digest_path = resolved_output / _DIGEST_FILENAME
            if digest_path.is_file():
                click.echo(
                    f"[mod-db-schema-analyze] --continue: run already finalized; "
                    f"digest at {digest_path}. Nothing to do.",
                    err=True,
                )
                return
            click.echo(
                f"Error: no pipeline state under {resolved_state_dir} and no finalized "
                f"digest at {digest_path} — run --extract-only first, then "
                "--continue --judgments <path>.",
                err=True,
            )
            sys.exit(2)

        tables = [Table.model_validate(d) for d in tables_raw]
        views = [View.model_validate(d) for d in views_raw]
        sequences = [Sequence.model_validate(d) for d in sequences_raw]

        # Read and apply judgments
        assert judgments is not None  # guaranteed by validate_skill_first_flags
        judgment_envelope = read_judgments(Path(judgments))
        judgment_map = {item.id: item.response for item in judgment_envelope.items}
        classified_tables = apply_judgments(tables, judgment_map)

        _render_digest(
            tables=classified_tables,
            views=views,
            sequences=sequences,
            dialect=dialect,
            hotspot_threshold=hotspot_threshold,
            output_dir=resolved_output,
            format_=format_,
        )
        cleanup_state(resolved_state_dir, explicit=(state_dir is not None))
        return

    # =========================================================================
    # Common preamble: validate --ddl-path and run Stages A + B
    # (shared by --extract-only and CI/harness mode)
    # =========================================================================

    # --- --connect not supported at T1 (exit 6) --------------------------
    if connect is not None:
        click.echo(
            "Error: --connect URI is not implemented at T1; deferred to T1.5 "
            "via the _db_connector adapter. Use --ddl-path instead.",
            err=True,
        )
        sys.exit(6)

    # --- --ddl-path is required when --connect is absent -----------------
    if ddl_path is None:
        click.echo(
            "Error: --ddl-path is required (use --connect when the T1.5 adapter ships).",
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
        expressions, parse_errors = parse_ddl_files(resolved_path, dialect=dialect)
    except FileNotFoundError as exc:  # pragma: no cover
        # Defensive — Path.exists() above already caught this. Kept so
        # the parser's own check still maps to exit 2.
        click.echo(f"Error: --ddl-path not found: {exc}", err=True)
        sys.exit(2)
    except ValueError as exc:
        # parse_ddl_files raises ValueError for unsupported --dialect.
        click.echo(f"Error: unsupported dialect: {exc}", err=True)
        sys.exit(3)

    # Surface per-file parse failures on stderr but do not abort the
    # run — per SPEC.md §"Exit codes": "A run with per-file parse failures
    # still exits 0 — those statements land in the digest with
    # parse_status=unparseable and a stderr note. Exit code 5 fires only
    # when every statement fails."
    for err in parse_errors:
        click.echo(f"[mod-db-schema-analyze] parse error in {err.path}: {err.message}", err=True)

    if not expressions:
        # Two cases: empty DDL (no parseable content) → 5; truly missing
        # input → 2 (caught above). The walker returns [] for an empty
        # directory or a file with no parseable statements.
        click.echo(
            "Error: --ddl-path resolved but contained no parseable DDL objects.",
            err=True,
        )
        sys.exit(5)

    # --- Stage B: inventory ----------------------------------------------
    tables, views, sequences = build_inventory(expressions)

    if not tables and not views and not sequences:
        click.echo(
            "Error: --ddl-path produced no tables, views, or sequences after parsing.",
            err=True,
        )
        sys.exit(5)

    # =========================================================================
    # Branch: --extract-only (Stages A→B, save intermediates, emit prompts)
    # =========================================================================
    if extract_only:
        # Save Stage B intermediates for --continue
        save_intermediate(
            "tables", [t.model_dump(by_alias=True) for t in tables], resolved_state_dir
        )
        save_intermediate("views", [v.model_dump(by_alias=True) for v in views], resolved_state_dir)
        save_intermediate(
            "sequences", [s.model_dump(by_alias=True) for s in sequences], resolved_state_dir
        )

        # Build prompt items for regex-unmatched tables
        prompt_pairs = build_prompt_items(tables)

        lob_schema: dict[str, object] = {
            "type": "object",
            "properties": {"lob": {"type": "string"}},
            "required": ["lob"],
        }
        prompt_items = [
            PromptItem(
                id=item_id,
                system=SYSTEM_PROMPT,
                user=build_user_prompt(table),
                response_format=ResponseFormat(type="json", schema=lob_schema),
            )
            for item_id, table in prompt_pairs
        ]

        # Every table was classified deterministically by the regex pack — there
        # is no model step to defer. Apply the deterministic classifications (an
        # empty judgment map resolves every table via the regex-first path),
        # render the artifacts, and exit 0 — no empty PromptEnvelope, no round.
        if not prompt_items:
            classified = apply_judgments(tables, {})
            _render_digest(
                tables=classified,
                views=views,
                sequences=sequences,
                dialect=dialect,
                hotspot_threshold=hotspot_threshold,
                output_dir=resolved_output,
                format_=format_,
            )
            cleanup_state(resolved_state_dir, explicit=(state_dir is not None))
            click.echo(
                "[mod-db-schema-analyze] all tables classified deterministically; "
                "no judgments needed.",
                err=True,
            )
            sys.exit(0)

        envelope = PromptEnvelope(
            primitive="mod-db-schema-analyze",
            round=1,
            items=prompt_items,
        )
        prompts_path = resolved_state_dir / "round-1-prompts.json"
        write_prompts(envelope, prompts_path)
        click.echo(f"WROTE {prompts_path}")

        # Save pipeline state
        save_state(
            PipelineState(
                primitive="mod-db-schema-analyze",
                round=1,
                completed_stages=["A", "B"],
                next_stage="D_continue",
                status="awaiting_judgments",
            ),
            resolved_state_dir,
        )

        sys.exit(7)

    # The pipeline only runs in --extract-only or --continue mode; without
    # either flag there is nothing to do (the LLM phase lived on the deleted
    # harness path). validate_skill_first_flags already gated the inputs.
    click.echo(
        "Error: run with --extract-only (then --continue --judgments) — "
        "the deterministic stages defer Stage D classification to the SKILL.md body.",
        err=True,
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _qn(schema: str, name: str) -> str:
    """Return ``schema.name`` if schema is set, else just ``name``.

    Mirrors the qualified-name helper in
    :mod:`primitives.db_schema_analyze.shared.object_inventory` so the
    CLI's anti-hallucination set is in lockstep with Stage B's
    inventory keys.
    """
    if schema:
        return f"{schema}.{name}"
    return name


def _qn_set(tables: list) -> set[str]:  # type: ignore[type-arg]
    """Build the qualified-name set used by the anti-hallucination guard.

    Walks the table list exactly once and returns the set of qualified
    names. Returned as a ``set[str]`` so the guard can do O(1) membership
    checks regardless of how many tables the digest contains. The
    parameter is typed loosely (``list``) to avoid an import-time
    coupling on the Table model from the type checker's perspective; the
    runtime contract is "anything with a ``schema_`` and ``name``".
    """
    return {_qn(t.schema_, t.name) for t in tables}


def _render_digest(  # type: ignore[no-untyped-def]
    *,
    tables,  # list[Table], already LoB-classified
    views,  # list[View]
    sequences,  # list[Sequence]
    dialect: str,
    hotspot_threshold: int | None,
    output_dir: Path,
    format_: str,
) -> None:
    """Run Stages C/E + the anti-hallucination guard and render the digest.

    Shared by the ``--continue`` branch (after judgments are applied) and the
    ``--extract-only`` fast path that fires when every table is classified
    deterministically (no prompts, no model round). *tables* must already carry
    their final ``lob_classification``.
    """
    from primitives.db_schema_analyze.shared.anti_hallucination import (  # noqa: PLC0415
        guard_table,
    )
    from primitives.db_schema_analyze.shared.dependency_graph import (  # noqa: PLC0415
        build_dependency_dag,
    )
    from primitives.db_schema_analyze.shared.hotspot_detector import (  # noqa: PLC0415
        DEFAULT_HOTSPOT_THRESHOLD,
        detect_hotspots,
    )
    from primitives.db_schema_analyze.shared.models import SchemaDigest  # noqa: PLC0415
    from primitives.db_schema_analyze.shared.report_renderer import (  # noqa: PLC0415
        render_json,
        render_markdown,
        render_mermaid_dag,
    )

    edges, _cycles = build_dependency_dag(tables, views)

    effective_threshold = (
        hotspot_threshold if hotspot_threshold is not None else DEFAULT_HOTSPOT_THRESHOLD
    )
    flagged_tables = detect_hotspots(tables, edges, hotspot_threshold=effective_threshold)

    ast_table_qns = _qn_set(flagged_tables)
    guarded_tables = []
    for table in flagged_tables:
        report = guard_table(
            table_name=_qn(table.schema_, table.name),
            lob_classification=table.lob_classification,
            candidate_references=[],
            ast_tables=ast_table_qns,
        )
        for note in report.warnings:
            click.echo(f"[mod-db-schema-analyze] {note}", err=True)
        if report.lob_classification != table.lob_classification:
            guarded_tables.append(table.model_copy(update={"lob_classification": None}))
        else:
            guarded_tables.append(table)

    digest = SchemaDigest(
        source_dialect=dialect,
        tables=guarded_tables,
        views=views,
        sequences=sequences,
        dependency_dag_edges=edges,
    )

    if format_ in ("json", "both"):
        render_json(digest, output_dir / _DIGEST_FILENAME)
        click.echo(f"WROTE {output_dir / _DIGEST_FILENAME}")
    if format_ in ("markdown", "both"):
        render_markdown(digest, output_dir / _SUMMARY_FILENAME)
        click.echo(f"WROTE {output_dir / _SUMMARY_FILENAME}")
    if format_ == "both":
        render_mermaid_dag(digest, output_dir / _DAG_RELATIVE_PATH)
        click.echo(f"WROTE {output_dir / _DAG_RELATIVE_PATH}")
