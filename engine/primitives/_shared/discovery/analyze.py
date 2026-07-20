"""Discovery engine — Phase B polyglot aggregator (``analyze``).

The polyglot successor to the mainframe-only
:func:`primitives._shared.discovery.inventory.build_program_inventory`.
Where the inventory engine drives the ``cics_cobol`` flavor over COBOL /
copybook files and accumulates the concrete mainframe records, this
module drives *any* resolved flavor's per-file
:meth:`~primitives._shared.discovery.flavor.FlavorParser.parse` over a
broad cross-language extension set and accumulates the six **abstract**
entity records (#522) into an
:class:`~primitives._shared.discovery.models.AnalysisDigest`.

The universal ``"generic"`` flavor handles any stack out of the box; a
specific flavor (``python`` / ``java_ee`` / …) is used when one detects
or is named explicitly. An unknown flavor name falls back to
``"generic"`` rather than crashing — partial coverage of an unknown
stack beats no coverage (mirrors the recon walker's degrade discipline).

**Generic co-parser union (universality invariant, #888):** when a
specific (non-generic) flavor is active, the generic universal parser
runs as a *co-parser* over every file in the same walk. Per-stack flavor
results go into the accumulator first (first-occurrence wins the
dedup); the generic co-parser then surfaces any co-resident assets the
per-stack flavor did not recognise — React components (``.tsx``/``.jsx``),
SQL stored-procedure files (``.sql``), Kubernetes manifests (``.yaml``),
etc. This honours the universality invariant: per-stack flavors are
precision accelerators, not the only source of truth; every polyglot
repo gets a complete cross-language entity inventory regardless of which
primary flavor detected.

This stage is **pure-deterministic** (no LLM): the per-file parse loop
runs HERE in Python, never in-session. Files are processed in sorted
order and entities are de-duplicated by ``name`` within each category
(first occurrence wins) so the output is byte-stable run-over-run.

:func:`analyze_to_dir` additionally writes the three Phase B artefacts —
``analysis.json`` (the :class:`AnalysisDigest`), ``evidence-ledger.jsonl``
(one :class:`EvidenceRow` JSON per line), and ``analysis-digest.yaml``
(the token-budgeted compressed engine digest) — into ``--state-dir``.

Design spec §M5 Phase B
(``docs/superpowers/specs/2026-06-03-polyglot-discovery-design.md``)
+ issue [#638](https://github.com/arula-ai/forge-foundry-primitives/issues/638).
Fix: generic co-parser union
[#888](https://github.com/arula-ai/forge-foundry-primitives/issues/888).
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast

from primitives._shared.discovery.flavor import FLAVORS
from primitives._shared.discovery.models import (
    AnalysisDigest,
    BatchUnit,
    DataStore,
    DeploymentUnit,
    EntryPoint,
    EvidenceRow,
    ExecutionUnit,
    SharedComponent,
    UIElement,
)
from primitives._shared.scan_exclusions import DEFAULT_EXCLUDED_DIRS

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from primitives._shared.discovery.models import (
        DeploymentUnitKind,
        MainframeFlavor,
        ReconReport,
    )
    from primitives._shared.modernization import EngineFacts

logger = logging.getLogger(__name__)


class _NamedEntity(Protocol):
    """Structural view of the one field every abstract entity shares.

    The six abstract models (``ExecutionUnit`` / ``SharedComponent`` / …)
    each carry a ``name``; the accumulator de-dups on it without caring
    which concrete type it is holding.
    """

    name: str


# ---------------------------------------------------------------------------
# Fallback flavor + the six abstract category keys
# ---------------------------------------------------------------------------

#: The universal language-agnostic flavor every unknown / undetected stack
#: resolves to. Always registered in :data:`FLAVORS`; its parser produces a
#: useful inventory for ANY repo via stack-agnostic regex heuristics.
_GENERIC_FLAVOR: Final[str] = "generic"

#: The six abstract entity category keys a flavor ``parse()`` may emit
#: (#522). Order is the canonical category order the evidence ledger and
#: the per-category accumulation iterate in (deterministic output).
_CATEGORY_KEYS: Final[tuple[str, ...]] = (
    "execution_units",
    "shared_components",
    "entry_points",
    "data_stores",
    "ui_elements",
    "batch_units",
)

#: The default ``mainframe_flavor`` carried on the :class:`AnalysisDigest`
#: when no recon report supplies one. The legacy field is a closed
#: ``MainframeFlavor`` Literal and ``"generic"`` is NOT a member, so we keep
#: a valid Literal value here — the resolved-flavor identity lives on the
#: engine digest's ``flavor`` field instead.
_LEGACY_FLAVOR_DEFAULT: Final[MainframeFlavor] = "cics_cobol"


# ---------------------------------------------------------------------------
# Per-category accumulator scratch-state
# ---------------------------------------------------------------------------


class _AbstractAccumulator:
    """Per-category de-duplicating accumulator for the six abstract types.

    One list + one ``seen``-name set per category. ``add`` keeps the
    first occurrence of a given ``name`` within a category and tracks the
    repo-relative source path the entity first came from so the evidence
    ledger can cite it. Files are fed in sorted order so "first wins" is
    deterministic.
    """

    def __init__(self) -> None:
        self.execution_units: list[ExecutionUnit] = []
        self.shared_components: list[SharedComponent] = []
        self.entry_points: list[EntryPoint] = []
        self.data_stores: list[DataStore] = []
        self.ui_elements: list[UIElement] = []
        self.batch_units: list[BatchUnit] = []
        self._seen: dict[str, set[str]] = {key: set() for key in _CATEGORY_KEYS}
        #: ``(category, name) -> repo-relative path`` of the first occurrence.
        self.provenance: dict[tuple[str, str], str] = {}

    def add(self, category: str, entity: _NamedEntity, *, rel_path: str) -> None:
        """Append ``entity`` to ``category`` unless its name was already seen."""
        name = entity.name
        seen = self._seen[category]
        if name in seen:
            return
        seen.add(name)
        self.provenance[category, name] = rel_path
        getattr(self, category).append(entity)


# ---------------------------------------------------------------------------
# Public entry point — analyze
# ---------------------------------------------------------------------------


def analyze(
    code_root: Path,
    *,
    flavor: str,
    recon: ReconReport | None = None,
) -> AnalysisDigest:
    """Aggregate ``code_root`` into a polyglot :class:`AnalysisDigest`.

    Resolves ``flavor`` to a registered parser (falling back to
    ``"generic"`` for any unknown name), walks every source / template /
    config file under ``code_root`` in sorted order, drives the parser
    over each file, and accumulates the six abstract entity categories —
    de-duplicating by ``name`` within each category (first occurrence
    wins). Mirrors the recon walker's OSError-degrade discipline: a single
    unreadable file logs a warning and is skipped rather than aborting the
    run.

    Args:
        code_root: Directory containing the source tree.
        flavor: The resolved flavor name. ``"generic"`` (the universal
            fallback) handles any stack; a specific flavor is used when
            named. An unknown name (not in :data:`FLAVORS`) falls back to
            ``"generic"`` with a logged warning — never crashes.
        recon: The optional Phase A :class:`ReconReport`. When supplied,
            its ``mainframe_flavor`` is carried onto the digest's legacy
            field; otherwise the digest defaults to ``"cics_cobol"`` (the
            legacy Literal default — ``"generic"`` is not a member of the
            closed ``MainframeFlavor`` Literal).

    Returns:
        An :class:`AnalysisDigest` with the six abstract lists populated
        and the concrete mainframe lists left empty.

    Raises:
        FileNotFoundError: If ``code_root`` does not exist.
        NotADirectoryError: If ``code_root`` exists but is not a directory.
    """
    if not code_root.exists():
        raise FileNotFoundError(f"--code-root not found: {code_root}")
    if not code_root.is_dir():
        raise NotADirectoryError(f"--code-root is not a directory: {code_root}")

    resolved_flavor = flavor if flavor in FLAVORS else _GENERIC_FLAVOR
    if resolved_flavor != flavor:
        logger.warning(
            "analyze: flavor %r not registered — falling back to %r",
            flavor,
            _GENERIC_FLAVOR,
        )
    parser = FLAVORS[resolved_flavor]
    # Generic co-parser — runs alongside the resolved flavor to surface
    # co-resident non-primary-stack assets (React components, SQL procs,
    # IaC manifests, …) that the per-stack flavor does not recognise.
    # Only activated when the resolved flavor is NOT already generic;
    # when the resolved flavor IS generic, the co-parser would be a
    # redundant second call over the same function.
    generic_parser = FLAVORS[_GENERIC_FLAVOR] if resolved_flavor != _GENERIC_FLAVOR else None
    acc = _AbstractAccumulator()

    for source_file in _iter_sources(code_root):
        try:
            content = source_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("analyze: could not read %s: %s", source_file, exc)
            continue
        rel_path = _relative_path(source_file, code_root)

        # Per-stack flavor first — its results take dedup priority.
        parsed = parser(source_file, content)
        for category in _CATEGORY_KEYS:
            for entity in parsed.get(category, []):
                acc.add(category, cast("_NamedEntity", entity), rel_path=rel_path)

        # Generic co-parser second — adds any co-resident assets the
        # per-stack flavor did not recognise (dedup keeps first winner,
        # so per-stack results always take precedence).
        if generic_parser is not None:
            generic_parsed = generic_parser(source_file, content)
            for category in _CATEGORY_KEYS:
                for entity in generic_parsed.get(category, []):
                    acc.add(category, cast("_NamedEntity", entity), rel_path=rel_path)

    mainframe_flavor: MainframeFlavor = (
        recon.mainframe_flavor
        if recon is not None and recon.mainframe_flavor is not None
        else _LEGACY_FLAVOR_DEFAULT
    )
    # Attach each batch unit's descriptor path from the accumulator provenance
    # (the file it was first detected in) so e.g. a k8s CronJob cites its
    # ops/k8s/*.yaml instead of surfacing as a bare row (#949).
    batch_units = [
        (
            bu.model_copy(update={"source_path": acc.provenance[("batch_units", bu.name)]})
            if not bu.source_path and ("batch_units", bu.name) in acc.provenance
            else bu
        )
        for bu in acc.batch_units
    ]
    return AnalysisDigest(
        code_root=str(code_root),
        mainframe_flavor=mainframe_flavor,
        # Persist the real resolved flavor (open string) so the continue leg reports
        # the SAME flavor the extract leg detected, not the mainframe Literal default
        # (#1650). resolved_flavor was computed above (line ~209).
        resolved_flavor=resolved_flavor,
        execution_units=acc.execution_units,
        shared_components=acc.shared_components,
        entry_points=acc.entry_points,
        data_stores=acc.data_stores,
        ui_elements=acc.ui_elements,
        batch_units=batch_units,
        deployment_units=_scan_deployment_manifests(code_root),
        security_findings=[],
    )


# ---------------------------------------------------------------------------
# Writer — analyze_to_dir
# ---------------------------------------------------------------------------


def analyze_to_dir(
    code_root: Path,
    *,
    flavor: str,
    state_dir: Path,
    recon: ReconReport | None = None,
    generated_at: datetime | None = None,
) -> AnalysisDigest:
    """Run :func:`analyze` and write the three Phase B artefacts to ``state_dir``.

    Writes ``analysis.json`` (the :class:`AnalysisDigest`),
    ``evidence-ledger.jsonl`` (one :class:`EvidenceRow` JSON per line), and
    ``analysis-digest.yaml`` (the token-budgeted compressed engine digest)
    into ``state_dir`` (created if missing). Determinism: passing a fixed
    ``generated_at`` makes ``analysis.json`` byte-identical run-over-run.

    Args:
        code_root: Directory containing the source tree.
        flavor: The resolved flavor name (see :func:`analyze`).
        state_dir: Output directory the three artefacts are written into.
        recon: The optional Phase A :class:`ReconReport` (carries the
            legacy ``mainframe_flavor`` + ``loc_by_language`` for the
            engine digest).
        generated_at: Optional fixed timestamp for the digest's
            ``generated_at`` (deterministic output); defaults to the model
            default (current UTC) when omitted.

    Returns:
        The :class:`AnalysisDigest` that was written.
    """
    # Lazy imports (yaml + the modernization engine) on the call path — keeps
    # ``analyze`` itself import-light and mirrors recon.py's lazy-import
    # discipline for the heavier downstream surface.
    import yaml  # noqa: PLC0415

    from primitives._shared.modernization import compress  # noqa: PLC0415

    digest = analyze(code_root, flavor=flavor, recon=recon)
    if generated_at is not None:
        digest = digest.model_copy(update={"generated_at": generated_at})

    state_dir.mkdir(parents=True, exist_ok=True)

    # analysis.json — the full AnalysisDigest.
    analysis_path = state_dir / "analysis.json"
    analysis_path.write_text(
        json.dumps(digest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # evidence-ledger.jsonl — one EvidenceRow JSON per line.
    ledger_path = state_dir / "evidence-ledger.jsonl"
    rows = _build_evidence_ledger(digest, code_root, flavor=flavor)
    ledger_path.write_text(
        "".join(row.model_dump_json() + "\n" for row in rows),
        encoding="utf-8",
    )

    # analysis-digest.yaml — the compressed engine digest.
    facts = _to_engine_facts(digest, recon, flavor=flavor)
    digest_path = state_dir / "analysis-digest.yaml"
    digest_path.write_text(
        yaml.safe_dump(
            compress(facts).model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    return digest


# ---------------------------------------------------------------------------
# Evidence ledger
# ---------------------------------------------------------------------------


def _build_evidence_ledger(
    digest: AnalysisDigest,
    code_root: Path,
    *,
    flavor: str,
) -> list[EvidenceRow]:
    """Build one :class:`EvidenceRow` per accumulated abstract entity.

    Deterministic order: category order (``_CATEGORY_KEYS``) then ``name``.
    Best-effort line span: the abstract entities do not all carry line
    numbers, so every row uses ``line_start=1`` / ``line_end=1`` (the model
    requires ``line_end >= 1``). ``path`` is the repo-relative file the
    entity was first accumulated from (re-derived here from the entity's
    ``source_path`` when it carries one, else the ``code_root`` itself —
    the deterministic-walk + dedup guarantees a stable mapping).
    """
    rows: list[EvidenceRow] = []
    for category in _CATEGORY_KEYS:
        entities = sorted(getattr(digest, category), key=lambda e: e.name)
        for entity in entities:
            rows.append(
                EvidenceRow(
                    path=_evidence_path(entity, code_root),
                    line_start=1,
                    line_end=1,
                    claim=f"{category}: {entity.kind} {entity.name}",
                    confidence="medium",
                    cross_references=[],
                )
            )
    del flavor  # reserved — the ledger claim is flavor-neutral
    return rows


def _evidence_path(entity: object, code_root: Path) -> str:
    """Return the repo-relative source path an entity cites.

    ``ExecutionUnit`` and ``SharedComponent`` carry a ``source_path``
    (absolute, as the flavor recorded it); render it relative to
    ``code_root``. The remaining abstract types carry no path field, so
    ``""`` (best-effort) — Phase D surfaces the gap rather than the
    aggregator inventing a citation.
    """
    raw = getattr(entity, "source_path", "")
    if not raw:
        return ""
    try:
        return Path(raw).relative_to(code_root).as_posix()
    except ValueError:
        return raw


# ---------------------------------------------------------------------------
# Engine-facts bridge
# ---------------------------------------------------------------------------


def _to_engine_facts(
    digest: AnalysisDigest,
    recon: ReconReport | None,
    *,
    flavor: str,
) -> EngineFacts:
    """Map an :class:`AnalysisDigest` onto the engine's :class:`EngineFacts`.

    The abstract execution units become the ``units`` (weighted by ``loc``),
    entry points + data stores become the exposed / consumed ``interfaces``,
    shared components + security findings carry through, and ``recon``'s
    ``loc_by_language`` supplies the stack mix. The interpretive fields
    (``domains`` / ``migration_state`` / ``tier_hint``) are empty for M5.
    """
    from primitives._shared.modernization import (  # noqa: PLC0415
        EngineFacts,
        FactsInterface,
        FactsUnit,
    )

    units = [
        FactsUnit(
            name=eu.name,
            kind=eu.kind,
            weight=eu.loc,
            calls_to=tuple(eu.calls_to),
        )
        for eu in digest.execution_units
    ]
    interfaces = [
        FactsInterface(name=ep.name, kind=ep.kind, direction="exposed")
        for ep in digest.entry_points
    ] + [
        FactsInterface(name=ds.name, kind=ds.kind, direction="consumed")
        for ds in digest.data_stores
    ]
    return EngineFacts(
        code_root=digest.code_root,
        flavor=flavor,
        loc_by_language=dict(recon.loc_by_language) if recon is not None else {},
        units=units,
        interfaces=interfaces,
        shared_components=[sc.name for sc in digest.shared_components],
        debt=list(digest.security_findings),
        migration_state=None,
        domains=[],
        tier_hint=None,
        generated_at=digest.generated_at,
    )


# ---------------------------------------------------------------------------
# Walker helpers — mirrors shared/inventory.py + recon.py _iter_sources
# ---------------------------------------------------------------------------

#: Broad source / template / config extension superset the polyglot walk
#: considers — the union of the generic flavor's source + template + config
#: catalogs. Any file whose suffix is here is fed to the parser; everything
#: else is skipped cheaply. Kept local (a superset, deliberately) so a
#: future flavor with an unusual extension can be wired here without
#: coupling on the generic flavor's private frozensets.
_SOURCE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        # source-code
        ".py",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".java",
        ".kt",
        ".go",
        ".rb",
        ".php",
        ".cs",
        ".rs",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cc",
        ".scala",
        ".swift",
        ".ex",
        ".exs",
        ".clj",
        ".cljs",
        ".sql",
        # template / view
        ".html",
        ".htm",
        ".jsp",
        ".jspx",
        ".erb",
        ".ejs",
        ".hbs",
        ".handlebars",
        ".mustache",
        ".vue",
        ".svelte",
        ".razor",
        ".twig",
        ".astro",
        # config (scheduled-work signals)
        ".yml",
        ".yaml",
        ".toml",
        ".ini",
        ".cfg",
        ".properties",
        ".json",
        # mainframe (so a specific mainframe flavor still sees its files)
        ".cbl",
        ".cob",
        ".cpy",
        ".copy",
        ".pli",
        ".jcl",
        ".bms",
        ".asm",
    }
)

#: Filenames (lowercased) treated as parseable even without a known suffix
#: (mirrors the generic flavor's crontab handling).
_EXTENSIONLESS_NAMES: Final[frozenset[str]] = frozenset({"crontab"})


def _iter_sources(root: Path) -> Iterable[Path]:
    """Yield every parseable file under ``root`` in deterministic order.

    Mirrors :func:`primitives._shared.discovery.inventory._iter_sources`:
    breadth-first, sorted within each directory (byte-stable ordering),
    dot-directories skipped, symlinks skipped (a crafted symlink cannot
    escape ``root``), and unreadable directories logged + skipped rather
    than aborting the walk.
    """
    queue: deque[Path] = deque([root])
    while queue:
        current = queue.popleft()
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError as exc:  # pragma: no cover — defended against
            logger.warning("analyze: could not list %s: %s", current, exc)
            continue
        for child in children:
            if child.is_symlink():
                continue
            if child.is_dir():
                # Skip dot-dirs AND build-output / dependency dirs (#1359): a
                # non-dot ``target/`` / ``build/`` / ``node_modules/`` otherwise
                # ingests compiled + test artifacts as source, inflating the
                # inventory and topping §15 with test classes.
                if child.name.startswith(".") or child.name in DEFAULT_EXCLUDED_DIRS:
                    continue
                queue.append(child)
                continue
            if (
                child.suffix.lower() in _SOURCE_EXTENSIONS
                or child.name.lower() in _EXTENSIONLESS_NAMES
            ):
                yield child


def _relative_path(path: Path, code_root: Path) -> str:
    """Return ``path`` rendered relative to ``code_root`` (POSIX style)."""
    try:
        return path.relative_to(code_root).as_posix()
    except ValueError:  # pragma: no cover — defended against
        return path.as_posix()


# ---------------------------------------------------------------------------
# Kubernetes manifest scan — deployment-topology objects (#949)
# ---------------------------------------------------------------------------

#: K8s manifest kinds mapped to ``DeploymentUnitKind`` (lowercased). ``CronJob``
#: is intentionally excluded — scheduled work surfaces as a ``cron_task``
#: ``BatchUnit`` (§12 Automation), not a deployment-topology object (§11).
_K8S_WORKLOAD_KINDS: Final[frozenset[str]] = frozenset(
    {"deployment", "statefulset", "daemonset", "replicaset", "job", "service", "ingress", "pod"}
)

#: A top-level (column-0) ``kind:`` declaration in a manifest document.
_KIND_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^kind:\s*\S", re.MULTILINE)


def _k8s_replicas(doc: dict[str, object]) -> int | None:
    """Best-effort ``spec.replicas`` extraction (None when absent/non-int)."""
    spec = doc.get("spec")
    if isinstance(spec, dict):
        replicas = spec.get("replicas")
        if isinstance(replicas, int) and not isinstance(replicas, bool) and replicas >= 0:
            return replicas
    return None


def _k8s_image(doc: dict[str, object]) -> str | None:
    """Best-effort first-container image from ``spec.template.spec.containers``."""
    spec = doc.get("spec")
    template = spec.get("template") if isinstance(spec, dict) else None
    pod_spec = template.get("spec") if isinstance(template, dict) else None
    containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
    if isinstance(containers, list) and containers and isinstance(containers[0], dict):
        image = containers[0].get("image")
        if isinstance(image, str) and image:
            return image
    return None


def _scan_deployment_manifests(code_root: Path) -> list[DeploymentUnit]:
    """Scan infrastructure manifests for k8s deployment-topology objects (#949).

    Multi-document YAML aware: one :class:`DeploymentUnit` per workload document
    (``Deployment`` / ``StatefulSet`` / ``Service`` / …), carrying
    ``metadata.name``, the lowercase ``kind``, and the repo-relative
    ``source_path`` with the top-level ``kind:`` line. ``CronJob`` documents are
    deliberately skipped here — they surface as ``cron_task`` ``BatchUnit``s
    (§12). Deterministic: files walked sorted, the result sorted by
    ``(kind, name, source_path)`` and deduped on the same key.
    """
    import yaml  # noqa: PLC0415 — lazy, mirrors analyze_to_dir's yaml import

    units: list[DeploymentUnit] = []
    seen: set[tuple[str, str, str]] = set()
    for source_file in _iter_sources(code_root):
        if source_file.suffix.lower() not in {".yaml", ".yml"}:
            continue
        try:
            text = source_file.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover — defended against
            continue
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            continue
        # Each document that carries a top-level ``kind`` consumes one ``^kind:``
        # line, in document order — so the source line aligns to the right doc
        # even across a multi-document file.
        kind_lines = [text.count("\n", 0, m.start()) + 1 for m in _KIND_LINE_RE.finditer(text)]
        rel = _relative_path(source_file, code_root)
        line_idx = 0
        for doc in docs:
            if not isinstance(doc, dict) or "kind" not in doc:
                continue
            line = kind_lines[line_idx] if line_idx < len(kind_lines) else None
            line_idx += 1
            raw_kind = str(doc.get("kind", "")).strip().lower()
            if raw_kind not in _K8S_WORKLOAD_KINDS:
                continue
            metadata = doc.get("metadata")
            name = str(metadata.get("name") or "").strip() if isinstance(metadata, dict) else ""
            if not name:
                name = source_file.stem
            source_path = f"{rel}:{line}" if line is not None else rel
            key = (name, raw_kind, source_path)
            if key in seen:
                continue
            seen.add(key)
            units.append(
                DeploymentUnit(
                    name=name,
                    kind=cast("DeploymentUnitKind", raw_kind),
                    source_path=source_path,
                    replicas=_k8s_replicas(doc),
                    image=_k8s_image(doc),
                )
            )
    units.sort(key=lambda u: (u.kind, u.name, u.source_path))
    return units


__all__ = ["analyze", "analyze_to_dir"]
