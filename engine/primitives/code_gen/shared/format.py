"""Repo-detected formatter runner for the C3 code_gen PLB primitive.

Formatting is Python's job, not the model's: after a change-set is applied,
the touched files are formatted with the stack's configured formatter before
the test runs. A formatter that cannot parse a file is an early "broken
syntax" signal surfaced into the revise loop.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003

#: stack -> formatter argv prefix (the touched file paths are appended).
_FORMATTERS: dict[str, list[str]] = {
    "python": ["ruff", "format"],
    "js": ["npx", "prettier", "--write"],
    "java": ["mvn", "-q", "spotless:apply"],
}


def resolve_formatter(stack: str, root: Path) -> list[str] | None:  # noqa: ARG001
    return _FORMATTERS.get(stack)


@dataclass(frozen=True)
class FormatResult:
    ok: bool
    error: str | None


def format_files(stack: str, root: Path, files: list[str]) -> FormatResult:
    argv = resolve_formatter(stack, root)
    if argv is None or not files:
        return FormatResult(ok=True, error=None)
    try:
        proc = subprocess.run(  # noqa: S603
            [*argv, *files],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        # Formatter not installed — treat as "none configured", don't block.
        return FormatResult(ok=True, error=None)
    if proc.returncode != 0:
        return FormatResult(ok=False, error=(proc.stderr or proc.stdout).strip()[:2000])
    return FormatResult(ok=True, error=None)
