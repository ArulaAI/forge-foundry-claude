"""S60 /data-flow-map — Stage D: Retention marker detection.

Parses three forms of retention markers:
  1. Comment marker: # retention: 90d  or  # retention: GDPR-Art-17-erasure
  2. Decorator:     @retention("90d")  or  @personal_data(retention_days=90)
  3. YAML config:   per-field retention_days in classification YAML entries

For each PIISource, checks if any retention marker applies and sets
RetentionStatus to explicit / missing / expired.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple

from primitives._shared.scan_exclusions import is_excluded as _is_excluded
from primitives.data_flow_map.shared.models import PIISource, RetentionMarker

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.data_flow_map.shared.classification_loader import ClassificationEntry

# ---------------------------------------------------------------------------
# Retention comment pattern
# ---------------------------------------------------------------------------

_RETENTION_COMMENT_RE = re.compile(
    r"#\s*retention\s*:\s*([^\s#]+)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Decorator pattern: @retention("90d") or @personal_data(retention_days=90)
# ---------------------------------------------------------------------------

_RETENTION_DECORATOR_RE = re.compile(
    r"""(?x)
    @(?:retention|personal_data)\s*\(
    \s*
    (?:
        ['"]([\w\-]+)['"]          # @retention("90d")
        |
        retention_days\s*=\s*(\d+) # @personal_data(retention_days=90)
    )
    """,
    re.IGNORECASE,
)


class _RetentionHit(NamedTuple):
    field_name: str
    period: str | None
    reference: str


def _parse_period_to_days(period: str) -> int | None:
    """Parse a retention period string to days.  Returns None if unparseable."""
    m = re.match(r"^(\d+)\s*d(?:ays?)?$", period, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.match(r"^(\d+)\s*y(?:ears?)?$", period, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 365
    m = re.match(r"^(\d+)\s*m(?:onths?)?$", period, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 30
    return None


def _scan_file_for_retention_markers(path: Path) -> list[_RetentionHit]:
    """Scan a single file for retention comment + decorator markers.

    Strategy:
    - Comment marker: look at the SAME line or NEXT line for a field name.
      Pattern: ``# retention: 90d`` on same line as ``email = ...``
      or ``# retention: 90d`` on line N, field on line N+1.
    - Decorator marker: ``@retention("90d")`` → field on next line.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    hits: list[_RetentionHit] = []
    lines = source.splitlines()

    for i, line in enumerate(lines, 1):
        # Comment marker — look on same line or next line for field assignment
        m = _RETENTION_COMMENT_RE.search(line)
        if m:
            period_str = m.group(1)
            # Check same line first (e.g., "email = x  # retention: 90d")
            same_line_without_comment = line[: m.start()]
            field_match = re.search(r"\b([a-zA-Z_]\w*)\s*[=:]\s*", same_line_without_comment)
            if not field_match:
                # Look at next line (e.g., "# retention: 90d\nemail = x")
                next_line = lines[i] if i < len(lines) else ""
                field_match = re.search(r"^\s*([a-zA-Z_]\w*)\s*[=:]\s*", next_line)
            field_name = field_match.group(1) if field_match else f"<comment:line{i}>"
            # Filter out keywords that might be accidentally matched
            if field_name in {
                "return",
                "def",
                "class",
                "if",
                "for",
                "while",
                "import",
                "from",
                "with",
                "as",
                "try",
                "except",
                "finally",
                "retention",
                "self",
                "cls",
            }:
                field_name = f"<comment:line{i}>"
            hits.append(
                _RetentionHit(
                    field_name=field_name,
                    period=period_str,
                    reference=f"comment-marker:{path}:{i}",
                )
            )

        # Decorator marker: @retention("90d") → field on next line
        dm = _RETENTION_DECORATOR_RE.search(line)
        if dm:
            period = dm.group(1) or (f"{dm.group(2)}d" if dm.group(2) else None)
            # Look at next line for field name
            next_line = lines[i] if i < len(lines) else ""
            field_match = re.search(r"^\s*(?:def\s+)?([a-zA-Z_]\w*)\s*[=:(]", next_line)
            field_name = field_match.group(1) if field_match else f"<decorator:line{i}>"
            if field_name in {"def", "class", "return", "self"}:
                field_name = f"<decorator:line{i}>"
            hits.append(
                _RetentionHit(
                    field_name=field_name,
                    period=period,
                    reference=f"decorator:{path}:{i}",
                )
            )

    return hits


def _get_yaml_retention(
    field_name: str,
    catalog: list[ClassificationEntry],
) -> str | None:
    """Return retention_days from catalog entry matching field_name, if present."""
    for entry in catalog:
        if entry.retention_days is not None and re.search(entry.pattern, field_name, re.IGNORECASE):
            return f"{entry.retention_days}d"
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check_retention(
    code_path: Path,
    sources: list[PIISource],
    catalog: list[ClassificationEntry] | None = None,
    language: str = "auto",  # noqa: ARG001 — reserved for future per-language scanner selection
    *,
    include_vendored: bool = False,
) -> list[RetentionMarker]:
    """Check retention markers for each PIISource.

    For each source, checks if any of the three retention forms applies:
    - comment marker in source files
    - decorator marker in source files
    - YAML config retention_days in catalog entry

    Returns a RetentionMarker for every PIISource.

    Args:
        code_path: Directory or single file to scan.
        sources: PIISource objects from Stage A.
        catalog: Classification catalog (optional, for YAML-based retention).
        language: 'python', 'java', 'ts', or 'auto'.

    Returns:
        List of RetentionMarker objects, one per PIISource.
    """
    if not sources:
        return []

    # Collect all retention hits from files
    all_hits: list[_RetentionHit] = []
    files: list[Path]
    files = (
        [code_path]
        if code_path.is_file()
        else [
            p
            for p in code_path.rglob("*")
            if not p.is_symlink()
            and not _is_excluded(p, code_path, include_vendored=include_vendored)
        ]
    )

    for file_path in sorted(files):
        if not file_path.is_file():
            continue
        suffix = file_path.suffix.lower()
        if suffix not in {".py", ".ts", ".tsx", ".java", ".js"}:
            continue
        all_hits.extend(_scan_file_for_retention_markers(file_path))

    # Build a lookup: field_name -> _RetentionHit
    hit_by_field: dict[str, _RetentionHit] = {}
    for hit in all_hits:
        hit_by_field[hit.field_name] = hit

    markers: list[RetentionMarker] = []
    for source in sources:
        # 1. Check YAML catalog retention_days
        yaml_period = _get_yaml_retention(source.name, catalog) if catalog else None
        if yaml_period:
            markers.append(
                RetentionMarker(
                    field_or_sink=source.name,
                    retention_period=yaml_period,
                    status="explicit",
                    reference="yaml-catalog",
                )
            )
            continue

        # 2. Check comment / decorator markers
        if source.name in hit_by_field:
            hit = hit_by_field[source.name]
            markers.append(
                RetentionMarker(
                    field_or_sink=source.name,
                    retention_period=hit.period,
                    status="explicit",
                    reference=hit.reference,
                )
            )
            continue

        # 3. Missing retention — GDPR Art. 5(1)(e) storage limitation
        markers.append(
            RetentionMarker(
                field_or_sink=source.name,
                retention_period=None,
                status="missing",
                reference="GDPR-Art-5(1)(e)-storage-limitation",
            )
        )

    return markers
