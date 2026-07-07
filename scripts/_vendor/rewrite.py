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
_STATE_DIR = re.compile(r"\.foundry/context")

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


def rewrite_skill_body(text: str) -> tuple[str, int]:
    new, n = _UV_PREFIX.subn(_SHIM, text)
    new, np = _PYTHON_M.subn(_ENGINE_PY, new)
    new = _STATE_DIR.sub(".forge-foundry/context", new)
    return new, n + np


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
