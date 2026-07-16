#!/usr/bin/env python3
"""Deterministic scan + filter + render for ``/frontend-design`` (Stages A, C, D, E).

Token-offload: the model supplies its Stage B *judgment* — per-lens findings with
file/line/severity/confidence/lens/component/wcag_criterion/design_token_violated —
as a compact JSON payload.  This stdlib-only script does the deterministic remainder:

* **Stage A** — enumerate ``.tsx/.ts/.jsx/.js`` files (skipping noise dirs + test files),
  filter to a PR diff when ``--git-diff`` is given, detect React components per file via
  documented heuristics (default-export fn, named-export fn, class extends Component,
  arrow/React.FC), build the reviewed-files set + per-file component map.
* **Stage C** — apply the four anti-hallucination guards: **file** DROP, **component**
  CLEAR, **wcag_criterion** CLEAR, **design_token_violated** CLEAR.
* **Stage D** — confidence score → level mapping (``>0.7`` high, ``0.3-0.7`` medium,
  ``<0.3`` low); drop findings below ``--confidence-threshold`` (default ``medium``);
  re-validate design tokens (token not in design-system set → CLEAR, finding kept).
* **Stage E** — compliance linkage: deterministic keyword/lens matching for
  ``session_timeout``, ``pci_masking``, ``a11y_assistive`` against a loaded
  ``.foundry/context/compliance.yaml``; no LLM on this stage.
* **C10/C11 dedup** — move a C10 FM2 or C11 FM6 finding at the same (file, line) as
  an S30 finding to ``ai_quality.deduplicated_findings``; C10/C11 lists are pruned.
* **Render** — markdown (default), json, or sarif.

Runnable with a bare ``python3`` (stdlib only — no installed foundry CLI,
no PyYAML), which is why the skill body can invoke it in any harness session.

Usage::

    python3 fe_review.py --findings-json payload.json \\
        --scan-root . --format markdown --output ./review.md

    python3 fe_review.py --findings-json - --scan-root . --dry-run < payload.json

    # Scan only (no model findings), just emit the file/component inventory:
    python3 fe_review.py --scan-only --scan-root .

Each finding's ``file`` MUST match a scanned path exactly — paths are enumerated
relative to ``--scan-root`` (``path.relative_to(scan_root)``) and the file guard
DROPS any finding whose ``file`` is not in that set. Keep ``--scan-root`` and the
finding paths on the same base (e.g. ``--scan-root .`` with ``src/Button.tsx``, or
``--scan-root ./src`` with ``Button.tsx``).
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

#: Characters disallowed in user-supplied output filename components.
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(value: str) -> str:
    """Collapse anything outside ``[A-Za-z0-9._-]`` to ``_``.

    Prevents a model-supplied or user-supplied name from injecting path
    separators into the output path.
    """
    return _UNSAFE_FILENAME_RE.sub("_", value) or "output"


# ---------------------------------------------------------------------------
# Stage A — File enumeration + component detection
# ---------------------------------------------------------------------------

#: Directories that never contain reviewable frontend source.
_NOISE_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        "dist",
        "build",
        ".next",
        "__tests__",
        "__mocks__",
        ".git",
        "coverage",
        ".turbo",
        "out",
        ".cache",
    }
)

#: File extensions that are in scope.
_FRONTEND_EXTS: frozenset[str] = frozenset({".tsx", ".ts", ".jsx", ".js"})

#: Glob patterns for test / spec / story files (excluded from review).
_NOISE_FILE_PATTERNS: tuple[str, ...] = (
    "*.test.*",
    "*.spec.*",
    "*.stories.*",
    "*.test.ts",
    "*.test.tsx",
    "*.spec.ts",
    "*.spec.tsx",
    "*.stories.ts",
    "*.stories.tsx",
)

# React component detection regexes (heuristics applied per file).
_RE_DEFAULT_EXPORT_FN = re.compile(
    r"^\s*export\s+default\s+function\s+([A-Z][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_RE_NAMED_EXPORT_FN = re.compile(
    r"^\s*export\s+(?:function|async\s+function)\s+([A-Z][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_RE_CLASS_COMPONENT = re.compile(
    r"^\s*(?:export\s+)?class\s+([A-Z][A-Za-z0-9_]*)\s+extends\s+(?:React\.)?(?:Pure)?Component",
    re.MULTILINE,
)
_RE_ARROW_COMPONENT = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Z][A-Za-z0-9_]*)\s*(?::\s*React\.FC[^=]*)?=",
    re.MULTILINE,
)


def _is_noise_file(filename: str) -> bool:
    """True when the file matches any noise-file glob pattern."""
    return any(fnmatch.fnmatch(filename, pat) for pat in _NOISE_FILE_PATTERNS)


def detect_components(source: str) -> set[str]:
    """Return the set of React component names detected in *source*.

    Applies four heuristics in order:
    1. Default-export function with PascalCase name.
    2. Named-export function with PascalCase name.
    3. Class extending (React.)Component.
    4. Arrow function / ``React.FC`` const with PascalCase name.
    """
    names: set[str] = set()
    for pattern in (
        _RE_DEFAULT_EXPORT_FN,
        _RE_NAMED_EXPORT_FN,
        _RE_CLASS_COMPONENT,
        _RE_ARROW_COMPONENT,
    ):
        names.update(m.group(1) for m in pattern.finditer(source))
    return names


def enumerate_files(
    root: Path,
    *,
    file_pattern: str | None = None,
    diff_files: set[str] | None = None,
) -> dict[str, set[str]]:
    """Walk *root*, returning a ``{relative_path: {component_names}}`` dict.

    Skips noise directories and noise file patterns.  When *diff_files* is
    provided only those paths (relative to *root*) are included.  When
    *file_pattern* is provided only files matching the glob are included.
    """
    result: dict[str, set[str]] = {}
    for fpath in root.rglob("*"):
        if not fpath.is_file():
            continue
        # Skip noise directories (any ancestor is a noise dir).
        if any(part in _NOISE_DIRS for part in fpath.relative_to(root).parts[:-1]):
            continue
        if fpath.suffix not in _FRONTEND_EXTS:
            continue
        if _is_noise_file(fpath.name):
            continue
        rel = str(fpath.relative_to(root))
        if file_pattern and not fnmatch.fnmatch(rel, file_pattern):
            continue
        if diff_files is not None and rel not in diff_files:
            continue
        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            source = ""
        result[rel] = detect_components(source)
    return result


def parse_git_diff(diff_output: str, root: Path) -> set[str]:
    """Parse ``git diff --name-only`` output into a set of root-relative paths."""
    paths: set[str] = set()
    for line in diff_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        candidate = Path(stripped)
        # Accept either absolute paths under root or plain relative paths.
        try:
            rel = candidate.relative_to(root)
        except ValueError:
            rel = candidate
        if Path(stripped).suffix in _FRONTEND_EXTS:
            paths.add(str(rel))
    return paths


# ---------------------------------------------------------------------------
# Stage C/D — Anti-hallucination guards + confidence filter
# ---------------------------------------------------------------------------

_WCAG_CRITERION_RE = re.compile(r"^\d+\.\d+\.\d+$")

VALID_SEVERITIES: frozenset[str] = frozenset({"low", "medium", "high", "critical"})
VALID_LENSES: frozenset[str] = frozenset(
    {
        "a11y",
        "component_design",
        "state_management",
        "design_system_alignment",
        "performance",
        "i18n_readiness",
        "mobile_responsive",
    }
)

CONFIDENCE_THRESHOLDS: dict[str, float] = {"high": 0.7, "medium": 0.3, "low": 0.0}


_CONFIDENCE_HIGH: float = 0.7
_CONFIDENCE_MEDIUM: float = 0.3


def score_to_level(score: float) -> str:
    """Map a numeric confidence score to high/medium/low."""
    if score > _CONFIDENCE_HIGH:
        return "high"
    if score >= _CONFIDENCE_MEDIUM:
        return "medium"
    return "low"


@dataclass
class Finding:
    """One normalised S30 finding after anti-hallucination + confidence filter."""

    lens: str
    file: str
    line: int
    severity: str
    confidence_score: float
    confidence_level: str
    title: str
    description: str
    suggestion: str
    component: str | None = None
    wcag_criterion: str | None = None
    wcag_level: str | None = None
    design_token_violated: str | None = None
    linked_controls: list[str] = field(default_factory=list)


def _apply_guards(
    raw: object,
    reviewed_files: set[str],
    components_map: dict[str, set[str]],
    token_set: set[str] | None,
) -> Finding | None:
    """Apply four anti-hallucination guards to a raw model finding dict.

    Returns a :class:`Finding` or ``None`` (when the file guard fires — DROP).
    Non-dict items are silently skipped.
    """
    if not isinstance(raw, dict):
        return None

    file_path = str(raw.get("file", ""))
    # Guard 1: file — DROP if not in reviewed set.
    if file_path not in reviewed_files:
        return None

    # Guard 2: component — CLEAR (finding retained) if not in detected set.
    component = raw.get("component")
    if component and file_path in components_map and component not in components_map[file_path]:
        component = None

    # Guard 3: wcag_criterion — CLEAR if format invalid.
    wcag = raw.get("wcag_criterion")
    if wcag and not _WCAG_CRITERION_RE.match(str(wcag)):
        wcag = None

    # Guard 4: design_token_violated — CLEAR if not in loaded token set.
    token = raw.get("design_token_violated")
    if token and token_set is not None and str(token) not in token_set:
        token = None

    severity = str(raw.get("severity", "medium"))
    if severity not in VALID_SEVERITIES:
        severity = "medium"

    try:
        conf_score = float(raw.get("confidence_score", 0.5))
    except (TypeError, ValueError):
        conf_score = 0.5

    lens = str(raw.get("lens", ""))
    if lens not in VALID_LENSES:
        print(
            f"[frontend-design] WARNING: unknown lens {lens!r} remapped to 'component_design' "
            f"(canonical set: {', '.join(sorted(VALID_LENSES))})",
            file=sys.stderr,
        )
        lens = "component_design"

    return Finding(
        lens=lens,
        file=file_path,
        line=int(raw.get("line", 0) or 0),
        severity=severity,
        confidence_score=conf_score,
        confidence_level=score_to_level(conf_score),
        title=str(raw.get("title", "")),
        description=str(raw.get("description", "")),
        suggestion=str(raw.get("suggestion", "")),
        component=str(component) if component else None,
        wcag_criterion=str(wcag) if wcag else None,
        wcag_level=str(raw.get("wcag_level", "")) or None,
        design_token_violated=str(token) if token else None,
    )


def filter_by_confidence(findings: list[Finding], threshold: str) -> list[Finding]:
    """Drop findings whose confidence level is below *threshold*."""
    min_score = CONFIDENCE_THRESHOLDS.get(threshold, 0.3)
    return [f for f in findings if f.confidence_score >= min_score]


def revalidate_tokens(findings: list[Finding], token_set: set[str] | None) -> list[Finding]:
    """Stage D token re-validation: CLEAR ``design_token_violated`` not in *token_set*."""
    if token_set is None:
        return findings
    out: list[Finding] = []
    for f in findings:
        if f.design_token_violated and f.design_token_violated not in token_set:
            out.append(
                Finding(
                    lens=f.lens,
                    file=f.file,
                    line=f.line,
                    severity=f.severity,
                    confidence_score=f.confidence_score,
                    confidence_level=f.confidence_level,
                    title=f.title,
                    description=f.description,
                    suggestion=f.suggestion,
                    component=f.component,
                    wcag_criterion=f.wcag_criterion,
                    wcag_level=f.wcag_level,
                    design_token_violated=None,
                    linked_controls=list(f.linked_controls),
                )
            )
        else:
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# Stage E — Compliance linkage (deterministic, no LLM)
# ---------------------------------------------------------------------------

#: Keyword cues per BFSI area (lowercase matching).
_COMPLIANCE_CUES: dict[str, dict[str, object]] = {
    "session_timeout": {
        "keywords": {"session timeout", "idle timeout", "auto-logout", "session_timeout"},
        "lenses": set(),
    },
    "pci_masking": {
        "keywords": {"cardholder", "card number", "pan", "mask"},
        "lenses": set(),
    },
    "a11y_assistive": {
        "keywords": {"screen reader", "aria-label", "alt text", "aria", "assistive"},
        "lenses": {"a11y"},
    },
}


def load_compliance_controls(path: Path) -> dict[str, list[str]]:
    """Load ``.foundry/context/compliance.yaml`` into area → control-id list.

    Accepts three YAML shapes without PyYAML:
    - structured ``controls:`` mapping (``id: title``)
    - flat list of ids
    - list-of-dicts with ``id:`` keys

    Returns ``{}`` on absent / unreadable / malformed / empty file.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.strip():
        return {}

    # Minimal YAML parser for the three allowed shapes.
    # We parse enough to extract control ids without importing PyYAML.
    controls: list[str] = []
    with contextlib.suppress(Exception):
        controls = _parse_compliance_yaml(text)
    if not controls:
        return {}

    # Partition controls into areas by keyword/prefix heuristic.
    area_map: dict[str, list[str]] = {area: [] for area in _COMPLIANCE_CUES}
    for ctrl_id in controls:
        lower = ctrl_id.lower()
        if "ac-12" in lower or "8.2.8" in lower:
            area_map["session_timeout"].append(ctrl_id)
        if "3.4" in lower:
            area_map["pci_masking"].append(ctrl_id)
        if "wcag" in lower or "a11y" in lower:
            area_map["a11y_assistive"].append(ctrl_id)
    return area_map


def _parse_compliance_yaml(text: str) -> list[str]:
    """Extract control ids from minimal compliance YAML (stdlib-only).

    Handles three shapes:
    - ``controls:`` mapping block with ``id: title`` lines
    - flat list (``- pci-dss-4.0:3.4.1``)
    - list-of-dicts (``- id: pci-dss-4.0:3.4.1``)
    """
    ids: list[str] = []
    in_controls = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("controls:"):
            in_controls = True
            continue
        if in_controls:
            if line.startswith("-"):
                # list item: "- id: foo" or "- foo"
                item = line[1:].strip()
                if item.startswith("id:"):
                    ids.append(item[3:].strip().strip("'\""))
                elif ":" not in item or item.startswith("pci") or item.startswith("nist"):
                    ids.append(item.strip("'\""))
            elif ":" in line and not line.startswith(" "):
                # mapping key: "pci-dss-4.0:8.2.8: Session timeout"
                # Split on ": " (colon + space) to get the full key which may
                # itself contain colons (e.g. pci-dss-4.0:8.2.8).
                parts = line.split(": ", 1)
                ids.append(parts[0].strip())
            elif line and not line[0].isspace():
                in_controls = False
        elif line.startswith("-"):
            item = line[1:].strip()
            if item.startswith("id:"):
                ids.append(item[3:].strip().strip("'\""))
            elif item:
                ids.append(item.strip("'\""))
    return ids


def _finding_matches_area(finding: Finding, area: str) -> bool:
    """True when *finding* text or lens matches the BFSI area cues."""
    cue = _COMPLIANCE_CUES[area]
    keywords: set[str] = cue["keywords"]  # type: ignore[assignment]
    lenses: set[str] = cue["lenses"]  # type: ignore[assignment]

    haystack = " ".join(
        [
            finding.title,
            finding.description,
            finding.suggestion,
        ]
    ).lower()
    if any(kw in haystack for kw in keywords):
        return True
    return bool(lenses and finding.lens in lenses)


def link_compliance(findings: list[Finding], area_map: dict[str, list[str]]) -> list[Finding]:
    """Stage E: populate ``linked_controls`` on each finding (deterministic)."""
    out: list[Finding] = []
    for f in findings:
        controls: list[str] = []
        for area, ctrl_ids in area_map.items():
            if _finding_matches_area(f, area):
                controls.extend(ctrl_ids)
        out.append(
            Finding(
                lens=f.lens,
                file=f.file,
                line=f.line,
                severity=f.severity,
                confidence_score=f.confidence_score,
                confidence_level=f.confidence_level,
                title=f.title,
                description=f.description,
                suggestion=f.suggestion,
                component=f.component,
                wcag_criterion=f.wcag_criterion,
                wcag_level=f.wcag_level,
                design_token_violated=f.design_token_violated,
                linked_controls=controls,
            )
        )
    return out


# ---------------------------------------------------------------------------
# C10/C11 dedup
# ---------------------------------------------------------------------------


@dataclass
class AiQuality:
    """Aggregated C10/C11 findings after dedup."""

    c10_findings: list[dict[str, object]]
    c11_findings: list[dict[str, object]]
    deduplicated_findings: list[dict[str, object]]


def dedup_ai_quality(
    s30_findings: list[Finding],
    c10_raw: list[object],
    c11_raw: list[object],
) -> AiQuality:
    """Move C10 FM2 / C11 FM6 findings at the same (file, line) as an S30 finding.

    The moved entries go to ``deduplicated_findings``; the S30 finding is canonical.
    """
    s30_keys: set[tuple[str, int]] = {(f.file, f.line) for f in s30_findings}

    deduped: list[dict[str, object]] = []
    c10_kept: list[dict[str, object]] = []
    for item in c10_raw:
        if not isinstance(item, dict):
            c10_kept.append({} if not isinstance(item, dict) else item)
            continue
        fm = str(item.get("failure_mode", ""))
        key = (str(item.get("file", "")), int(item.get("line", 0) or 0))
        if fm == "FM2" and key in s30_keys:
            deduped.append(item)
        else:
            c10_kept.append(item)

    c11_kept: list[dict[str, object]] = []
    for item in c11_raw:
        if not isinstance(item, dict):
            c11_kept.append({} if not isinstance(item, dict) else item)
            continue
        fm = str(item.get("failure_mode", ""))
        key = (str(item.get("file", "")), int(item.get("line", 0) or 0))
        if fm == "FM6" and key in s30_keys:
            deduped.append(item)
        else:
            c11_kept.append(item)

    return AiQuality(
        c10_findings=c10_kept,
        c11_findings=c11_kept,
        deduplicated_findings=deduped,
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

_SARIF_VERSION = "2.1.0"
_SARIF_SCHEMA = "https://schemastore.azurewebsites.net/schemas/json/sarif-2.1.0.json"
_SEVERITY_SARIF: dict[str, str] = {
    "low": "note",
    "medium": "warning",
    "high": "error",
    "critical": "error",
}


def render_markdown(
    findings: list[Finding],
    ai_quality: AiQuality | None,
    *,
    title: str = "Frontend Design Review",
) -> str:
    """Render S30 findings + C10/C11 AI-quality section as Markdown."""
    lines = [f"# {title}", ""]
    if not findings:
        lines += ["_No findings above the confidence threshold._", ""]
    else:
        lines += [
            f"**{len(findings)} findings** across {len({f.file for f in findings})} files.",
            "",
            "| Lens | File | Line | Severity | Confidence | Title |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for f in findings:
            wcag = f" ({f.wcag_criterion})" if f.wcag_criterion else ""
            lines.append(
                f"| {f.lens} | `{f.file}` | {f.line} | {f.severity} "
                f"| {f.confidence_level}{wcag} | {f.title} |"
            )
        lines.append("")
        # Per-finding detail blocks.
        for f in findings:
            lines.append(f"## {f.title}")
            lines.append("")
            lines.append(f"- **Lens**: {f.lens}")
            lines.append(f"- **File**: `{f.file}` line {f.line}")
            if f.component:
                lines.append(f"- **Component**: `{f.component}`")
            lines.append(f"- **Severity**: {f.severity}  **Confidence**: {f.confidence_level}")
            if f.wcag_criterion:
                wcag_level = f" ({f.wcag_level})" if f.wcag_level else ""
                lines.append(f"- **WCAG**: {f.wcag_criterion}{wcag_level}")
            if f.design_token_violated:
                lines.append(f"- **Design token violated**: `{f.design_token_violated}`")
            if f.linked_controls:
                lines.append(f"- **Compliance**: {', '.join(f.linked_controls)}")
            lines.append(f"- **Description**: {f.description}")
            lines.append(f"- **Suggestion**: {f.suggestion}")
            lines.append("")

    if ai_quality:
        lines.append("## AI Quality (C10 / C11)")
        lines.append("")
        total_c10 = len(ai_quality.c10_findings)
        total_c11 = len(ai_quality.c11_findings)
        total_dedup = len(ai_quality.deduplicated_findings)
        lines.append(
            f"- C10 findings: {total_c10}  C11 findings: {total_c11}  "
            f"Deduplicated (overlap): {total_dedup}"
        )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_json(
    findings: list[Finding],
    ai_quality: AiQuality | None,
    code_path: str = "",
) -> str:
    """Render S30 + C10/C11 findings as JSON.

    Emits the ``code_path`` / ``components`` / ``lenses`` top-level keys
    alongside ``findings`` so the report is consumable by C6 ``/confidence-gate``
    (its sniffer identifies S30 by exactly those keys, matching the S11/S40
    sibling renderers' shape — #953). ``components`` and ``lenses`` are derived
    deterministically from the findings (sorted, de-duplicated).
    """

    def _finding_dict(f: Finding) -> dict[str, object]:
        return {
            "lens": f.lens,
            "file": f.file,
            "line": f.line,
            "severity": f.severity,
            "confidence_score": f.confidence_score,
            "confidence_level": f.confidence_level,
            "title": f.title,
            "description": f.description,
            "suggestion": f.suggestion,
            "component": f.component,
            "wcag_criterion": f.wcag_criterion,
            "wcag_level": f.wcag_level,
            "design_token_violated": f.design_token_violated,
            "linked_controls": f.linked_controls,
        }

    components = sorted({f.component for f in findings if f.component})
    lenses = sorted({f.lens for f in findings if f.lens})
    payload: dict[str, object] = {
        "code_path": code_path,
        "components": components,
        "lenses": lenses,
        "findings": [_finding_dict(f) for f in findings],
    }
    if ai_quality:
        payload["ai_quality"] = {
            "c10_findings": ai_quality.c10_findings,
            "c11_findings": ai_quality.c11_findings,
            "deduplicated_findings": ai_quality.deduplicated_findings,
        }
    return json.dumps(payload, indent=2) + "\n"


def render_sarif(
    findings: list[Finding],
    *,
    tool_name: str = "frontend-design",
    tool_version: str = "0.17.0",
) -> str:
    """Render S30 findings as SARIF 2.1.0 JSON."""
    rules: list[dict[str, object]] = []
    rule_ids: dict[str, str] = {}
    for f in findings:
        rid = f"S30-{f.lens.upper()}"
        if rid not in rule_ids:
            rule_ids[rid] = rid
            rules.append(
                {
                    "id": rid,
                    "name": f.lens,
                    "shortDescription": {"text": f"{f.lens} finding"},
                }
            )

    results: list[dict[str, object]] = []
    for f in findings:
        rid = f"S30-{f.lens.upper()}"
        result: dict[str, object] = {
            "ruleId": rid,
            "level": _SEVERITY_SARIF.get(f.severity, "warning"),
            "message": {"text": f"{f.title} — {f.description}"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f.file},
                        "region": {"startLine": max(1, f.line)},
                    }
                }
            ],
        }
        props: dict[str, object] = {
            "confidence": f.confidence_level,
            "severity": f.severity,
        }
        if f.wcag_criterion:
            props["wcag_criterion"] = f.wcag_criterion
        if f.design_token_violated:
            props["design_token_violated"] = f.design_token_violated
        if f.linked_controls:
            props["linked_controls"] = f.linked_controls
        result["properties"] = props
        results.append(result)

    sarif: dict[str, object] = {
        "$schema": _SARIF_SCHEMA,
        "version": _SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": tool_name,
                        "version": tool_version,
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }
    return json.dumps(sarif, indent=2) + "\n"


# ---------------------------------------------------------------------------
# Design-system token loader (minimal YAML, stdlib-only)
# ---------------------------------------------------------------------------


def load_token_set(path: Path) -> set[str] | None:
    """Load token names from ``.foundry/design-system.yaml``.

    Returns a flat set of all token names across colors/spacing/typography/shadows,
    or ``None`` when the file is absent (heuristic-only mode).
    Malformed file (exists but unreadable/unparseable token section) returns ``None``
    with a stderr warning rather than hard-erroring.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[frontend-design] WARNING: cannot read design-system: {exc}", file=sys.stderr)
        return None

    tokens: set[str] = set()
    in_tokens = False
    in_category = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("tokens:"):
            in_tokens = True
            in_category = False
            continue
        if in_tokens:
            # Category header line (e.g. "  colors:", "  spacing:") — 2-space indent, no leading -
            if re.match(r"^\s{2}[a-z]+:\s*$", raw_line):
                in_category = True
                continue
            # List item under a category: "    - brand.primary"
            if in_category and stripped.startswith("- "):
                token_name = stripped[2:].strip().strip("'\"")
                if token_name:
                    tokens.add(token_name)
                continue
            # End of tokens block if we hit a non-indented key.
            if stripped and not raw_line.startswith(" "):
                in_tokens = False
                in_category = False
    return tokens if tokens else None


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@dataclass
class ReviewResult:
    """Full pipeline result."""

    findings: list[Finding]
    ai_quality: AiQuality | None
    reviewed_files: set[str]
    components_map: dict[str, set[str]]


def run_pipeline(
    payload: dict[str, object],
    scan_root: Path,
    *,
    file_pattern: str | None = None,
    diff_output: str | None = None,
    confidence_threshold: str = "medium",
    design_system_path: Path | None = None,
    compliance_path: Path | None = None,
) -> ReviewResult:
    """Execute the full deterministic pipeline on a model payload.

    *payload* keys:
    - ``findings``: list of raw model finding dicts (Stage B output).
    - ``c10_findings`` (optional): C10 FM1-FM5 raw list.
    - ``c11_findings`` (optional): C11 FM6-FM10 raw list.
    """
    # Stage A — enumerate files + detect components.
    diff_files: set[str] | None = None
    if diff_output is not None:
        diff_files = parse_git_diff(diff_output, scan_root)
    components_map = enumerate_files(scan_root, file_pattern=file_pattern, diff_files=diff_files)
    reviewed_files = set(components_map.keys())

    # Load optional design-system tokens.
    token_set: set[str] | None = None
    if design_system_path:
        token_set = load_token_set(design_system_path)
    else:
        auto_ds = scan_root / ".foundry" / "design-system.yaml"
        if auto_ds.exists():
            token_set = load_token_set(auto_ds)

    # Stage C — anti-hallucination guards.
    raw_findings: list[object] = []
    raw = payload.get("findings")
    if isinstance(raw, list):
        raw_findings = raw
    guarded: list[Finding] = []
    for item in raw_findings:
        f = _apply_guards(item, reviewed_files, components_map, token_set)
        if f is not None:
            guarded.append(f)

    # Stage D — confidence filter + token re-validation.
    filtered = filter_by_confidence(guarded, confidence_threshold)
    revalidated = revalidate_tokens(filtered, token_set)

    # Stage E — compliance linkage.
    area_map: dict[str, list[str]] = {}
    if compliance_path:
        area_map = load_compliance_controls(compliance_path)
    else:
        auto_comp = scan_root / ".foundry" / "context" / "compliance.yaml"
        if auto_comp.exists():
            area_map = load_compliance_controls(auto_comp)
    linked = link_compliance(revalidated, area_map)

    # C10/C11 dedup.
    c10_raw: list[object] = []
    c11_raw: list[object] = []
    raw_c10 = payload.get("c10_findings")
    raw_c11 = payload.get("c11_findings")
    if isinstance(raw_c10, list):
        c10_raw = raw_c10
    if isinstance(raw_c11, list):
        c11_raw = raw_c11
    ai_quality: AiQuality | None = None
    if c10_raw or c11_raw:
        ai_quality = dedup_ai_quality(linked, c10_raw, c11_raw)

    return ReviewResult(
        findings=linked,
        ai_quality=ai_quality,
        reviewed_files=reviewed_files,
        components_map=components_map,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

EXPORT_FORMATS: tuple[str, ...] = ("markdown", "json", "sarif")


def _write_output(output: str, out_path_raw: str, result: ReviewResult) -> int:
    """Write *output* to *out_path_raw*, sanitizing the filename component.

    Returns 0 on success, 2 on any path or write error.
    """
    out_path = Path(out_path_raw)
    parts = out_path.parts
    if ".." in parts:
        print(
            f"Error: --output path contains '..': {out_path_raw!r}",
            file=sys.stderr,
        )
        return 2
    # Sanitize only the filename component.
    safe_name = _safe_filename(out_path.name)
    final_path = out_path.parent / safe_name
    try:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_text(output, encoding="utf-8")
    except OSError as exc:
        print(f"Error: cannot write --output: {exc}", file=sys.stderr)
        return 2
    summary: dict[str, object] = {
        "findings_count": len(result.findings),
        "files_reviewed": len(result.reviewed_files),
        "written": str(final_path),
    }
    if result.ai_quality:
        summary["deduplicated"] = str(len(result.ai_quality.deduplicated_findings))
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic S30 scan + filter + render.")
    parser.add_argument(
        "--findings-json",
        help="Model findings JSON path, or - for stdin.  Required unless --scan-only.",
    )
    parser.add_argument(
        "--scan-root",
        default=".",
        help="Root directory to enumerate frontend files from.",
    )
    parser.add_argument(
        "--format",
        choices=list(EXPORT_FORMATS),
        default="markdown",
        help="Output format (default: markdown).",
    )
    parser.add_argument(
        "--output",
        help="Write output to this path (default: stdout).  "
        "User-supplied value is sanitized against path traversal.",
    )
    parser.add_argument("--file-pattern", help="Glob filter for reviewed files.")
    parser.add_argument(
        "--git-diff",
        help="``git diff --name-only`` output path (or - for stdin) to filter to PR changes.",
    )
    parser.add_argument(
        "--confidence-threshold",
        choices=["low", "medium", "high"],
        default="medium",
    )
    parser.add_argument("--design-system", help="Path to .foundry/design-system.yaml.")
    parser.add_argument("--compliance-context", help="Path to .foundry/context/compliance.yaml.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print to stdout; do not write --output file.",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Enumerate files and components, print JSON inventory, exit.",
    )
    args = parser.parse_args(argv)

    scan_root = Path(args.scan_root)
    if not scan_root.exists():
        print(f"Error: --scan-root {args.scan_root!r} does not exist.", file=sys.stderr)
        return 2

    # --scan-only mode: emit inventory JSON and exit.
    if args.scan_only:
        components_map = enumerate_files(
            scan_root,
            file_pattern=args.file_pattern,
        )
        inventory = {path: sorted(comps) for path, comps in sorted(components_map.items())}
        sys.stdout.write(json.dumps({"scan_root": str(scan_root), "files": inventory}, indent=2))
        sys.stdout.write("\n")
        return 0

    if not args.findings_json:
        print("Error: --findings-json is required (or pass - for stdin).", file=sys.stderr)
        return 2

    # Read model findings payload.
    if args.findings_json == "-":
        raw_text = sys.stdin.read()
    else:
        try:
            raw_text = Path(args.findings_json).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error: cannot read --findings-json: {exc}", file=sys.stderr)
            return 2

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid --findings-json JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("Error: --findings-json must be a JSON object.", file=sys.stderr)
        return 2

    # Read optional git-diff.
    diff_output: str | None = None
    if args.git_diff:
        if args.git_diff == "-":
            diff_output = sys.stdin.read()
        else:
            try:
                diff_output = Path(args.git_diff).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"[frontend-design] WARNING: cannot read --git-diff: {exc}", file=sys.stderr)

    ds_path = Path(args.design_system) if args.design_system else None
    comp_path = Path(args.compliance_context) if args.compliance_context else None

    result = run_pipeline(
        payload,
        scan_root,
        file_pattern=args.file_pattern,
        diff_output=diff_output,
        confidence_threshold=args.confidence_threshold,
        design_system_path=ds_path,
        compliance_path=comp_path,
    )

    # Render.
    fmt = args.format
    if fmt == "markdown":
        output = render_markdown(result.findings, result.ai_quality)
    elif fmt == "json":
        output = render_json(result.findings, result.ai_quality, code_path=str(scan_root))
    else:
        output = render_sarif(result.findings)

    if args.dry_run or not args.output:
        sys.stdout.write(output)
        return 0

    return _write_output(output, args.output, result)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
