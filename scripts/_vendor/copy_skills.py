from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

# Maintainer-only commands that must never ship in the consumer plugin.
_DENYLIST = {"autowork", "review-pr"}


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
    """Convert canonical agents/*.agent.yaml → Claude agent .md via the source emitter.

    The source emitter (forge_sync.emitter.agents.emit_agent) is importable because
    vendor.py runs with src_repo on sys.path. Falls back to a verbatim copy of the
    yaml (renamed .md with a fenced block) if the emitter import is unavailable.
    """
    import sys
    out_agents = Path(out_agents)
    if out_agents.exists():
        shutil.rmtree(out_agents)
    prims_with_agents = [p for p in prims if p.agents_dir]
    if not prims_with_agents:
        return 0
    out_agents.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(src_repo))
    try:
        from forge_sync.emitter.loader import load_canonical_sources  # type: ignore
        from forge_sync.emitter.agents import emit_agent  # type: ignore
        emitter_ok = True
    except Exception:
        emitter_ok = False
    n = 0
    for p in prims_with_agents:
        for yml in sorted(p.agents_dir.glob("*.agent.yaml")):
            stem = yml.name.replace(".agent.yaml", "")
            if emitter_ok:
                sources = load_canonical_sources(p.skill_dir.parent)
                emitted = emit_agent(sources.agents[stem], "claude_code")  # type: ignore
                for _relpath, content in emitted.items():
                    (out_agents / f"{stem}.md").write_text(content)
            else:
                (out_agents / f"{stem}.md").write_text(
                    f"<!-- vendored from {yml.name} -->\n\n```yaml\n{yml.read_text()}\n```\n")
            n += 1
    return n
