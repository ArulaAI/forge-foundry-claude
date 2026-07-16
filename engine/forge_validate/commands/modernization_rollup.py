"""Forge Foundry — ``forge-validate modernization-rollup`` subcommand (SP4 convenience wrapper).

Wraps the SP4 ``/mod-modernization`` roll-up renderer so operators can invoke it
without knowing the internal package path or Python interpreter quirks::

    forge-validate modernization-rollup \\
        --state .foundry/context/modernization/.state/<run-id>/state.json \\
        --output .foundry/context/MODERNIZATION-SUMMARY.md

The actual rendering logic lives in
``primitives.modernization.shared.rollup_aggregator`` (the charter §4.1
orchestrator-shape carve-out module).  This command is a thin CLI wrapper — it
does not duplicate any logic; all rendering is delegated to ``aggregate()``.

This command is intentionally **not** the ``delegates-to-cli`` PLB path for the
``modernization`` primitive (which remains skill-first).  It is a standalone
convenience subcommand that exposes the deterministic aggregator via the
``forge-validate`` entry-point so operators do not need to spell out
``PYTHONPATH=.foundry python3 -m primitives.modernization.shared.rollup_aggregator``.

Exit codes:

* ``0`` — summary written successfully
* ``1`` — I/O error or malformed state JSON
* ``2`` — ``--state`` path does not exist
"""

from __future__ import annotations

import sys
from pathlib import Path

import click


@click.command("modernization-rollup")
@click.option(
    "--state",
    "state_path",
    required=True,
    type=click.Path(),
    metavar="PATH",
    help="Path to the resumable stage state JSON (state.json) written by /mod-modernization.",
)
@click.option(
    "--output",
    "output_path",
    required=True,
    type=click.Path(),
    metavar="PATH",
    help="Output path for MODERNIZATION-SUMMARY.md.",
)
def cli(state_path: str, output_path: str) -> None:
    """Roll up the /mod-modernization stage state into MODERNIZATION-SUMMARY.md.

    Reads the resumable stage state file written by the ``/mod-modernization``
    orchestrator and renders the deterministic top-level summary (overall status,
    per-stage table, resume hint when blocked, skipped-stage callout).

    Exit codes: 0 = success, 1 = I/O / JSON error, 2 = --state not found.
    """
    # Lazy import to keep cold-start cheap (TOOL-2).
    from primitives.modernization.shared.rollup_aggregator import (  # noqa: PLC0415
        aggregate,
    )

    resolved_state = Path(state_path).resolve()
    resolved_output = Path(output_path).resolve()

    if not resolved_state.is_file():
        click.echo(f"Error: --state {resolved_state} does not exist.", err=True)
        sys.exit(2)

    try:
        aggregate(state_path=resolved_state, output=resolved_output)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"WROTE {resolved_output}")
