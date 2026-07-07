"""C6 /confidence-gate — report dispatcher package.

Exports detect_report_type and parse_report for use by the orchestrator.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from primitives.confidence_gate.shared.report_dispatchers.dispatch import (
    _JUDGMENTS_ENVELOPE_REJECTION,
    _looks_like_judgments_envelope,
    dispatch_report,
    sniff_report_type,
)

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.confidence_gate.shared.models import NormalizedFinding, ReportType


def detect_report_type(report_path: Path) -> ReportType:
    """Load a JSON report file and sniff its report type.

    Args:
        report_path: Path to a JSON report file.

    Returns:
        The detected ReportType.

    Raises:
        ValueError: If the file cannot be read, is not valid JSON,
                    or the report type cannot be determined.
    """
    raw = report_path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Report file is not valid JSON: {report_path}: {exc}") from exc

    report_type = sniff_report_type(data)
    if report_type is None:
        if _looks_like_judgments_envelope(data):
            raise ValueError(f"{report_path}: {_JUDGMENTS_ENVELOPE_REJECTION}")
        raise ValueError(f"Cannot determine report type from: {report_path}")
    return report_type


def parse_report(report_path: Path) -> tuple[ReportType, list[NormalizedFinding]]:
    """Load a JSON report file and extract normalized findings.

    Args:
        report_path: Path to a JSON report file.

    Returns:
        A tuple (report_type, findings).

    Raises:
        ValueError: If the file cannot be read, is not valid JSON,
                    or the report type cannot be determined.
    """
    raw = report_path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Report file is not valid JSON: {report_path}: {exc}") from exc

    return dispatch_report(data)


__all__ = [
    "detect_report_type",
    "dispatch_report",
    "parse_report",
    "sniff_report_type",
]
