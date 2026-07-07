from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

# Maintainer-only commands that must never ship in the consumer plugin.
_DENYLIST = {"autowork", "review-pr"}

# Agent names that must never ship in the consumer plugin. ``mainframe-recon`` is
# a backwards-compat re-export shim (see its .agent.yaml body) for legacy
# consumer-synced ``.claude/skills/.../subagents/mainframe-recon.md`` paths that do
# NOT exist in a fresh plugin install — vendoring it would ship a dead pointer to a
# renamed file (canonical: flavor-recon). Deny it; keep the real ``flavor-recon``.
_AGENT_DENYLIST = {"mainframe-recon"}


@dataclass(frozen=True)
class PrimitiveSrc:
    name: str
    slug: str
    skill_dir: Path
    agents_dir: Path | None
    has_shared: bool
    delegates_to_cli: bool


def _read_frontmatter(skill_md: Path) -> dict:
    text = skill_md.read_text()
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else ""
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def discover_primitives(src_primitives: Path) -> list[PrimitiveSrc]:
    src_primitives = Path(src_primitives)
    found: list[PrimitiveSrc] = []
    for child in sorted(src_primitives.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        skill_md = child / "skill" / "SKILL.md"
        if not skill_md.is_file():
            continue
        fm = _read_frontmatter(skill_md)
        slug = fm.get("name", child.name.replace("_", "-"))
        if slug in _DENYLIST:
            continue
        agents = child / "agents"
        found.append(PrimitiveSrc(
            name=child.name, slug=slug, skill_dir=child / "skill",
            agents_dir=agents if agents.is_dir() else None,
            has_shared=(child / "shared").is_dir(),
            delegates_to_cli=(fm.get("delegates-to-cli", "").lower() == "true"),
        ))
    return found


def _copy_tree_no_symlinks(src: Path, dst: Path) -> None:
    for item in sorted(src.rglob("*")):
        if item.is_symlink():
            continue
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def copy_skills(prims: list[PrimitiveSrc], out_skills: Path) -> int:
    out_skills = Path(out_skills)
    if out_skills.exists():
        shutil.rmtree(out_skills)
    n = 0
    for p in prims:
        _copy_tree_no_symlinks(p.skill_dir, out_skills / p.slug)
        n += 1
    return n


def copy_agents(prims: list[PrimitiveSrc], out_agents: Path, src_repo: Path) -> int:
    """Convert canonical ``agents/*.agent.yaml`` → Claude Code agent ``.md``.

    Uses the source emitter (``forge_sync.emitter.agents.emit_agent`` with the
    ``claude_code`` target), which produces valid ``---``-frontmatter agent
    markdown (name/description/tools/model + system-prompt body). The emitter is
    importable because ``vendor.py`` runs with *src_repo* on ``sys.path``.

    FAIL LOUD: if the real emitter/loader API cannot be imported, this raises
    rather than silently emitting yaml-in-a-fence stubs — a silent fallback is
    exactly what shipped unloadable agents before. Agents in ``_AGENT_DENYLIST``
    (e.g. the ``mainframe-recon`` compat shim) are skipped.
    """
    import sys
    out_agents = Path(out_agents)
    if out_agents.exists():
        shutil.rmtree(out_agents)
    prims_with_agents = [p for p in prims if p.agents_dir]
    if not prims_with_agents:
        return 0
    out_agents.mkdir(parents=True, exist_ok=True)
    if str(src_repo) not in sys.path:
        sys.path.insert(0, str(src_repo))
    try:
        from forge_sync.emitter.loader import load_canonical_agents  # type: ignore
        from forge_sync.emitter.agents import emit_agent  # type: ignore
    except Exception as exc:  # noqa: BLE001 — re-raised with context below
        raise RuntimeError(
            "copy_agents: could not import the real agent emitter API "
            "(forge_sync.emitter.loader.load_canonical_agents / "
            "forge_sync.emitter.agents.emit_agent) from "
            f"{src_repo!r}. Refusing to emit yaml-in-fence stub agents "
            "(they are unloadable by Claude Code). Fix the source path / API."
        ) from exc
    n = 0
    for p in prims_with_agents:
        primitive_dir = p.skill_dir.parent
        for agent in load_canonical_agents(primitive_dir):
            if agent.name in _AGENT_DENYLIST:
                print(
                    f"copy_agents: skipping denied agent {agent.name!r} "
                    f"(from {p.name}) — compat shim, not vendored",
                    file=sys.stderr,
                )
                continue
            emitted = emit_agent(agent, "claude_code")
            if not emitted:
                raise RuntimeError(
                    f"copy_agents: emit_agent returned no output for agent "
                    f"{agent.name!r} (from {p.name})"
                )
            # emit_agent returns {dest_relpath: content}; for claude_code there is
            # exactly one entry. Write the content under agents/<name>.md.
            (content,) = tuple(emitted.values())
            if not content.startswith("---"):
                raise RuntimeError(
                    f"copy_agents: emitted agent {agent.name!r} does not start with "
                    "'---' frontmatter; refusing to write an invalid agent"
                )
            (out_agents / f"{agent.name}.md").write_text(content)
            n += 1
    return n
