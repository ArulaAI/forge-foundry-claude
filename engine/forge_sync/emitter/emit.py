from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from forge_sync.emitter.agents import emit_agent
from forge_sync.emitter.ci import emit_ci
from forge_sync.emitter.loader import (
    load_canonical_agents,
    load_canonical_ci,
    load_canonical_rules,
    load_canonical_skill,
)
from forge_sync.emitter.rules import emit_rule
from forge_sync.emitter.skills import emit_skill

if TYPE_CHECKING:
    from pathlib import Path

    from forge_sync.emitter.models import CanonicalAgent, CanonicalRule, CanonicalSkill


@dataclass(frozen=True)
class CanonicalSources:
    """The parsed canonical artifacts of one primitive (skill/agents/rules).

    Loading these from disk is harness-independent, so the propagator parses
    them ONCE per primitive (via :func:`load_canonical_sources`) and reuses the
    bundle across every enabled harness instead of re-reading + re-parsing the
    same YAML/Markdown on each harness pass (#731).
    """

    skill: CanonicalSkill | None
    agents: list[CanonicalAgent]
    rules: list[CanonicalRule]


def load_canonical_sources(primitive_dir: Path) -> CanonicalSources:
    """Load (parse) a primitive's canonical skill/agents/rules from disk, once."""
    return CanonicalSources(
        skill=load_canonical_skill(primitive_dir),
        agents=load_canonical_agents(primitive_dir),
        rules=load_canonical_rules(primitive_dir),
    )


def emit_from_sources(sources: CanonicalSources, harness: str) -> dict[str, str]:
    """Emit every canonical artifact in *sources* for *harness* (no disk I/O).

    The pure, cache-friendly core of :func:`emit_primitive`: it does the
    per-harness rendering only, so a caller that already parsed the sources can
    fan out across harnesses without re-reading them.
    """
    out: dict[str, str] = {}
    if sources.skill is not None:
        _merge(out, emit_skill(sources.skill, harness, tools=_skill_tools(sources.skill)))
    for agent in sources.agents:
        _merge(out, emit_agent(agent, harness))
    for rule in sources.rules:
        _merge(out, emit_rule(rule, harness))
    return out


def emit_ci_for_primitive(primitive_dir: Path) -> dict[str, str]:
    """Emit the three CI templates for *primitive_dir*, or ``{}`` if it has no ci.yaml.

    Unlike :func:`emit_primitive` (per-harness skill/agent/rule emission), CI is
    emitted **once per primitive** — it is harness-independent (the harness it
    invokes is pinned in ``ci.yaml``). The propagator calls this once per primitive
    alongside (not inside) its per-harness loop.
    """
    ci = load_canonical_ci(primitive_dir)
    return emit_ci(ci) if ci is not None else {}


def _merge(out: dict[str, str], new: dict[str, str]) -> None:
    """Merge *new* into *out*.

    Distinct content at a colliding path is concatenated (so multiple
    always-scoped rules accumulate into one AGENTS.md); identical content is
    deduplicated (so the repeated CLAUDE.md @AGENTS.md shim stays single).
    """
    for path, content in new.items():
        if path not in out:
            out[path] = content
        elif out[path] == content:
            continue  # idempotent (e.g. the CLAUDE.md shim) — no duplication
        else:
            out[path] = out[path].rstrip() + "\n\n" + content


def emit_primitive(primitive_dir: Path, harness: str) -> dict[str, str]:
    """Emit every canonical artifact under *primitive_dir* for *harness*.

    Returns {project-relative path: content}. Empty only if the primitive has no
    canonical sources at all (no skill, no agents, no rules). Thin wrapper that
    loads the sources then renders; callers fanning out across harnesses should
    instead call :func:`load_canonical_sources` once and :func:`emit_from_sources`
    per harness to avoid re-parsing (#731).
    """
    return emit_from_sources(load_canonical_sources(primitive_dir), harness)


def _skill_tools(skill: object) -> tuple[str, ...]:  # noqa: ARG001 — Phase 2b will consult skill body
    # Skills declare tool-intent in body prose; structured frontmatter tools are
    # opt-in. For Phase 2a the default tool surface is shell+read (the common
    # case). Per-skill overrides arrive with body authoring in Phase 2b.
    return ("shell", "read")
