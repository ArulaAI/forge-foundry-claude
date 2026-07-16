from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Severity = Literal["low", "medium", "high", "critical"]
Impact = Literal[
    "info-disclosure",
    "privilege-escalation",
    "account-takeover",
    "data-exfiltration",
    "denial-of-service",
    "rce",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ThreatNode(_Frozen):
    id: str
    source: Literal["S11", "S10", "C10", "C11"]
    severity: Severity
    title: str
    file: str
    line: int


class FindingGraph(_Frozen):
    nodes: tuple[ThreatNode, ...]

    def deduped(self) -> tuple[ThreatNode, ...]:
        seen: set[tuple[str, int]] = set()
        out: list[ThreatNode] = []
        for n in sorted(self.nodes, key=lambda x: (-_rank(x.severity), x.id)):
            key = (n.file, n.line)
            if key in seen:
                continue
            seen.add(key)
            out.append(n)
        return tuple(out)


class ChainStep(_Frozen):
    order: int
    finding_id: str
    technique: str
    file: str
    line: int
    effect: str


class ExploitChain(_Frozen):
    id: str  # EC-NNN
    objective: str
    steps: tuple[ChainStep, ...]
    impact: Impact


class AttackTree(_Frozen):
    objective: str
    mermaid: str


class AbuseCase(_Frozen):
    id: str  # AB-NNN
    role: str
    scenario: str
    finding_ids: tuple[str, ...]


class RemediationItem(_Frozen):
    rank: int
    fix: str
    addresses: tuple[str, ...]  # chain / finding ids
    reachability: Severity
    blast_radius: Severity


class RedTeamReport(_Frozen):
    graph: FindingGraph
    chains: tuple[ExploitChain, ...]
    trees: tuple[AttackTree, ...]
    abuse_cases: tuple[AbuseCase, ...]
    remediation: tuple[RemediationItem, ...]


def _rank(sev: str) -> int:
    return {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(sev, 0)
