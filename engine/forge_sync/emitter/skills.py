from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from forge_sync.emitter.targets import map_tools, skill_dest_dir

if TYPE_CHECKING:
    from forge_sync.emitter.models import CanonicalSkill


def _frontmatter(fields: dict[str, object]) -> str:
    # Deterministic key order; block style.
    dumped = yaml.safe_dump(fields, sort_keys=False, default_flow_style=False).strip()
    return f"---\n{dumped}\n---\n"


def emit_skill(skill: CanonicalSkill, harness: str, *, tools: tuple[str, ...]) -> dict[str, str]:
    """Return {project-relative path: content} for *skill* on *harness*.

    If *tools* is empty (or the harness has no per-skill tool field), no tool
    field is emitted.  Specifically: ``map_tools`` returns ``[]`` for an empty
    *tools* tuple or for harnesses such as ``codex`` that carry no per-skill
    tool field; the ``if mapped:`` guard then skips the ``allowed-tools``/
    ``tools`` frontmatter key entirely, which is the intended behaviour.
    """
    dest = skill_dest_dir(harness, skill.name)
    if harness == "cursor":
        # Cursor has no skills concept — a skill is a rule file with a rich body.
        # Frontmatter is `description` + `alwaysApply: false` (the description is
        # Cursor's trigger condition); no name/tools/delegates-to-cli field. The
        # file is `.mdc` (Cursor's rule extension), under a per-skill subdirectory
        # so references/ + scripts/ co-locate (#749).
        cursor_content = _frontmatter({"description": skill.description, "alwaysApply": False})
        cursor_content += skill.body.rstrip() + "\n"
        cursor_out: dict[str, str] = {f"{dest}/SKILL.mdc": cursor_content}
        for rel, text in skill.references.items():
            cursor_out[f"{dest}/references/{rel}"] = text
        for rel, text in skill.scripts.items():
            cursor_out[f"{dest}/scripts/{rel}"] = text
        return cursor_out

    fields: dict[str, object] = {"name": skill.name, "description": skill.description}
    # Phase 5: pass the CLI-delegation marker through to every harness's emitted
    # frontmatter so it survives the canonical-source round-trip (only when set,
    # to keep the default frontmatter of the 24 non-delegating bodies unchanged).
    if skill.delegates_to_cli:
        fields["delegates-to-cli"] = True
    mapped = map_tools(tools, harness)
    if mapped:
        fields["allowed-tools" if harness == "claude_code" else "tools"] = mapped
    content = _frontmatter(fields) + skill.body.rstrip() + "\n"

    out: dict[str, str] = {f"{dest}/SKILL.md": content}
    for rel, text in skill.references.items():
        out[f"{dest}/references/{rel}"] = text
    for rel, text in skill.scripts.items():
        out[f"{dest}/scripts/{rel}"] = text
    return out
