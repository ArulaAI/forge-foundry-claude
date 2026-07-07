"""C1 — produce_digest() composer.

Runs all 6 extractors and writes their output to
<project_root>/.foundry/context/ (or a custom output_dir).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives.repo_digest.shared.config import DigestConfig
from primitives.repo_digest.shared.extractors.api_surface import extract_api_surface
from primitives.repo_digest.shared.extractors.architecture import extract_architecture
from primitives.repo_digest.shared.extractors.conventions import extract_conventions
from primitives.repo_digest.shared.extractors.dependency_map import extract_dependency_map
from primitives.repo_digest.shared.extractors.entry_points import extract_entry_points
from primitives.repo_digest.shared.extractors.recent_deltas import extract_recent_deltas

if TYPE_CHECKING:
    from pathlib import Path


def produce_digest(
    project_root: Path,
    *,
    config: DigestConfig | None = None,
    output_dir: Path | None = None,
    include_vendored: bool = False,
) -> list[Path]:
    """Run all 6 extractors and write digest files. Return list of written paths.

    Args:
        project_root: Project root to analyze.
        config: DigestConfig (defaults if None).
        output_dir: Override default <project_root>/.foundry/context/.
        include_vendored: When True, dependency/build dirs are still excluded but
            the emitted/vendored harness trees (.foundry, .claude, .github/skills,
            …) are re-included in the source-file walks.

    The deterministic extractors run unconditionally; the optional LLM prose
    augmentation now runs in-session via the skill-first protocol
    (``build_prompt_items_for_prose`` / ``apply_prose_judgment``), not here.
    """
    if config is None:
        config = DigestConfig()

    project_root = project_root.resolve()
    target_dir = output_dir or (project_root / ".foundry" / "context")
    target_dir.mkdir(parents=True, exist_ok=True)

    arch = extract_architecture(project_root, config=config, include_vendored=include_vendored)
    deltas = extract_recent_deltas(project_root, config=config)

    files: list[tuple[str, str]] = [
        ("repo-digest.md", arch),
        (
            "conventions.md",
            extract_conventions(project_root, config=config, include_vendored=include_vendored),
        ),
        (
            "api-surface.md",
            extract_api_surface(project_root, config=config, include_vendored=include_vendored),
        ),
        (
            "entry-points.md",
            extract_entry_points(project_root, config=config, include_vendored=include_vendored),
        ),
        ("recent-deltas.md", deltas),
        (
            "dependency-map.md",
            extract_dependency_map(project_root, config=config, include_vendored=include_vendored),
        ),
    ]

    written: list[Path] = []
    for name, content in files:
        target = target_dir / name
        target.write_text(content, encoding="utf-8")
        written.append(target)
    return written
