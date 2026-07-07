"""forge-foundry-sync CLI entry point.

Vendored from forge-prowess-primitives@v0.4.0 — see CHANGELOG-VENDOR.md for divergence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from forge_sync.cache import get_primitives_path
from forge_sync.copier import copy_primitives
from forge_sync.idempotency import compute_project_state_hash
from forge_sync.manifest_reader import ManifestNotFound, ManifestParseError, read_manifest
from forge_sync.propagator import propagate_adapters, propagate_manifest_env

_STATE_FILE = ".foundry/.sync-state"


def _read_cached_hash(project_root: Path) -> str | None:
    """Return the previously stored state hash, or None if absent/unreadable."""
    state_path = project_root / _STATE_FILE
    try:
        return state_path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_state_hash(project_root: Path, h: str) -> None:
    """Persist *h* to .foundry/.sync-state."""
    state_path = project_root / _STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(h, encoding="utf-8")


@click.command()
@click.option(
    "--project-dir",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory (default: current directory).",
)
@click.option("--dry-run", is_flag=True, help="Print plan without writing anything.")
@click.option(
    "--primitives",
    default=None,
    help="Comma-separated list of primitive names to sync (default: all).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Skip idempotency check and always re-sync.",
)
def sync(project_dir: str, dry_run: bool, primitives: str | None, force: bool) -> None:
    """Sync Foundry primitives from the pinned version into this project.

    Populates .foundry/primitives/ with the primitive sources, then emits the
    per-harness skills into .claude/skills/, .github/skills/, .agents/skills/
    (Codex), and .agent/skills/ (Antigravity). For hybrid/Python-load-bearing
    primitives, CI is emitted at sync time from each primitive's ci.yaml
    (forge_sync/emitter/ci.py) into .github/workflows/forge-<command>.yml,
    azure-pipelines/, and buildspec/. The emitted GH Actions workflows are
    dormant (workflow_dispatch only) — opt into push/pull_request triggers
    yourself before relying on them.
    """
    project_root = Path(project_dir).resolve()

    # --- Read manifest ---
    try:
        manifest = read_manifest(project_root)
    except ManifestNotFound as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except ManifestParseError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    # --- Idempotency check ---
    if not force:
        current_hash = compute_project_state_hash(project_root)
        cached_hash = _read_cached_hash(project_root)
        if current_hash == cached_hash:
            ver = manifest.foundry_primitives_version
            click.echo(
                f"forge-foundry-sync: already up-to-date (version {ver}). Use --force to re-sync."
            )
            return

    # --- Resolve primitive names filter ---
    primitive_names: list[str] | None = None
    if primitives:
        primitive_names = [p.strip() for p in primitives.split(",") if p.strip()]

    if dry_run:
        ver = manifest.foundry_primitives_version
        click.echo(
            f"forge-foundry-sync (dry-run): would sync version {ver} "
            f"for project {manifest.project_id!r}"
        )
        if primitive_names:
            click.echo(f"  Primitives filter: {primitive_names}")
        return

    # --- Resolve library path from cache ---
    library_root = get_primitives_path(manifest.foundry_primitives_version)

    # --- Copy primitives ---
    copied = copy_primitives(
        library_root=library_root,
        project_root=project_root,
        manifest=manifest,
        primitive_names=primitive_names,
    )

    # --- Propagate adapters ---
    propagate_adapters(
        project_root=project_root,
        primitive_names=copied,
        enabled_harnesses=manifest.harnesses,
    )

    # --- Propagate manifest.env (HarnessBackend SPEC §13 OQ7 / issue #435) ---
    propagate_manifest_env(project_root=project_root, env=dict(manifest.env))

    # --- Write new state hash ---
    new_hash = compute_project_state_hash(project_root)
    _write_state_hash(project_root, new_hash)

    # --- Summary ---
    n = len(copied)
    click.echo(
        f"forge-foundry-sync: Synced {n} primitive{'s' if n != 1 else ''} "
        f"at version {manifest.foundry_primitives_version}."
    )
