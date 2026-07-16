"""Forge Foundry — `forge-validate repo-digest` subcommand.

Was inlined in `forge_validate/cli.py` before #101 / TOOL-2.

Skill-first (--extract-only / --continue) path added in #780.  Only the
--produce branch supports skill-first; --federate is deterministic-only.
When config.mode != "llm" the deterministic path is taken unconditionally
and no LLM infrastructure is initialised.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from forge_validate._cli_common import (
    continue_option,
    extract_only_option,
    judgments_option,
    state_dir_option,
    validate_skill_first_flags,
)
from primitives.repo_digest import (
    DigestConfig,
    federate_peers,
)


@click.command("repo-digest")
@click.argument("path", type=click.Path(exists=True))
@click.option("--produce", "produce_flag", is_flag=True, default=False)
@click.option("--federate", "federate_flag", is_flag=True, default=False)
@click.option("--output-dir", type=click.Path(), default=None)
@click.option("--shallow", is_flag=True, default=False)
@click.option("--max-tokens", type=int, default=None)
@click.option("--format", "format_", default="markdown", type=click.Choice(["markdown"]))
@click.option(
    "--include-vendored",
    is_flag=True,
    default=False,
    help="Include emitted/vendored harness trees (.foundry, .claude, .github/skills, …) "
    "in the source-file walks. Dependency/build dirs stay excluded.",
)
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(  # noqa: PLR0913
    path: str,
    produce_flag: bool,
    federate_flag: bool,
    output_dir: str | None,
    shallow: bool,
    max_tokens: int | None,
    format_: str,
    include_vendored: bool,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """Generate or federate repo digest files."""
    # --- Validate skill-first flags (--continue requires --judgments) ---
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )

    if produce_flag == federate_flag:
        # both False or both True
        click.echo("Error: exactly one of --produce or --federate is required", err=True)
        sys.exit(2)

    project_root = Path(path).resolve()
    config = DigestConfig.from_project(project_root)
    if max_tokens is not None:
        config = config.model_copy(
            update={"budget": config.budget.model_copy(update={"total_tokens": max_tokens})}
        )

    # --- federate branch (always deterministic, no skill-first path) ----------
    if federate_flag:
        result = federate_peers(project_root, config=config)
        click.echo(
            f"Federated {len(result)} peer(s). Files in {project_root}/.foundry/context/peers/."
        )
        return

    # =========================================================================
    # --produce branch
    # =========================================================================

    # Lazy imports to keep cold-start cheap (TOOL-2).
    from primitives._shared.pipeline_state import (  # noqa: PLC0415
        PipelineState,
        cleanup_state,
        load_intermediate,
        save_intermediate,
        save_state,
    )
    from primitives._shared.protocol import (  # noqa: PLC0415
        PromptEnvelope,
        read_judgments,
        write_prompts,
    )
    from primitives.repo_digest.shared.extractors.architecture import (  # noqa: PLC0415
        extract_architecture,
    )
    from primitives.repo_digest.shared.extractors.recent_deltas import (  # noqa: PLC0415
        extract_recent_deltas,
    )
    from primitives.repo_digest.shared.llm_prose import (  # noqa: PLC0415
        apply_prose_judgment,
        build_prompt_items_for_prose,
    )
    from primitives.repo_digest.shared.produce import produce_digest  # noqa: PLC0415

    out = Path(output_dir) if output_dir else project_root / ".foundry" / "context"
    resolved_state_dir = Path(state_dir) if state_dir is not None else out / ".state"

    # =========================================================================
    # Branch: --continue --judgments (resume after in-session judgment)
    # =========================================================================
    if continue_:
        # Load intermediates saved during --extract-only
        arch_content: str = load_intermediate("architecture", resolved_state_dir)
        deltas_content: str = load_intermediate("recent_deltas", resolved_state_dir)

        # Read judgments
        assert judgments is not None  # guaranteed by validate_skill_first_flags
        judgment_envelope = read_judgments(Path(judgments))
        judgment_by_id = judgment_envelope.by_id()

        # Apply prose judgments using the grounding guard
        arch_judgment = judgment_by_id.get("narrative:architecture")
        if arch_judgment is not None and isinstance(arch_judgment.response, dict):
            arch_content = apply_prose_judgment(
                arch_content,
                section="architecture",
                response=arch_judgment.response,
                position="top",
            )

        deltas_judgment = judgment_by_id.get("narrative:recent-deltas")
        if deltas_judgment is not None and isinstance(deltas_judgment.response, dict):
            deltas_content = apply_prose_judgment(
                deltas_content,
                section="recent-deltas",
                response=deltas_judgment.response,
                position="bottom",
            )

        # Load remaining deterministic intermediates and write all files
        from primitives.repo_digest.shared.extractors.api_surface import (  # noqa: PLC0415
            extract_api_surface,
        )
        from primitives.repo_digest.shared.extractors.conventions import (  # noqa: PLC0415
            extract_conventions,
        )
        from primitives.repo_digest.shared.extractors.dependency_map import (  # noqa: PLC0415
            extract_dependency_map,
        )
        from primitives.repo_digest.shared.extractors.entry_points import (  # noqa: PLC0415
            extract_entry_points,
        )

        out.mkdir(parents=True, exist_ok=True)
        files: list[tuple[str, str]] = [
            ("repo-digest.md", arch_content),
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
                extract_entry_points(
                    project_root, config=config, include_vendored=include_vendored
                ),
            ),
            ("recent-deltas.md", deltas_content),
            (
                "dependency-map.md",
                extract_dependency_map(
                    project_root, config=config, include_vendored=include_vendored
                ),
            ),
        ]
        written: list[Path] = []
        for name, content in files:
            target = out / name
            target.write_text(content, encoding="utf-8")
            written.append(target)

        cleanup_state(resolved_state_dir, explicit=(state_dir is not None))
        click.echo(f"Wrote {len(written)} digest files to {out}")
        return

    # =========================================================================
    # Branch: --extract-only (emit prompts and exit 7, or exit 0 if non-llm)
    # =========================================================================
    if extract_only:
        # If mode is not "llm", run deterministically and exit 0 — no prompts needed.
        if config.mode != "llm":
            written = produce_digest(
                project_root, config=config, output_dir=out, include_vendored=include_vendored
            )
            dest = written[0].parent if written else out
            click.echo(f"Wrote {len(written)} digest files to {dest}")
            return

        # mode == "llm": run deterministic sections, save intermediates, emit prompts.
        arch = extract_architecture(project_root, config=config, include_vendored=include_vendored)
        deltas = extract_recent_deltas(project_root, config=config)

        # Persist the pre-prose section texts for --continue to pick up.
        save_intermediate("architecture", arch, resolved_state_dir)
        save_intermediate("recent_deltas", deltas, resolved_state_dir)

        # Build prompt items (0-2 items depending on content).
        prompt_items = build_prompt_items_for_prose(
            architecture_content=arch if arch else None,
            deltas_content=deltas if deltas else None,
        )

        if not prompt_items:
            # Both sections are empty — run remaining stages and exit 0.
            written = produce_digest(
                project_root, config=config, output_dir=out, include_vendored=include_vendored
            )
            dest = written[0].parent if written else out
            click.echo(f"Wrote {len(written)} digest files to {dest}")
            return

        envelope = PromptEnvelope(
            primitive="repo-digest",
            round=1,
            items=prompt_items,
        )
        prompts_path = resolved_state_dir / "round-1-prompts.json"
        write_prompts(envelope, prompts_path)
        click.echo(f"WROTE {prompts_path}")

        save_state(
            PipelineState(
                primitive="repo-digest",
                round=1,
                completed_stages=["extract"],
                next_stage="B_continue",
                status="awaiting_judgments",
            ),
            resolved_state_dir,
        )

        sys.exit(7)

    # =========================================================================
    # Branch: normal (deterministic) mode
    # =========================================================================
    # LLM prose augmentation runs only via the skill-first --extract-only /
    # --continue path; normal mode writes the deterministic digest sections.
    written = produce_digest(
        project_root, config=config, output_dir=out, include_vendored=include_vendored
    )
    dest = written[0].parent if written else out
    click.echo(f"Wrote {len(written)} digest files to {dest}")
