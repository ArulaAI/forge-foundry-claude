"""R1 /rules-gen — orchestrator.

Implements Stage C of the R1 pipeline per
``primitives/rules_gen/SPEC.md`` §"Pipeline stages": load the requested
catalogs (Stage A), render each one to its two output strings (Stage B),
then write the rendered files into the consuming repo's
``.claude/rules/`` and ``.github/instructions/`` trees.

This module is the only Stage in the pipeline that touches the
filesystem of the consuming repo — Stages A and B are pure functions.
``--dry-run`` therefore short-circuits the writer entirely; the
returned :class:`GenerateResult` still reports which paths *would* have
been written so the CLI can print a useful preview.

Behaviour matrix for an existing target file:

* default (``merge=True`` / ``force=False``) → skip the file, append to
  ``GenerateResult.skipped``.
* ``force=True`` → overwrite unconditionally, append to
  ``GenerateResult.written``.

``merge=False`` and ``force=False`` together would surface as exit code 4
at the CLI layer (SPEC §"Exit codes"); this module never raises for that
case — it simply records the path under ``skipped`` so the CLI can
distinguish "I refused to overwrite" from "I successfully wrote".

The default catalog directory is the one shipped under
``primitives/rules_gen/shared/catalogs/``. Tests pass an explicit
``catalog_dir`` so they don't depend on the curated YAML landing in
later R1 issues.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from primitives.rules_gen.shared.catalog_loader import load_catalogs
from primitives.rules_gen.shared.rule_renderer import (
    render_claude_rule,
    render_copilot_instruction,
)

if TYPE_CHECKING:
    from primitives.rules_gen.shared.catalog_loader import Catalog

# Type alias for the renderer callable shape — both ``render_claude_rule``
# and ``render_copilot_instruction`` satisfy ``(Catalog) -> str``.
_RendererFn = Callable[["Catalog"], str]

__all__ = [
    "DEFAULT_CATALOG_DIR",
    "GenerateResult",
    "generate_rules",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Shipped catalog directory — the YAML files curated by R1 follow-on
# issues live here. Tests that don't want a dependency on the shipped
# catalogs pass an explicit ``catalog_dir`` argument.
DEFAULT_CATALOG_DIR: Path = Path(__file__).resolve().parent / "catalogs"

# Output-tree shapes per SPEC §"Output".
_CLAUDE_RULES_SUBDIR = Path(".claude") / "rules"
_COPILOT_INSTRUCTIONS_SUBDIR = Path(".github") / "instructions"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerateResult:
    """Outcome of a :func:`generate_rules` invocation.

    Attributes:
        written: Paths that were written (or, in dry-run mode, would have
            been written). Order matches the catalog iteration order: for
            each catalog the Claude Code file precedes its Copilot
            counterpart.
        skipped: Paths the writer chose not to overwrite because the file
            already existed and ``force`` was ``False``. Empty in
            ``--force`` mode.
        dry_run: Echo of the request flag. When ``True`` the filesystem
            was never touched, regardless of what ``written`` contains.
        merge_acknowledged: Echo of the ``merge`` request flag. Lets the
            CLI distinguish "the user opted in to skip-on-exists with
            ``--merge``" (exit 0 even if files were skipped) from "the
            user did not, and we hit a collision" (exit 4 per SPEC.md
            §"Exit codes"). The generator itself never raises for the
            collision case — that exit-code decision lives at the CLI.
    """

    written: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    dry_run: bool = False
    merge_acknowledged: bool = True


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------


def generate_rules(
    project_root: Path,
    profiles: list[str],
    output_dir: Path | None = None,
    *,
    dry_run: bool = False,
    merge: bool = True,
    force: bool = False,
    catalog_dir: Path | None = None,
) -> GenerateResult:
    """Generate dual-harness rule files for *profiles*.

    Args:
        project_root: Root of the consuming repo. Used only when
            ``output_dir`` is omitted (then ``output_dir`` defaults to it).
            Kept distinct from ``output_dir`` so a caller can stage
            generated rules elsewhere (e.g. a build dir) while preserving
            the original repo root for future use.
        profiles: Catalog name slugs to render, e.g.
            ``["bfsi-core", "java", "aws"]``. Each must resolve to
            ``<catalog_dir>/<name>.yaml`` or ``<name>.yml``.
        output_dir: Root under which ``.claude/rules/`` and
            ``.github/instructions/`` are written. Defaults to
            ``project_root``.
        dry_run: If ``True``, no files are written. The returned
            :class:`GenerateResult` still enumerates the paths that
            *would* have been written under ``written``.
        merge: If ``True`` (the default) and a target file exists, skip
            it (record under ``skipped``). If ``False``, the writer still
            refuses to overwrite — only ``force=True`` overwrites. This
            mirrors the SPEC.md exit-code surface: code 4 is "target
            exists and neither --merge nor --force was supplied", which
            the CLI emits when it sees ``skipped`` non-empty without
            ``merge`` being asserted.
        force: If ``True``, overwrite existing target files. Takes
            precedence over ``merge``.
        catalog_dir: Override for the curated catalog directory. Defaults
            to the shipped ``primitives/rules_gen/shared/catalogs/``.

    Returns:
        :class:`GenerateResult` enumerating written + skipped paths.

    Raises:
        primitives.rules_gen.shared.catalog_loader.CatalogNotFoundError:
            ``catalog_dir`` does not exist or a requested profile name
            has no matching file there.
        primitives.rules_gen.shared.catalog_loader.CatalogParseError:
            A matched catalog file is malformed.
    """
    resolved_catalog_dir = catalog_dir if catalog_dir is not None else DEFAULT_CATALOG_DIR
    resolved_output = output_dir if output_dir is not None else project_root

    catalogs = load_catalogs(resolved_catalog_dir, profiles)

    claude_dir = resolved_output / _CLAUDE_RULES_SUBDIR
    copilot_dir = resolved_output / _COPILOT_INSTRUCTIONS_SUBDIR

    written: list[Path] = []
    skipped: list[Path] = []

    for catalog in catalogs:
        claude_path = claude_dir / f"{catalog.name}.md"
        copilot_path = copilot_dir / f"{catalog.name}.instructions.md"

        _emit_one(
            catalog=catalog,
            target=claude_path,
            renderer=render_claude_rule,
            dry_run=dry_run,
            force=force,
            written=written,
            skipped=skipped,
        )
        _emit_one(
            catalog=catalog,
            target=copilot_path,
            renderer=render_copilot_instruction,
            dry_run=dry_run,
            force=force,
            written=written,
            skipped=skipped,
        )

    return GenerateResult(
        written=written,
        skipped=skipped,
        dry_run=dry_run,
        merge_acknowledged=merge,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _emit_one(
    *,
    catalog: Catalog,
    target: Path,
    renderer: _RendererFn,
    dry_run: bool,
    force: bool,
    written: list[Path],
    skipped: list[Path],
) -> None:
    """Render a single (catalog, target) pair and write or skip per flags.

    The renderer is passed in so this helper handles both the Claude
    Code and the Copilot output without duplicating the dry-run / merge /
    force decision tree.
    """
    if target.exists() and not force:
        skipped.append(target)
        return

    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(renderer(catalog), encoding="utf-8")

    written.append(target)
