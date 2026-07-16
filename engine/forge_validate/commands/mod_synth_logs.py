"""Forge Foundry — ``forge-validate mod-synth-logs`` subcommand (M6-CLI-1).

Drives the M6 four-phase pipeline (Phase A ingest → B plan → C generate
→ D validate) for a mainframe-application modernisation analysis.
Consumes either an M5 ``PLAYBOOK.md`` (preferred) or, when absent,
falls back to the source root directly via ``--code-root``.

The orchestrator handler is intentionally thin: each phase delegates
to ``primitives.synth_logs.shared.{ingest,plan,generate,validate}``.
Until the phase modules land (#465, #467, #469-#472, #473, #474), the
Phase B/C/D call sites raise ``NotImplementedError`` with explicit
forward-issue references — the CLI surface (flags + exit codes +
``--help``) is the contract this issue ships.

Follows the established M-tier CLI pattern: a thin Click handler with
lazy imports of heavy modules so cold-start ``forge-validate --help``
stays cheap (TOOL-2 / #101).

The ``mod-`` slash-command prefix follows the M-tier invariant in
``AGENTS.md`` so the modernization namespace cannot collide with the
compliance-shaped S60 ``data-flow-map`` (charter §3.2.M).

Exit codes (per SPEC.md §"CLI exit codes"):

* ``0`` — success: every requested phase completed and (when
  ``--run-script`` is not ``skip``) Phase D validation passed.
* ``2`` — missing input: neither ``--code-root`` nor ``--playbook``
  resolves to a readable path.
* ``3`` — invalid enum value: ``--scenario`` or ``--scale`` not in the
  closed set (Click reports its own exit ``2`` for unknown choices; this
  handler reserves ``3`` for runtime mismatches against the closed
  ``_ALL_*`` tuples).
* ``4`` — Phase D content-accuracy below floor; also the transitional
  code raised by the not-yet-wired Phase B/C/D stub. (Phase B is
  deterministic — see ``shared/plan.py`` — so there is no LLM-backend
  error class here.)
* ``5`` — Phase C ``--run-script subprocess`` execution returned
  non-zero (the generator script crashed; stderr is captured in
  ``logs-manifest.json``).
* ``6`` — two M6-specific triggers: (a) Phase D correlation rate below
  floor (default 95% — cross-file task-ID correlation insufficient), or
  (b) no log catalog is registered for the resolved platform flavor
  (capability not available — T2 work; see SPEC.md §"Extension surface"
  and ``shared/catalog.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

# Default paths per design spec §M6 "Input flags".
_DEFAULT_OUTPUT_DIR = Path("./logs/")
_DEFAULT_SCRIPT_PATH = Path("./generate_logs.py")
_DEFAULT_WINDOW_HOURS = 72
_DEFAULT_SCENARIO = "incident"
_DEFAULT_SCALE = "mid"
_DEFAULT_RUN_SCRIPT = "subprocess"

# Closed enums — keep aligned with primitives.synth_logs.shared.models
# (``_ALL_SCENARIOS``). Mirrored here so the CLI's ``click.Choice``
# stays honest at help-render time without importing the models module
# (TOOL-2 cold-start budget).
_SCENARIOS: tuple[str, ...] = ("baseline", "peak", "incident")
_SCALES: tuple[str, ...] = ("small", "mid", "large")
_RUN_SCRIPT_MODES: tuple[str, ...] = ("subprocess", "import", "skip")


@click.command("mod-synth-logs")
@click.option(
    "--code-root",
    "code_root",
    type=click.Path(),
    default=None,
    help=(
        "Directory containing the mainframe source tree. Required when "
        "--playbook is not given or does not resolve."
    ),
)
@click.option(
    "--playbook",
    "playbook",
    type=click.Path(),
    default=None,
    help=(
        "Path to the M5 PLAYBOOK.md grounding every log entity in real "
        "source citations. Optional but strongly recommended."
    ),
)
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(),
    default=str(_DEFAULT_OUTPUT_DIR),
    show_default=True,
    help="Destination for log files + logs-manifest.json.",
)
@click.option(
    "--window-hours",
    "window_hours",
    type=click.INT,
    default=_DEFAULT_WINDOW_HOURS,
    show_default=True,
    help="Total scenario duration in hours (minimum 24).",
)
@click.option(
    "--scenario",
    "scenario",
    type=click.Choice(list(_SCENARIOS)),
    default=_DEFAULT_SCENARIO,
    show_default=True,
    help=(
        "Narrative arc: baseline = day-1 normal; peak = month-end / "
        "heavy-batch overlay; incident = 3-day arc with day-3 cascading "
        "abend (the BankDemo default)."
    ),
)
@click.option(
    "--scale",
    "scale",
    type=click.Choice(list(_SCALES)),
    default=_DEFAULT_SCALE,
    show_default=True,
    help=(
        "Traffic volume: small = 20-50 txn/hr off-peak; mid = 50-200 "
        "off-peak / 500 peak; large = 200-1000 off-peak / 5000 peak."
    ),
)
@click.option(
    "--seed",
    "seed",
    type=click.INT,
    default=None,
    help="Deterministic seed for reproducible generation (None = wall-clock).",
)
@click.option(
    "--run-script",
    "run_script",
    type=click.Choice(list(_RUN_SCRIPT_MODES)),
    default=_DEFAULT_RUN_SCRIPT,
    show_default=True,
    help=(
        "Phase C execution model: subprocess = isolated + structured "
        "exit code; import = in-process / faster + traceback-rich for "
        "tests; skip = emit generate_logs.py only, consumer runs it."
    ),
)
def cli(
    code_root: str | None,
    playbook: str | None,
    output_dir: str,
    window_hours: int,
    scenario: str,
    scale: str,
    seed: int | None,
    run_script: str,
) -> None:
    """Generate synthetic CICS / VSAM / JCL log files for a 72-hour scenario.

    Drives the M6 four-phase pipeline (ingest → plan → generate →
    validate) against either an M5 ``PLAYBOOK.md`` (preferred) or the
    mainframe source root via ``--code-root``. Emits
    ``{--output-dir}/*.log`` (ten files) plus ``logs-manifest.json``
    alongside ``generate_logs.py``.

    Charter §3.2.M `mod-` invariant: this subcommand carries the
    `mod-` prefix so the modernization namespace cannot collide with
    the compliance-shaped S60 ``data-flow-map``.
    """
    # --- Validate closed enums against the runtime tuples ----------------
    # Click already enforces these via ``click.Choice``; the runtime guard
    # below catches programmatic invocations that bypass Click's parser
    # (e.g. a downstream wrapper passing values through ``main.main()``).
    if scenario not in _SCENARIOS:
        click.echo(
            f"Error: unknown --scenario {scenario!r}; expected one of {list(_SCENARIOS)}.",
            err=True,
        )
        sys.exit(3)
    if scale not in _SCALES:
        click.echo(
            f"Error: unknown --scale {scale!r}; expected one of {list(_SCALES)}.",
            err=True,
        )
        sys.exit(3)

    # --- Exit code 2 — neither --code-root nor --playbook resolves ------
    resolved_playbook = Path(playbook) if playbook else None
    resolved_code_root = Path(code_root) if code_root else None

    have_playbook = resolved_playbook is not None and resolved_playbook.is_file()
    have_code_root = resolved_code_root is not None and resolved_code_root.is_dir()

    if not have_playbook and not have_code_root:
        click.echo(
            "Error: missing input — pass --playbook PATH (recommended) or "
            "--code-root DIR. M6 needs at least one to derive the "
            "EntitiesDigest ground-truth (design spec §M6, exit code 2).",
            err=True,
        )
        sys.exit(2)

    # --- Catalog dispatch — exit 6 when no catalog is registered for the -----
    # resolved platform flavor. Runs BEFORE Phase A (#1198 7a) so a non-mainframe
    # repo gets a SPECIFIC "no catalog registered for flavor X" message (exit 6)
    # instead of running the mainframe-only Phase A ingest to an empty digest and
    # then hitting the generic "Phase B not implemented" exit 4. Both invocation
    # modes now resolve the real flavor, so exit 6 is consistent for any
    # non-``cics_cobol`` repo (previously a codebase-only walk blindly assumed
    # cics_cobol and hit exit 4).
    from primitives.synth_logs.shared.catalog import (  # noqa: PLC0415
        UnsupportedFlavorError,
        assert_catalog_available,
        detect_flavor_from_playbook,
    )

    if have_playbook and resolved_playbook is not None:
        _detected_flavor = (
            detect_flavor_from_playbook(resolved_playbook.read_text(encoding="utf-8")) or "generic"
        )
    else:
        # Codebase-only walk: detect the REAL platform flavor from the source
        # root (reusing the discovery engine) rather than blindly assuming
        # cics_cobol — that masqueraded a Python repo as mainframe and produced
        # the misleading exit 4. Falls back to the universal "generic" flavor
        # when no pack matches, so assert_catalog_available reports it honestly.
        from primitives._shared.discovery.recon import detect_platform_flavor  # noqa: PLC0415

        assert resolved_code_root is not None  # guaranteed by the input check above
        _flavor_name, _ = detect_platform_flavor(resolved_code_root, manifest_hits=[])
        _detected_flavor = _flavor_name or "generic"

    try:
        assert_catalog_available(_detected_flavor)
    except UnsupportedFlavorError as exc:
        click.echo(
            f"Error: {exc} — pass a cics_cobol playbook or a mainframe "
            "source root to use the v1 catalog. Other platform catalogs "
            'land as T2 work (see SPEC.md §"Extension surface").',
            err=True,
        )
        sys.exit(6)

    # --- Phase A — ingest -----------------------------------------------
    # ``primitives.synth_logs.shared.ingest`` is fully wired (#465,
    # M6-PHASE-A-1). The importlib dance is kept so the test suite can
    # inject a ``ModuleType`` stub via ``sys.modules`` for hermetic
    # unit tests of the Phase B/C/D stub exit-code paths.
    import importlib  # noqa: PLC0415

    try:
        _ingest_mod = importlib.import_module("primitives.synth_logs.shared.ingest")
    except ImportError as exc:
        click.echo(
            "Error: primitives.synth_logs.shared.ingest is not available "
            f"({exc}). This is unexpected — the module shipped with #465 "
            "(M6-PHASE-A-1); check your installation.",
            err=True,
        )
        raise click.exceptions.Exit(1) from exc

    try:
        entities = _ingest_mod.run_phase_a(
            playbook_path=resolved_playbook,
            code_root=resolved_code_root,
        )
    except AttributeError as exc:
        # ``run_phase_a`` should always be present (#465, M6-PHASE-A-1).
        # Guard kept for forward compatibility with local edits / stubs.
        click.echo(
            "Error: primitives.synth_logs.shared.ingest.run_phase_a is "
            "not available — see issue #867 for context.",
            err=True,
        )
        raise click.exceptions.Exit(1) from exc

    # Surface the Phase A digest summary so the next phase's stub can
    # reference real counts in its error message rather than hide them. Only a
    # ``cics_cobol`` repo reaches here — non-mainframe flavors exited 6 above.
    _programs_count = len(getattr(entities, "programs", []))
    _transactions_count = len(getattr(entities, "transactions", []))

    # --- Phase B — scenario planning -------------------------------------
    # Stub call site — the real synthesiser lands with M6-PHASE-B-* (#467,
    # #469-#472). The NotImplementedError carries the parsed CLI inputs so
    # CI logs from a premature invocation point straight at the missing
    # phase rather than at the CLI handler.
    #
    # Phase B is DETERMINISTIC (seeded random, fixed constants, zero LLM —
    # see the determinism contract in ``shared/plan.py``). The wiring tickets
    # call ``plan.plan_scenario(...)`` directly; there is no LLM harness to
    # thread through here.
    try:
        _phase_b_stub(
            scenario=scenario,
            scale=scale,
            window_hours=window_hours,
            seed=seed,
            run_script=run_script,
            output_dir=output_dir,
            programs_count=_programs_count,
            transactions_count=_transactions_count,
        )
    except NotImplementedError as exc:
        click.echo(f"Error: Phase B not yet implemented: {exc}", err=True)
        sys.exit(4)


def _phase_b_stub(
    *,
    scenario: str,
    scale: str,
    window_hours: int,
    seed: int | None,
    run_script: str,
    output_dir: str,
    programs_count: int,
    transactions_count: int,
) -> None:
    """Raise :class:`NotImplementedError` for the not-yet-wired Phase B stub.

    Phase A ingest is fully wired (#465, M6-PHASE-A-1) and produces the
    ``EntitiesDigest`` passed in via ``programs_count`` / ``transactions_count``.
    Phases B / C / D are not yet wired — exit code 4 is the transitional code
    for this stub state (SPEC.md §"Exit codes", #868).

    Extracted into a helper so the CLI happy-path test can monkey-patch
    it to a no-op and still exercise Phase A wiring + exit-code flow.

    The real implementation arrives across:

    * #467 — M6-PHASE-B-1: ``shared/plan.py`` scenario synthesiser.
    * #469-#472 — M6-PHASE-B-2..5: per-LogFormat planner refinements.
    * #473 — M6-PHASE-C-1: ``shared/generate.py`` + Jinja2 templates.
    * #474 — M6-PHASE-D-1: ``shared/validate.py`` correlation rules.
    """
    raise NotImplementedError(
        "M6 Phase B (scenario planning) lands with issues #467 / "
        "#469-#472; Phase C with #473; Phase D with #474. "
        f"CLI parsed inputs: scenario={scenario!r}, scale={scale!r}, "
        f"window_hours={window_hours}, seed={seed!r}, "
        f"run_script={run_script!r}, "
        f"output_dir={output_dir!r}. "
        f"Phase A produced an EntitiesDigest with {programs_count} "
        f"programs, {transactions_count} transactions."
    )


__all__ = ["_phase_b_stub", "cli"]
