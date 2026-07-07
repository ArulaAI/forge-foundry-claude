#!/usr/bin/env python3
"""Deterministic SLO compute + render for ``/slo-author`` (Stages C-det, D, E).

Token-offload: the model supplies its Stage B/C *judgment* — which SLIs (from
the 7 canonical kinds) and their raw target/window proposals + metric/good/total
expressions — as a compact JSON payload. This stdlib-only script does the
deterministic remainder:

* **Stage B guard** — drop any SLI whose ``kind`` is not one of the 7 canonical
  kinds (warn on stderr), and drop malformed (non-dict) items.
* **Stage C (deterministic)** — clamp ``target_percent`` to ``[0, 100]``, snap
  ``window_days`` to the valid set ``{1, 7, 28, 30}`` (nearest), and fill any
  missing target/window from the criticality-tier default.
* **Stage D** — compute ``budget_minutes = (1 - target/100) * window_days * 24 *
  60`` and the three Google SRE Workbook Ch.5 multi-window multi-burn-rate
  alerts (high 14.4x / medium 6x / low 1x).
* **Stage E** — render the Markdown / Prometheus / Grafana / OpenSLO artifacts.

Runnable with a bare ``python3`` (stdlib only — JSON in; Markdown/YAML/JSON out
via string formatting, so no PyYAML and no ``forge-validate`` dependency), which
is why the skill body can invoke it in any harness session.

Usage::

    python3 slo_render.py --slos-json payload.json \\
        --export markdown,prometheus --output-dir ./slo/

    python3 slo_render.py --slos-json - --dry-run < payload.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Characters disallowed in the artifact filename component (path-injection guard).
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(value: str) -> str:
    """Collapse anything outside ``[A-Za-z0-9._-]`` to ``_`` so a payload-supplied
    name (e.g. a service name) cannot inject path separators into the output path."""
    return _UNSAFE_FILENAME_RE.sub("_", value) or "service"


#: The 7 canonical SLI kinds (Stage B closed universe).
CANONICAL_KINDS: tuple[str, ...] = (
    "availability",
    "latency_p50",
    "latency_p95",
    "latency_p99",
    "freshness",
    "correctness",
    "throughput",
)

#: Valid SLO measurement windows in days; ``window_days`` snaps to the nearest.
VALID_WINDOWS: tuple[int, ...] = (1, 7, 28, 30)

#: Per-criticality-tier defaults: (target_percent, window_days).
TIER_DEFAULTS: dict[str, tuple[float, int]] = {
    "tier-1": (99.9, 30),
    "tier-2": (99.5, 28),
    "tier-3": (99.0, 7),
}

#: Multi-window multi-burn-rate alerts: (name, burn_rate, consumed_fraction, window_label).
BURN_ALERTS: tuple[tuple[str, float, float, str], ...] = (
    ("high", 14.4, 0.05, "1h"),
    ("medium", 6.0, 0.10, "6h"),
    ("low", 1.0, 1.0, "window"),
)

EXPORT_FORMATS: tuple[str, ...] = ("markdown", "prometheus", "grafana", "openslo")
_EXT = {"markdown": "md", "prometheus": "yaml", "grafana": "json", "openslo": "yaml"}


@dataclass(frozen=True)
class Alert:
    """One burn-rate alert attached to an SLO."""

    name: str
    burn_rate: float
    consumed_fraction: float
    window_label: str


@dataclass(frozen=True)
class Slo:
    """A normalized SLO: model judgment + deterministic budget/alerts."""

    name: str
    kind: str
    metric: str
    good: str
    total: str
    target_percent: float
    window_days: int

    @property
    def budget_minutes(self) -> float:
        """Error budget in minutes: ``(1 - target/100) * window_days * 24 * 60``."""
        return round((1.0 - self.target_percent / 100.0) * self.window_days * 24 * 60, 4)

    @property
    def alerts(self) -> list[Alert]:
        return [Alert(n, br, cf, wl) for (n, br, cf, wl) in BURN_ALERTS]


def clamp_target(value: float) -> float:
    """Clamp a target percentage into ``[0.0, 100.0]``."""
    return max(0.0, min(100.0, value))


def snap_window(value: int) -> int:
    """Snap a window (days) to the nearest member of :data:`VALID_WINDOWS`."""
    return min(VALID_WINDOWS, key=lambda w: (abs(w - value), w))


def normalize_slo(raw: object, criticality: str) -> Slo | None:
    """Validate + normalize one model-proposed SLI into an :class:`Slo`.

    Returns ``None`` (with a stderr warning) for a non-dict item or an SLI whose
    ``kind`` is outside the 7 canonical kinds. Missing target/window fall back to
    the criticality-tier default.
    """
    if not isinstance(raw, dict):
        print(f"[slo-author] WARNING: dropping non-dict SLI item: {raw!r}", file=sys.stderr)
        return None
    kind = raw.get("kind")
    if kind not in CANONICAL_KINDS:
        print(
            f"[slo-author] WARNING: dropping SLI with non-canonical kind {kind!r} "
            f"(expected one of {', '.join(CANONICAL_KINDS)})",
            file=sys.stderr,
        )
        return None

    default_target, default_window = TIER_DEFAULTS.get(criticality, TIER_DEFAULTS["tier-2"])
    raw_target = raw.get("target_percent")
    raw_window = raw.get("window_days")
    target = (
        clamp_target(float(raw_target)) if isinstance(raw_target, int | float) else default_target
    )
    window = snap_window(int(raw_window)) if isinstance(raw_window, int | float) else default_window

    return Slo(
        name=str(raw.get("name") or kind),
        kind=str(kind),
        metric=str(raw.get("metric", "")),
        good=str(raw.get("good", "")),
        total=str(raw.get("total", "")),
        target_percent=target,
        window_days=window,
    )


@dataclass(frozen=True)
class SloDocument:
    """The full normalized SLO set for a service."""

    service_name: str
    criticality: str
    slos: list[Slo]


def build_document(payload: dict[str, object]) -> SloDocument:
    """Normalize a raw model payload into an :class:`SloDocument`."""
    criticality = str(payload.get("criticality") or "tier-2")
    raw_slos = payload.get("slos")
    items = raw_slos if isinstance(raw_slos, list) else []
    slos = [s for s in (normalize_slo(item, criticality) for item in items) if s is not None]
    return SloDocument(
        service_name=str(payload.get("service_name") or "service"),
        criticality=criticality,
        slos=slos,
    )


# --- Stage E renderers (deterministic) --------------------------------------


def render_markdown(doc: SloDocument) -> str:
    """Human-readable SLO narrative document (error-budget + SLO summary)."""
    total_budget = round(sum(s.budget_minutes for s in doc.slos), 4)
    lines = [
        f"# SLO Report — {doc.service_name}",
        "",
        f"Criticality: **{doc.criticality}**  |  SLO count: **{len(doc.slos)}**"
        f"  |  Total error budget: **{total_budget} min**",
        "",
        "## Error Budget Summary",
        "",
        "| SLI | Kind | Target | Window | Error budget (min) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for s in doc.slos:
        lines.append(
            f"| {s.name} | {s.kind} | {s.target_percent}% | {s.window_days}d | {s.budget_minutes} |"
        )
    lines.append("")
    lines.append("## SLO Detail")
    lines.append("")
    for s in doc.slos:
        lines.append(f"### {s.name} ({s.kind})")
        lines.append("")
        lines.append(f"- **Metric**: `{s.metric}`")
        lines.append(f"- **Good / total**: `{s.good}` / `{s.total}`")
        lines.append(
            f"- **Target**: {s.target_percent}% over {s.window_days}d"
            f" → error budget **{s.budget_minutes} min**"
        )
        lines.append("- **Burn-rate alerts** (Google SRE Workbook Ch.5 multi-window):")
        for a in s.alerts:
            lines.append(
                f"  - **{a.name}**: {a.burn_rate}x burn "
                f"({int(a.consumed_fraction * 100)}% consumed in {a.window_label})"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _yaml_scalar(value: str) -> str:
    """Single-quote a YAML scalar, escaping embedded single quotes."""
    return "'" + value.replace("'", "''") + "'"


def render_prometheus(doc: SloDocument) -> str:
    """Prometheus recording (SLI ratio) + alerting (burn-rate) rules."""
    lines = ["groups:"]
    lines.append(f"  - name: slo_{doc.service_name}_recording")
    lines.append("    rules:")
    for s in doc.slos:
        expr = f"({s.good}) / ({s.total})" if s.good and s.total else s.metric
        lines.append(f"      - record: sli:{s.name}:ratio")
        lines.append(f"        expr: {_yaml_scalar(expr)}")
    lines.append(f"  - name: slo_{doc.service_name}_alerting")
    lines.append("    rules:")
    for s in doc.slos:
        for a in s.alerts:
            lines.append(f"      - alert: SLOBurn_{s.name}_{a.name}")
            threshold = round(a.burn_rate * (1.0 - s.target_percent / 100.0), 6)
            lines.append(f"        expr: {_yaml_scalar(f'(1 - sli:{s.name}:ratio) > {threshold}')}")
            lines.append("        labels:")
            lines.append(f"          severity: {a.name}")
            lines.append(f"          service: {_yaml_scalar(doc.service_name)}")
            lines.append("        annotations:")
            summary = f"{s.name} burning error budget at {a.burn_rate}x"
            lines.append(f"          summary: {_yaml_scalar(summary)}")
    return "\n".join(lines) + "\n"


def render_grafana(doc: SloDocument) -> str:
    """Minimal Grafana dashboard JSON — one stat panel per SLO."""
    panels: list[dict[str, object]] = []
    for i, s in enumerate(doc.slos):
        panels.append(
            {
                "id": i + 1,
                "title": f"{s.name} ({s.kind}) — target {s.target_percent}%",
                "type": "stat",
                "targets": [{"expr": f"sli:{s.name}:ratio", "refId": "A"}],
                "gridPos": {"h": 8, "w": 12, "x": (i % 2) * 12, "y": (i // 2) * 8},
            }
        )
    dashboard = {
        "title": f"SLO — {doc.service_name}",
        "tags": ["slo", doc.criticality],
        "schemaVersion": 39,
        "panels": panels,
    }
    return json.dumps(dashboard, indent=2, sort_keys=True) + "\n"


def render_openslo(doc: SloDocument) -> str:
    """OpenSLO v1alpha documents — one SLO object per SLI."""
    blocks: list[str] = []
    for s in doc.slos:
        blocks.append(
            "\n".join(
                [
                    "apiVersion: openslo/v1alpha",
                    "kind: SLO",
                    "metadata:",
                    f"  name: {_yaml_scalar(s.name)}",
                    "spec:",
                    f"  service: {_yaml_scalar(doc.service_name)}",
                    f"  description: {_yaml_scalar(f'{s.kind} SLO ({doc.criticality})')}",
                    "  objectives:",
                    "    - ratioMetric:",
                    f"        good: {_yaml_scalar(s.good)}",
                    f"        total: {_yaml_scalar(s.total)}",
                    f"      target: {s.target_percent / 100.0}",
                    "  timeWindow:",
                    f"    - duration: {s.window_days}d",
                    "      isRolling: true",
                ]
            )
        )
    return "\n---\n".join(blocks) + "\n"


_RENDERERS = {
    "markdown": render_markdown,
    "prometheus": render_prometheus,
    "grafana": render_grafana,
    "openslo": render_openslo,
}


def render(doc: SloDocument, fmt: str) -> str:
    """Render *doc* to one export format."""
    return _RENDERERS[fmt](doc)


def _parse_exports(raw: str) -> list[str]:
    fmts = [f.strip() for f in raw.split(",") if f.strip()]
    for f in fmts:
        if f not in EXPORT_FORMATS:
            raise SystemExit(f"Error: unknown --export {f!r}; expected from {list(EXPORT_FORMATS)}")
    return fmts or ["markdown"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic SLO compute + render.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Export formats: markdown (human-readable SLO narrative + error-budget summary),\n"
            "  prometheus (recording + alerting rules YAML),\n"
            "  grafana (dashboard JSON), openslo (OpenSLO v1alpha YAML).\n"
            "\n"
            "Quick single-file markdown narrative:\n"
            "  python3 slo_render.py --slos-json payload.json --output slo-report.md\n"
            "\n"
            "Multi-format output directory:\n"
            "  python3 slo_render.py --slos-json payload.json \\\n"
            "    --export markdown,prometheus --output-dir ./slo/"
        ),
    )
    parser.add_argument("--slos-json", required=True, help="SLO payload JSON path, or - for stdin.")
    parser.add_argument(
        "--export",
        default="markdown",
        help=(
            "Comma-separated export formats: "
            "markdown,prometheus,grafana,openslo (default: markdown)."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Write the primary (first --export) format to this single file. "
            "Mutually exclusive with --output-dir when both are specified "
            "(--output takes precedence). "
            "The filename component is sanitized against path traversal."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="./slo/",
        help=(
            "Directory for multi-format artifact output (default: ./slo/). "
            "Ignored when --output is set."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Print primary format; write none.")
    args = parser.parse_args(argv)

    if args.slos_json == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(args.slos_json).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid --slos-json: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("Error: --slos-json must be a JSON object", file=sys.stderr)
        return 2

    doc = build_document(payload)
    fmts = _parse_exports(args.export)

    if args.dry_run:
        sys.stdout.write(render(doc, fmts[0]))
        return 0

    # --output: write the primary format to a single file (like fe_review.py).
    if args.output is not None:
        out_path = Path(args.output)
        if ".." in out_path.parts:
            print(
                f"Error: --output path contains '..': {args.output!r}",
                file=sys.stderr,
            )
            return 2
        safe_name = _safe_filename(out_path.name)
        dest = out_path.parent / safe_name
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(render(doc, fmts[0]), encoding="utf-8")
        except OSError as exc:
            print(f"Error: cannot write --output: {exc}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "service_name": doc.service_name,
                    "criticality": doc.criticality,
                    "slo_count": len(doc.slos),
                    "budgets_min": {s.name: s.budget_minutes for s in doc.slos},
                    "written": str(dest),
                },
                indent=2,
            )
        )
        return 0

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # ``service_name`` comes from the model-assembled payload (derived from a
    # possibly-attacker-controlled spec), so sanitize it out of the filename —
    # a value with path separators must not escape --output-dir.
    safe_service = _safe_filename(doc.service_name)
    written: list[str] = []
    for fmt in fmts:
        dest = out_dir / f"slo-{safe_service}-{fmt}.{_EXT[fmt]}"
        dest.write_text(render(doc, fmt), encoding="utf-8")
        written.append(str(dest))

    print(
        json.dumps(
            {
                "service_name": doc.service_name,
                "criticality": doc.criticality,
                "slo_count": len(doc.slos),
                "budgets_min": {s.name: s.budget_minutes for s in doc.slos},
                "written": written,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
