from __future__ import annotations

import re
from pathlib import Path

# Match `uv run --project "<anything>" forge-validate` (the command PREFIX only).
_UV_PREFIX = re.compile(
    r'uv\s+run\s+--project\s+"[^"]*"\s+forge-validate\b'
)
_SHIM = '"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate"'
_STATE_DIR = re.compile(r"\.foundry/context")


def rewrite_skill_body(text: str) -> tuple[str, int]:
    new, n = _UV_PREFIX.subn(_SHIM, text)
    new = _STATE_DIR.sub(".forge-foundry/context", new)
    return new, n


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
