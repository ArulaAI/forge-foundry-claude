"""forge-validate mod-modernization-scope subcommand (SP2, PLB).

The app-tier modernization synthesizer. Requires the four analysis artifacts
(functional-map S61, db-schema M1, api-contract S40, app-digest from
mod-app-analysis) and correlates them into the four deliverables (deconstruction,
role-complete specs, standalone ADRs, wave-by-wave roadmap).

Exit codes (conventional vocabulary; see SPEC.md):

* ``0`` — success.
* ``2`` — a required analysis artifact is missing (names the analyzer to run) or
  the ``--code-root`` is not a directory.
* ``3`` — the ``--continue`` judgments file failed synthesis-schema validation.
* ``6`` — a rendered spec/ADR citation did not resolve (refcheck below floor).
* ``7`` — ``--extract-only`` wrote ``synthesis_prompts.json``; author
  ``synthesis.json`` and re-invoke with ``--continue --judgments <path>``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from pydantic import ValidationError

from primitives._shared.modernization.repos import RepoSetParseError, build_repo_set
from primitives.modernization_scope.shared.pipeline import ArtifactMissingError, run


@click.command("mod-modernization-scope")
@click.option("--code-root", required=True, type=click.Path(path_type=Path))
@click.option(
    "--context-root",
    default=".foundry/context",
    type=click.Path(path_type=Path),
    help="Root under which the analysis artifacts are auto-discovered.",
)
@click.option(
    "--output",
    "output_dir",
    default=".foundry/context/modernization",
    type=click.Path(path_type=Path),
    help="Durable output directory for MODERNIZATION.md / specs / ADRs / ROADMAP.",
)
@click.option(
    "--state-dir",
    default=".foundry/context/modernization/.state",
    type=click.Path(path_type=Path),
)
@click.option("--run-id", default="run")
@click.option("--functional-map", "functional_map", default=None, type=click.Path(path_type=Path))
@click.option("--schema", default=None, type=click.Path(path_type=Path))
@click.option("--api", default=None, type=click.Path(path_type=Path))
@click.option("--app-digest", "app_digest", default=None, type=click.Path(path_type=Path))
@click.option(
    "--test-inventory",
    "test_inventory",
    default=None,
    type=click.Path(path_type=Path),
    help="Optional test-inventory.json (SP-G) — legacy tests carried into §11 as parity oracles.",
)
@click.option(
    "--target",
    "target_path",
    default=None,
    type=click.Path(path_type=Path),
    help=(
        "Optional target-stack.json (SP2, #1410) — the modernization target stack. "
        "Auto-discovered under <state-dir>/<run-id>/ when omitted; absent → descriptive."
    ),
)
@click.option(
    "--hla",
    "hla_path",
    default=None,
    type=click.Path(path_type=Path),
    help=(
        "Optional hla-source.json (Track A, #1428) — the stated High-Level "
        "Architecture. Auto-discovered under <state-dir>/<run-id>/ when omitted; "
        "absent → no Architecture Conformance section (descriptive, unchanged)."
    ),
)
@click.option(
    "--context",
    "context_scope",
    multiple=True,
    help=(
        "Scope the run to one bounded context (repeatable). Only these contexts' specs / "
        "BRDs / ADRs + a scoped ROADMAP are written; their dependencies are documented as "
        "Boundary Contracts, not re-specced (#1542). Omit for a whole-app run (unchanged)."
    ),
)
@click.option(
    "--feature",
    "feature_scope",
    multiple=True,
    help=(
        "Scope the run to a human feature (repeatable) — the SKILL body resolves each to "
        "its bounded context(s) and re-invokes with the resolved --context (#1542)."
    ),
)
@click.option("--no-synthesis", is_flag=True, default=False)
@click.option(
    "--no-migration-synthesis",
    is_flag=True,
    default=False,
    help=(
        "Skip the multi-repo migration-state confirmation (L3, MR4). Every repo's band "
        "stays as the deterministic MR3 scorer decided it (uncertain → legacy/low, "
        "source=deterministic); no migration prompts, no exit-7-for-migration handshake. "
        "No effect on single-repo runs (which never score migration state)."
    ),
)
@click.option("--extract-only", is_flag=True, default=False)
@click.option("--continue", "continue_flag", is_flag=True, default=False)
@click.option("--judgments", default=None, type=click.Path(path_type=Path))
@click.option(
    "--repo",
    "repo_overrides",
    multiple=True,
    help=(
        "role=path override for multi-repo layout (MR1, #1550). "
        "E.g. --repo api=../orders-api. Role must be one of: ui, api, db, iac."
    ),
)
@click.option(
    "--modernized",
    "modernized_overrides",
    multiple=True,
    help=(
        "Force a unit to already-modern regardless of evidence (MR5, #1554; repeatable). "
        "A token matches a bounded-context/domain label, else a repo/role/node-id substring "
        "(e.g. --modernized api). Matched units are demoted to Boundary Contracts "
        "(integrate against, do not rebuild) and dropped from the extraction roadmap."
    ),
)
def mod_modernization_scope(  # noqa: PLR0913 — the documented CLI surface (SPEC §5)
    code_root: Path,
    context_root: Path,
    output_dir: Path,
    state_dir: Path,
    run_id: str,
    functional_map: Path | None,
    schema: Path | None,
    api: Path | None,
    app_digest: Path | None,
    test_inventory: Path | None,
    target_path: Path | None,
    hla_path: Path | None,
    context_scope: tuple[str, ...],
    feature_scope: tuple[str, ...],
    no_synthesis: bool,
    no_migration_synthesis: bool,
    extract_only: bool,
    continue_flag: bool,
    judgments: Path | None,
    repo_overrides: tuple[str, ...],
    modernized_overrides: tuple[str, ...],
) -> None:
    """SP2 — correlate analysis artifacts → deconstruction + specs + ADRs + roadmap."""
    if continue_flag and judgments is None:
        raise click.UsageError("--continue requires --judgments <path>")
    if not Path(code_root).is_dir():
        print(f"error: --code-root not a directory: {code_root}", file=sys.stderr)
        raise SystemExit(2)

    # --feature resolution is the SKILL body's job (in-session judgment over the deterministic
    # candidate ranking): it resolves each feature to its bounded context(s) and re-invokes
    # with --context. Passing --feature to the CLI directly is a usage error pointing there.
    if feature_scope:
        raise click.UsageError(
            "--feature is resolved by the /mod-modernization-scope SKILL body (which ranks "
            "candidate contexts, judges the mapping, and re-invokes with the resolved "
            "--context). Pass the resolved --context here, or run the skill."
        )
    scope_contexts = list(context_scope) or None

    try:
        repo_set = build_repo_set(Path.cwd(), repo_overrides)
    except RepoSetParseError as exc:
        raise click.UsageError(str(exc)) from exc

    try:
        result = run(
            Path(code_root),
            Path(context_root),
            output_dir=Path(output_dir),
            state_dir=Path(state_dir),
            run_id=run_id,
            functional_map=functional_map,
            schema=schema,
            api=api,
            app_digest=app_digest,
            test_inventory=test_inventory,
            target_path=Path(target_path) if target_path is not None else None,
            hla_path=Path(hla_path) if hla_path is not None else None,
            no_synthesis=no_synthesis,
            no_migration_synthesis=no_migration_synthesis,
            extract_only=extract_only,
            continue_path=Path(judgments) if (continue_flag and judgments is not None) else None,
            scope_contexts=scope_contexts,
            repo_set=repo_set if repo_set.entries else None,
            modernized_overrides=set(modernized_overrides),
        )
    except ArtifactMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except (ValidationError, ValueError) as exc:
        print(f"error: synthesis.json failed validation:\n{exc}", file=sys.stderr)
        raise SystemExit(3) from exc

    for path in result.written:
        click.echo(f"mod-modernization-scope: wrote {path}")
    if result.prompts_path is not None:
        click.echo(f"mod-modernization-scope: awaiting judgments — see {result.prompts_path}")

    raise SystemExit(result.exit_code)


cli = mod_modernization_scope
