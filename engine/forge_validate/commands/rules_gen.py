"""Forge Foundry — ``forge-validate rules-gen`` subcommand (R1).

Thin wrapper over :func:`primitives.rules_gen.shared.generator.generate_rules`.
Loads the requested catalog profile(s), renders each catalog to its
two harness-native files, and writes them under
``<output_dir>/.claude/rules/`` and ``<output_dir>/.github/instructions/``.

Two reporting modes:

* ``--format list`` (default) — one line per file written or skipped,
  prefixed with ``WROTE`` / ``SKIP`` / ``WOULD WRITE`` (the last in
  ``--dry-run`` mode).
* ``--format detail`` — the same header lines, then the full rendered
  content of each file (separated by a thematic break). Useful for
  ``--dry-run`` previews when a reviewer wants to see exactly what
  ``rules-gen`` would write.

Exit codes:

* ``0`` — success
* ``1`` — no profiles supplied (``--profile`` missing or empty)
* ``2`` — requested catalog not found, the catalog directory itself is
  missing, or ``project_root`` does not exist
* ``3`` — a matched catalog file failed schema validation
* ``4`` — a target file already exists and neither ``--merge`` nor
  ``--force`` was supplied
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from primitives.rules_gen.shared.catalog_loader import (
    CatalogNotFoundError,
    CatalogParseError,
)
from primitives.rules_gen.shared.generator import GenerateResult, generate_rules
from primitives.rules_gen.shared.rule_renderer import (
    render_claude_rule,
    render_copilot_instruction,
)


@click.command("rules-gen")
@click.argument("project_root", type=click.Path(exists=False))
@click.option(
    "--profile",
    "profile",
    type=str,
    default=None,
    help="Comma-separated catalog names (e.g. 'bfsi-core,java,aws'). Required.",
)
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(),
    default=None,
    help="Where to write rules. Defaults to PROJECT_ROOT.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would be generated without writing any files.",
)
@click.option(
    "--format",
    "format_",
    type=click.Choice(["list", "detail"]),
    default="list",
    help="Output verbosity (default: list).",
)
@click.option(
    "--merge",
    is_flag=True,
    default=False,
    help="Skip existing files rather than treating the collision as a hard error.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite existing rule files. Takes precedence over --merge.",
)
@click.option(
    "--catalog-dir",
    "catalog_dir",
    type=click.Path(exists=False),
    default=None,
    help="Override the shipped catalog directory (mainly for tests).",
)
def cli(
    project_root: str,
    profile: str | None,
    output_dir: str | None,
    dry_run: bool,
    format_: str,
    merge: bool,
    force: bool,
    catalog_dir: str | None,
) -> None:
    """Generate dual-harness coding rules for a consuming repo (R1 /rules-gen)."""
    # --- Profile validation (exit 1) -------------------------------------
    profile_names = _parse_profile(profile)
    if not profile_names:
        click.echo(
            "Error: --profile is required (comma-separated catalog names, "
            "e.g. 'bfsi-core,java,aws').",
            err=True,
        )
        sys.exit(1)

    # --- project_root validation (exit 2) --------------------------------
    resolved_root = Path(project_root)
    if not resolved_root.exists():
        click.echo(f"Error: project_root not found: {resolved_root}", err=True)
        sys.exit(2)

    resolved_output = Path(output_dir) if output_dir else None
    resolved_catalog = Path(catalog_dir) if catalog_dir else None

    # --- Run the generator -----------------------------------------------
    try:
        result = generate_rules(
            resolved_root,
            profile_names,
            output_dir=resolved_output,
            dry_run=dry_run,
            merge=merge,
            force=force,
            catalog_dir=resolved_catalog,
        )
    except CatalogNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)
    except CatalogParseError as exc:
        click.echo(f"Error: catalog failed validation: {exc}", err=True)
        sys.exit(3)

    # --- Render the report -----------------------------------------------
    _print_report(
        result,
        format_=format_,
        profile_names=profile_names,
        catalog_dir=resolved_catalog,
    )

    # --- Exit-code surface for file collisions (exit 4) ------------------
    # If files were skipped because they already existed AND the user did
    # not opt in to either --merge or --force, treat that as the SPEC
    # exit-4 condition.
    if result.skipped and not result.merge_acknowledged and not force:
        sys.exit(4)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_profile(raw: str | None) -> list[str]:
    """Split the comma-separated --profile value into a clean list.

    Empty entries (e.g. trailing commas, double commas) are dropped so
    ``"bfsi-core,,java"`` resolves cleanly. An empty or whitespace-only
    string yields an empty list, which the caller surfaces as exit 1.
    """
    if not raw:
        return []
    return [n.strip() for n in raw.split(",") if n.strip()]


def _print_report(
    result: GenerateResult,
    *,
    format_: str,
    profile_names: list[str],
    catalog_dir: Path | None,
) -> None:
    """Emit the CLI report for *result* in either ``list`` or ``detail`` form.

    ``list`` prints one line per target, tagged with the writer outcome
    (``WROTE`` / ``WOULD WRITE`` / ``SKIP``). ``detail`` adds the full
    rendered content beneath each written entry, separated by a markdown
    thematic break so a reviewer can pipe the output to a pager.
    """
    written_prefix = "WOULD WRITE" if result.dry_run else "WROTE"

    for path in result.written:
        click.echo(f"{written_prefix} {path}")
    for path in result.skipped:
        click.echo(f"SKIP {path}")

    if format_ == "detail":
        _print_detail(
            result,
            profile_names=profile_names,
            catalog_dir=catalog_dir,
        )


def _print_detail(
    result: GenerateResult,
    *,
    profile_names: list[str],
    catalog_dir: Path | None,
) -> None:
    """Print the full rendered content of every *written* file.

    In dry-run mode the files don't exist on disk yet — we re-render from
    the catalogs so the preview matches what would be written. In a real
    run we read the freshly written file straight from disk so the output
    matches what the user will see if they open the file themselves.
    """
    if result.dry_run:
        _print_detail_via_rerender(profile_names, catalog_dir)
        return

    for path in result.written:
        click.echo("\n---\n")
        click.echo(f"# {path}\n")
        click.echo(path.read_text(encoding="utf-8"))


def _print_detail_via_rerender(
    profile_names: list[str],
    catalog_dir: Path | None,
) -> None:
    """Re-render each catalog's two outputs so dry-run --format detail works.

    Reading from disk doesn't help in dry-run mode (we didn't write
    anything). The catalog files are already validated by the time we
    reach this function, so a second ``load_catalogs`` call is safe and
    cheap.
    """
    # Lazy import to keep the CLI cold-start cheap when --format detail
    # is not requested.
    from primitives.rules_gen.shared.catalog_loader import (  # noqa: PLC0415
        load_catalogs,
    )
    from primitives.rules_gen.shared.generator import (  # noqa: PLC0415
        DEFAULT_CATALOG_DIR,
    )

    resolved_dir = catalog_dir if catalog_dir is not None else DEFAULT_CATALOG_DIR
    for catalog in load_catalogs(resolved_dir, profile_names):
        click.echo("\n---\n")
        click.echo(f"# {catalog.name} (Claude Code)\n")
        click.echo(render_claude_rule(catalog))
        click.echo("\n---\n")
        click.echo(f"# {catalog.name} (Copilot)\n")
        click.echo(render_copilot_instruction(catalog))
