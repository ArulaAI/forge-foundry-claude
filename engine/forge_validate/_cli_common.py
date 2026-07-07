"""Shared CLI helpers used by multiple subcommands."""

from __future__ import annotations

import importlib
import re
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from collections.abc import Callable

_SEVERITY_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}

_RECOMMENDATION_ORDER: dict[str, int] = {"pass": 0, "review": 1, "fail": 2}

#: SEC-8: a git ref flows into a ``git`` subprocess argv. The first character is
#: constrained to a safe set (no leading ``-``) so a value can't be parsed as a
#: flag, and the body admits only the characters a real ref uses.
_VALID_GIT_REF = re.compile(r"^[A-Za-z0-9._/~^@:][A-Za-z0-9._/~^@{}:-]*$")


def validate_git_ref(
    ctx: click.Context,  # noqa: ARG001 — Click callback signature is fixed.
    param: click.Parameter,  # noqa: ARG001 — Click callback signature is fixed.
    value: str | None,
) -> str | None:
    """SEC-8 Click callback: reject a ``--<ref>`` value that isn't a safe git ref.

    Shared by every subcommand whose option flows into a ``git`` argv
    (``api-contract-review --baseline``, ``test-maintainer --diff-against``), so
    the CLI-side mirror of the CI-wrapper validation lives in exactly one place.
    """
    if value is not None and not _VALID_GIT_REF.match(value):
        raise click.BadParameter(f"invalid git ref: {value!r}")
    return value


def format_option(default: str = "markdown") -> Callable[[Any], Any]:
    """FLEX-3 (#175): the canonical ``--format`` option for review primitives.

    Type is :class:`click.STRING` (not :class:`click.Choice`) because the legal
    set is dynamic — a plugin can register a renderer at
    ``forge_foundry.renderers.<primitive>``. Each subcommand pairs this with a
    ``--list-formats`` flag that prints the live set.
    """
    return click.option(
        "--format",
        "format_",
        type=click.STRING,
        default=default,
        help=f"Output format (default: {default}; use --list-formats for the live set).",
    )


def list_formats_option(renderer_module: str) -> Callable[[Any], Any]:
    """The canonical ``--list-formats`` flag for review primitives (#885).

    Implemented as an **eager** option (``is_eager=True``) with a callback that
    prints the live renderer set and ``ctx.exit(0)`` **before** Click parses or
    validates the required positional argument. This is why
    ``forge-validate <cmd> --list-formats`` works without supplying the
    ``REPORT_PATHS...`` / ``code_path`` / ``contract_path`` positional — the
    help text always advertised this, but a non-eager flag fired only after the
    positional had already been required (``Error: Missing argument``).

    *renderer_module* is the dotted path of the primitive's renderer module,
    which must expose ``list_formats() -> Iterable[str]``. It is imported lazily
    inside the callback so importing the command module stays cheap.
    """

    def _callback(ctx: click.Context, param: click.Parameter, value: bool) -> None:  # noqa: ARG001, FBT001 — Click callback signature is fixed.
        # is_eager fires this for every invocation; only act when the flag is set
        # and we're not merely completing a shell-completion resolution.
        if not value or ctx.resilient_parsing:
            return
        module = importlib.import_module(renderer_module)
        for name in module.list_formats():
            click.echo(name)
        ctx.exit(0)

    return click.option(
        "--list-formats",
        "list_formats",
        is_flag=True,
        is_eager=True,
        expose_value=False,
        callback=_callback,
        help="List available output formats and exit (works without the positional arg).",
    )


def emit_report(rendered: str) -> None:
    """Emit *rendered* to stdout — the report stream.

    Commands call this only when no ``--output PATH`` is given, so the report
    always goes to stdout. Click's ``echo`` handles encoding + the
    trailing-newline contract.
    """
    click.echo(rendered)


# ---------------------------------------------------------------------------
# Skill-first protocol flags (§2.2 of harness-elimination spec)
# ---------------------------------------------------------------------------


def extract_only_option() -> Callable[[Any], Any]:
    """The ``--extract-only`` flag for skill-first mode."""
    return click.option(
        "--extract-only",
        "extract_only",
        is_flag=True,
        default=False,
        help="Run deterministic stages, emit prompts file, exit 7 (skill-first mode).",
    )


def continue_option() -> Callable[[Any], Any]:
    """The ``--continue`` flag for skill-first mode."""
    return click.option(
        "--continue",
        "continue_",
        is_flag=True,
        default=False,
        help="Resume from saved state after judgments (skill-first mode).",
    )


def judgments_option() -> Callable[[Any], Any]:
    """The ``--judgments`` path option for skill-first mode."""
    return click.option(
        "--judgments",
        type=click.Path(),
        default=None,
        help="Path to judgments JSON file (required with --continue).",
    )


def state_dir_option() -> Callable[[Any], Any]:
    """The ``--state-dir`` path option for skill-first mode."""
    return click.option(
        "--state-dir",
        "state_dir",
        type=click.Path(),
        default=None,
        help="Where to persist pipeline state between rounds (default: <output-dir>/.state/).",
    )


def validate_skill_first_flags(
    *,
    extract_only: bool,  # noqa: ARG001 — kept for a uniform call signature across commands.
    continue_: bool,
    judgments: str | None,
) -> None:
    """Validate the skill-first protocol flags.

    Raises click.UsageError if ``--continue`` is given without ``--judgments``.
    """
    if continue_ and judgments is None:
        raise click.UsageError("--continue requires --judgments <path>.")
