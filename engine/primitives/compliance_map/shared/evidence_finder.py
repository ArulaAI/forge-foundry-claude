"""S70 /compliance-map — Stage D: evidence finder with anti-hallucination validator.

Skill-first protocol per touched control:
  D.1: regex pre-scan using control.code_signatures against the code_path
       (``build_prompt_items_for_evidence`` emits one prompt per (control, hit)).
  D.2: ``apply_evidence_judgments`` applies the in-session judgments and the
       anti-hallucination file-existence guard — any EvidenceRef whose ``file``
       does not exist on disk is dropped with a stderr warning.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

from primitives._shared.scan_exclusions import is_excluded as _is_excluded
from primitives.compliance_map.shared.models import ControlMatch, EvidenceRef

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compliance-map-specific extra scan exclusions (#941)
#
# These are NOT added to scan_exclusions.DEFAULT_EXCLUDED_DIRS — they are
# compliance-map-local so other walkers (repo-digest, data-flow-map) are
# unaffected.  Gated by include_vendored: when True, these trees are included
# so an operator scanning a repo-as-code project can find evidence in docs.
# ---------------------------------------------------------------------------

#: Directory name segments excluded from compliance evidence scans by default.
#: Matched as whole path components (same mechanic as scan_exclusions.extra_excludes).
_COMPLIANCE_EXTRA_DIR_EXCLUDES: frozenset[str] = frozenset({"docs"})

#: Root-relative POSIX path prefixes excluded from compliance evidence scans by default.
#: Extends DEFAULT_EXCLUDED_SUBPATHS with compliance-specific doc/CI/agent trees.
#: Matched on a proper path boundary (equal-to or under).
_COMPLIANCE_EXTRA_SUBPATH_EXCLUDES: frozenset[str] = frozenset(
    {
        ".github/agents",  # agent-instruction files — not real compliance evidence
        ".github/workflows",  # CI workflow YAML — noisy false-positives
        "TESTING.md",  # testing guide — not an implementation artifact
    }
)


def _compliance_subpath_excluded(rel_posix: str) -> bool:
    """Return True if *rel_posix* equals or is under a compliance-extra excluded subpath.

    Proper path-boundary match (same discipline as scan_exclusions._subpath_is_excluded).
    """
    return any(
        rel_posix == prefix or rel_posix.startswith(prefix + "/")
        for prefix in _COMPLIANCE_EXTRA_SUBPATH_EXCLUDES
    )


# ---------------------------------------------------------------------------
# Internal: regex pre-scan (Stage D.1)
# ---------------------------------------------------------------------------


def _scan_signatures(
    code_signatures: list[str],
    code_path: Path,
    max_hits: int = 10,
    *,
    include_vendored: bool = False,
) -> list[dict[str, object]]:
    """Scan *code_path* for files matching *code_signatures*.

    Args:
        code_signatures: List of regex patterns from the control's code_signatures field.
        code_path: Directory (or file) to scan.
        max_hits: Maximum number of hits to return (to bound LLM calls).
        include_vendored: When True, re-include vendored harness trees in the walk.

    Returns:
        List of dicts with keys: file (str), line (int), snippet (str), pattern (str).
    """
    if not code_signatures:
        return []

    compiled = []
    for sig in code_signatures:
        try:
            compiled.append((sig, re.compile(sig, re.IGNORECASE | re.MULTILINE)))
        except re.error:
            print(
                f"WARNING: invalid code_signature regex {sig!r} — skipping",
                file=sys.stderr,
            )

    hits: list[dict[str, object]] = []
    target = code_path

    if target.is_file():
        files = [target]
    else:
        # Source-extension allowlist plus a few config/doc extensions that frequently
        # carry compliance evidence (Markdown runbooks, .env templates, .conf/.ini,
        # shell scripts).  Determinism (#889): sort the walk so the bounded
        # ``max_hits`` truncation — and therefore whether a round-2 evidence prompt
        # is emitted at all — is a pure function of the inputs, not OS walk order.
        _code_exts = {
            ".py",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".java",
            ".go",
            ".rb",
            ".cs",
            ".php",
            ".sql",
            ".tf",
            ".yaml",
            ".yml",
            ".json",
            ".toml",
            ".ini",
            ".conf",
            ".cfg",
            ".env",
            ".sh",
            ".md",
        }
        files = sorted(
            (
                p
                for p in target.rglob("*")
                if p.is_file()
                and p.suffix in _code_exts
                and not _is_excluded(
                    p,
                    target,
                    include_vendored=include_vendored,
                    extra_excludes=(
                        frozenset() if include_vendored else _COMPLIANCE_EXTRA_DIR_EXCLUDES
                    ),
                )
                and (
                    include_vendored
                    or not _compliance_subpath_excluded(p.relative_to(target).as_posix())
                )
            ),
            key=lambda p: p.as_posix(),
        )

    for fpath in files:
        if len(hits) >= max_hits:
            break
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError) as exc:
            _logger.warning("Skipping %s during evidence scan: %s", fpath, exc)
            continue

        # Path-aware match (line 0): a signature that names the file/path itself
        # (e.g. ``auth`` / ``middleware`` / ``rbac``) resolves even when the file's
        # *content* uses different tokens.  This makes present-but-textually-divergent
        # evidence resolve (e.g. NIST AC-3/IA-2 vs ``api/middleware/auth.py``) instead
        # of over-reporting a gap (#889).  Bounded to one path hit per file so it can
        # never manufacture false positives at volume; the file-existence guard in
        # ``apply_evidence_judgments`` plus the in-session ``is_evidence`` judgment
        # remain the authoritative anti-hallucination gates.
        try:
            rel_for_match = fpath.relative_to(target).as_posix()
        except ValueError:
            rel_for_match = fpath.name
        for sig_str, pattern in compiled:
            if pattern.search(rel_for_match):
                hits.append(
                    {
                        "file": str(fpath),
                        "line": 0,
                        "snippet": f"(path) {rel_for_match}"[:200],
                        "pattern": sig_str,
                    }
                )
                break  # at most one path hit per file
        if len(hits) >= max_hits:
            break

        for line_no, line in enumerate(text.splitlines(), start=1):
            if len(hits) >= max_hits:
                break
            for sig_str, pattern in compiled:
                if pattern.search(line):
                    hits.append(
                        {
                            "file": str(fpath),
                            "line": line_no,
                            "snippet": line.strip()[:200],
                            "pattern": sig_str,
                        }
                    )
                    break  # one hit per line (avoid double-counting)

    return hits


# ---------------------------------------------------------------------------
# Internal: LLM verification (Stage D.2)
# ---------------------------------------------------------------------------


def _build_verification_user_message(
    hit: dict[str, object],
    control: object,
    code_path: Path,
) -> str:
    """Build the user message for an evidence verification LLM call."""
    # Make path relative to code_path for display
    hit_file = str(hit["file"])
    try:
        rel_path = Path(hit_file).relative_to(code_path)
        display_path = str(rel_path)
    except ValueError:
        display_path = hit_file

    from primitives.compliance_map.shared.models import Control  # noqa: PLC0415

    assert isinstance(control, Control)  # type safety; mypy sees this
    ctrl: Control = control

    return (
        f"# Control\n\n"
        f"ID: {ctrl.id}\n"
        f"Title: {ctrl.title}\n"
        f"Family: {ctrl.family}\n"
        f"Evidence hints:\n"
        + "\n".join(f"  - {h}" for h in ctrl.evidence_hints)
        + f"\n\n# Code Hit\n\n"
        f"File: {display_path}\n"
        f"Line: {hit['line']}\n"
        f"Matched pattern: {hit['pattern']}\n"
        f"Snippet: {hit['snippet']}\n\n"
        f"Is this snippet genuine evidence that this control is implemented? "
        f"Return JSON only. Use exactly the file path above (do not invent paths)."
    )


def _parse_verification_response(
    raw: str,
    fallback_file: str,
    fallback_line: int,
    fallback_snippet: str,
) -> dict[str, object]:
    """Parse LLM verification response. Returns dict with is_evidence bool."""
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        # Fallback: treat as not-evidence on parse failure
        return {
            "is_evidence": False,
            "file": fallback_file,
            "line": fallback_line,
            "snippet": fallback_snippet,
            "confidence": "llm",
        }

    if not isinstance(data, dict):
        return {
            "is_evidence": False,
            "file": fallback_file,
            "line": fallback_line,
            "snippet": fallback_snippet,
            "confidence": "llm",
        }

    return data


# ---------------------------------------------------------------------------
# Skill-first helpers
# ---------------------------------------------------------------------------

_EVIDENCE_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "is_evidence": {"type": "boolean"},
        "file": {"type": "string"},
        "line": {"type": "integer"},
        "snippet": {"type": "string"},
        "confidence": {"type": "string", "enum": ["regex", "llm", "spec"]},
    },
    "required": ["is_evidence", "file", "line", "snippet"],
}


def build_prompt_items_for_evidence(
    touched: list[ControlMatch],
    code_path: Path,
    *,
    include_vendored: bool = False,
) -> list[tuple[str, dict[str, object], object]]:
    """Run D.1 regex pre-scan and build one prompt-item spec per (control, hit) pair.

    Returns a list of ``(item_id, hit_dict, ctrl)`` triples.  The caller uses
    these to construct :class:`PromptItem` objects so that the lazy
    ``PromptItem`` / ``ResponseFormat`` import stays inside the CLI handler
    (cold-start budget).  Returning plain tuples keeps this helper importable
    without the protocol module.

    Args:
        touched: ControlMatch list from Stage C judgments.
        code_path: Code directory to scan for regex hits.

    Returns:
        List of (item_id, hit, ctrl) triples.  May be empty if no signatures
        match any file.
    """
    triples: list[tuple[str, dict[str, object], object]] = []
    for match in touched:
        ctrl = match.control
        hits = _scan_signatures(ctrl.code_signatures, code_path, include_vendored=include_vendored)
        for idx, hit in enumerate(hits):
            item_id = f"evidence:{ctrl.id}:{idx}"
            triples.append((item_id, hit, ctrl))
    return triples


def apply_evidence_judgments(
    touched: list[ControlMatch],
    judgment_map: dict[str, object],
    code_path: Path,
    *,
    include_vendored: bool = False,
) -> list[ControlMatch]:
    """Apply in-session evidence judgments and return updated ControlMatch list.

    Applies the same anti-hallucination file-existence guard as ``_verify_hit``:
    any judgment whose returned ``file`` does not exist on disk is silently
    dropped with a stderr warning.

    Args:
        touched: ControlMatch list from Stage C (used to re-run D.1 scan so
            hit indices are stable with those emitted by
            ``build_prompt_items_for_evidence``).
        judgment_map: Dict mapping item id → raw judgment dict.
        code_path: Code directory (used for file-existence resolution).

    Returns:
        Updated list of ControlMatch objects with ``evidence`` populated.
    """
    updated: list[ControlMatch] = []
    for match in touched:
        ctrl = match.control
        hits = _scan_signatures(ctrl.code_signatures, code_path, include_vendored=include_vendored)
        new_evidence: list[EvidenceRef] = []
        for idx, hit in enumerate(hits):
            item_id = f"evidence:{ctrl.id}:{idx}"
            raw_judgment = judgment_map.get(item_id)
            if raw_judgment is None:
                continue
            if isinstance(raw_judgment, dict):
                verified = raw_judgment
            else:
                # JSON string — parse it
                verified = _parse_verification_response(
                    str(raw_judgment),
                    fallback_file=str(hit["file"]),
                    fallback_line=int(str(hit["line"])),
                    fallback_snippet=str(hit["snippet"]),
                )

            if not verified.get("is_evidence"):
                continue

            # Anti-hallucination: file must exist on disk
            evidence_file_str = str(verified.get("file", hit["file"]))
            evidence_path = Path(evidence_file_str)
            if not evidence_path.is_absolute():
                evidence_path = code_path / evidence_file_str
            if not evidence_path.exists():
                print(
                    f"WARNING: dropped fabricated evidence ref to {evidence_file_str!r} "
                    f"(file does not exist under {code_path})",
                    file=sys.stderr,
                )
                continue

            raw_confidence = str(verified.get("confidence", "llm"))
            confidence_val = raw_confidence if raw_confidence in ("regex", "llm", "spec") else "llm"
            new_evidence.append(
                EvidenceRef(
                    file=evidence_file_str,
                    line=int(str(verified.get("line", hit["line"]))),
                    snippet=str(verified.get("snippet", hit["snippet"])),
                    confidence=confidence_val,  # type: ignore[arg-type]
                )
            )

        updated.append(
            ControlMatch(
                control=ctrl,
                status=match.status,
                evidence=new_evidence,
                gap_description=match.gap_description,
                remediation_owner=match.remediation_owner,
            )
        )
    return updated
