"""Forge Foundry — `forge-validate validate` subcommand (slug naming + manifest).

Was inlined in `forge_validate/cli.py` before #101 / TOOL-2.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from forge_sync.manifest_reader import (
    ManifestNotFound,
    ManifestParseError,
    read_manifest,
)
from forge_validate.checks.naming import NamingViolation, parse_repo_slug


@click.command()
@click.argument("project_dir", required=False, type=click.Path(exists=True, file_okay=False))
@click.option(
    "--naming-only",
    is_flag=True,
    default=False,
    help="Only run naming convention checks. Use with --slug or pointing at a project dir.",
)
@click.option(
    "--slug",
    type=str,
    default=None,
    help="Validate a single slug string (use with --naming-only).",
)
def cli(project_dir: str | None, naming_only: bool, slug: str | None) -> None:
    """Validate a consuming project's slug + .foundry/manifest.yaml."""
    exit_code = 0

    if naming_only and slug:
        try:
            parse_repo_slug(slug)
            click.echo(f"✓ slug `{slug}` is valid")
        except NamingViolation as exc:
            click.echo(f"✗ {exc}", err=True)
            sys.exit(1)
        return

    if project_dir is None:
        click.echo("Error: provide a project directory or use --naming-only --slug", err=True)
        sys.exit(2)

    project_root = Path(project_dir)
    click.echo(f"→ Validating {project_root}")

    try:
        parse_repo_slug(project_root.name)
        click.echo(f"  ✓ slug `{project_root.name}` valid")
    except NamingViolation as exc:
        click.echo(f"  ✗ {exc}", err=True)
        exit_code = 1

    if naming_only:
        sys.exit(exit_code)

    try:
        manifest = read_manifest(project_root)
        click.echo(
            f"  ✓ .foundry/manifest.yaml schema valid "
            f"(pinned to v{manifest.foundry_primitives_version})"
        )
    except ManifestNotFound as exc:
        click.echo(f"  ✗ {exc}", err=True)
        exit_code = 1
    except ManifestParseError as exc:
        click.echo(f"  ✗ {exc}", err=True)
        exit_code = 1

    if exit_code == 0:
        click.echo("✓ PASS — project is naming + manifest compliant")
    else:
        click.echo("✗ FAIL — see violations above", err=True)

    sys.exit(exit_code)
