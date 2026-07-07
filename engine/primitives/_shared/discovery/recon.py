"""M5 Phase A — ``run_recon`` (file-tree walk + LoC + manifest + doc scan).

Walks ``--code-root`` to a configurable max depth and emits a
:class:`~primitives._shared.discovery.models.ReconReport`
summarising the source tree at a level cheap enough to run before the
LLM-narrative-synthesis stages light up:

* a curated file-tree digest (one line per directory + file, depth-capped)
* lines-of-code per :data:`~primitives._shared.discovery.models.SourceLanguage`
  bucket, with non-mainframe files folded into the ``other`` catch-all
* manifest hits (``build.xml`` / ``pom.xml`` / ``pyproject.toml`` /
  ``requirements.txt`` / ``setup.py`` / ``.classpath`` / ``.project`` /
  ``Makefile``) — recorded as repo-relative path + file size
* existing documentation (``README*`` and ``*.md`` at the root, plus the
  presence of a ``docs/`` directory)

Design spec §M5 Phase A
(`docs/superpowers/specs/2026-06-02-mainframe-playbook-synth-logs-design.md`)
+ issue [#445](https://github.com/arula-ai/forge-foundry-primitives/issues/445).
M5 — like M1 / M2 / M3 / M4 — is **Context-class**: every claim must be
grounded in a real source citation. Phase A is the cheap pass that
bounds the universe Phase B will deep-analyse.

Critical design (matches the M1 Stage A precedent at
``primitives/db_schema_analyze/shared/ddl_parser.py``): a single broken
file (unreadable, undecodable, OS error) must not abort the run. The
walker logs a warning, skips the file, and continues — partial coverage
of a legacy tree beats no coverage. The ``code_root not found`` case is
the lone exception because it is a configuration error, not a
content error.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from typing import TYPE_CHECKING, get_args

from primitives._shared.discovery.models import (
    Confidence,
    MainframeFlavor,
    ReconReport,
    SourceLanguage,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auto-detect — confidence ranking
# ---------------------------------------------------------------------------

#: Numeric rank for the closed :data:`Confidence` Literal so the
#: auto-detect pick can sort. ``high`` > ``medium`` > ``low``. A
#: dict (instead of the obvious Enum) keeps the Literal closed for
#: Pydantic round-trips while still giving the sort a stable key.
_CONFIDENCE_RANK: dict[Confidence, int] = {"high": 3, "medium": 2, "low": 1}


# ---------------------------------------------------------------------------
# Extension → SourceLanguage map
# ---------------------------------------------------------------------------

# Lowercased extension (including the leading ``.``) → ``SourceLanguage``
# bucket. The issue body lists the eight first-class extensions; every
# other file folds into the ``other`` catch-all so the LoC table sums
# to the full tree size (consumers double-check this in the audit).
#
# COBOL has historical extension drift: ``.cbl`` is the modern Micro
# Focus / GnuCOBOL convention; ``.cob`` is the IBM mainframe / vintage
# convention. We accept both so canonical IBM dumps and modern fixtures
# share the same bucket. Copybooks similarly accept ``.cpy`` (modern)
# and ``.copy`` (some IBM dumps emit this).
_EXTENSION_TO_LANGUAGE: dict[str, SourceLanguage] = {
    ".cbl": "cobol",
    ".cob": "cobol",
    ".cpy": "copybook",
    ".copy": "copybook",
    ".pli": "pli",
    ".jcl": "jcl",
    ".bms": "bms",
    ".asm": "asm",
    ".sql": "sql",
    ".py": "python",
}


# ---------------------------------------------------------------------------
# Manifest filenames
# ---------------------------------------------------------------------------

# Filenames Phase A treats as build-descriptor / project-config manifests.
# Match is case-sensitive on POSIX and case-insensitive on Windows — we
# normalise via the lower-cased filename comparison below so callers
# don't need to know. The set is closed (per issue body) — extensions
# arrive via future-work issues.
_MANIFEST_NAMES: frozenset[str] = frozenset(
    {
        "build.xml",
        "pom.xml",
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        ".classpath",
        ".project",
        "Makefile",
    }
)

# Pre-lowered manifest names so the case-insensitive check in _is_manifest does
# not rebuild a set on every file in the tree walk (#739).
_MANIFEST_NAMES_LOWER: frozenset[str] = frozenset(n.lower() for n in _MANIFEST_NAMES)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_recon(
    code_root: Path,
    *,
    max_depth: int = 7,
    mainframe_flavor: MainframeFlavor | str = "cics_cobol",
) -> ReconReport:
    """Walk ``code_root`` and return a :class:`ReconReport`.

    Args:
        code_root: Directory containing the mainframe source tree. The
            walk is depth-capped (see ``max_depth``) but otherwise
            recursive. Symlinks are not followed so a crafted symlink
            cannot escape the supplied root (matches the M1 Stage A
            walker invariant).
        max_depth: Maximum directory depth relative to ``code_root``.
            ``code_root`` itself is depth 0; a file directly inside it
            is depth 1. The design-spec default is 5 (``find -maxdepth
            5``); the issue body broadens it to 7 to accommodate
            CICS-COBOL trees with nested ``src/main/resources/copybooks/<area>/``
            layouts. Values less than 0 are clamped to 0.
        mainframe_flavor: Either a closed :data:`MainframeFlavor`
            Literal carried through to the :class:`ReconReport`, or the
            sentinel ``"auto"`` (M-EPIC-3 / #525): when ``"auto"``,
            Phase A calls :func:`detect_platform_flavor` against every
            registered flavor in :data:`FLAVOR_PACKS` and picks the
            highest-confidence match. The detected flavor is recorded
            on :attr:`ReconReport.detected_flavor` +
            :attr:`detected_flavor_confidence`. If the detected flavor
            is one of the closed :data:`MainframeFlavor` Literal values,
            ``mainframe_flavor`` is set to that value; otherwise it is
            ``None`` (e.g. ``"generic"`` is not a mainframe flavor).
            When auto-detect returns no high/medium-confidence match
            the universal ``"generic"`` flavor is used, and
            ``mainframe_flavor`` is ``None`` — consumers should read
            :attr:`ReconReport.detected_flavor` instead of the
            legacy ``mainframe_flavor`` field for the resolved platform.

    Returns:
        A :class:`ReconReport` with the four substantive fields filled:
        ``file_tree_summary``, ``loc_by_language``, ``manifest_hits``,
        ``existing_doc_inventory``. ``schema_version`` and
        ``generated_at`` come from the model defaults. When auto-detect
        ran, ``detected_flavor`` + ``detected_flavor_confidence`` are
        also populated.

    Raises:
        FileNotFoundError: If ``code_root`` does not exist.
        NotADirectoryError: If ``code_root`` exists but is not a
            directory. (A file path is a configuration error — Phase A
            walks trees, not single files.)
    """
    if not code_root.exists():
        raise FileNotFoundError(f"--code-root not found: {code_root}")
    if not code_root.is_dir():
        raise NotADirectoryError(f"--code-root is not a directory: {code_root}")

    effective_depth = max(max_depth, 0)

    loc_by_language: dict[str, int] = _zero_loc_table()
    manifest_hits: list[str] = []
    file_tree_lines: list[str] = []
    existing_docs: list[str] = []
    # Resolve the root once; _is_root_doc compares against it per file (#739).
    resolved_root = code_root.resolve()

    for entry in _walk(code_root, max_depth=effective_depth):
        rel = entry.relative_to(code_root).as_posix()
        depth = 0 if rel == "." else rel.count("/") + 1
        indent = "  " * depth
        suffix = "/" if entry.is_dir() else ""
        file_tree_lines.append(f"{indent}{entry.name}{suffix}" if rel != "." else f"{entry.name}/")

        if entry.is_dir():
            continue

        # LoC bucket
        language = _classify(entry)
        try:
            loc_by_language[language] += _count_lines(entry)
        except OSError as exc:
            # An unreadable file should not abort the run — log + skip.
            # ``other``-bucket files use the same path so the total still
            # counts the file's existence (zero lines) and the digest
            # records its name.
            logger.warning("recon: could not read %s: %s", entry, exc)

        # Manifest hits — record path + size
        if _is_manifest(entry):
            try:
                size = entry.stat().st_size
            except OSError as exc:  # pragma: no cover — race against the walker
                logger.warning("recon: could not stat manifest %s: %s", entry, exc)
                size = 0
            manifest_hits.append(f"{rel} ({size} bytes)")

        # Existing-doc inventory — README* and *.md at the root only
        # (the design spec calls these out separately from the deep
        # tree walk because they go into §16 Appendices)
        if _is_root_doc(entry, resolved_root):
            existing_docs.append(rel)

    # ``docs/`` directory presence is a stand-alone signal — append it
    # as a sentinel entry so consumers can grep for it cheaply.
    docs_dir = code_root / "docs"
    if docs_dir.is_dir():
        existing_docs.append("docs/")

    # ---- Auto-detect resolution (M-EPIC-3 / #525) ----------------------
    detected_flavor: str | None = None
    detected_confidence: Confidence | None = None
    if mainframe_flavor == "auto":
        detected_flavor, detected_confidence = detect_platform_flavor(
            code_root, manifest_hits=manifest_hits
        )
        # Universality: when no specific flavor returns high/medium confidence,
        # ``detect_platform_flavor`` returns ``None`` — resolve to the universal
        # language-agnostic ``"generic"`` flavor (NOT the mainframe default) so
        # discovery produces a useful inventory for ANY repo, whatever its stack.
        if detected_flavor is None:
            detected_flavor = "generic"
            detected_confidence = "low"
        # Closed-Literal projection back onto ``mainframe_flavor``:
        # if the detected name happens to be one of the v1 mainframe
        # Literal values, surface it on the legacy field too so existing
        # M-EPIC-2 consumers (Phase B / C / D) reading ``mainframe_flavor``
        # see the same value the auto-detect picked. For non-mainframe
        # flavors (e.g. ``"generic"``, ``"python"``, ``"java_ee"``) the
        # field is ``None`` — consumers should read ``detected_flavor``
        # for the resolved platform name.
        resolved: MainframeFlavor | None = (
            detected_flavor  # type: ignore[assignment]
            if detected_flavor in get_args(MainframeFlavor)
            else None
        )
    else:
        # The non-auto path keeps the historical contract: the caller
        # passes a closed Literal value, we trust it. (The CLI maps
        # kebab→snake before the handoff; out-of-set values reach this
        # function only via direct programmatic misuse, in which case
        # Pydantic surfaces the ValidationError below.)
        resolved = mainframe_flavor  # type: ignore[assignment]

    return ReconReport(
        code_root=str(code_root),
        mainframe_flavor=resolved,
        file_tree_summary="\n".join(file_tree_lines),
        loc_by_language=loc_by_language,
        manifest_hits=manifest_hits,
        existing_doc_inventory=existing_docs,
        detected_flavor=detected_flavor,
        detected_flavor_confidence=detected_confidence,
    )


# ---------------------------------------------------------------------------
# Auto-detect — walk every registered flavor and pick the winner
# ---------------------------------------------------------------------------


def detect_platform_flavor(
    code_root: Path,
    *,
    manifest_hits: list[str],
) -> tuple[str | None, Confidence]:
    """Pick the registered flavor with the highest ``detect()`` confidence.

    Walks every entry in
    :data:`primitives._shared.discovery.flavor.FLAVOR_PACKS`
    (built-ins + entry-point registered + runtime-registered third
    parties), calls each pack's :meth:`FlavorParser.detect` against
    ``code_root``, and returns the highest-confidence winner.

    Tiebreaker: lexicographic on the flavor's registered name. This is
    a determinism guarantee, not a heuristic-quality decision — the
    design spec §"Conflict resolution" mandates "tie-break on
    lexicographic name order when two flavors return the same
    confidence and no manifest-specificity signal applies".

    Args:
        code_root: Directory the dispatcher is considering. Each
            registered flavor receives the same path; each is
            responsible for cheap manifest sniffing without expanding
            the walk surface.
        manifest_hits: The Phase A manifest scan output (repo-relative
            ``pyproject.toml`` / ``pom.xml`` / etc. hits with size
            annotations). Reserved for future per-flavor heuristics
            that want to short-circuit on manifest presence without
            re-walking the tree; the built-in flavor packs ignore it
            today, but the parameter is part of the public contract so
            third-party packs can opt in. Kept as a keyword-only
            argument so the signature can evolve without breaking
            callers.

    Returns:
        A ``(flavor_name, confidence)`` tuple. ``flavor_name`` is
        ``None`` only when no registered pack returns ``high`` or
        ``medium`` confidence — the caller should treat that case as
        "fall back to the v1 default and warn" rather than as a hard
        error (per the spec §"Auto-detection heuristic" open question
        3 recommendation). In the no-match case the returned
        ``confidence`` is ``"low"``.
    """
    # Lazy import: ``primitives._shared.discovery.flavor``
    # imports ``cics_cobol`` which (for typing) imports back from
    # ``models`` — leave the import on the call path so module-load
    # order remains the conservative one Phase A's history has used.
    del manifest_hits  # reserved for future per-flavor heuristics
    from primitives._shared.discovery.flavor import FLAVOR_PACKS  # noqa: PLC0415

    if not FLAVOR_PACKS:
        return (None, "low")

    # ``(rank, lexicographic-name)`` so ``max()`` picks the highest
    # rank first, then ties break lexicographically. ``max()`` returns
    # the *largest* tuple, so for the lex-tiebreak we negate via
    # ``-key`` — but ``str`` has no negation, so we sort + take the
    # head instead of using max() for deterministic behavior.
    scored: list[tuple[int, str, Confidence]] = []
    for name in sorted(FLAVOR_PACKS):
        pack = FLAVOR_PACKS[name]
        try:
            confidence = pack.detect(code_root)
        except (OSError, ValueError) as exc:
            # A misbehaving pack should not abort auto-detect for every
            # other registered flavor. Log + skip.
            logger.warning(
                "recon: flavor %r raised %s during detect(); treating as 'low'",
                name,
                exc,
            )
            confidence = "low"
        scored.append((_CONFIDENCE_RANK.get(confidence, 0), name, confidence))

    # Sort by (-rank, name) so highest rank wins, lex-asc on tie.
    scored.sort(key=lambda row: (-row[0], row[1]))

    best_rank, best_name, best_confidence = scored[0]
    # "No match" = the best pack still came back ``low``. Surface that
    # honestly so the caller can fall back to the v1 default and warn.
    if best_rank <= _CONFIDENCE_RANK["low"]:
        return (None, "low")
    return (best_name, best_confidence)


# ---------------------------------------------------------------------------
# recon.json writer
# ---------------------------------------------------------------------------


def write_recon_json(report: ReconReport, state_dir: Path, run_id: str) -> Path:
    """Persist ``report`` to ``<state_dir>/<run_id>/recon.json``.

    The state directory + per-run subdirectory are created if missing
    (the M5 CLI calls this with a freshly-minted ``run_id`` so the
    subdirectory is expected to be new). Pretty-printed JSON with a
    trailing newline for diff-friendliness — Phase B / D consumers
    re-parse this without canonical-form expectations.

    Args:
        report: The :class:`ReconReport` to write.
        state_dir: The ``--state-dir`` root (typically
            ``.foundry/context/mainframe/``).
        run_id: Per-run identifier — keeps reruns distinguishable on
            re-entry to ``--resume``.

    Returns:
        The path the JSON was written to.
    """
    target_dir = state_dir / run_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "recon.json"
    payload = report.model_dump(mode="json")
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _zero_loc_table() -> dict[str, int]:
    """Return a fresh LoC table with every ``SourceLanguage`` bucket at 0.

    Pre-populating every bucket guarantees the LoC dict's *shape* is
    stable across runs — consumers can index by language without a
    ``KeyError`` even when the source tree has no Python files. The
    model's :class:`~pydantic.BaseModel` will round-trip the zeros
    losslessly.
    """
    return {lang: 0 for lang in get_args(SourceLanguage)}


def _walk(root: Path, *, max_depth: int) -> Iterable[Path]:
    """Yield ``root`` then every descendant up to ``max_depth``.

    Deterministic order (sorted within each directory) so the curated
    file-tree digest is stable across runs — a test for the digest
    string would otherwise be flaky. Symlinks are skipped so a crafted
    symlink cannot escape the root (matches the M1 Stage A walker
    invariant). Unreadable directories log + skip rather than abort.
    """
    yield root

    if max_depth < 1:
        return

    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError as exc:  # pragma: no cover — defended against
            logger.warning("recon: could not list %s: %s", current, exc)
            continue
        for child in children:
            if child.is_symlink():
                continue
            yield child
            if child.is_dir():
                queue.append((child, depth + 1))


def _classify(path: Path) -> SourceLanguage:
    """Return the ``SourceLanguage`` bucket for ``path``'s extension.

    Unknown extensions fall into the ``other`` catch-all (per issue
    body) so the LoC table sums to the file count of every readable
    file in the tree.
    """
    return _EXTENSION_TO_LANGUAGE.get(path.suffix.lower(), "other")


def _count_lines(path: Path) -> int:
    """Return the line count of ``path``.

    Lines are delimited by ``\\n`` — the trailing-newline convention
    typical of source files (a file without a trailing newline still
    has its last line counted). Binary or non-UTF-8 files decode with
    ``errors="replace"`` so an embedded EBCDIC blob doesn't crash the
    run; the resulting line count is a heuristic but consistent.
    """
    # Stream the file line-by-line rather than reading the whole text + building
    # a splitlines() copy — halves peak memory on multi-MB sources (#739).
    # Universal-newline text mode collapses ``\r\n``/``\r`` to one line each, and a
    # file without a trailing newline still yields its last line — matching the
    # previous ``splitlines()`` semantics (empty file → 0).
    with path.open(encoding="utf-8", errors="replace") as fh:
        return sum(1 for _ in fh)


def _is_manifest(path: Path) -> bool:
    """Return True if ``path`` is one of the closed manifest filenames."""
    name = path.name
    # Case-insensitive comparison so Windows callers don't get spurious
    # misses on ``Makefile`` vs ``makefile``. ``.classpath`` / ``.project``
    # are POSIX-style hidden files and survive lower-casing unchanged.
    return name in _MANIFEST_NAMES or name.lower() in _MANIFEST_NAMES_LOWER


def _is_root_doc(path: Path, resolved_root: Path) -> bool:
    """Return True if ``path`` is a root-level ``README*`` or ``*.md`` file.

    "Root-level" = direct child of ``resolved_root`` (the caller's already-resolved
    ``code_root`` — passed in so this hot per-file check does not re-``.resolve()``
    the root on every entry, #739). Nested docs under a ``docs/`` subdirectory are
    not enumerated individually here — the sentinel ``docs/`` entry on
    :attr:`ReconReport.existing_doc_inventory` captures their presence. Consumers
    that want the per-file listing use Phase B's evidence ledger.
    """
    if not path.is_file():
        return False
    if path.parent.resolve() != resolved_root:
        return False
    name_lower = path.name.lower()
    return name_lower.startswith("readme") or name_lower.endswith(".md")


__all__ = ["detect_platform_flavor", "run_recon", "write_recon_json"]
