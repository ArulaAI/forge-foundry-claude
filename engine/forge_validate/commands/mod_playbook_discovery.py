"""Forge Foundry — ``forge-validate mod-playbook-discovery`` subcommand (M5-11).

Drives the M5 four-phase discovery pipeline (recon → analyze → render →
audit) over a polyglot source tree and writes the SPEC.md §"Outputs"
artefacts under ``--state-dir / <run-id>``:

* ``PLAYBOOK.md`` — the 16-section discovery playbook with a Phase D
  §Validation Report appended.
* ``diagrams/<name>.mmd`` — the standalone Mermaid diagram bodies.
* ``audit.json`` — the Phase D :class:`AuditReport`.
* (Phase B also writes ``analysis.json`` / ``evidence-ledger.jsonl`` /
  ``analysis-digest.yaml`` into the same run directory.)

Mirrors M1's :mod:`forge_validate.commands.mod_db_schema_analyze` shape:
a thin Click handler over the deterministic pipeline orchestrator, with
lazy imports of the heavy engine modules so cold-start
``forge-validate --help`` stays cheap (TOOL-2 / #101).

The ``mod-`` slash-command prefix follows the M-tier invariant in
``AGENTS.md`` so the modernization namespace cannot collide with the
compliance-shaped S60 ``data-flow-map`` (charter §3.2.M).

The pipeline is **pure-deterministic** — it never invokes an LLM.

Three entry paths (design spec §3):

* **Default / ``--extract-only``**: Phase A + B, writes ``synthesis_prompts.json``
  under ``--state-dir/<run-id>/``, exits 7.  The in-session harness reads
  the prompts, fills them, and writes ``synthesis.json`` back.
* **``--continue --judgments <path>``**: loads the persisted ``analysis.json``
  from the run dir, validates ``synthesis.json``, Phase C + D, writes
  ``PLAYBOOK.md`` / ``audit.json`` / ``diagrams/``, exits 0 (or 6).
* **``--no-synthesis``**: today's exact A→B→C→D single-shot with
  ``synthesis=None``; BYTE-IDENTICAL to the pre-synthesis output.

Exit codes (M5 table, per SPEC.md §"Exit codes"):

* ``0`` — success: PLAYBOOK.md emitted (synthesis merged, or deterministic
  stubs under ``--no-synthesis``).
* ``1`` — generic / unexpected error.
* ``2`` — ``--code-root`` not found / not a directory / unreadable; or
  ``--continue`` cannot find the run dir's ``analysis.json`` (wrong
  ``--state-dir`` / ``--run-id``).
* ``3`` — ``--judgments`` file malformed / fails ``synthesis.json`` schema.
* ``6`` — Phase D gate: accuracy below floor, or ``--strict-audit`` with any
  finding.
* ``7`` — extract-only wrote ``synthesis-prompts.json``; awaiting judgments.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

# Default state directory matches the SPEC §"Outputs" contract and the
# ``.foundry/context/playbook/`` convention shipped by the M-tier workbench.
_DEFAULT_STATE_DIR = Path(".foundry/context/playbook")

#: Phase D accuracy floor: a run whose self-audit accuracy is below this
#: exits 6 (matches the SKILL.md "default 60%" contract).
_DEFAULT_ACCURACY_FLOOR = 0.60


@click.command("mod-playbook-discovery")
@click.option(
    "--code-root",
    "code_root",
    type=click.Path(exists=True, file_okay=False),
    required=True,
    help="Directory containing the source tree to discover (walked recursively).",
)
@click.option(
    "--state-dir",
    "state_dir",
    type=click.Path(),
    default=str(_DEFAULT_STATE_DIR),
    show_default=True,
    help=(
        "Where the per-run artefacts (PLAYBOOK.md, diagrams/, audit.json) are written. "
        "Both the extract and continue legs use this as the root; the run dir is "
        "<state-dir>/<run-id>."
    ),
)
@click.option(
    "--platform-flavor",
    "platform_flavor",
    type=click.STRING,
    default="auto",
    show_default=True,
    help="Platform flavor (auto runs the flavor-pack detect-and-pick registry).",
)
@click.option(
    "--run-id",
    "run_id",
    type=click.STRING,
    default="run",
    show_default=True,
    help=(
        "Per-run subdirectory name under --state-dir. "
        "Must be the same value for both the extract leg and --continue leg."
    ),
)
@click.option(
    "--strict-audit",
    "strict_audit",
    is_flag=True,
    default=False,
    help="Fail (exit 6) on ANY Phase D finding, not just when accuracy < the floor.",
)
@click.option(
    "--extract-only",
    "extract_only",
    is_flag=True,
    default=False,
    help=(
        "Run Phase A + B, write synthesis_prompts.json, exit 7 "
        "(default behavior; explicit flag for clarity)."
    ),
)
@click.option(
    "--continue",
    "continue_",
    is_flag=True,
    default=False,
    help=(
        "Resume from a persisted run dir. Requires --judgments. "
        "Loads analysis.json from <state-dir>/<run-id>, validates synthesis.json, "
        "runs Phase C + D, writes PLAYBOOK.md."
    ),
)
@click.option(
    "--judgments",
    "judgments",
    type=click.Path(),
    default=None,
    help="Path to the harness-written synthesis.json (required with --continue).",
)
@click.option(
    "--no-synthesis",
    "no_synthesis",
    is_flag=True,
    default=False,
    help=(
        "Skip the harness synthesis round entirely; run A→B→C→D in a single "
        "deterministic shot with ⚠️ UNVERIFIED stubs for §1/§2/§8/§14/§15. "
        "Output is byte-identical to pre-synthesis behavior. Useful for CI "
        "smoke runs, M6 ground-truth regeneration, and headless tests."
    ),
)
def cli(
    code_root: str,
    state_dir: str,
    platform_flavor: str,
    run_id: str,
    strict_audit: bool,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    no_synthesis: bool,
) -> None:
    """Run the M5 discovery pipeline on a source tree (/mod-playbook-discovery).

    Default (synthesis on): Phase A + B → writes synthesis_prompts.json → exit 7.
    Pass ``--no-synthesis`` for the legacy single-shot A→B→C→D path.
    Pass ``--continue --judgments <path>`` to finish after the harness fills the prompts.

    Outputs land under ``--state-dir / <run-id>``.

    Exit codes:
    - 0: success (PLAYBOOK.md written)
    - 1: generic / unexpected error
    - 2: --code-root not found / unreadable, or --continue can't find analysis.json
    - 3: --judgments file malformed / fails synthesis.json schema
    - 6: Phase D gate (accuracy below floor or --strict-audit with any finding)
    - 7: extract-only wrote synthesis_prompts.json; awaiting judgments
    """
    # --- Validate flag combinations ----------------------------------------
    # Invalid: --no-synthesis + --extract-only
    if no_synthesis and extract_only:
        raise click.UsageError("--no-synthesis and --extract-only are mutually exclusive.")

    # Invalid: --no-synthesis + --continue
    if no_synthesis and continue_:
        raise click.UsageError("--no-synthesis and --continue are mutually exclusive.")

    # Invalid: --extract-only + --continue
    if extract_only and continue_:
        raise click.UsageError("--extract-only and --continue are mutually exclusive.")

    # Invalid: --continue without --judgments
    if continue_ and judgments is None:
        raise click.UsageError("--continue requires --judgments <path>.")

    # --- Lazy imports to keep cold-start cheap (TOOL-2). -------------------
    resolved_root = Path(code_root)
    resolved_state = Path(state_dir)

    # =========================================================================
    # Branch: --no-synthesis  (A→B→C→D single-shot, no harness round)
    # =========================================================================
    if no_synthesis:
        from primitives.playbook_discovery.shared.pipeline import (  # noqa: PLC0415
            run_pipeline,
        )

        try:
            result = run_pipeline(
                resolved_root,
                resolved_state,
                platform_flavor=platform_flavor,
                run_id=run_id,
            )
        except (FileNotFoundError, NotADirectoryError) as exc:
            click.echo(f"Error: --code-root not usable: {exc}", err=True)
            sys.exit(2)
        except OSError as exc:
            click.echo(f"Error: --code-root could not be read: {exc}", err=True)
            sys.exit(2)
        except Exception as exc:
            click.echo(f"Error: mod-playbook-discovery failed: {exc}", err=True)
            sys.exit(1)

        _emit_phase_d_summary(result)
        _check_phase_d_gate(result, strict_audit)
        sys.exit(0)

    # =========================================================================
    # Branch: --continue --judgments <path>  (Phase C + D)
    # =========================================================================
    if continue_:
        assert judgments is not None  # guaranteed by validation above

        from primitives._shared.discovery.synthesis import (  # noqa: PLC0415
            SynthesisValidationError,
        )
        from primitives.playbook_discovery.shared.pipeline import (  # noqa: PLC0415
            run_pipeline_continue,
        )

        synthesis_path = Path(judgments)
        try:
            result = run_pipeline_continue(
                resolved_root,
                resolved_state,
                synthesis_path,
                run_id=run_id,
            )
        except SynthesisValidationError as exc:
            click.echo(f"Error: --judgments failed validation: {exc}", err=True)
            sys.exit(3)
        except FileNotFoundError as exc:
            # Covers both bad --code-root and missing analysis.json from the run dir.
            click.echo(f"Error: {exc}", err=True)
            sys.exit(2)
        except (NotADirectoryError, OSError) as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(2)
        except ValueError as exc:
            # _load_analysis raises ValueError for malformed analysis.json.
            click.echo(f"Error: could not load persisted analysis: {exc}", err=True)
            sys.exit(2)
        except Exception as exc:
            click.echo(f"Error: mod-playbook-discovery --continue failed: {exc}", err=True)
            sys.exit(1)

        _emit_phase_d_summary(result)
        _check_phase_d_gate(result, strict_audit)
        sys.exit(0)

    # =========================================================================
    # Default / --extract-only  (Phase A + B; exit 7)
    # =========================================================================
    from primitives.playbook_discovery.shared.pipeline import (  # noqa: PLC0415
        run_pipeline_extract,
    )

    try:
        extract_result = run_pipeline_extract(
            resolved_root,
            resolved_state,
            platform_flavor=platform_flavor,
            run_id=run_id,
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        click.echo(f"Error: --code-root not usable: {exc}", err=True)
        sys.exit(2)
    except OSError as exc:
        click.echo(f"Error: --code-root could not be read: {exc}", err=True)
        sys.exit(2)
    except Exception as exc:
        click.echo(f"Error: mod-playbook-discovery extract failed: {exc}", err=True)
        sys.exit(1)

    click.echo(f"flavor={extract_result.flavor} run_dir={extract_result.run_dir}")
    click.echo(f"WROTE {extract_result.synthesis_prompts_path}")
    sys.exit(7)


# ---------------------------------------------------------------------------
# Phase D summary / gate helpers (shared by no-synthesis and continue legs)
# ---------------------------------------------------------------------------


def _emit_phase_d_summary(result: object) -> None:
    """Print the standard Phase D summary line."""
    from primitives.playbook_discovery.shared.pipeline import PipelineResult  # noqa: PLC0415

    assert isinstance(result, PipelineResult)
    click.echo(
        f"flavor={result.flavor} "
        f"accuracy={result.accuracy * 100:.1f}% "
        f"findings={result.finding_count} "
        f"sections={result.section_count}"
    )
    click.echo(f"WROTE {result.playbook_path}")
    click.echo(f"WROTE {Path(result.run_dir) / 'audit.json'}")


def _check_phase_d_gate(result: object, strict_audit: bool) -> None:
    """Enforce the Phase D accuracy / strict-audit gate; calls sys.exit(6) if breached."""
    from primitives.playbook_discovery.shared.pipeline import PipelineResult  # noqa: PLC0415

    assert isinstance(result, PipelineResult)
    if strict_audit and result.finding_count > 0:
        click.echo(
            f"Error: --strict-audit: Phase D reported {result.finding_count} finding(s).",
            err=True,
        )
        sys.exit(6)
    if result.accuracy < _DEFAULT_ACCURACY_FLOOR:
        click.echo(
            f"Error: Phase D accuracy {result.accuracy * 100:.1f}% is below the "
            f"{_DEFAULT_ACCURACY_FLOOR * 100:.0f}% floor.",
            err=True,
        )
        sys.exit(6)
