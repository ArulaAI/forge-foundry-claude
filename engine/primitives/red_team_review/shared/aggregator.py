from __future__ import annotations

import json as _json
from typing import Any, Literal

from primitives.red_team_review.shared.models import (
    FindingGraph,
    RedTeamReport,
    ThreatNode,
    _rank,
)

_SEV_MAP = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "critical": "critical",
    "info": "low",
    "warning": "medium",
    "error": "high",
}


def _nodes(report: dict[str, Any] | None, key: str, source: str) -> list[ThreatNode]:
    if not report:
        return []
    out: list[ThreatNode] = []
    for i, item in enumerate(report.get(key, []), 1):
        node_id = f"{source}-{item['id']}" if "id" in item else f"{source}-idx{i}"
        out.append(
            ThreatNode(
                id=node_id,
                source=source,  # type: ignore[arg-type]
                severity=_SEV_MAP.get(  # type: ignore[arg-type]
                    str(item.get("severity", "medium")).lower(), "medium"
                ),
                title=str(item.get("title", "untitled")),
                file=str(item.get("file", "unknown")),
                line=int(item.get("line", 0)),
            )
        )
    return out


def ingest(
    security_json: dict[str, Any] | None, threat_json: dict[str, Any] | None
) -> FindingGraph:
    nodes = _nodes(security_json, "findings", "S11") + _nodes(threat_json, "threats", "S10")
    return FindingGraph(nodes=tuple(nodes))


_THRESH = {"none": 99, "high": 3, "critical": 4}


def gate(report: RedTeamReport, max_severity: str) -> int:
    thresh = _THRESH.get(max_severity, 99)
    # Exit-5 (C10/C11 critical) is intentionally unreachable here: ingest() only creates
    # S11/S10 nodes. Kept dormant per #813 (inactive in skill-first mode).
    if any(_rank(n.severity) >= thresh for n in report.graph.deduped()):
        return 4
    return 0


def render(report: RedTeamReport, fmt: Literal["markdown", "json", "sarif"]) -> str:
    if fmt == "json":
        return _json.dumps(
            {
                "primitive": "red-team-review",
                "chains": [c.model_dump() for c in report.chains],
                "trees": [t.model_dump() for t in report.trees],
                "abuse_cases": [a.model_dump() for a in report.abuse_cases],
                "remediation": [r.model_dump() for r in report.remediation],
                "inherited_findings": [n.model_dump() for n in report.graph.deduped()],
            },
            indent=2,
            sort_keys=True,
        )
    if fmt == "sarif":
        return _json.dumps(_sarif(report), indent=2)
    return _markdown(report)


def _markdown(report: RedTeamReport) -> str:
    out = [
        "# Red-Team Review\n",
        "## Executive Summary\n",
        f"{len(report.chains)} viable exploit chain(s); "
        f"{len(report.graph.deduped())} inherited finding(s).\n",
        "## Exploit Chains\n",
    ]
    for c in report.chains:
        out.append(f"### {c.id} — {c.objective} (impact: {c.impact})")
        for s in c.steps:
            out.append(
                f"{s.order}. **{s.technique}** → {s.effect} (`{s.finding_id}` @ {s.file}:{s.line})"
            )
        out.append("")
    out.append("## Attack Trees\n")
    for t in report.trees:
        out.append(f"### {t.objective}\n\n```mermaid\n{t.mermaid}\n```\n")
    out.append("## Abuse Cases\n")
    for a in report.abuse_cases:
        out.append(f"- **{a.id}** ({a.role}): {a.scenario} [{', '.join(a.finding_ids)}]")
    out.append("\n## Prioritized Remediation\n")
    for r in sorted(report.remediation, key=lambda x: x.rank):
        out.append(
            f"{r.rank}. {r.fix} — addresses {', '.join(r.addresses)} "
            f"(reach: {r.reachability}, blast: {r.blast_radius})"
        )
    out.append("\n## Inherited Findings\n")
    for n in report.graph.deduped():
        out.append(f"- [{n.source}] {n.severity.upper()} {n.title} ({n.file}:{n.line})")
    return "\n".join(out) + "\n"


def _sarif(report: RedTeamReport) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for c in report.chains:
        results.append(
            {
                "ruleId": "red-team/exploit-chain",
                "level": "error",
                "message": {"text": f"{c.id}: {c.objective} → {c.impact}"},
                "codeFlows": [
                    {
                        "threadFlows": [
                            {
                                "locations": [
                                    {
                                        "location": {
                                            "physicalLocation": {
                                                "artifactLocation": {"uri": s.file},
                                                "region": {"startLine": s.line},
                                            },
                                            "message": {"text": f"{s.technique}: {s.effect}"},
                                        }
                                    }
                                    for s in c.steps
                                ]
                            }
                        ]
                    }
                ],
            }
        )
    for n in report.graph.deduped():
        results.append(
            {
                "ruleId": f"red-team/{n.source.lower()}",
                "level": "error" if _rank(n.severity) >= _rank("high") else "warning",
                "message": {"text": n.title},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": n.file},
                            "region": {"startLine": n.line},
                        }
                    }
                ],
            }
        )
    unique_rule_ids = dict.fromkeys(r["ruleId"] for r in results)
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "red-team-review",
                        "rules": [{"id": rid} for rid in unique_rule_ids],
                    }
                },
                "results": results,
            }
        ],
    }
