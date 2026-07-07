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
