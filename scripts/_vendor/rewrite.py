from __future__ import annotations

import re
from pathlib import Path

# Match a `forge-validate <subcommand>` invocation (the command PREFIX only),
# with an OPTIONAL leading `uv run [--project "<anything>"] ` prefix. Anchored on
# a following hyphenated subcommand slug (e.g. `repo-digest`, `data-flow-map`) so
# prose like "delegates to forge-validate under the hood" (no subcommand) is left
# untouched, while every real PLB invocation shape is rewritten to the shim.
#
# Source skills at v5.0.x emit BARE `forge-validate <cmd>` (no `uv run` wrapper);
# earlier drafts wrapped it in `uv run --project "..."`. Both are handled.
_UV_PREFIX = re.compile(
    r'(?:uv\s+run\s+(?:--project\s+"[^"]*"\s+)?)?'  # optional `uv run [--project "…"] `
    r"forge-validate"
    r"(?=\s+[a-z][a-z0-9]*-[a-z0-9-]*)"  # lookahead: a hyphenated subcommand follows
)
_SHIM = '"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate"'

# NOTE: The state dir ``.foundry/context`` is intentionally NOT rewritten. The
# engine's Python defaults and every cross-primitive read use ``.foundry/context``
# (CWD-relative, so it already lands in the user's project). Rewriting only the
# docs to ``.forge-foundry/context`` created an 18-file lie that hard-failed the
# M-tier chain (e.g. M3 could not find M1's digest at the documented path). Leave
# ``.foundry/context`` untouched so docs match the engine.

# Match a `python -m primitives.<...>` invocation (e.g. the M0 workflow.yaml
# `command: python -m primitives.legacy_modernization.shared.report_aggregator`),
# rewriting the interpreter to the bundled engine venv python. Anchored on the
# `primitives.` module prefix so generic prose like `python -m pytest` is left
# untouched. Idempotent: the `(?<!/bin/)` guard skips an already-rewritten line.
_ENGINE_PY = '"${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python"'
_PYTHON_M = re.compile(
    r'(?<!")'  # not already the quoted shim path
    r"\bpython"
    r"(?=\s+-m\s+primitives\.)"  # lookahead: `-m primitives.<...>` follows
)


# Strip the forge-internal ``delegates-to-cli: true`` frontmatter key. Claude Code
# does not recognize it; leaving it in emits a spurious key. Only the exact line
# inside the leading ``---`` … ``---`` frontmatter block is removed — the body and
# every other frontmatter key are untouched. (The vendor discovery step reads this
# key from the SOURCE tree before copy, so stripping it from the vendored output is
# safe.)
_DELEGATES_LINE = re.compile(r"^[ \t]*delegates-to-cli[ \t]*:[ \t]*true[ \t]*\r?\n", re.MULTILINE)


def strip_delegates_to_cli(text: str) -> tuple[str, int]:
    """Remove the ``delegates-to-cli: true`` line from the leading frontmatter only."""
    if not text.startswith("---"):
        return text, 0
    end = text.find("\n---", 3)
    if end == -1:
        return text, 0
    fm_end = end + 1  # index just past the newline before the closing ``---``
    front, body = text[:fm_end], text[fm_end:]
    new_front, n = _DELEGATES_LINE.subn("", front)
    return new_front + body, n


def rewrite_skill_body(text: str) -> tuple[str, int]:
    new, n = _UV_PREFIX.subn(_SHIM, text)
    new, np = _PYTHON_M.subn(_ENGINE_PY, new)
    new, nd = strip_delegates_to_cli(new)
    return new, n + np + nd


_REFERENCE_SUFFIXES = (".yaml", ".yml", ".md")


def rewrite_reference_files(skills_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    root = Path(skills_dir)
    for ref in sorted(root.glob("*/references/**/*")):
        if not ref.is_file() or ref.suffix.lower() not in _REFERENCE_SUFFIXES:
            continue
        text = ref.read_text()
        new, n = rewrite_skill_body(text)
        if new != text:
            ref.write_text(new)
        counts[str(ref.relative_to(root))] = n
    return counts


def rewrite_skills_tree(skills_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for skill_md in sorted(Path(skills_dir).glob("*/SKILL.md")):
        slug = skill_md.parent.name
        text = skill_md.read_text()
        new, n = rewrite_skill_body(text)
        if new != text:
            skill_md.write_text(new)
        counts[slug] = n
    return counts
