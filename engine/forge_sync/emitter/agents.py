from __future__ import annotations

from typing import TYPE_CHECKING

import tomli_w
import yaml

from forge_sync.emitter.targets import agent_dest_path, map_model_tier, map_tools

if TYPE_CHECKING:
    from forge_sync.emitter.models import CanonicalAgent


def _md_agent(agent: CanonicalAgent, harness: str) -> str:
    # name/description/tools/model frontmatter is used by both Claude Code (.md)
    # and GitHub Copilot custom agents (.agent.md) — do not remove.
    fields: dict[str, object] = {"name": agent.name, "description": agent.description}
    tools = map_tools(agent.tools, harness)
    if tools:
        fields["tools"] = tools
    fields["model"] = map_model_tier(agent.model_tier, harness)
    fm = yaml.safe_dump(fields, sort_keys=False, default_flow_style=False).strip()
    return f"---\n{fm}\n---\n\n{agent.instructions.rstrip()}\n"


def _toml_agent(agent: CanonicalAgent) -> str:
    # Codex agents are TOML with developer_instructions; no per-tool allowlist.
    doc = {
        "name": agent.name,
        "description": agent.description,
        "developer_instructions": agent.instructions.rstrip(),
        "model": map_model_tier(agent.model_tier, "codex"),
    }
    return tomli_w.dumps(doc, multiline_strings=True)


def _antigravity_workflow(agent: CanonicalAgent) -> str:
    # B4: `model` is intentionally omitted — Antigravity workflow frontmatter
    # carries no model field, so agent.model_tier maps to nothing for this harness.
    fm = yaml.safe_dump(
        {"description": agent.description}, sort_keys=False, default_flow_style=False
    ).strip()
    return f"---\n{fm}\n---\n\n{agent.instructions.rstrip()}\n"


def _cursor_rule_agent(agent: CanonicalAgent) -> str:
    # Cursor has no agents directory — an agent is delivered as a rule file with
    # alwaysApply: false; the description is the trigger and the system prompt is
    # the body. No model/tools field (Cursor selects the model in its UI). (#749)
    fm = yaml.safe_dump(
        {"description": agent.description, "alwaysApply": False},
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    return f"---\n{fm}\n---\n\n{agent.instructions.rstrip()}\n"


def emit_agent(agent: CanonicalAgent, harness: str) -> dict[str, str]:
    path = agent_dest_path(harness, agent.name)
    if harness == "codex":
        return {path: _toml_agent(agent)}
    if harness == "antigravity":
        return {path: _antigravity_workflow(agent)}
    if harness == "cursor":
        return {path: _cursor_rule_agent(agent)}
    return {path: _md_agent(agent, harness)}
