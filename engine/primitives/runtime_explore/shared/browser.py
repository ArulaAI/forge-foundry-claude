"""Playwright availability probe for runtime_explore (#1472).

Playwright is an optional ``[browser]`` extra so the core install and the air-gapped
single binary (#1419) stay browser-free. The crawler / step-CLI call
:func:`playwright_available` and, when it is absent, exit **6** ("capability not
available") with the exact install commands — never a traceback. Actual browser
driving lives in ``crawler.py`` / ``step_cli.py`` (which lazily import playwright).
"""

from __future__ import annotations

import importlib.util

#: Conventional exit code when Playwright / a browser binary is not installed.
CAPABILITY_UNAVAILABLE_EXIT = 6

_INSTALL_HINT = (
    "runtime_explore needs Playwright (the optional [browser] extra). Install it with the\n"
    "form that matches how the workbench CLI was installed, then install the browser with\n"
    "the SAME interpreter the CLI runs from:\n"
    "\n"
    "  # uv tool from git (the default for a pinned-tag install):\n"
    "  uv tool install --force \\\n"
    "    'forge-foundry-primitives[browser] @ git+https://github.com/arula-ai/forge-foundry-primitives@<tag>'\n"
    "  uv tool run --from 'forge-foundry-primitives[browser]' playwright install chromium\n"
    "\n"
    "  # OR project checkout:  uv sync --extra browser && uv run playwright install chromium\n"
    "  # OR registry pip:\n"
    "  pip install 'forge-foundry-primitives[browser]' && playwright install chromium\n"
    "\n"
    "Note: a plain `pip install ...[browser]` targets whatever `pip` is on PATH, NOT the\n"
    "uv tool venv the CLI runs from — so runtime_explore still won't see Playwright."
)


def playwright_available() -> tuple[bool, str]:
    """Return ``(ok, hint)``.

    ``ok`` is ``True`` when the ``playwright`` package is importable. When ``False``,
    ``hint`` carries the copy-pasteable install commands (empty string when ``ok``).
    Note: this checks the Python package only — a missing *browser binary* surfaces at
    launch time in the crawler/step-CLI, which map it to the same exit 6 + hint.
    """
    if importlib.util.find_spec("playwright") is None:
        return False, _INSTALL_HINT
    return True, ""


__all__ = ["CAPABILITY_UNAVAILABLE_EXIT", "playwright_available"]
