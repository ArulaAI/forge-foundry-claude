#!/usr/bin/env python3
"""Deterministic DREAD scoring + report rendering for ``/threat-model``.

Token-offload: the model supplies its STRIDE threat *judgment* — which threats
exist, their STRIDE category, a description, affected component, mitigation,
residual-risk note, optional CWE IDs, and the five raw DREAD integer dimensions
— as a compact JSON payload. This stdlib-only script does the deterministic
remainder:

* **DREAD mean** — ``risk = mean(damage, reproducibility, exploitability,
  affected_users, discoverability)``; a threat whose payload omits or nulls a
  DREAD block falls back to ``5/5/5/5/5`` (neutral baseline).
* **Risk label** — CRITICAL ≥ 9.0, HIGH ≥ 7.0, MEDIUM ≥ 4.0, else LOW.
* **``--max-severity`` gate** — exits 4 when any threat exceeds the threshold
  (``high`` = DREAD ≥ 7.0; ``critical`` = DREAD ≥ 9.0).
* **Report rendering** — ``markdown`` (default), ``json``, ``sarif`` (SARIF
  2.1.0 multi-run: threat-model findings in ``runs[0]``; each AI-quality
  auditor that produced findings appended as its own ``runs[]`` — C10 before
  C11).

The model keeps STRIDE threat enumeration (trust-boundary reasoning) and
assigns the 5 DREAD integer dimensions; this script handles means, labels,
the gate, and all three renderers.

Runnable with a bare ``python3`` (stdlib only — JSON in; Markdown/JSON/SARIF
out via string formatting), which is why the skill body can invoke it in any
harness session.

Usage::

    python3 threat_score.py --threats-json payload.json \\
        --format markdown --output threat-report.md

    python3 threat_score.py --threats-json - --dry-run < payload.json

    python3 threat_score.py --threats-json payload.json \\
        --format sarif --max-severity high

Payload schema (compact JSON object)::

    {
      "spec_title": "...",
      "methodology": "stride",
      "threats": [
        {
          "id": "T-001",
          "category": "spoofing",
          "title": "...",
          "description": "...",
          "component": "...",
          "mitigation": "...",
          "residual_risk": "...",
          "cwe_ids": ["CWE-287"],          ← "cwe_ids" (array); a bare "cwe" is salvaged + warned
          "dread": {                        ← MUST be a NESTED block named "dread"
            "damage": 8,                   ← flat keys at top level are silently ignored
            "reproducibility": 7,
            "exploitability": 6,
            "affected_users": 9,
            "discoverability": 5
          }
        }
      ],
      "ai_quality_runs": [
        {
          "tool": "failure-mode-audit",
          "role": "C10",
          "results": [{"rule_id": "FM1", "message": "...", "level": "warning"}]
        }
      ]
    }

**Common mis-shapings** (both emit a stderr WARNING rather than silently defaulting):

* ``cwe`` instead of ``cwe_ids`` — the CWE field must be the array key ``cwe_ids``.
  A bare ``cwe`` key is salvaged (its value is used, a lone string wrapped into a
  one-element list) with a stderr WARNING, not silently dropped (#948).

* Flat DREAD keys — the five DREAD dimensions (damage, reproducibility, etc.) must
  be inside a nested ``"dread": { ... }`` block.  Placing them at the threat's top
  level causes every dimension to default to 5 (MEDIUM risk).  A WARNING is emitted
  when flat keys are detected without a nested block so the payload can be corrected.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Characters disallowed in an output filename component (path-injection guard).
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")

STRIDE_CATEGORIES = (
    "spoofing",
    "tampering",
    "repudiation",
    "info_disclosure",
    "denial_of_service",
    "elevation_of_privilege",
)

DREAD_DIMS = ("damage", "reproducibility", "exploitability", "affected_users", "discoverability")
_DEFAULT_DREAD = 5
_DREAD_MIN = 1
_DREAD_MAX = 10

#: Risk-label thresholds (inclusive lower bounds).
_THRESHOLD_CRITICAL = 9.0
_THRESHOLD_HIGH = 7.0
_THRESHOLD_MEDIUM = 4.0

EXPORT_FORMATS = ("markdown", "json", "sarif")


def _safe_filename(value: str) -> str:
    """Collapse anything outside ``[A-Za-z0-9._-]`` to ``_`` so a payload-supplied
    name cannot inject path separators into the output path."""
    return _UNSAFE_FILENAME_RE.sub("_", value) or "report"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DreadScore:
    damage: int
    reproducibility: int
    exploitability: int
    affected_users: int
    discoverability: int

    @property
    def risk(self) -> float:
        """Mean of the 5 DREAD dimensions, rounded to 2 decimal places."""
        total = self.damage + self.reproducibility + self.exploitability
        total += self.affected_users + self.discoverability
        return round(total / 5.0, 2)

    @property
    def label(self) -> str:
        r = self.risk
        if r >= _THRESHOLD_CRITICAL:
            return "CRITICAL"
        if r >= _THRESHOLD_HIGH:
            return "HIGH"
        if r >= _THRESHOLD_MEDIUM:
            return "MEDIUM"
        return "LOW"


@dataclass(frozen=True)
class Threat:
    id: str
    category: str
    title: str
    description: str
    component: str
    mitigation: str
    residual_risk: str
    cwe_ids: tuple[str, ...]
    dread: DreadScore


@dataclass(frozen=True)
class AiQualityRun:
    tool: str
    role: str
    results: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class ThreatReport:
    spec_title: str
    methodology: str
    threats: tuple[Threat, ...]
    ai_quality_runs: tuple[AiQualityRun, ...]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_dread(raw: object) -> DreadScore:
    """Parse a DREAD block from the payload or return the neutral default."""
    if not isinstance(raw, dict):
        return DreadScore(*([_DEFAULT_DREAD] * 5))
    dims: list[int] = []
    for dim in DREAD_DIMS:
        val = raw.get(dim)
        if isinstance(val, int | float) and _DREAD_MIN <= int(val) <= _DREAD_MAX:
            dims.append(int(val))
        else:
            dims.append(_DEFAULT_DREAD)
    return DreadScore(*dims)


def _parse_threat(raw: object) -> Threat | None:
    """Normalize one model-proposed threat; returns None for non-dict items.

    Emits stderr WARNINGs for two common mis-shapings that bite silently:

    * **Flat DREAD keys**: when top-level keys ``damage``, ``reproducibility``,
      etc. are present but there is no nested ``dread: {...}`` block, every
      DREAD dimension silently defaults to 5 (MEDIUM).  The warning names the
      flat keys that were found so the caller can restructure the payload.
    * **``cwe`` instead of ``cwe_ids``**: the correct key is ``cwe_ids``
      (an array); a bare ``cwe`` key is salvaged (its value used, a lone string
      wrapped into a one-element list) with a warning, not silently dropped.
    """
    if not isinstance(raw, dict):
        print(
            f"[threat-model] WARNING: dropping non-dict threat item: {raw!r}",
            file=sys.stderr,
        )
        return None

    threat_id = str(raw.get("id") or "T-?")

    # Warn on flat DREAD keys at the top level when no nested dread block exists.
    dread_raw = raw.get("dread")
    if not isinstance(dread_raw, dict):
        flat_dread_keys = [dim for dim in DREAD_DIMS if dim in raw]
        if flat_dread_keys:
            print(
                f"[threat-model] WARNING: {threat_id}: flat DREAD keys "
                f"{flat_dread_keys} found at top level but no nested 'dread' block — "
                f"all DREAD dimensions will default to {_DEFAULT_DREAD}. "
                f'Restructure as: {{"dread": {{"damage": N, ...}}}}',
                file=sys.stderr,
            )

    # `cwe` instead of the correct `cwe_ids`: warn and SALVAGE the value (a lone
    # string is wrapped into a one-element list) rather than dropping it (#948).
    cwe_raw = raw.get("cwe_ids")
    if cwe_raw is None and "cwe" in raw:
        print(
            f"[threat-model] WARNING: {threat_id}: found key 'cwe' but the correct key "
            f"is 'cwe_ids' (an array, e.g. [\"CWE-287\"]) — salvaging its value; "
            f"rename to 'cwe_ids' to silence this.",
            file=sys.stderr,
        )
        cwe_raw = raw.get("cwe")

    if isinstance(cwe_raw, str):
        cwe_ids: tuple[str, ...] = (cwe_raw,) if cwe_raw else ()
    elif isinstance(cwe_raw, list):
        cwe_ids = tuple(str(c) for c in cwe_raw)
    else:
        cwe_ids = ()
    return Threat(
        id=threat_id,
        category=str(raw.get("category") or ""),
        title=str(raw.get("title") or ""),
        description=str(raw.get("description") or ""),
        component=str(raw.get("component") or ""),
        mitigation=str(raw.get("mitigation") or ""),
        residual_risk=str(raw.get("residual_risk") or ""),
        cwe_ids=cwe_ids,
        dread=_parse_dread(dread_raw),
    )


def _parse_ai_quality_run(raw: object) -> AiQualityRun | None:
    if not isinstance(raw, dict):
        return None
    results_raw = raw.get("results")
    results: tuple[dict[str, object], ...] = (
        tuple(r for r in results_raw if isinstance(r, dict))
        if isinstance(results_raw, list)
        else ()
    )
    return AiQualityRun(
        tool=str(raw.get("tool") or "unknown"),
        role=str(raw.get("role") or ""),
        results=results,
    )


def build_report(payload: dict[str, object]) -> ThreatReport:
    """Normalize a raw model payload into a :class:`ThreatReport`."""
    threats_raw = payload.get("threats")
    items = threats_raw if isinstance(threats_raw, list) else []
    threats = tuple(t for t in (_parse_threat(i) for i in items) if t is not None)

    ai_runs_raw = payload.get("ai_quality_runs")
    ai_items = ai_runs_raw if isinstance(ai_runs_raw, list) else []
    ai_runs = tuple(r for r in (_parse_ai_quality_run(i) for i in ai_items) if r is not None)
    # Enforce C10 before C11 ordering.
    ai_runs = tuple(
        sorted(ai_runs, key=lambda r: 0 if r.role == "C10" else (1 if r.role == "C11" else 2))
    )

    return ThreatReport(
        spec_title=str(payload.get("spec_title") or "Threat Model"),
        methodology=str(payload.get("methodology") or "stride"),
        threats=threats,
        ai_quality_runs=ai_runs,
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

_STRIDE_LABELS: dict[str, str] = {
    "spoofing": "Spoofing",
    "tampering": "Tampering",
    "repudiation": "Repudiation",
    "info_disclosure": "Information Disclosure",
    "denial_of_service": "Denial of Service",
    "elevation_of_privilege": "Elevation of Privilege",
}


def render_markdown(report: ThreatReport) -> str:
    lines = [
        f"# Threat Model — {report.spec_title}",
        "",
        f"Methodology: **{report.methodology.upper()}**",
        "",
        "## Threat Summary",
        "",
        "| ID | Category | Title | DREAD Risk | Label | Component |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for t in report.threats:
        cat_label = _STRIDE_LABELS.get(t.category, t.category)
        lines.append(
            f"| {t.id} | {cat_label} | {t.title} | {t.dread.risk} | {t.dread.label}"
            f" | {t.component} |"
        )
    lines.append("")

    for cat in STRIDE_CATEGORIES:
        cat_threats = [t for t in report.threats if t.category == cat]
        if not cat_threats:
            continue
        lines.append(f"## {_STRIDE_LABELS.get(cat, cat)}")
        lines.append("")
        for t in cat_threats:
            lines.append(f"### {t.id} — {t.title}")
            lines.append("")
            lines.append(f"**Risk:** {t.dread.risk} ({t.dread.label})")
            lines.append("")
            lines.append(f"**Component:** {t.component}")
            lines.append("")
            lines.append(f"**Description:** {t.description}")
            lines.append("")
            lines.append(f"**Mitigation:** {t.mitigation}")
            lines.append("")
            lines.append(f"**Residual Risk:** {t.residual_risk}")
            if t.cwe_ids:
                lines.append("")
                lines.append(f"**CWEs:** {', '.join(t.cwe_ids)}")
            lines.append("")
            lines.append(
                f"DREAD: D={t.dread.damage} R={t.dread.reproducibility}"
                f" E={t.dread.exploitability} A={t.dread.affected_users}"
                f" D={t.dread.discoverability}"
            )
            lines.append("")

    if report.ai_quality_runs:
        lines.append("## AI Quality Findings")
        lines.append("")
        for run in report.ai_quality_runs:
            lines.append(f"### {run.tool} ({run.role})")
            lines.append("")
            if not run.results:
                lines.append("No findings.")
            else:
                for r in run.results:
                    rule = r.get("rule_id", "")
                    msg = r.get("message", "")
                    lvl = r.get("level", "")
                    lines.append(f"- **{rule}** ({lvl}): {msg}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_json(report: ThreatReport) -> str:
    threats_out = []
    for t in report.threats:
        threats_out.append(
            {
                "id": t.id,
                "category": t.category,
                "title": t.title,
                "description": t.description,
                "component": t.component,
                "mitigation": t.mitigation,
                "residual_risk": t.residual_risk,
                "cwe_ids": list(t.cwe_ids),
                "dread": {
                    "damage": t.dread.damage,
                    "reproducibility": t.dread.reproducibility,
                    "exploitability": t.dread.exploitability,
                    "affected_users": t.dread.affected_users,
                    "discoverability": t.dread.discoverability,
                    "risk": t.dread.risk,
                    "label": t.dread.label,
                },
            }
        )
    ai_runs_out = []
    for run in report.ai_quality_runs:
        ai_runs_out.append({"tool": run.tool, "role": run.role, "results": list(run.results)})
    doc = {
        "spec_title": report.spec_title,
        "methodology": report.methodology,
        "threats": threats_out,
        "ai_quality_runs": ai_runs_out,
    }
    return json.dumps(doc, indent=2) + "\n"


_SARIF_SCHEMA = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
)
_SARIF_VERSION = "2.1.0"

_DREAD_LEVEL: dict[str, str] = {
    "CRITICAL": "error",
    "HIGH": "error",
    "MEDIUM": "warning",
    "LOW": "note",
}


def _sarif_threat_run(report: ThreatReport) -> dict[str, object]:
    rules: list[dict[str, object]] = []
    seen_rules: set[str] = set()
    results: list[dict[str, object]] = []
    for t in report.threats:
        rule_id = t.id
        if rule_id not in seen_rules:
            seen_rules.add(rule_id)
            rules.append(
                {
                    "id": rule_id,
                    "shortDescription": {"text": t.title},
                    "fullDescription": {"text": t.description},
                    "properties": {
                        "category": t.category,
                        "dread_risk": t.dread.risk,
                        "dread_label": t.dread.label,
                    },
                }
            )
        results.append(
            {
                "ruleId": rule_id,
                "level": _DREAD_LEVEL.get(t.dread.label, "note"),
                "message": {"text": f"{t.mitigation} | Residual: {t.residual_risk}"},
                "properties": {
                    "component": t.component,
                    "dread": {
                        "damage": t.dread.damage,
                        "reproducibility": t.dread.reproducibility,
                        "exploitability": t.dread.exploitability,
                        "affected_users": t.dread.affected_users,
                        "discoverability": t.dread.discoverability,
                        "risk": t.dread.risk,
                        "label": t.dread.label,
                    },
                    "cwe_ids": list(t.cwe_ids),
                },
            }
        )
    return {
        "tool": {
            "driver": {
                "name": "threat-model",
                "version": "1.0.0",
                "informationUri": "https://github.com/arula-ai/forge-foundry-primitives",
                "rules": rules,
            }
        },
        "results": results,
    }


def _sarif_ai_run(run: AiQualityRun) -> dict[str, object]:
    results: list[dict[str, object]] = []
    for r in run.results:
        results.append(
            {
                "ruleId": str(r.get("rule_id", "")),
                "level": str(r.get("level", "note")),
                "message": {"text": str(r.get("message", ""))},
            }
        )
    return {
        "tool": {
            "driver": {
                "name": run.tool,
                "version": "1.0.0",
                "properties": {"role": run.role},
            }
        },
        "results": results,
    }


def render_sarif(report: ThreatReport) -> str:
    runs: list[dict[str, object]] = [_sarif_threat_run(report)]
    for ai_run in report.ai_quality_runs:
        if ai_run.results:
            runs.append(_sarif_ai_run(ai_run))
    sarif_doc: dict[str, object] = {
        "$schema": _SARIF_SCHEMA,
        "version": _SARIF_VERSION,
        "runs": runs,
    }
    return json.dumps(sarif_doc, indent=2) + "\n"


_RENDERERS = {
    "markdown": render_markdown,
    "json": render_json,
    "sarif": render_sarif,
}


def render(report: ThreatReport, fmt: str) -> str:
    return _RENDERERS[fmt](report)


# ---------------------------------------------------------------------------
# Max-severity gate
# ---------------------------------------------------------------------------

_GATE_THRESHOLD: dict[str, float] = {
    "high": _THRESHOLD_HIGH,
    "critical": _THRESHOLD_CRITICAL,
}


def check_max_severity(report: ThreatReport, max_severity: str) -> list[Threat]:
    """Return threats that breach *max_severity*; empty list = gate passed."""
    threshold = _GATE_THRESHOLD.get(max_severity)
    if threshold is None:
        raise SystemExit(
            f"Error: unknown --max-severity {max_severity!r}; expected 'high' or 'critical'"
        )
    return [t for t in report.threats if t.dread.risk >= threshold]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic DREAD scoring + threat-model report rendering."
    )
    parser.add_argument(
        "--threats-json",
        required=True,
        help="Threat payload JSON path, or - for stdin.",
    )
    parser.add_argument(
        "--format",
        default="markdown",
        choices=list(EXPORT_FORMATS),
        help="Output format (markdown/json/sarif).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write output to this file; default stdout.",
    )
    parser.add_argument(
        "--max-severity",
        default=None,
        choices=["high", "critical"],
        help="Gate: exit 4 when any threat >= threshold.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print to stdout; write no files.",
    )
    args = parser.parse_args(argv)

    if args.threats_json == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(args.threats_json).read_text(encoding="utf-8")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid --threats-json: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("Error: --threats-json must be a JSON object", file=sys.stderr)
        return 2

    report = build_report(payload)
    output_text = render(report, args.format)

    if args.dry_run or args.output is None:
        sys.stdout.write(output_text)
    else:
        # ``--output`` may come from a model-assembled value: reject any '..'
        # traversal segment in the supplied path, then sanitize the filename
        # component (defense in depth, mirroring the other skill/scripts/).
        if ".." in Path(args.output).parts:
            print("Error: --output must not contain '..' segments", file=sys.stderr)
            return 2
        out_path = Path(args.output)
        safe_name = _safe_filename(out_path.name)
        dest = out_path.parent / safe_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(output_text, encoding="utf-8")
        print(
            json.dumps(
                {
                    "spec_title": report.spec_title,
                    "threat_count": len(report.threats),
                    "ai_quality_run_count": len(report.ai_quality_runs),
                    "written": str(dest),
                },
                indent=2,
            )
        )

    if args.max_severity:
        breaching = check_max_severity(report, args.max_severity)
        if breaching:
            ids = ", ".join(t.id for t in breaching)
            print(
                f"[threat-model] GATE FAILED: {len(breaching)} threat(s) exceed "
                f"--max-severity={args.max_severity}: {ids}",
                file=sys.stderr,
            )
            return 4

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
