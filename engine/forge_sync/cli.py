"""forge-foundry-sync CLI entry point.

Vendored from forge-prowess-primitives@v0.4.0 — see CHANGELOG-VENDOR.md for divergence.
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import click

from forge_sync.cache import default_repo_url, get_primitives_path_for
from forge_sync.copier import copy_primitives
from forge_sync.frozen import frozen_primitives_root
from forge_sync.idempotency import compute_project_state_hash
from forge_sync.manifest_reader import (
    ManifestNotFound,
    ManifestParseError,
    SyncManifest,
    read_manifest,
)
from forge_sync.propagator import propagate_adapters, propagate_manifest_env

_STATE_FILE = ".foundry/.sync-state"

# Operator escape hatch: point the sync at a locally staged/vetted primitives
# library root (a directory containing ``primitives/<name>/skill/SKILL.md``).
# Takes precedence over every other source, frozen or not (#1419 T2).
_PRIMITIVES_PATH_ENV = "FOUNDRY_PRIMITIVES_PATH"

# forge-foundry-sync exit code 8: CLI/manifest major version skew.
# The installed forge-validate CLI is a major version behind the manifest pin —
# renamed/new subcommands are genuinely missing.  Exit non-zero so CI/scripts catch it.
# Minor/patch skews remain a soft warning (exit 0).
EXIT_MAJOR_VERSION_SKEW = 8


def _installed_cli_version() -> str | None:
    """The version of the installed ``forge-foundry-primitives`` package, or None."""
    try:
        return version("forge-foundry-primitives")
    except PackageNotFoundError:  # pragma: no cover — running from a source checkout
        return None


def _normalize_version(v: str) -> str:
    """Strip a leading ``v`` (manifest pins are ``v6.0.0``; metadata is ``6.0.0``)."""
    return v[1:] if v.startswith(("v", "V")) else v


def _parse_major(v: str) -> int | None:
    """Return the major component of a ``MAJOR.MINOR.PATCH`` version string, or None."""
    try:
        return int(v.split(".", maxsplit=1)[0])
    except (ValueError, IndexError):
        return None


def _check_cli_version_skew(manifest_version: str) -> bool:
    """Detect and report a CLI/manifest version skew (#1352, #1407).

    Prints a warning on any version mismatch and returns ``True`` when the skew is
    a **major-version regression** (installed CLI major < manifest major) — that is
    the only case where renamed/new subcommands are genuinely absent and the operator
    MUST reinstall before the synced skills will work.

    Returns ``False`` for no skew, minor/patch skew (soft warning, exit 0 still
    applies), or any case where a comparison can't be made (dev checkout, non-semver
    pin).

    ``forge-foundry-sync`` updates ``.foundry/``/``.claude/skills/`` from the pinned
    tag, but the globally-installed ``forge-validate`` / ``forge-foundry-sync``
    executables stay at whatever tag they were ``uv tool install``-ed from. When they
    diverge, v7.0.0 skills run against a stale CLI that lacks the renamed/new
    subcommands — a silent failure. We warn (never auto-mutate the user's global tool
    install) with the exact reinstall command. A local/dev version (metadata absent, or
    a non-pin manifest such as a filesystem path) is skipped — the comparison only makes
    sense for a tagged pin.
    """
    installed = _installed_cli_version()
    if installed is None:
        return False
    pinned = _normalize_version(manifest_version)
    # Only compare against a semver-shaped tag pin (``v6.0.0`` / ``6.0.0``); a fork
    # branch name or filesystem version isn't a CLI-tool tag to reconcile against.
    if not pinned or not pinned[0].isdigit():
        return False
    installed_norm = _normalize_version(installed)
    if installed_norm == pinned:
        return False

    reinstall_cmd = (
        f"    uv tool install --force "
        f"'forge-foundry-primitives @ "
        f"git+https://github.com/arula-ai/forge-foundry-primitives@{manifest_version}'"
    )

    installed_major = _parse_major(installed_norm)
    pinned_major = _parse_major(pinned)
    is_major_skew = (
        installed_major is not None and pinned_major is not None and installed_major < pinned_major
    )

    if is_major_skew:
        click.echo(
            f"ERROR: forge-foundry-sync: CLI/manifest major version skew — the installed "
            f"forge-validate CLI is {installed} but the manifest pins "
            f"{manifest_version}. The synced skills expect the manifest version's "
            f"subcommands; renamed/new commands will be missing until you reinstall:\n"
            f"{reinstall_cmd}\n"
            f"Sync aborted. Run the reinstall command above, then re-run forge-foundry-sync.",
            err=True,
        )
    else:
        click.echo(
            f"⚠️  forge-foundry-sync: CLI/manifest version skew — the installed "
            f"forge-validate CLI is {installed} but the manifest pins "
            f"{manifest_version}. The synced skills expect the manifest version's "
            f"subcommands; the CLI will be missing renamed/new commands until you "
            f"reinstall it:\n"
            f"{reinstall_cmd}",
            err=True,
        )
    return is_major_skew


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


def _validate_primitives_path(raw: str) -> Path:
    """Validate + resolve an operator-supplied ``--primitives-path`` value (#1419 T2).

    The value is operator-supplied (CLI flag or ``FOUNDRY_PRIMITIVES_PATH`` env),
    so it is constrained before use — mirroring the SEC-7 discipline the copier and
    cache apply to manifest-sourced paths:

    * ``..`` segments are rejected on the **raw** string, *before* ``.resolve()``
      collapses them (a ``..``-before-``.resolve()`` guard).
    * The resolved path must exist and be a directory.
    * It must be a primitives **library root** — i.e. contain at least one
      ``primitives/<name>/skill/SKILL.md`` — so a stray directory can't be handed
      to the copier (which does ``library_root / "primitives"``) and silently emit
      nothing.

    Unlike the frozen embedded path (read-only, vetted at build time), this is a
    live filesystem directory, so it gets full validation. The copier's own
    ``_ignore_git_and_symlinks`` (SEC-10 symlink skip) still governs the copy from
    the returned root — this validation does not weaken any downstream guard.

    Returns the resolved library-root path.

    Raises:
        ValueError: if the raw value contains a ``..`` segment.
        click.ClickException: if the path is missing, not a directory, or is not a
            primitives library root.
    """
    if ".." in Path(raw).parts:
        raise ValueError(
            f"--primitives-path contains a path-traversal segment: {raw!r} "
            "— must not contain a '..' segment (SEC-7)"
        )
    root = Path(raw).resolve()
    if not root.exists():
        raise click.ClickException(f"--primitives-path does not exist: {root}")
    if not root.is_dir():
        raise click.ClickException(f"--primitives-path is not a directory: {root}")
    primitives_dir = root / "primitives"
    if not primitives_dir.is_dir() or not any(
        (child / "skill" / "SKILL.md").is_file()
        for child in primitives_dir.iterdir()
        if child.is_dir()
    ):
        raise click.ClickException(
            f"--primitives-path is not a primitives library root: {root} "
            "(expected a 'primitives/<name>/skill/SKILL.md' under it)"
        )
    return root


def _resolve_library_root(manifest: SyncManifest, primitives_path: str | None) -> Path:
    """Decide where the primitive library comes from (#1419 T2).

    The single point where "where do primitives come from?" is decided. The
    fallback order, highest precedence first:

    1. ``--primitives-path`` / ``FOUNDRY_PRIMITIVES_PATH`` — explicit operator
       override to a locally staged/vetted library root. Validated
       (:func:`_validate_primitives_path`).
    2. ``manifest.foundry_primitives_source`` — an internal git-mirror URL (#1282).
       Cloned into the cache exactly as before, whether frozen or not. In a fully
       air-gapped build with no git this fails loudly at clone time — that is the
       intended behavior of an opt-in escape hatch.
    3. **Embedded frozen copy** — when running as a frozen PyInstaller binary and
       neither (1) nor (2) is set, emit from the bundled ``primitives/`` tree
       (``sys._MEIPASS/primitives``, via :func:`frozen_primitives_root`). This is
       the zero-config air-gapped default; it does **not** git-clone. The binary is
       already the pinned version, so a ``foundry_primitives_version`` mismatch is
       advisory (WARNING), not a re-clone.
    4. **Git clone** — not frozen, no overrides: today's behavior byte-for-byte,
       ``get_primitives_path_for(default_repo_url(), version)``.

    Returns a **library root** (a directory containing ``primitives/``), which is
    what :func:`forge_sync.copier.copy_primitives` expects.
    """
    # (1) explicit operator override — highest priority, frozen or not.
    if primitives_path:
        return _validate_primitives_path(primitives_path)

    # (2) internal git-mirror URL from the manifest — honored frozen or not.
    if manifest.foundry_primitives_source:
        return get_primitives_path_for(
            manifest.foundry_primitives_source,
            manifest.foundry_primitives_version,
        )

    # (3) embedded frozen copy — the default air-gapped path (no clone).
    embedded = frozen_primitives_root()
    if embedded is not None:
        bundled_version = _installed_cli_version()
        pinned = manifest.foundry_primitives_version
        if bundled_version is not None and _normalize_version(
            bundled_version
        ) != _normalize_version(pinned):
            click.echo(
                f"⚠️  forge-foundry-sync: the manifest pins {pinned} but this frozen "
                f"binary embeds primitives at version {bundled_version}. The embedded "
                "copy is used as-is (no clone). Rebuild the binary or point "
                "--primitives-path / foundry_primitives_source at the pinned version to "
                "change it.",
                err=True,
            )
        # frozen_primitives_root() returns the embedded ``primitives/`` dir; the
        # copier wants the library root (its parent), then re-derives ``primitives``.
        return embedded.parent

    # (4) not frozen, no overrides — today's default, unchanged.
    return get_primitives_path_for(default_repo_url(), manifest.foundry_primitives_version)


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
@click.option(
    "--primitives-path",
    default=None,
    help=(
        "Local primitives library root to sync FROM (a directory containing "
        "primitives/<name>/skill/SKILL.md). Overrides the manifest source and the "
        "embedded/frozen copy. Also settable via FOUNDRY_PRIMITIVES_PATH."
    ),
)
def sync(
    project_dir: str,
    dry_run: bool,
    primitives: str | None,
    force: bool,
    primitives_path: str | None,
) -> None:
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

    # --- Check CLI-vs-manifest version skew (#1352, #1407) ---
    # Major skew → exit 8 (subcommands genuinely missing); minor/patch → soft warning only.
    if _check_cli_version_skew(manifest.foundry_primitives_version):
        sys.exit(EXIT_MAJOR_VERSION_SKEW)

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

    # --- Resolve the primitive library root (#1419 T2 fallback order) ---
    # Precedence: --primitives-path / FOUNDRY_PRIMITIVES_PATH  >  manifest
    # foundry_primitives_source (clone)  >  embedded frozen copy  >  git clone.
    effective_primitives_path = primitives_path or os.environ.get(_PRIMITIVES_PATH_ENV)
    try:
        library_root = _resolve_library_root(manifest, effective_primitives_path)
    except ValueError as exc:
        # SEC-7 traversal reject on an operator-supplied --primitives-path.
        click.echo(f"Error: {exc}", err=True)
        sys.exit(3)

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
