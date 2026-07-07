from __future__ import annotations

# Skill directory per harness (relative to project root). Codex uses .agents/skills;
# Copilot uses .github/skills (its conventional home); Claude uses .claude/skills.
#
# ANTIGRAVITY-VERIFY note (checked 2026-06-05, MEDIUM confidence):
#   Workspace skills:  .agent/skills/<name>/SKILL.md   (.agent/ SINGULAR)
#   Workspace workflows: .agent/workflows/<name>.md    (.agent/ SINGULAR)
#   Global skills:     ~/.gemini/antigravity/skills/
#
# Sources consulted (all JS-rendered official docs returned empty):
#   - https://antigravity.google/docs/skills  (JS-rendered; empty)
#   - https://antigravity.google/docs/rules-workflows  (JS-rendered; empty)
#   Best proxies:
#   - https://codelabs.developers.google.com/getting-started-with-antigravity-skills
#     (Google Codelabs, references official docs): ".agent/skills/" SINGULAR confirmed
#   - https://github.com/rmyndharis/antigravity-skills README: ".agent/skills/" SINGULAR
#     explicitly noted as distinct from Gemini CLI's ".agents/" plural
#   - https://medium.com/google-cloud/creating-an-adk-agent-skill-in-antigravity-0031f5f82ccb
#     (Google Cloud Community, 2026): ".agent/skills/adk-skill/" SINGULAR
#   - https://agentpedia.codes/blog/workflows: workflows "MUST be in .agent/workflows/" SINGULAR
#   - https://github.com/harikrishna8121999/antigravity-workflows: ".agent/workflows/" SINGULAR
#   Conflicting (community tutorials / Antigravity CLI context, not confirmed as Antigravity IDE):
#   - https://codelabs.developers.google.com/autonomous-ai-developer-pipelines-antigravity:
#     uses ".agents/" PLURAL (mkdir -p .agents/workflows .agents/skills)
#   - https://medium.com/google-cloud/tutorial-getting-started-with-antigravity-skills-864041811e0d:
#     workspace ".agents/skills/" PLURAL in Antigravity CLI context
#
# The key open question: Antigravity IDE vs Antigravity CLI may differ on singular/plural.
# The .agent/ (singular) values here are supported by the more authoritative sources (official
# Codelabs + GitHub community collection explicitly citing antigravity.google/docs/skills).
# A live-install check remains outstanding before v2.0.0 tag — see §9 of
# docs/superpowers/specs/2026-06-05-phase2-skill-first-multiharness-emitter-design.md.
# CURSOR-VERIFY note (checked 2026-06-16, HIGH confidence):
#   Cursor's modern convention is `.cursor/rules/*.mdc` (Markdown + Cursor
#   metadata frontmatter). There is no separate skills directory — a skill is
#   emitted as a rule file with a rich instruction body. To co-locate a skill's
#   references/ and scripts/, skills go under a per-skill subdirectory:
#   `.cursor/rules/<name>/SKILL.mdc` (see emit_skill's cursor branch). Frontmatter
#   uses `description` + `alwaysApply`; the legacy single-file `.cursorrules` root
#   format is intentionally NOT emitted (#749).
_SKILL_DIRS = {
    "claude_code": ".claude/skills",
    "copilot": ".github/skills",
    "codex": ".agents/skills",
    # .agent/ SINGULAR — see ANTIGRAVITY-VERIFY note above; MEDIUM confidence
    "antigravity": ".agent/skills",
    # Cursor has no separate skills dir — skills are rule files; per-skill
    # subdirectory so references/ + scripts/ co-locate. See CURSOR-VERIFY above.
    "cursor": ".cursor/rules",
}

# Agent destination FILE (relative to project root), `{name}` substituted.
_AGENT_PATHS = {
    "claude_code": ".claude/agents/{name}.md",
    "copilot": ".github/agents/{name}.agent.md",
    "codex": ".codex/agents/{name}.toml",
    # .agent/workflows/ SINGULAR — see ANTIGRAVITY-VERIFY note above; MEDIUM confidence
    "antigravity": ".agent/workflows/{name}.md",
    # Cursor agents are delivered as rules; `.agent.mdc` distinguishes them from
    # plain rule files in the same directory. See CURSOR-VERIFY above.
    "cursor": ".cursor/rules/{name}.agent.mdc",
}

# Neutral tool-intent -> per-harness skill/agent tool name. Harnesses absent from
# this map have no per-skill tool field (Codex/Antigravity skills).
_TOOL_MAP = {
    "claude_code": {"shell": "Bash", "read": "Read", "edit": "Edit", "search": "Grep"},
    "copilot": {
        "shell": "runCommands",
        "read": "codebase",
        "edit": "editFiles",
        "search": "search",
    },
}

_MODEL_MAP = {
    "claude_code": {"default": "inherit", "fast": "haiku", "deep": "opus"},
    "copilot": {"default": "inherit", "fast": "gpt-4o-mini", "deep": "gpt-4o"},
    "codex": {"default": "inherit", "fast": "gpt-5-codex-mini", "deep": "gpt-5-codex"},
    "antigravity": {"default": "inherit", "fast": "gemini-flash", "deep": "gemini-pro"},
    # Cursor model names may change; these are informational only (Cursor selects
    # the model per-rule via its UI, not file frontmatter). See CURSOR-VERIFY.
    "cursor": {"default": "inherit", "fast": "cursor-small", "deep": "claude-sonnet-4"},
}


def skill_dest_dir(harness: str, skill_name: str) -> str:
    try:
        return f"{_SKILL_DIRS[harness]}/{skill_name}"
    except KeyError:
        raise ValueError(f"unknown harness {harness!r} for skill emission") from None


def agent_dest_path(harness: str, agent_name: str) -> str:
    try:
        return _AGENT_PATHS[harness].format(name=agent_name)
    except KeyError:
        raise ValueError(f"unknown harness {harness!r} for agent emission") from None


def map_tools(tools: tuple[str, ...], harness: str) -> list[str]:
    """Map neutral tool names to *harness*-native tool names.

    Returns an empty list for harnesses absent from ``_TOOL_MAP`` (e.g. Codex,
    Antigravity), which carry no per-skill tool field.  Raises ``KeyError`` for
    any tool not in ``NEUTRAL_TOOLS`` — callers must pass validated values.
    """
    sub = _TOOL_MAP.get(harness)
    if sub is None:
        return []
    return [sub[t] for t in tools]  # KeyError on unknown neutral tool = fail loud


def map_model_tier(tier: str, harness: str) -> str:
    """Map a neutral model tier to *harness*'s native model id.

    Raises ``ValueError`` if *harness* or *tier* is unknown — callers should
    pass validated values (an emitted harness + a tier from a
    ``CanonicalAgent``).
    """
    try:
        return _MODEL_MAP[harness][tier]
    except KeyError:
        raise ValueError(
            f"unknown harness {harness!r} or model tier {tier!r} for model mapping"
        ) from None
