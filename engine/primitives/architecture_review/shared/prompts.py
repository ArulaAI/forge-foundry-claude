"""C8 /architecture-review — LLM prompts.

Stage E: subjective review (abstraction / api_design / cross_cutting_leak).
Stage F: prioritization.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Stage E — Subjective review
# ---------------------------------------------------------------------------

STAGE_E_SYSTEM_PROMPT = """\
You are an expert software architect performing a system-shape review.

Given the dependency graph summary, spec text (if available), and selected module source,
evaluate the following three dimensions:

1. **abstraction** — Is each layer appropriately abstract vs concrete? Are there \
abstraction leaks where implementation details bleed across layer boundaries?

2. **api_design** — Assess cohesion, naming consistency, and completeness of the \
module's public interface. Flag inconsistent naming, missing error paths, or \
overly broad/narrow contracts.

3. **cross_cutting_leak** — Are cross-cutting concerns (logging, auth, transactions, \
tracing, retry logic) scattered across modules instead of being centralized? \
Flag locations where these concerns leak into layers that shouldn't own them.

Return a strict JSON array of issue objects. Each object must have:
  {
    "kind": <one of: "abstraction", "api_design", "cross_cutting_leak">,
    "module": <module name string or null if architectural/global>,
    "description": <clear description of the issue>,
    "severity": <one of: "low", "medium", "high", "critical">
  }

IMPORTANT:
- Only reference module names that appear in the dependency graph provided.
- If you cannot identify any issues, return an empty array: []
- Do not include any text outside the JSON array.
- The "kind" field MUST be exactly one of: abstraction, api_design, cross_cutting_leak
"""


def build_stage_e_user_message(
    dep_graph_summary: str,
    spec_text: str | None,
    module_source: str | None = None,
) -> str:
    """Build the Stage E user message for the LLM."""
    parts: list[str] = []

    parts.append("## Dependency Graph Summary\n\n" + dep_graph_summary)

    if spec_text:
        truncated_spec = spec_text[:4000]
        parts.append(f"## Spec Text\n\n{truncated_spec}")

    if module_source:
        truncated_source = module_source[:3000]
        parts.append(f"## Module Source (sample)\n\n```\n{truncated_source}\n```")

    parts.append(
        "Please evaluate the code architecture and return a JSON array of issues "
        "covering abstraction quality, api_design cohesion, and cross_cutting_leak concerns."
    )

    return "\n\n".join(parts)


def build_dep_graph_summary(dep_graph: object) -> str:
    """Build a textual summary of the dep graph for the LLM prompt."""
    from primitives.architecture_review.shared.models import DepGraph  # noqa: PLC0415

    assert isinstance(dep_graph, DepGraph)

    _max_modules_in_summary = 50
    _max_edges_in_summary = 100

    lines: list[str] = [
        f"Modules ({len(dep_graph.modules)} total):",
    ]
    for mod in dep_graph.modules[:_max_modules_in_summary]:
        lines.append(f"  - {mod.name} ({mod.language}, {mod.path})")

    if len(dep_graph.modules) > _max_modules_in_summary:
        lines.append(f"  ... and {len(dep_graph.modules) - _max_modules_in_summary} more")

    lines.append(f"\nEdges ({len(dep_graph.edges)} total):")
    for src, dst in dep_graph.edges[:_max_edges_in_summary]:
        lines.append(f"  {src} -> {dst}")
    if len(dep_graph.edges) > _max_edges_in_summary:
        lines.append(f"  ... and {len(dep_graph.edges) - _max_edges_in_summary} more")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage F — Prioritization
# ---------------------------------------------------------------------------

STAGE_F_SYSTEM_PROMPT = """\
You are an expert software architect.

Given a list of architecture findings (layer violations, coupling issues, cyclic \
dependencies, and subjective LLM findings), rank them by severity and impact.

Return a JSON array where each element has:
  {
    "issue_index": <0-based integer index from the input list>,
    "priority_rank": <1-based integer rank (1 = highest priority)>,
    "rationale": <one sentence explaining why this rank>
  }

Rank 1 is the most urgent. Issues with "critical" severity should come first,
followed by "high", "medium", and "low". Within the same severity level, prefer
issues that affect more modules or create systemic risk.

Do not include any text outside the JSON array.
"""


def build_stage_f_user_message(issues_summary: str) -> str:
    """Build the Stage F user message for the LLM."""
    return (
        "Please rank the following architecture findings by severity and impact:\n\n"
        + issues_summary
    )
