"""Skill-first pipeline state machine.

Persists pipeline progress between --extract-only and --continue
invocations. The state directory holds pipeline.json (current stage),
intermediate outputs, and prompts/judgments files.

See docs/superpowers/specs/2026-06-18-skill-first-harness-elimination-design.md §2.4.
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

import click
from pydantic import BaseModel, Field


class PipelineState(BaseModel, frozen=True):
    """Tracks which stages have completed and what comes next."""

    primitive: str
    round: int = Field(ge=1)
    completed_stages: list[str]
    next_stage: str | None
    status: Literal["running", "awaiting_judgments", "done"]


_PIPELINE_FILENAME = "pipeline.json"
_INTERMEDIATES_DIR = "intermediates"


class PipelineStateNotFoundError(click.ClickException):
    """No pipeline state exists at the expected location (#885).

    Raised by :func:`load_state` when ``--continue`` (or a re-run of
    ``--continue --format json`` after the run already finalized and cleaned up
    its state) finds no ``pipeline.json``. Subclasses
    :class:`click.ClickException` so Click renders the message to stderr and
    exits 1 — a friendly, actionable message rather than a raw
    ``FileNotFoundError`` traceback — for both the real CLI and ``CliRunner``.
    """

    exit_code = 1


def save_state(state: PipelineState, state_dir: Path) -> None:
    """Write pipeline state to state_dir/pipeline.json."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / _PIPELINE_FILENAME).write_text(
        state.model_dump_json(indent=2),
        encoding="utf-8",
    )


def load_state(state_dir: Path) -> PipelineState:
    """Read pipeline state from state_dir/pipeline.json.

    Raises :class:`PipelineStateNotFoundError` (not a raw ``FileNotFoundError``)
    with an actionable message when no state exists — e.g. the run already
    finalized and cleaned up its state, or ``--extract-only`` was never run
    (#885).
    """
    path = state_dir / _PIPELINE_FILENAME
    if not path.exists():
        raise PipelineStateNotFoundError(
            f"no pipeline state at {state_dir} — the run already finalized "
            "(state is cleaned up after the final --continue) or --extract-only "
            "was not run. Re-run with --extract-only to start a new round, then "
            "--continue --judgments <path> to resume."
        )
    return PipelineState.model_validate_json(path.read_text(encoding="utf-8"))


def save_intermediate(name: str, data: Any, state_dir: Path) -> None:
    """Persist a stage's intermediate output for later resumption."""
    out_dir = state_dir / _INTERMEDIATES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    # NOTE: default=str silently stringifies non-JSON-serialisable values (e.g.
    # datetime, Path, Decimal).  Callers must pass fully-serialisable data or
    # the round-trip via load_intermediate will return strings instead of the
    # original type (foot-gun; no error is raised at write time).
    (out_dir / f"{name}.json").write_text(
        json.dumps(data, indent=2, default=str),
        encoding="utf-8",
    )


def load_intermediate(name: str, state_dir: Path) -> Any:
    """Load a previously saved intermediate output.

    Raises :class:`PipelineStateNotFoundError` (a friendly ``click.ClickException``,
    not a raw ``FileNotFoundError``) when the intermediate is absent — e.g. the run
    already finalized and cleaned up its state, or ``--extract-only`` was not run
    (#885).
    """
    path = state_dir / _INTERMEDIATES_DIR / f"{name}.json"
    if not path.exists():
        raise PipelineStateNotFoundError(
            f"no pipeline state at {state_dir} (missing intermediate '{name}') — "
            "the run already finalized (state is cleaned up after the final "
            "--continue) or --extract-only was not run. Re-run with --extract-only "
            "to start a new round, then --continue --judgments <path> to resume."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def cleanup_state(state_dir: Path) -> None:
    """Remove the state directory after a successful run."""
    if state_dir.exists():
        shutil.rmtree(state_dir)
