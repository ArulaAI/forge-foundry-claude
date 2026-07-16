from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from primitives.red_team_review.shared.aggregator import gate, ingest, render
from primitives.red_team_review.shared.models import (
    AbuseCase,
    AttackTree,
    ExploitChain,
    RedTeamReport,
    RemediationItem,
)


def _load(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    except json.JSONDecodeError as exc:
        print(f"error: {path.name} contains invalid JSON: {exc}", file=sys.stderr)
        raise SystemExit(3) from exc


@click.command("red-team-review")
@click.option("--state-dir", required=True, type=click.Path(path_type=Path))
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["markdown", "json", "sarif"]),
    default="markdown",
)
@click.option("--output", default=None, type=click.Path(path_type=Path))
@click.option(
    "--max-severity",
    type=click.Choice(["none", "high", "critical"]),
    default="none",
)
def red_team_review(state_dir: Path, fmt: str, output: Path | None, max_severity: str) -> None:
    """S12 — assemble red-team report from S11/S10 sub-call outputs + harness synthesis."""
    sd = Path(state_dir)
    if not sd.is_dir():
        raise SystemExit(2)
    graph = ingest(_load(sd / "security.json"), _load(sd / "threat.json"))
    syn = _load(sd / "synthesis.json") or {}
    valid_ids = {n.id for n in graph.nodes}
    raw_chains: list[ExploitChain] = []
    for c in syn.get("chains", []):
        chain = ExploitChain(**c)
        bad = [s.finding_id for s in chain.steps if s.finding_id not in valid_ids]
        if bad:
            ids_str = ", ".join(bad)
            print(
                f"warning: chain {chain.id} has unknown finding_id(s): {ids_str}; dropping steps",
                file=sys.stderr,
            )
            chain = ExploitChain(
                **{**c, "steps": [s for s in chain.steps if s.finding_id in valid_ids]}
            )
        raw_chains.append(chain)
    report = RedTeamReport(
        graph=graph,
        chains=tuple(raw_chains),
        trees=tuple(AttackTree(**t) for t in syn.get("trees", [])),
        abuse_cases=tuple(AbuseCase(**a) for a in syn.get("abuse_cases", [])),
        remediation=tuple(RemediationItem(**r) for r in syn.get("remediation", [])),
    )
    payload = render(report, fmt)  # type: ignore[arg-type]
    if output:
        Path(output).write_text(payload)
    else:
        click.echo(payload)
    raise SystemExit(gate(report, max_severity))


cli = red_team_review
