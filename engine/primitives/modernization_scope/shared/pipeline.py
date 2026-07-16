"""Deterministic synthesizer pipeline for mod-modernization-scope (SP2, PLB).

Orchestrates the app-tier modernization flow (design spec §4-§6):

    discover artifacts → adapters → build_system_map → deconstruct
      → (extract) spec/ADR skeletons + synthesis_prompts.json  (exit 7)
      → (continue) merge authored judgments → specs + ADRs + ROADMAP + refcheck

Requires the four analysis artifacts (functional-map S61, db-schema M1,
api-contract S40, app-digest from mod-app-analysis). A missing *required*
artifact hard-errors naming the analyzer to run (``ArtifactMissingError`` → the
CLI exits 2). Pure/deterministic apart from filesystem I/O and the injected
``generated_at`` for reproducible golden output.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

from primitives._shared.modernization.adapters import (
    PROVISIONS_PREFIX,
    api_to_digest,
    app_digest_to_digest,
    discover_runtime_journeys,
    functional_map_to_digest,
    functional_map_to_entry_points,
    functional_map_to_inventory,
    iac_to_digest,
    runtime_journeys_section,
    runtime_journeys_to_flows,
)
from primitives._shared.modernization.brd import render_brd
from primitives._shared.modernization.context_resolver import (
    build_member_index,
    resolve_context,
)
from primitives._shared.modernization.correlate import build_system_map
from primitives._shared.modernization.deconstruct import render_deconstruction
from primitives._shared.modernization.evidence import (
    ConfirmedMigrationState,
    MigrationEvidence,
    MigrationStates,
    deterministic_state,
    is_uncertain,
    migration_prompt_item,
    score_migration_evidence,
    unverified_placeholder,
)
from primitives._shared.modernization.hla import HlaSource, render_architecture_conformance
from primitives._shared.modernization.iface import http_iface
from primitives._shared.modernization.models import (
    FunctionalInventory,
    ModernizationDigest,
    RepoSet,
    SystemMap,
    SystemMapEdge,
)
from primitives._shared.modernization.refcheck import RefCheckReport
from primitives._shared.modernization.refcheck import check as refcheck_check
from primitives._shared.modernization.render import heading, md_table
from primitives._shared.modernization.scope import (
    render_adr,
    render_spec,
    target_stack_adr_decision,
)
from primitives._shared.modernization.sequencer import order
from primitives._shared.modernization.slice import (
    SliceClosure,
    compute_slice_closure,
    induced_subgraph,
    nodes_for_domains,
)
from primitives._shared.modernization.target_stack import TargetStack
from primitives.functional_map import (
    FunctionalMapReport,
    contexts_from_system_map,
    fallback_contexts,
    slice_functional_map,
)
from primitives.modernization_scope.shared.models import (
    ApiEndpoint,
    ScopeSynthesis,
    validate_synthesis,
)

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ArtifactMissingError(Exception):
    """A required analysis artifact was not found.

    ``message`` is the actionable, analyzer-naming string the CLI prints before
    exiting 2 (e.g. ``run /functional-map first``).
    """


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineResult:
    """Outcome of one ``run`` invocation."""

    exit_code: int
    #: Durable artifact paths written this run (for the CLI's stdout summary).
    written: list[Path] = field(default_factory=list)
    #: Present after the extract leg — the ``synthesis_prompts.json`` path.
    prompts_path: Path | None = None
    #: Present after a --no-synthesis / continue leg — the MODERNIZATION.md path.
    deconstruction_path: Path | None = None


# ---------------------------------------------------------------------------
# Artifact discovery (SP2.2, #1319)
# ---------------------------------------------------------------------------

#: Default discovery locations under ``.foundry/context/`` (relative to the
#: context root). Each maps a logical artifact → (relative path, analyzer slash
#: command to name in a missing-artifact error).
_DISCOVERY: dict[str, tuple[str, str]] = {
    "functional_map": ("functional-map/functional-map.json", "/functional-map"),
    "schema": ("db/schema.json", "/mod-db-schema-analyze"),
    "api": ("api/api-contract.json", "/api-contract-review"),
    "app_digest": ("modernization/{repo}/app-digest.json", "/mod-app-analysis"),
}


@dataclass(frozen=True)
class DiscoveredArtifacts:
    """The four resolved artifact paths."""

    functional_map: Path
    schema: Path
    api: Path
    app_digest: Path


def _first_app_digest(context_root: Path) -> Path | None:
    """Find ``modernization/<repo>/app-digest.json`` under *context_root*.

    Returns the lexicographically-first match so discovery is deterministic when
    several repos were analysed; ``None`` when none exists.
    """
    base = context_root / "modernization"
    if not base.is_dir():
        return None
    matches = sorted(p for p in base.glob("*/app-digest.json") if p.is_file())
    return matches[0] if matches else None


def _first_test_inventory(context_root: Path) -> Path | None:
    """Find ``modernization/<repo>/test-inventory.json`` under *context_root* (SP-G).

    Optional artifact (from ``/mod-test-inventory``): returns the
    lexicographically-first match, or ``None`` when none exists. Absent, the §11
    parity-oracle integration is a no-op and §11 renders synthesized scenarios.
    """
    base = context_root / "modernization"
    if not base.is_dir():
        return None
    matches = sorted(p for p in base.glob("*/test-inventory.json") if p.is_file())
    return matches[0] if matches else None


def _load_runtime_journeys(context_root: Path) -> dict[str, Any] | None:
    """Load ``runtime/runtime-journeys.json`` (from /mod-runtime-explore) if present (#1478).

    Tolerant + optional: absent or unparseable → ``None`` (the additive runtime section
    is a no-op and MODERNIZATION.md stays byte-identical to today).
    """
    found = discover_runtime_journeys(context_root)
    if found is None:
        return None
    try:
        obj = json.loads(found.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def discover_artifacts(
    context_root: Path,
    *,
    functional_map: Path | None = None,
    schema: Path | None = None,
    api: Path | None = None,
    app_digest: Path | None = None,
) -> DiscoveredArtifacts:
    """Auto-discover the four required artifacts under *context_root*.

    Override flags (``functional_map`` / ``schema`` / ``api`` / ``app_digest``)
    take precedence over auto-discovery. A missing *required* artifact raises
    :class:`ArtifactMissingError` naming the analyzer to run.
    """

    def _resolve(logical: str, override: Path | None) -> Path:
        rel, analyzer = _DISCOVERY[logical]
        if override is not None:
            if not override.is_file():
                raise ArtifactMissingError(
                    f"no {logical} artifact at {override} (override) — run {analyzer} first"
                )
            return override
        if logical == "app_digest":
            found = _first_app_digest(context_root)
            if found is None:
                loc = context_root / "modernization" / "<repo>" / "app-digest.json"
                raise ArtifactMissingError(
                    f"no app-digest.json under {loc.parent.parent} — run {analyzer} first"
                )
            return found
        candidate = context_root / rel
        if not candidate.is_file():
            raise ArtifactMissingError(
                f"no {Path(rel).name} under {candidate.parent} — run {analyzer} first"
            )
        return candidate

    return DiscoveredArtifacts(
        functional_map=_resolve("functional_map", functional_map),
        schema=_resolve("schema", schema),
        api=_resolve("api", api),
        app_digest=_resolve("app_digest", app_digest),
    )


# ---------------------------------------------------------------------------
# DB schema → digest (SP2.3): tables = exposed data resources, FKs = edges.
# ---------------------------------------------------------------------------

#: Prefix for the canonical data-resource interface string a table exposes and an
#: app/service unit consumes — the API→DB half of a UI→API→DB thread.
_DATA_IFACE = "data:"


def schema_to_digest(schema_json: dict[str, Any], *, code_root: str) -> ModernizationDigest:
    """Normalize an M1 ``schema.json`` dict into a :class:`ModernizationDigest`.

    Each table becomes an *exposed* ``data:<table>`` interface (the DB unit
    "owns" its tables); each foreign key becomes a *consumed* ``data:<ref>``
    interface (a cross-table dependency). Table names are the key entities.
    Unknown keys are ignored (forward-compatible).
    """
    tables: list[dict[str, Any]] = list(schema_json.get("tables") or [])
    exposed: list[str] = []
    consumed: list[str] = []
    entities: list[str] = []
    for tbl in tables:
        name = str(tbl.get("name") or "").strip()
        if not name:
            continue
        iface = f"{_DATA_IFACE}{name.lower()}"
        if iface not in exposed:
            exposed.append(iface)
        if name not in entities:
            entities.append(name)
        for fk in tbl.get("foreign_keys") or []:
            ref = str(fk.get("referenced_table") or "").strip()
            if ref:
                ref_iface = f"{_DATA_IFACE}{ref.lower()}"
                if ref_iface not in consumed:
                    consumed.append(ref_iface)
    return ModernizationDigest(
        code_root=code_root,
        flavor=str(schema_json.get("source_dialect") or "sql"),
        exposed_interfaces=exposed[:50],
        consumed_interfaces=consumed[:50],
        domains=["Data"],
        key_entities=entities[:20],
        unit_count=len(tables),
    )


# ---------------------------------------------------------------------------
# Build the system map from the four artifacts (SP2.3, #1320)
# ---------------------------------------------------------------------------


def _load(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ArtifactMissingError(f"{path} did not parse to a JSON object")
    return obj


def _handled_routes(functional_map_json: dict[str, Any]) -> list[str]:
    """The ``http_iface`` strings for every functional-map endpoint (the handlers).

    Reuses the shared :func:`http_iface` canonicalizer (#1348) — the *same* join
    contract the functional-map / api-contract adapters use — so these strings are
    byte-identical to the routes the ``.#ui`` unit consumes as nav targets.
    """
    routes: list[str] = []
    seen: set[str] = set()
    for ep in functional_map_json.get("endpoints") or []:
        if not isinstance(ep, dict):
            continue
        path = ep.get("path") or ""
        if not path:
            continue
        iface = http_iface(ep.get("method") or "", path)
        if iface not in seen:
            seen.add(iface)
            routes.append(iface)
    return routes


def build_digests(
    artifacts: DiscoveredArtifacts,
    *,
    code_root: str,
) -> list[ModernizationDigest]:
    """Run the four adapters on the discovered artifacts → one digest per unit.

    Each unit gets a distinct ``code_root`` (the correlation node id) so the
    system-map threads them cross-layer into a connected ``UI → app → DB`` spine:

    * the UI unit (functional-map) consumes HTTP routes as nav targets;
    * the **app/handler unit** exposes those same routes (#1374) — the
      functional-map *endpoints* ARE the handlers the app owns, so ``.#ui``'s
      consumed nav routes resolve to ``.#app`` (not just to the api-contract
      ``.#api`` unit, which would otherwise dead-end);
    * the app/service unit consumes ``data:<table>`` interfaces the DB unit
      exposes (app → DB).

    The api-contract ``.#api`` unit still exposes its declared operations (so a
    genuine API surface like ``/vets.json`` is not orphaned), but the spine no
    longer depends on it: when S40 is skipped the ``.#ui → .#app → .#db`` thread
    still forms.
    """
    fm_json = _load(artifacts.functional_map)
    fm = functional_map_to_digest(fm_json, code_root=f"{code_root}#ui")
    api = api_to_digest(_load(artifacts.api), code_root=f"{code_root}#api")
    app = app_digest_to_digest(_load(artifacts.app_digest), code_root=f"{code_root}#app")
    db = schema_to_digest(_load(artifacts.schema), code_root=f"{code_root}#db")

    # #1374: the app is the handler owner — expose the functional-map routes on it
    # so the UI unit's consumed nav routes thread ``.#ui → .#app`` (a connected
    # spine), not just ``.#ui → .#api``. The app digest normally carries no exposed
    # HTTP surface (its adapter handles the module layer only); merge the handled
    # routes in without losing any exposed interface it already declared.
    handled = _handled_routes(fm_json)
    if handled:
        merged = list(dict.fromkeys([*app.exposed_interfaces, *handled]))
        app = app.model_copy(update={"exposed_interfaces": merged})

    return [fm, api, app, db]


def build_system_map_from_artifacts(
    artifacts: DiscoveredArtifacts,
    *,
    code_root: str,
    entry_points: Sequence[object] | None = None,
    generated_at: datetime | None = None,
) -> SystemMap:
    """Adapters → ``build_system_map``. Cross-layer edges emerge from the join.

    Track B (#1427/#1455): *entry_points* (typed inbound triggers) thread through
    onto the map as typed inbound nodes so the Entry Points rollup renders.

    Discovery / app-analysis capture batch / scheduled / messaging / CLI entry
    points for the stacks they parse, but a plain HTTP web app surfaces its inbound
    surface only in the functional-map's ``endpoints[]``. So we ALWAYS derive HTTP
    entry points from the functional-map artifact and MERGE them with any
    caller-supplied *entry_points* (de-duplicated downstream by ``(kind, name)`` in
    :func:`~primitives._shared.modernization.correlate.build_system_map`). A Spring
    app with both ``@GetMapping`` controllers AND ``@Scheduled`` jobs thus surfaces
    all of them. No functional-map endpoints and no caller entry points → no
    entry-point tier (byte-identical to prior behavior).
    """
    digests = build_digests(artifacts, code_root=code_root)
    http_eps = functional_map_to_entry_points(_load(artifacts.functional_map))
    merged: list[object] = [*(entry_points or ()), *http_eps]
    return build_system_map(digests, entry_points=merged or None, generated_at=generated_at)


def _repo_artifact(context_root: Path, repo_name: str, filename: str) -> Path | None:
    """Per-repo artifact under ``context_root/modernization/<repo-name>/<filename>``.

    Returns the path when it exists as a file, else ``None``.
    """
    candidate = context_root / "modernization" / repo_name / filename
    return candidate if candidate.is_file() else None


def _resolve_repo_source(context_root: Path, repo_path: str) -> Path | None:
    """Resolve a ``RepoEntry.path`` to the repo's on-disk **source** root (MR6, #1555).

    Unlike the app/api/db roles (which read a pre-built analysis *artifact* under
    ``context_root/modernization/<repo>/``), the IaC role walks the repo's actual
    source tree. ``repo.path`` is recorded relative to the PROJECT root — the parent
    of ``.foundry/context`` (i.e. ``context_root.parent.parent``). Resolve against
    that first; fall back to a cwd-relative / absolute interpretation. Returns the
    first candidate that is an existing directory, else ``None`` (the caller then
    contributes no IaC node — never crashes on a missing/misconfigured path).
    """
    candidates: list[Path] = []
    p = Path(repo_path)
    if p.is_absolute():
        candidates.append(p)
    else:
        # ``.foundry/context`` → project root is two levels up.
        project_root = context_root.parent.parent
        candidates.append(project_root / p)
        candidates.append(p)  # cwd-relative fallback
    for cand in candidates:
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return None


def _normalize_provision_key(name: str) -> str:
    """Normalize a provisioned-resource name / unit id for name matching.

    Lowercase, then drop everything that is not alphanumeric — so ``orders_db``,
    ``orders-db``, and ``OrdersDB`` all collapse to ``ordersdb``. Applied to both the
    ``provisions:<name>`` resource and the candidate unit ids/domains so an IaC
    resource named for a service/table matches that unit regardless of separators.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def attach_provision_edges(
    system_map: SystemMap,
    iac_digests: Sequence[ModernizationDigest],
    *,
    generated_at: datetime | None = None,
) -> SystemMap:
    """Post-correlate enrichment (MR6, #1555, Option A): draw ``provisions`` edges.

    For every ``provisions:<name>`` interface an IaC digest exposes, match ``<name>``
    (normalized: lowercase, separators stripped) against the non-IaC unit nodes' ids
    and domains; each match adds a
    ``SystemMapEdge(src=<iac_node>, dst=<unit>, kind="provisions", confidence="heuristic")``.
    Unmatched provisioned resources are fine — an IaC repo provisions plenty a
    modernization-scope repo-set never analyses (VPCs, IAM roles); they simply draw no
    edge (not an error).

    This keeps :func:`~primitives._shared.modernization.correlate.build_system_map`
    UNTOUCHED (it has many callers): each IaC digest already flowed through it as a
    normal ``kind="iac"`` node; we only add edges here, reading the provisioned names
    off the *iac_digests* (the ``SystemMapNode`` carries no interfaces). A map with no
    IaC digests (the common case, and every existing golden fixture) returns
    byte-identically — the early return on empty *iac_digests* guarantees it.
    """
    if not iac_digests:
        return system_map

    iac_node_ids = {d.code_root for d in iac_digests}

    # Build the match index over non-IaC nodes: normalized-key → node id. A node
    # contributes its full id, its trailing token (``orders-db`` from
    # ``../orders-db#db``), and every domain, so a resource named for a service/table
    # matches regardless of separators or the ``#layer`` suffix.
    unit_by_key: dict[str, list[str]] = {}
    for node in system_map.nodes:
        if node.id in iac_node_ids or node.kind in {"external", "shared_lib"}:
            continue
        candidates = {node.id, node.id.rsplit("#", 1)[0].rsplit("/", 1)[-1], *node.domains}
        for cand in candidates:
            key = _normalize_provision_key(cand)
            if key:
                unit_by_key.setdefault(key, []).append(node.id)

    new_edges: set[SystemMapEdge] = set()
    for digest in iac_digests:
        for iface in digest.exposed_interfaces:
            if not iface.startswith(PROVISIONS_PREFIX):
                continue
            resource = iface[len(PROVISIONS_PREFIX) :]
            key = _normalize_provision_key(resource)
            for dst in sorted(set(unit_by_key.get(key, []))):
                new_edges.add(
                    SystemMapEdge(
                        src=digest.code_root,
                        dst=dst,
                        kind="provisions",
                        evidence=iface,
                        confidence="heuristic",
                    )
                )

    if not new_edges:
        return system_map

    merged_edges = sorted(
        {*system_map.edges, *new_edges},
        key=lambda e: (e.src, e.dst, e.kind, e.evidence, e.confidence),
    )
    update: dict[str, Any] = {"edges": merged_edges}
    if generated_at is not None:
        update["generated_at"] = generated_at
    return system_map.model_copy(update=update)


def build_system_map_from_repo_set(
    repo_set: RepoSet,
    context_root: Path,
    *,
    generated_at: datetime | None = None,
) -> SystemMap:
    """Multi-repo (MR2, #1551): build ONE system-map correlating N repos.

    For each repo in *repo_set*, discover its per-repo artifacts under
    ``context_root/modernization/<repo-name>/`` and build role-appropriate
    digests keyed by ``<repo.path>#<layer>``. All repos' digests + entry points
    feed one ``build_system_map`` — cross-repo edges (UI endpoint -> API, API
    data:<table> -> DB) form by interface-name string match in correlate.py.

    ``<repo-name>`` is the basename of ``repo.path`` — the directory
    ``/mod-app-analysis`` writes each repo's artifacts under. Node ids are
    ``<repo.path>#<layer>`` so two repos never collide (their ``repo.path``
    differs). A repo contributing NO artifact at all is surfaced (logged +
    skipped), never silently dropped.
    """
    all_digests: list[ModernizationDigest] = []
    all_entry_points: list[object] = []
    iac_digests: list[ModernizationDigest] = []
    skipped: list[str] = []

    for repo in repo_set.entries:
        repo_name = Path(repo.path).name
        repo_digests: list[ModernizationDigest] = []

        # --- every repo: its app-digest, if present ---
        app_digest: ModernizationDigest | None = None
        app_path = _repo_artifact(context_root, repo_name, "app-digest.json")
        if app_path is not None:
            app_digest = app_digest_to_digest(_load(app_path), code_root=f"{repo.path}#app")

        if repo.role == "ui":
            # The UI repo's functional-map — per-repo location first, then the
            # conventional single-UI-repo fallback.
            fm_path = _repo_artifact(context_root, repo_name, "functional-map.json")
            if fm_path is None:
                conventional = context_root / "functional-map" / "functional-map.json"
                fm_path = conventional if conventional.is_file() else None
            if fm_path is not None:
                fm_json = _load(fm_path)
                repo_digests.append(functional_map_to_digest(fm_json, code_root=f"{repo.path}#ui"))
                all_entry_points.extend(functional_map_to_entry_points(fm_json))
                # #1374: the app owns the handlers — expose the functional-map
                # routes on THIS repo's app digest so ``#ui -> #app`` threads.
                handled = _handled_routes(fm_json)
                if app_digest is not None and handled:
                    merged = list(dict.fromkeys([*app_digest.exposed_interfaces, *handled]))
                    app_digest = app_digest.model_copy(update={"exposed_interfaces": merged})

        elif repo.role == "api":
            api_path = _repo_artifact(context_root, repo_name, "api-contract.json")
            if api_path is None:
                conventional = context_root / "api" / "api-contract.json"
                api_path = conventional if conventional.is_file() else None
            if api_path is not None:
                repo_digests.append(api_to_digest(_load(api_path), code_root=f"{repo.path}#api"))

        elif repo.role == "db":
            schema_path = _repo_artifact(context_root, repo_name, "schema.json")
            if schema_path is None:
                conventional = context_root / "db" / "schema.json"
                schema_path = conventional if conventional.is_file() else None
            if schema_path is not None:
                repo_digests.append(
                    schema_to_digest(_load(schema_path), code_root=f"{repo.path}#db")
                )

        elif repo.role == "iac":
            # MR6 (#1555): IaC node kind. Walk the IaC repo source directly (an IaC
            # repo has no prior analysis artifact) → a ``flavor="iac"`` digest whose
            # provisioned resources are exposed as ``provisions:<name>``. The
            # ``attach_provision_edges`` enrichment (after build_system_map) then
            # draws ``kind="provisions"`` edges from this node to the app/db units it
            # provisions. ``repo.path`` is relative to the PROJECT root (the parent of
            # ``.foundry/context``); resolve against it, falling back to cwd-relative.
            iac_root = _resolve_repo_source(context_root, repo.path)
            iac_digest = (
                iac_to_digest(iac_root, code_root=f"{repo.path}#iac")
                if iac_root is not None
                else None
            )
            if iac_digest is not None:
                repo_digests.append(iac_digest)
                iac_digests.append(iac_digest)

        if app_digest is not None:
            repo_digests.append(app_digest)

        if repo_digests:
            all_digests.extend(repo_digests)
        else:
            skipped.append(repo.path)

    if skipped:
        _LOG.warning(
            "mod-modernization-scope: %d repo(s) contributed no artifact and were skipped: %s",
            len(skipped),
            ", ".join(skipped),
        )

    system_map = build_system_map(
        all_digests,
        entry_points=all_entry_points or None,
        generated_at=generated_at,
    )
    # MR6 (#1555): draw ``provisions`` edges from IaC nodes to the app/db units they
    # provision (Option A post-correlate enrichment). No IaC digests → byte-identical.
    return attach_provision_edges(system_map, iac_digests, generated_at=generated_at)


# ---------------------------------------------------------------------------
# Multi-repo migration-evidence scoring (MR4, #1553)
# ---------------------------------------------------------------------------


def score_repo_set_evidence(
    repo_set: RepoSet,
    context_root: Path,
) -> dict[str, MigrationEvidence]:
    """Score each repo's on-disk tree for its migration-state evidence (MR3 → MR4).

    Runs the MR3 :func:`score_migration_evidence` scorer against each repo's
    resolved source tree and keys the verdict by the repo's ``#app`` node id —
    the natural unit anchor for the repo (the same id the correlated system-map
    carries). A repo whose ``path`` cannot be resolved to an on-disk directory is
    logged and skipped (never crashes) — it simply contributes no evidence, and
    MR5 scopes it IN as an unverified unit.

    Sibling to :func:`build_system_map_from_repo_set` (keeps that signature stable
    so MR2's tests stay green); ``run()`` calls both in the multi-repo branch.
    """
    evidence: dict[str, MigrationEvidence] = {}
    unresolved: list[str] = []
    for repo in repo_set.entries:
        anchor = f"{repo.path}#app"
        repo_root = _resolve_repo_source(context_root, repo.path)
        if repo_root is None:
            unresolved.append(repo.path)
            continue
        evidence[anchor] = score_migration_evidence(repo_root, code_root=anchor)
    if unresolved:
        _LOG.warning(
            "mod-modernization-scope: %d repo(s) could not be resolved for migration "
            "scoring and were skipped: %s",
            len(unresolved),
            ", ".join(unresolved),
        )
    return evidence


def confirm_migration_states(
    evidence: dict[str, MigrationEvidence],
) -> tuple[list[ConfirmedMigrationState], list[dict[str, Any]]]:
    """Split scored evidence into confirmed deterministic states + uncertain prompts.

    Returns ``(states, prompts)``:

    * ``states`` — one :class:`ConfirmedMigrationState` per unit. A confident unit
      (``modern``/``legacy`` at ``high``/``medium``) is recorded ``source="deterministic"``;
      an uncertain unit (``partial`` or low-confidence) gets an ``unverified`` placeholder
      the continue leg overrides with the harness verdict (or leaves ``unverified`` if the
      harness never judged it — the honesty rule).
    * ``prompts`` — one migration-judgment prompt item per uncertain unit (empty when
      every unit was confident). Deterministically ordered by ``code_root``.
    """
    states: list[ConfirmedMigrationState] = []
    prompts: list[dict[str, Any]] = []
    for anchor in sorted(evidence):
        ev = evidence[anchor]
        if is_uncertain(ev):
            states.append(unverified_placeholder(ev))
            prompts.append(migration_prompt_item(ev))
        else:
            states.append(deterministic_state(ev))
    return states, prompts


def _valid_unit_ids(system_map: SystemMap) -> set[str]:
    """The set of unit node ids a harness migration verdict may name."""
    return {n.id for n in system_map.nodes}


def merge_migration_verdicts(
    pending: MigrationStates,
    verdicts: Sequence[dict[str, Any]],
    system_map: SystemMap,
    code_root: Path,
) -> MigrationStates:
    """Merge harness migration verdicts into the extract-leg :class:`MigrationStates`.

    For each verdict: validate its ``code_root`` names a real unit in
    *system_map* (unknown → :class:`ValueError` listing the valid ids, mirroring
    ``feature_resolver.validate_context_selection``); the harness band OVERRIDES
    the deterministic band for that unit, recorded ``source="harness"`` with the
    rationale. Any citation in the rationale is refchecked best-effort — a broken
    ``file:line`` ref is dropped from the rationale, never fatal. An uncertain unit
    the harness did NOT judge stays ``source="unverified"`` (MR5 scopes it IN with
    ⚠️; the honesty rule — never silently dropped).
    """
    valid = _valid_unit_ids(system_map)
    by_root = {v.get("code_root"): v for v in verdicts if isinstance(v, dict)}
    for judged_root in by_root:
        if judged_root not in valid:
            valid_list = ", ".join(sorted(valid)) or "(none)"
            raise ValueError(
                f"migration verdict names unknown unit {judged_root!r}; "
                f"valid units are: {valid_list}"
            )

    merged: list[ConfirmedMigrationState] = []
    for state in pending.states:
        verdict = by_root.get(state.code_root)
        if verdict is None or state.source != "unverified":
            # Confident (deterministic) units and un-judged uncertain units are
            # carried through unchanged — the latter stays ``unverified``.
            merged.append(state)
            continue
        band = verdict.get("band", state.band)
        confidence = verdict.get("confidence", state.confidence)
        rationale = _refcheck_rationale(str(verdict.get("rationale", "")), code_root)
        merged.append(
            ConfirmedMigrationState(
                code_root=state.code_root,
                band=band,
                confidence=confidence,
                source="harness",
                rationale=rationale,
            )
        )
    return MigrationStates(states=tuple(merged))


def _refcheck_rationale(rationale: str, code_root: Path) -> str:
    """Drop any broken ``file:line`` citation from a harness rationale (best-effort).

    Reuses the shared :func:`refcheck_check`. A rationale with no refs, or only
    resolving refs, is returned unchanged; a broken ref's token is stripped so the
    persisted rationale never carries a dangling citation. Never raises."""
    if not rationale:
        return rationale
    try:
        report = refcheck_check(rationale, code_root)
    except Exception:  # noqa: BLE001 — refcheck is best-effort; never fatal here
        return rationale
    out = rationale
    for broken in report.broken:
        out = out.replace(broken.ref, "").replace("  ", " ")
    return out.strip()


# ---------------------------------------------------------------------------
# migration-states.json / migration_prompts.json persistence (MR4, #1553)
# ---------------------------------------------------------------------------


def _write_migration_state(
    run_dir: Path,
    states: MigrationStates,
    prompts: list[dict[str, Any]] | None = None,
) -> None:
    """Persist ``migration-states.json`` (both legs) + ``migration_prompts.json`` (extract).

    MR5 (#1554) reads ``migration-states.json``. The prompts file is written only
    when there are uncertain units to judge (extract leg)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "migration-states.json").write_text(
        json.dumps(states.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    if prompts:
        (run_dir / "migration_prompts.json").write_text(
            json.dumps({"schema_version": "1.0", "prompts": prompts}, indent=2),
            encoding="utf-8",
        )


def _load_migration_state(run_dir: Path) -> MigrationStates | None:
    """Load the extract-leg ``migration-states.json`` (continue leg); ``None`` if absent."""
    path = run_dir / "migration-states.json"
    if not path.is_file():
        return None
    obj = json.loads(path.read_text(encoding="utf-8"))
    return MigrationStates.model_validate(obj)


# ---------------------------------------------------------------------------
# MR5 (#1554) — the "what's-left-to-modernize" scope filter.
#
# Consumes MR4's confirmed migration states to filter the roadmap:
#   * confirmed-``modern`` units → skipped from extraction (demoted to a Boundary
#     Contracts "already-modernized — integrate against, do not rebuild" section).
#   * ``legacy``/``partial`` units → planned normally.
#   * ``unverified`` units → scoped IN (planned) but ⚠️-marked, NEVER dropped
#     (the honesty rule — only confirmed-modern / --modernized-forced units skip).
# Additive/#1408: no migration_states + no --modernized → empty WhatsLeft → the
# sequencer/roadmap are byte-identical.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WhatsLeft:
    """The MR5 (#1554) modern-vs-unverified partition over the system-map units.

    ``modern_units`` are the confirmed-modern (or ``--modernized``-forced) unit node
    ids skipped from the roadmap and demoted to Boundary Contracts. ``unverified_units``
    are the units whose migration state the harness never confirmed — scoped IN
    (planned) but ⚠️-marked. Both empty → an additive no-op (byte-identical roadmap)."""

    modern_units: frozenset[str] = frozenset()
    unverified_units: frozenset[str] = frozenset()

    @property
    def active(self) -> bool:
        return bool(self.modern_units) or bool(self.unverified_units)


def resolve_whats_left(
    system_map: SystemMap,
    migration_states: MigrationStates | None,
    modernized_overrides: set[str],
) -> WhatsLeft:
    """Partition system-map units into modern (skip) vs unverified (plan + ⚠️).

    ``modern_units`` = every :class:`ConfirmedMigrationState` banded ``modern`` (any
    ``source``) → its ``code_root`` node id, PLUS every unit matched by a
    ``--modernized`` override token. A ``--modernized`` token matches by, in order:

    1. **domain / bounded-context label** — :func:`nodes_for_domains` maps the token
       to every unit tagged with that domain (case-insensitive);
    2. **node-id substring** — a token that matches no domain is treated as a role /
       repo / node-id fragment and matches every unit node id containing it
       (case-insensitive substring). This is how a ``--modernized api`` matches the
       ``<repo.path>#app`` anchor of the ``api`` repo (whose only unit node is its
       ``#app`` anchor) without needing per-role node kinds.

    A ``--modernized`` override forces a unit to modern regardless of its evidence band.

    ``unverified_units`` = every state with ``source == "unverified"`` → its node id
    (minus any that a ``--modernized`` override promoted to modern). No migration_states
    and no overrides → an empty :class:`WhatsLeft` (additive no-op, #1408).
    """
    known_ids = {n.id for n in system_map.nodes}
    modern: set[str] = set()
    unverified: set[str] = set()

    if migration_states is not None:
        for state in migration_states.states:
            if state.code_root not in known_ids:
                # A state for a node not in this (possibly scoped) map — ignore it;
                # it cannot be waved or demoted here.
                continue
            if state.band == "modern":
                modern.add(state.code_root)
            elif state.source == "unverified":
                unverified.add(state.code_root)

    # --modernized overrides: force the matched units to modern (evidence-independent).
    for token in modernized_overrides:
        tok = token.strip()
        if not tok:
            continue
        matched = nodes_for_domains(system_map, [tok])
        if not matched:
            low = tok.lower()
            matched = {nid for nid in known_ids if low in nid.lower()}
        modern |= matched

    # A forced-modern unit is never also reported unverified (modern wins the skip).
    unverified -= modern

    return WhatsLeft(
        modern_units=frozenset(modern),
        unverified_units=frozenset(unverified),
    )


def render_modernized_boundary_section(system_map: SystemMap, modern_units: frozenset[str]) -> str:
    """Render the **Boundary Contracts (already-modernized)** section (MR5, #1554).

    The confirmed-modern units are not rebuilt — they are integration boundaries. This
    lists each modern unit + the interface surface others consume from it (the edges
    where it is the ``dst``), so it reads as "the contract you integrate against, do not
    rebuild". Distinct from the #1539 slice-boundary (:func:`render_boundary_section`),
    which documents a scoped slice's out-edge dependencies. Returns ``""`` when there are
    no modern units (additive no-op — nothing appended to MODERNIZATION.md)."""
    if not modern_units:
        return ""
    lines = [
        "",
        heading("Boundary Contracts (already-modernized)", level=2),
        "",
        (
            "The units below are **already modernized** — integrate against them, do "
            "**not** rebuild them. They are excluded from the extraction roadmap; each "
            "is a boundary contract to hold stable. Any consumed interface is listed so "
            "the surviving work knows the surface it integrates against."
        ),
        "",
    ]
    # Consumed-interface surface: edges where a modern unit is the dst.
    consumers_of: dict[str, list[SystemMapEdge]] = {uid: [] for uid in modern_units}
    for edge in system_map.edges:
        if edge.dst in consumers_of:
            consumers_of[edge.dst].append(edge)
    rows: list[list[str]] = []
    for uid in sorted(modern_units):
        edges = sorted(consumers_of[uid], key=lambda e: (e.src, e.kind, e.evidence))
        if edges:
            for edge in edges:
                rows.append([uid, edge.src, edge.kind or "—", edge.evidence or "—"])
        else:
            rows.append([uid, "—", "—", "no consumed interface discovered"])
    lines.append(md_table(["Modern unit", "Consumed by", "Interface", "Evidence"], rows))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Spec / ADR skeletons (SP2.4, #1321)
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    """A filesystem-safe slug for a spec/ADR id or title."""
    out = "".join(c if c.isalnum() else "-" for c in text.lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "item"


def build_spec_skeletons(system_map: SystemMap) -> list[dict[str, Any]]:
    """One spec skeleton per bounded context (domain) in the system-map.

    Contexts come from node ``domains`` labels — a multi-domain node contributes
    to *every* one of its domains' specs (the union, #1375), so a petclinic app
    tagged ``owner``/``pet``/``vet``/``visit`` yields one spec per domain rather
    than collapsing three of them. Nodes with no domains fall into a single
    ``System`` catch-all so there is always at least one spec. The skeleton
    records the member component ids so the body can ground its prose.
    """
    by_domain: dict[str, list[str]] = {}
    for node in system_map.nodes:
        # ``iac`` (MR6, #1555) is a deployment boundary, not an extractable context —
        # skip it here (it surfaces in the deconstruction's Infrastructure section).
        if node.kind in {"external", "shared_lib", "iac"}:
            continue
        for domain in node.domains or ["System"]:
            by_domain.setdefault(domain, []).append(node.id)
    if not by_domain:
        by_domain = {"System": []}
    skeletons: list[dict[str, Any]] = []
    for domain in sorted(by_domain):
        skeletons.append(
            {
                "id": f"spec-{_slug(domain)}",
                "domain": domain,
                "title": f"{domain} — Modernization Specification",
                "components": sorted(by_domain[domain]),
            }
        )
    return skeletons


@dataclass(frozen=True)
class _Scope:
    """A resolved per-feature slice scope (#1542) — the contexts to fully spec + their
    dependency closure. ``active`` is False when no ``--feature``/``--context`` was given,
    in which case every downstream step is byte-identical to a whole-app run (#1408)."""

    active: bool = False
    contexts: list[str] = field(default_factory=list)
    closure: SliceClosure | None = None


def resolve_scope(system_map: SystemMap, scope_contexts: list[str] | None) -> _Scope:
    """Resolve the requested bounded contexts → a :class:`_Scope` (contexts + closure).

    ``scope_contexts`` are already-validated bounded-context (domain) labels — the CLI's
    ``--context`` values, or the harness-synthesized + ``validate_context_selection``-checked
    result of a ``--feature`` resolution. ``None``/empty → an inactive scope (whole-app,
    byte-identical). The closure (in-scope units + their out-edge dependencies) is computed
    once so the spec/slice filter, the boundary section, and the scoped roadmap all agree.
    """
    if not scope_contexts:
        return _Scope(active=False)
    scope_ids = nodes_for_domains(system_map, scope_contexts)
    closure = compute_slice_closure(system_map, scope_ids)
    return _Scope(active=True, contexts=sorted(set(scope_contexts)), closure=closure)


def filter_skeletons_to_scope(
    spec_skeletons: list[dict[str, Any]], scope: _Scope
) -> list[dict[str, Any]]:
    """Keep only the spec skeletons whose domain is in the active scope's contexts.

    An inactive scope returns the skeletons unchanged (whole-app, byte-identical). The
    dependency contexts are NOT specced — they surface as boundary contracts instead
    (see :func:`render_boundary_section`)."""
    if not scope.active:
        return spec_skeletons
    wanted = {c.lower() for c in scope.contexts}
    return [sk for sk in spec_skeletons if str(sk["domain"]).lower() in wanted]


def render_boundary_section(scope: _Scope) -> str:
    """Render the **Boundary Contracts** section for a scoped run (#1542).

    The slice's dependencies (units it consumes interfaces from / data it reads / shared
    libs) are documented here as interface contracts + a "depends on" list — NOT re-specced.
    Each boundary edge names the dependency + the interface evidence that proved the
    dependency. Returns ``""`` for an inactive scope (whole-app runs have no boundary)."""
    if not scope.active or scope.closure is None:
        return ""
    closure = scope.closure
    lines = [
        "",
        heading("Boundary Contracts", level=2),
        "",
        (
            "This slice was modernized in isolation. The units below are **dependencies** — "
            "the slice consumes their interfaces but they are **not re-specced here**; treat "
            "each as a boundary contract to hold stable (or coordinate separately)."
        ),
        "",
    ]
    if closure.dependencies:
        lines.append(f"**Depends on:** {', '.join(closure.dependencies)}")
        lines.append("")
        rows = [
            [edge.src, edge.dst, edge.kind or "—", edge.evidence or "—"]
            for edge in closure.boundary_edges
        ]
        if rows:
            lines.append(
                md_table(["In-scope unit", "Depends on", "Kind", "Interface evidence"], rows)
            )
            lines.append("")
    else:
        lines.append("_No cross-boundary dependencies — this slice is self-contained._")
        lines.append("")
    return "\n".join(lines)


def distribute_frs(
    inventory: FunctionalInventory,
    spec_skeletons: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assign each functional-map FR to the bounded-context spec it belongs to (#1356).

    Deterministic distribution — the FR→context assignment is auditable/reproducible;
    only the spec *prose* is LLM-authored. Matching, in priority order:

    1. The FR's own ``domain`` field, matched case-insensitively against a spec's
       ``domain`` label.
    2. Failing that, the FR falls to the single-context case (one spec) or is
       **deferred** with a reason — never silently dropped.

    Returns a payload: ``by_spec`` (spec-id → its FRs, each with related ACs +
    business rules), ``deferred`` (FRs with no matching context, each carrying a
    ``reason``), and a ``coverage`` record (distributed / deferred / total counts).
    The coverage mirrors functional-map's own "N of M FRs covered" audit (#1342):
    a dropped FR is visible and counted.
    """
    frs = inventory.fr_statements
    # Index ACs + business rules so each FR carries its own.
    acs_by_fr: dict[str, list[dict[str, Any]]] = {}
    for ac in inventory.acceptance_criteria:
        acs_by_fr.setdefault(str(ac.get("fr_id", "")), []).append(ac)
    # Business rules key off endpoint/step refs; attach any whose step_ref appears
    # in an FR's source_evidence.
    #
    # Reuse the shared context resolver (#1381) so the FR→context join here and the
    # endpoint→context join in ``slice_functional_map`` can never diverge: build a
    # ``spec-id → domain-label`` member map and invert it into the shared index.
    member_index = build_member_index({sk["id"]: [str(sk["domain"])] for sk in spec_skeletons})
    single_spec = spec_skeletons[0]["id"] if len(spec_skeletons) == 1 else None

    by_spec: dict[str, list[dict[str, Any]]] = {sk["id"]: [] for sk in spec_skeletons}
    deferred: list[dict[str, Any]] = []

    for fr in frs:
        fr_domain = str(fr.get("domain") or "").lower()
        target = resolve_context([fr_domain], member_index, single_context=single_spec)
        if target is None:
            deferred.append(
                {
                    "id": fr.get("id"),
                    "statement": fr.get("statement"),
                    "reason": (
                        f"no bounded context matches domain {fr_domain!r}"
                        if fr_domain
                        else "FR carries no domain and the map has multiple contexts"
                    ),
                }
            )
            continue
        fr_id = str(fr.get("id", ""))
        related_rules = [
            br
            for br in inventory.business_rules
            if str(br.get("step_ref", "")) in set(fr.get("source_evidence") or [])
        ]
        by_spec[target].append(
            {
                "id": fr.get("id"),
                "statement": fr.get("statement"),
                "priority": fr.get("priority"),
                "fr_type": fr.get("fr_type"),
                "acceptance_criteria": acs_by_fr.get(fr_id, []),
                "business_rules": related_rules,
            }
        )

    distributed = sum(len(v) for v in by_spec.values())
    return {
        "by_spec": by_spec,
        "deferred": deferred,
        "coverage": {
            "distributed": distributed,
            "deferred": len(deferred),
            "total": len(frs),
        },
    }


def build_adr_skeletons(system_map: SystemMap) -> list[dict[str, Any]]:
    """Cross-cutting ADR skeletons — one per structural concern in the map.

    v1 surfaces two deterministic concerns whenever the map warrants them: a
    *shared-library extraction* ADR when any ``shared_lib`` node exists, and an
    *external-dependency isolation* ADR when any ``unresolved`` edge exists. The
    body authors the decision prose; the engine assigns the ``ADR-`` ids.
    """
    skeletons: list[dict[str, Any]] = []
    has_shared_lib = any(n.kind == "shared_lib" for n in system_map.nodes)
    has_external = any(e.confidence == "unresolved" for e in system_map.edges)
    if has_shared_lib:
        skeletons.append(
            {
                "id": "adr-shared-library-extraction",
                "title": "Extract shared libraries into an independent module",
            }
        )
    if has_external:
        skeletons.append(
            {
                "id": "adr-external-dependency-isolation",
                "title": "Isolate unresolved external dependencies behind anti-corruption layers",
            }
        )
    # Always emit at least a strangler-fig sequencing ADR so ROADMAP has a
    # governing decision to cite.
    skeletons.append(
        {
            "id": "adr-strangler-fig-sequencing",
            "title": "Adopt strangler-fig extraction sequencing",
        }
    )
    return skeletons


# The benchmark's enumeration discipline, embedded verbatim into every spec
# prompt item (SP-D, #1384). This is *data* fed to the harness — deterministic
# prompt construction, no wall-clock / LLM in the builder. The five directives
# mirror the design spec §SP-D and the SP-C ~20-section render target so the
# harness authors the rich SP-B shape that SP-C renders.
_SPEC_INSTRUCTION = (
    "Author a role-complete, benchmark-parity spec (v2.0 SpecJudgment shape) for "
    "this bounded context, working from the pre-extracted `slice` below.\n\n"
    "ENUMERATE, DON'T SUMMARIZE — list EVERY business rule, field constraint, and "
    "acceptance criterion in the slice as its own R- / AC-; completeness matters "
    "more than brevity. One R- per distinct rule; do not collapse several rules "
    "into one requirement.\n\n"
    "CARRY-FORWARD, DON'T INVENT — the slice's `business_rules`, `validation_rules`, "
    "`acceptance_criteria`, `form_fields`, `nav_edges`, and `flows` are pre-extracted "
    "facts from the analyzers. Carry them into the spec's tables verbatim. Do NOT "
    "invent rules the analysis did not surface; mark any step you must infer "
    "`⚠️ INFERRED` in its statement/rationale.\n\n"
    "TRACEABILITY MANDATE — every R- gets ≥1 AC- and ≥1 T-; every AC- cites its "
    "R- (in `covers`) and its T- (in `verified_by`). The traceability matrix is "
    "enforced at render — an R- with no AC- or no T- is a defect.\n\n"
    "PER-SECTION CONTENT TEMPLATES (mirror the target shape):\n"
    "- Business Rules & Validation (§5) → one `requirements` object per rule: "
    "`id` (R-NNN) | `statement` (rule) | `scope` (entity/endpoint) | `constraint` "
    "(measurable bound) | `source` (file:line) | `rationale`.\n"
    "- Acceptance Criteria (§12) → one `acceptance_criteria` object per scenario, "
    "Given/When/Then: `given` / `when` / `then`, `covers` [R-…], `verified_by` [T-…], "
    "`is_parity` (true when it preserves legacy behaviour).\n"
    "- Test Specification (§11) → one `test_scenarios` object per scenario: `id` "
    "(T-NNN) | `type` (unit/integration/e2e) | `scenario` | `preconditions` | `input` "
    "| `expected` | `covers` [R-…]; include negative / edge cases, not just the "
    "happy path.\n"
    "- User & Navigation Flows (§6) → `user_flows` from the slice's `flows` / "
    "`nav_edges`; UX/UI notes (§7) from `form_fields`.\n\n"
    "INTERPRETIVE SCOPE — read cited source ONLY to author rationale, derive parity "
    "ACs, and judge migration risk; NOT to re-enumerate what the slice already "
    "extracted. Every citation must resolve to a real file:line under --code-root."
)

_SPEC_INSTRUCTION_FR_SUFFIX = (
    "\n\nGROUND each R- / AC- item in the pre-synthesized functional requirements "
    "below (functional_requirements) AND the full context `slice`; do not invent "
    "requirements the functional analysis did not surface."
)


def _slice_payload(slice_report: FunctionalMapReport) -> dict[str, Any]:
    """Compact, JSON-serializable view of a per-context slice for a prompt item (SP-D).

    Carries the full context slice — every extracted collection the harness must
    enumerate into the spec's tables (``business_rules``, ``validation_rules``,
    ``acceptance_criteria``, ``form_fields``, ``nav_edges``, ``flows``, ``guards``,
    ``business_contexts``, ``i18n``) — so the harness authors from the full slice
    rather than FR statements alone. Deterministic construction (a pure model dump),
    no wall-clock / LLM.
    """
    d = slice_report.digest
    return {
        "business_rules": [br.model_dump(mode="json") for br in slice_report.business_rules],
        "validation_rules": [vr.model_dump(mode="json") for vr in d.validation_rules],
        "acceptance_criteria": [
            ac.model_dump(mode="json") for ac in slice_report.acceptance_criteria
        ],
        "form_fields": [ff.model_dump(mode="json") for ff in d.form_fields],
        "nav_edges": [ne.model_dump(mode="json") for ne in d.nav_edges],
        "flows": [fl.model_dump(mode="json") for fl in slice_report.flows],
        "guards": [g.model_dump(mode="json") for g in d.guards],
        "business_contexts": [bc.model_dump(mode="json") for bc in d.business_contexts],
        "i18n": [e.model_dump(mode="json") for e in d.i18n],
    }


# ---------------------------------------------------------------------------
# SP-G (#1387): legacy-test parity-oracle slicing.
#
# The `/mod-test-inventory` primitive emits `test-inventory.json` — a catalog of
# the legacy repo's existing tests. Here we slice those tests by bounded context
# (the SAME endpoint→domain join SP-A/SP-C/SP-D use, via the shared
# `context_resolver` — NO new slicer) and thread the per-context tests into each
# spec prompt as `parity_oracles`, so the harness carries them into §11 with
# `is_parity_oracle=True`. The integration is ADDITIVE: absent test-inventory.json,
# `parity_oracles` is empty and §11 renders synthesized T- scenarios unchanged.
# ---------------------------------------------------------------------------


def _test_haystack(test: dict[str, Any]) -> str:
    """The lowercased text a test is matched on — its ``endpoint`` + ``unit``.

    Mirrors the ``f"{ep.path} {ep.handler}"`` haystack ``contexts_from_system_map``
    builds for an endpoint, so a test routes to the same bounded context its
    endpoint/handler would.
    """
    return f"{test.get('endpoint') or ''} {test.get('unit') or ''}".lower()


def slice_test_inventory(
    test_inventory: dict[str, Any],
    contexts: dict[str, list[str]],
    *,
    single_context: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """Partition ``test-inventory.json`` tests into ``{context: [test,...]}``.

    Routes each test to a bounded context by the **same containment join** the
    shared endpoint slicer (``contexts_from_system_map``) uses — a context's member
    token appearing in the test's ``endpoint``/``unit`` haystack — normalized
    through :func:`context_resolver.build_member_index` (the one shared normalizer,
    lowercase + strip). This is NOT a new slicer: it is the endpoint→domain join
    SP-A/SP-C/SP-D share, applied to a test's endpoint/unit instead of an endpoint.
    A test that matches no context falls to ``single_context`` (the one-context
    app-mod case) — or is dropped from the per-context slices when several contexts
    exist and none matched (it still exists in the raw inventory; only its
    parity-oracle grounding is deferred, never a silent loss of the artifact).
    """
    member_index = build_member_index(contexts)
    by_context: dict[str, list[dict[str, Any]]] = {c: [] for c in contexts}
    for test in test_inventory.get("tests") or []:
        haystack = _test_haystack(test)
        target: str | None = None
        # Containment match, mirroring contexts_from_system_map's endpoint join.
        for member_key, context in member_index.items():
            if member_key and member_key in haystack:
                target = context
                break
        if target is None:
            target = single_context
        if target is None:
            continue
        by_context.setdefault(target, []).append(test)
    return by_context


def _parity_oracle_payload(tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact, JSON-serializable view of a context's legacy tests for a prompt.

    Each entry carries exactly the fields the harness needs to author a parity
    oracle: the test ``name``, the ``unit``/``endpoint`` it exercises, its
    ``assertion_intent``, and its ``file:line`` citation.
    """
    payload: list[dict[str, Any]] = []
    for t in tests:
        payload.append(
            {
                "name": t.get("name", ""),
                "unit": t.get("unit", ""),
                "endpoint": t.get("endpoint"),
                "assertion_intent": t.get("assertion_intent", ""),
                "source": f"{t.get('file', '')}:{t.get('line', '')}",
            }
        )
    return payload


_PARITY_ORACLE_DIRECTIVE = (
    "\n\nPARITY ORACLES — the `parity_oracles` list below catalogs this context's "
    "EXISTING legacy tests (name, unit/endpoint, assertion_intent, source). Carry "
    "each one into `test_scenarios` as its own T- with `is_parity_oracle: true`, "
    "preserving its assertion intent — these ground §11 acceptance against actual "
    "legacy behavior. Add synthesized (non-parity) scenarios for gaps the legacy "
    "tests do not cover; leave those `is_parity_oracle: false`."
)


# ---------------------------------------------------------------------------
# Runtime-explore integration (design 2026-07-14): thread runtime-observed
# journeys/screens + route screenshots into the per-context spec + BRD.
#
# `/mod-runtime-explore` emits `runtime-journeys.json` (journeys, screens with
# route-named `screenshot` refs). Here we slice it by bounded context — the SAME
# endpoint→domain containment join SP-A/SP-C/SP-D/SP-G share, applied to a screen's
# url/title — and thread each context's slice into its spec prompt as
# `runtime_journeys` + `runtime_screens`, so the harness grounds §6 flows / §11 ACs
# in what the RUNNING app actually exposed (`runtime_observed=true`) and the BRD
# embeds the screenshots. ADDITIVE: absent/malformed artifact → an empty slice → the
# specs/BRDs are byte-identical (the #1408 invariant).
# ---------------------------------------------------------------------------


#: View-technology suffixes a server-rendered app (JSP/Thymeleaf) carries on its URLs
#: but the static functional-map's endpoint routes do not (``/owners/1.html`` vs
#: ``/owners/{}``). Stripped to produce a suffix-free join candidate (mirrors #1512).
_VIEW_SUFFIXES = (".html", ".htm")

#: A concrete, id-like path segment — a number, a UUID, or a long opaque hex id —
#: collapsed to ``{}`` so a runtime URL like ``/owners/123`` joins the endpoint template
#: ``/owners/{ownerId}`` (both → ``/owners/{}``). Kept in lockstep with the
#: ``runtime_explore.shared.screenshots`` regex, but deliberately NOT cross-imported:
#: QUAL-2 forbids importing another primitive's ``shared/`` internals, and this 1-line
#: collapse is not worth promoting to ``_shared`` for a single reuse.
_CONCRETE_SEGMENT_RE = re.compile(r"^(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}|[0-9a-fA-F]{16,})$")


def _templatize_path(path: str) -> str:
    """Collapse concrete id-like path segments to ``{}`` (the endpoint-template shape)."""
    return "/".join(
        "{}" if _CONCRETE_SEGMENT_RE.match(seg or "") else seg for seg in path.split("/")
    )


def _screen_haystack(screen: dict[str, Any]) -> str:
    """The lowercased text a runtime screen is matched on — its ``url`` + ``title``.

    Mirrors the ``f"{ep.path} {ep.handler}"`` haystack ``contexts_from_system_map``
    builds for an endpoint, so a screen routes to the same bounded context its URL
    would (a ``/owners/find`` screen lands in the same context as the owners endpoint).
    """
    return f"{screen.get('url') or ''} {screen.get('title') or ''}".lower()


def _app_relative_path(url: str, base_url: str) -> str:
    """App-relativize a crawler URL: strip the ``base_url`` origin + servlet context path.

    The crawler records absolute URLs (``http://host:8080/petclinic/owners/find.html``)
    while endpoint ifaces are app-relative (``GET /owners/find``). Stripping the
    base_url's origin AND path prefix app-relativizes the screen path so the two join
    (mirrors ``journey_aggregator._app_relative_path``, the #1512 fix)."""
    from urllib.parse import urlsplit  # noqa: PLC0415

    context_path = urlsplit(base_url).path.rstrip("/") if base_url else ""
    path = urlsplit(url).path or url
    path = path.split("?", 1)[0].split("#", 1)[0]
    if context_path and (path == context_path or path.startswith(context_path + "/")):
        path = path[len(context_path) :] or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def _screen_ifaces(url: str, base_url: str) -> set[str]:
    """The candidate ``http_iface`` join keys for a runtime screen's URL.

    App-relativizes (strips origin + context path), then emits both the suffix-stripped
    candidate (``/owners/find``, matching an extension-less endpoint route) and the raw
    candidate (so an app that DOES expose ``.html`` still joins). Concrete id segments
    are collapsed to ``{}`` via :func:`_templatize_path` before ``http_iface`` — the same
    normalization the #1512 reconciliation join uses — so a ``/owners/1.html`` screen
    meets the ``GET /owners/{}`` endpoint iface its context owns."""
    path = _app_relative_path(url, base_url)
    candidates = {path}
    for suffix in _VIEW_SUFFIXES:
        if path.endswith(suffix):
            candidates.add(path[: -len(suffix)])
    return {http_iface("GET", _templatize_path(c)) for c in candidates}


def slice_runtime_journeys(
    rj_json: dict[str, Any],
    contexts: dict[str, list[str]],
    *,
    single_context: str | None,
    endpoint_contexts: dict[str, list[str]] | None = None,
    base_url: str = "",
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Partition runtime screens + journeys into ``{context: {screens, journeys}}``.

    A screen routes to a context by a **two-tier join** (#1516):

    1. **Endpoint-iface join (primary).** When ``endpoint_contexts`` — a
       ``{context: [http_iface, ...]}`` map (from ``contexts_from_system_map``) — is
       supplied, the screen's app-relativized, templatized ``http_iface`` candidates
       (via :func:`_screen_ifaces`, using ``base_url`` to strip origin + servlet
       context) are matched against it: a ``/owners/find`` screen lands wherever the
       ``GET /owners/find`` endpoint landed. This is the join that WORKS for app-mod
       layer-named domains (``src``/``Data``/``Web``), whose tokens never appear in a
       route-path URL.
    2. **Token substring join (fallback).** Failing the iface join, the context's
       member token is substring-matched against the screen's ``url``/``title``
       haystack (the original behavior — still correct for business-named domains like
       ``owners``).

    A screen matching neither falls to ``single_context`` (the one-context case) or —
    when there are multiple contexts but the iface join placed at least one screen — is
    dropped from the per-context slices (it survives in the raw artifact + the
    MODERNIZATION.md rollup). A journey routes to the context owning the **majority** of
    its path screens (ties → first-seen). Tolerant: a malformed artifact yields empty
    slices, never raises.
    """
    member_index = build_member_index(contexts)
    iface_index = build_member_index(endpoint_contexts) if endpoint_contexts else {}
    by_context: dict[str, dict[str, list[dict[str, Any]]]] = {
        c: {"screens": [], "journeys": []} for c in contexts
    }

    def _route_screen_by_iface(url: str) -> str | None:
        if not iface_index:
            return None
        for iface in _screen_ifaces(url, base_url):
            ctx = iface_index.get(iface.strip().lower())
            if ctx is not None:
                return ctx
        return None

    def _route_screen(screen: dict[str, Any]) -> str | None:
        # Primary: the endpoint-iface join (#1516) — works for layer-named domains.
        by_iface = _route_screen_by_iface(str(screen.get("url") or ""))
        if by_iface is not None:
            return by_iface
        # Fallback: token substring match on url/title (business-named domains).
        haystack = _screen_haystack(screen)
        for member_key, context in member_index.items():
            if member_key and member_key in haystack:
                return context
        return single_context

    # Screens first — build a screen-id → context map to majority-vote journeys.
    screen_context: dict[str, str] = {}
    raw_screens = rj_json.get("screens")
    for screen in raw_screens if isinstance(raw_screens, list) else []:
        if not isinstance(screen, dict) or not screen.get("id"):
            continue
        target = _route_screen(screen)
        if target is None:
            continue
        screen_context[str(screen["id"])] = target
        by_context.setdefault(target, {"screens": [], "journeys": []})["screens"].append(
            {
                "id": str(screen["id"]),
                "url": str(screen.get("url") or ""),
                "title": str(screen.get("title") or ""),
                "screenshot": str(screen.get("screenshot") or ""),
                "reached_via": str(screen.get("reached_via") or ""),
            }
        )

    # Journeys route to the context owning the majority of their path screens.
    raw_journeys = rj_json.get("journeys")
    for journey in raw_journeys if isinstance(raw_journeys, list) else []:
        if not isinstance(journey, dict) or not journey.get("id"):
            continue
        raw_path = journey.get("path")
        path = [str(s) for s in raw_path] if isinstance(raw_path, list) else []
        votes: dict[str, int] = {}
        for sid in path:
            ctx = screen_context.get(sid)
            if ctx is not None:
                votes[ctx] = votes.get(ctx, 0) + 1
        target = max(votes, key=lambda c: votes[c]) if votes else single_context
        if target is None:
            continue
        by_context.setdefault(target, {"screens": [], "journeys": []})["journeys"].append(journey)
    return by_context


def _runtime_slice_payload(
    slice_data: dict[str, list[dict[str, Any]]],
    rj_json: dict[str, Any],
    *,
    screenshot_prefix: str,
) -> dict[str, list[dict[str, Any]]]:
    """Compact prompt payload for a context's runtime slice.

    ``journeys`` are resolved to flow dicts (id/persona/goal/steps/reached_via) via the
    shared :func:`runtime_journeys_to_flows` — restricted to this context's journeys.
    ``screens`` carry an ``image_path`` = the screenshot ref rewritten relative to the
    BRD's location (``screenshot_prefix`` + the ref), so the harness embeds a link that
    resolves; a screen with no screenshot gets an empty ``image_path``.
    """
    ctx_ids = {str(j.get("id")) for j in slice_data["journeys"] if j.get("id")}
    flows = [f for f in runtime_journeys_to_flows(rj_json) if f["id"] in ctx_ids]
    screens = [
        {
            "id": s["id"],
            "url": s["url"],
            "title": s["title"],
            "image_path": (screenshot_prefix + s["screenshot"]) if s["screenshot"] else "",
        }
        for s in slice_data["screens"]
    ]
    return {"journeys": flows, "screens": screens}


_RUNTIME_DIRECTIVE = (
    "\n\nRUNTIME-OBSERVED DISCOVERY — the `runtime_journeys` + `runtime_screens` below "
    "were captured by driving the RUNNING app (`/mod-runtime-explore`), so they are "
    "ground truth for what the UI actually exposes. Fold each runtime journey into "
    "`user_flows` as a flow whose steps are the observed screens, with "
    "`runtime_observed: true`. Add acceptance criteria grounded in these journeys with "
    "`runtime_observed: true`, IN ADDITION to (never replacing) the synthesized ones. "
    "In the BRD, drive the business-journey narrative + the user-journey diagram from "
    "these real journeys, and embed each runtime screen as `![<title>](<image_path>)` "
    "in a 'Runtime Screens (observed)' subsection — use the `image_path` verbatim."
)


# Track D (#1426): the bounded referenced-file-read directive. Present on every
# spec item that carries a `slice` — it instructs the harness to READ the cited
# files (bounded to this context's endpoints' `file:line`) and inline the literal
# filter/enum/dropdown values the structured extractors miss (dynamic SQL WHERE
# filters, config-driven dropdowns, JSP <option> lists, constant arrays) VERBATIM,
# each carrying its `file:line`. The slice's `form_fields[].options` already carry
# the structurally-extracted option lists; this fills the long tail. Bounded to the
# cited files (not the whole repo) to keep token cost tractable; no fabrication —
# a value with no backing source line is not emitted.
_FIDELITY_READ_DIRECTIVE = (
    "\n\nREAL-VALUE FIDELITY (Track D) — the slice's `form_fields` carry their "
    "structurally-extracted `options` (real select/dropdown literals) and "
    "`constraints`. For the long tail the extractors miss — dynamic SQL `WHERE` "
    "filter values, config/enum-driven dropdowns, JSP `<option>` lists, constant "
    "arrays — READ the specific cited files for THIS context's endpoints (bounded "
    "by their `file:line`; do NOT scan the whole repo) and inline the literal "
    "filter/enum/dropdown values VERBATIM into the §5 rule `constraint` and §7 "
    "UX/data-model tables. Every inlined value MUST carry its `file:line` "
    "provenance alongside it (the reference is kept; the content is ADDED, not "
    "replaced). No fabrication — a value with no real backing source line is "
    "omitted, never invented."
)


# Track C (#1429): the BRD (business narrative) directive, appended to every spec
# item. It instructs the harness to ALSO author the business-audience narrative for
# this context's BRD.md — a plain-language executive framing, KPIs, a business
# glossary, assumptions, and business risks — sharing the spec's R-/FR- ids (the
# BRD and spec trace the SAME requirement). The four business diagrams render
# deterministically from the already-extracted structure (flows/nav/entry-points/
# contexts/actors); the harness only authors narrative framing. No fabrication — a
# field left empty simply renders a sparser (honest) BRD section.
_BRD_DIRECTIVE = (
    "\n\nBUSINESS BRD (Track C) — a business-audience Business Requirements Document "
    "(BRD.md) is emitted beside this technical spec, sharing the SAME requirement ids "
    "(`R-`/`FR-`) so a business reader and an engineer trace the same requirement. In "
    "ADDITION to the technical fields, author the BRD narrative fields for this "
    "context: `business_narrative` (a plain-language executive framing of what this "
    "capability does and why it matters — NOT technical), `kpis` (measurable business "
    "success metrics as plain-language bullets), `business_glossary` (business terms "
    "as `Term: definition` bullets), `assumptions` (business assumptions), and "
    "`business_risks` (business impact if this capability is mis-migrated). Frame all "
    "of these for a non-technical audience. Leave any field empty when this context "
    "has nothing real to say for it (no fabrication — an empty field renders a "
    "sparser, honest BRD section). The business diagrams (user journeys, screen flow, "
    "capability map, actor/lifecycle) render deterministically from the extracted "
    "structure; only override `business_process_diagram` / `capability_map` with your "
    "own Mermaid if you have materially richer business framing."
)


def _target_api_directive(backend_label: str) -> str:
    """The §8b Target REST API synthesis directive for the loaded target (SP3, #1411).

    Instructs the harness to synthesize a resource-per-aggregate target REST API in
    the target backend's idiom, tracing each endpoint to its originating controller.
    Present only when a target is loaded; *backend_label* is the render label (e.g.
    ``"Spring Boot 3.3"``) or empty when the target declares no backend dimension.
    """
    idiom = f" in the {backend_label} idiom" if backend_label else ""
    return (
        "\n\nTARGET REST API (§8b) — a modernization target is set"
        f"{f' ({backend_label})' if backend_label else ''}. In ADDITION to the "
        "current-contract endpoints (kind `current`), synthesize a "
        "resource-per-aggregate TARGET REST API from this context's functional "
        "requirements + DB aggregates" + idiom + ". Emit each as its own "
        '`api_endpoints` entry with `kind: "target"`, a REST verb + resource path '
        "(e.g. `POST /api/owners`, `GET /api/owners/{ownerId}/pets`), the request/"
        "response DTO names, status codes, the `validation` R- ids it enforces, and "
        "`security` roles. TRACE each target endpoint to the current controller "
        "form-flow it replaces via `from_controller` "
        "(e.g. `OwnerController.processCreationForm`); leave `from_controller` empty "
        "ONLY for a genuinely new target endpoint. Do NOT fabricate resources with no "
        "backing FR or aggregate."
    )


def _target_ui_sequence_directive(frontend_label: str, backend_label: str) -> str:
    """The §7 target UI mockup + target sequence flow directives (SP5, #1413).

    Instructs the harness to author (1) a Mermaid UI mockup of the target screen in
    the target frontend's idiom for the ``ui_mockup`` field and (2) a Mermaid
    ``sequenceDiagram`` of the target request path per key flow for each user-flow's
    ``sequence_diagram`` field. Present only when a target is loaded; the labels are
    the target's frontend/backend render labels (or empty when not declared).
    """
    fe = f" in the {frontend_label} idiom" if frontend_label else ""
    path = (
        f"{frontend_label or 'the target frontend'} → REST → "
        f"{backend_label or 'the target service'} → DB"
    )
    return (
        "\n\nTARGET UI MOCKUP (§7) — a modernization target is set"
        f"{f' ({frontend_label})' if frontend_label else ''}. Author a Mermaid "
        "representation of THIS context's target UI screen" + fe + " (e.g. a "
        "`flowchart` of the component + its reactive-form fields, grouping the "
        "functional-map `form_fields` into the target screen's layout) and place it "
        "in the spec's `ui_mockup` field. Design the screen in the target frontend's "
        "idiom — do NOT merely echo the legacy page. Leave `ui_mockup` empty if this "
        "context has no UI surface (no fabrication)."
        "\n\nTARGET SEQUENCE FLOW (§6) — for each KEY `user_flows` entry, author a "
        f"Mermaid `sequenceDiagram` of the TARGET request path ({path}) and place it "
        "in that flow's `sequence_diagram` field. Show the real participants "
        "(component → controller → service → repository/DB) and the calls between "
        "them; design the target path, do NOT just echo the legacy nav-edges. Leave "
        "`sequence_diagram` empty for a flow with no request path (no fabrication)."
    )


def _hla_grounding_directive(hla: HlaSource) -> str:
    """The HLA grounding directive threaded into every spec prompt (Track A, #1428).

    Instructs the harness to align the synthesized specs/ADRs to the operator's
    stated High-Level Architecture and, for the Architecture Conformance section, to
    refine the deterministic ``confirmed`` / ``not-found`` baseline with the
    interpretive ``partial`` / ``contradicted`` verdicts — each with evidence (HLA
    says X → system-map shows Y). Present only when HLA documents exist; absent → the
    prompt payload is byte-identical to prior behavior.
    """
    stated_components = sorted(
        {c for doc in hla.documents for c in doc.stated_components if c.strip()}
    )
    stated_integrations = sorted(
        {i for doc in hla.documents for i in doc.integrations if i.strip()}
    )
    parts = (
        "\n\nHLA GROUNDING (Track A) — the operator supplied a stated High-Level "
        "Architecture. ALIGN the specs and ADRs to it: where a spec matches the stated "
        "architecture, say so; where the discovered system-map DEVIATES from the HLA, "
        "an ADR must justify the deviation (never silently override the stated intent). "
    )
    if stated_components:
        parts += f"Stated components: {', '.join(stated_components)}. "
    if stated_integrations:
        parts += f"Stated integrations: {', '.join(stated_integrations)}. "
    parts += (
        "For the Architecture Conformance verdicts, REFINE the deterministic baseline "
        "(`confirmed` where the system-map structurally confirms an element, "
        "`not-found` otherwise) with `partial` (present but shaped differently) and "
        "`contradicted` (the system-map disproves the stated element), citing evidence "
        "as 'HLA says X → system-map shows Y'. No fabrication — never invent a match."
    )
    return parts


def build_synthesis_prompts(
    system_map: SystemMap,
    spec_skeletons: list[dict[str, Any]],
    adr_skeletons: list[dict[str, Any]],
    fr_distribution: dict[str, Any] | None = None,
    *,
    slices: dict[str, FunctionalMapReport] | None = None,
    parity_oracles: dict[str, list[dict[str, Any]]] | None = None,
    runtime_slices: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
    target: TargetStack | None = None,
    hla: HlaSource | None = None,
) -> dict[str, Any]:
    """Assemble the ``synthesis_prompts.json`` payload (one item per skeleton).

    When ``fr_distribution`` is supplied (#1356), each spec item carries the
    functional requirements distributed to its context — so the harness authors
    R-/AC- items grounded in the pre-synthesized FRs instead of inventing them.

    When ``slices`` is supplied (SP-D, #1384), each spec item ALSO carries the
    **full per-context functional-map slice** (its ``business_rules``,
    ``validation_rules``, ``acceptance_criteria``, ``form_fields``, ``nav_edges``,
    ``flows``, …) matched by the same domain-slug join :func:`render_specs_and_adrs`
    uses, plus the benchmark's enumerate/carry-forward/traceability/interpretive
    instruction — so the harness enumerates every extracted rule/AC/field into the
    rich SP-B shape SP-C renders. Deterministic prompt construction; the slice is
    reused from SP-C's :func:`build_slices` / SP-A's ``slice_functional_map`` — no
    new slicer.

    When ``parity_oracles`` is supplied (SP-G, #1387), each spec item ALSO carries
    its bounded context's **legacy tests** (from ``test-inventory.json``, sliced by
    :func:`slice_test_inventory` — the same ``context_resolver`` join) as
    ``parity_oracles``, plus a directive to carry each into ``test_scenarios`` with
    ``is_parity_oracle: true``. Additive: absent the inventory the list is empty and
    §11 renders synthesized T- scenarios unchanged.

    When ``target`` is supplied (SP3, #1411), each spec item ALSO carries the §8b
    Target REST API synthesis directive — instructing the harness to synthesize a
    resource-per-aggregate target REST API in the target backend's idiom, each
    endpoint traced to its originating controller via ``from_controller`` and
    emitted with ``kind: "target"``. It ALSO carries (SP5, #1413) the §7 target UI
    mockup + §6 target sequence-flow authoring directives — instructing the harness
    to author a Mermaid ``ui_mockup`` of the target screen in the frontend's idiom
    and a Mermaid ``sequence_diagram`` of the target request path per key user-flow.
    Absent a target these blocks are omitted entirely, so the prompt payload is
    byte-identical to prior behavior.
    """
    context_nodes = [
        {"id": n.id, "kind": n.kind, "domains": list(n.domains)} for n in system_map.nodes
    ]
    by_spec: dict[str, list[dict[str, Any]]] = (fr_distribution or {}).get("by_spec") or {}
    # SP3 (#1411): the §8b directive (present only when a target is loaded). The
    # backend label is the target's backend dimension render label (or empty).
    target_directive = ""
    if target is not None and target.dimensions:
        backend_label = next(
            (d.label or f"{d.key}={d.value}" for d in target.dimensions if d.key == "backend"),
            "",
        )
        # SP5 (#1413): also thread the §7 target UI mockup + §6 target sequence
        # authoring directives, gated on the same "target is set" condition.
        frontend_label = next(
            (d.label or f"{d.key}={d.value}" for d in target.dimensions if d.key == "frontend"),
            "",
        )
        target_directive = _target_api_directive(backend_label) + _target_ui_sequence_directive(
            frontend_label, backend_label
        )
    # Track A (#1428): the HLA grounding directive (present only when HLA docs exist).
    hla_directive = _hla_grounding_directive(hla) if hla is not None and hla.documents else ""
    spec_items = []
    for sk in spec_skeletons:
        frs = by_spec.get(sk["id"], [])
        slice_report = _slice_key_for_skeleton(sk, slices) if slices else None
        item: dict[str, Any] = {
            "id": sk["id"],
            "kind": "spec",
            "domain": sk["domain"],
            "title": sk["title"],
            "components": sk["components"],
            "instruction": _SPEC_INSTRUCTION + (_SPEC_INSTRUCTION_FR_SUFFIX if frs else ""),
        }
        if slice_report is not None:
            item["slice"] = _slice_payload(slice_report)
            # Track D (#1426): with a slice present, the harness reads the cited
            # files for real filter/enum values inlined VERBATIM with file:line.
            item["instruction"] += _FIDELITY_READ_DIRECTIVE
        if frs:
            item["functional_requirements"] = frs
        # SP-G (#1387): thread this context's legacy tests as parity oracles.
        oracle_tests = (parity_oracles or {}).get(_slug(str(sk.get("domain", ""))))
        if oracle_tests:
            item["parity_oracles"] = _parity_oracle_payload(oracle_tests)
            item["instruction"] += _PARITY_ORACLE_DIRECTIVE
        # runtime-explore integration: thread this context's runtime-observed
        # journeys/screens (+ screenshot image paths) so the spec §6/§11 + BRD ground
        # in the running app. Additive — absent slice → no keys, no directive.
        rslice = (runtime_slices or {}).get(_slug(str(sk.get("domain", ""))))
        if rslice and (rslice.get("journeys") or rslice.get("screens")):
            item["runtime_journeys"] = rslice["journeys"]
            item["runtime_screens"] = rslice["screens"]
            item["instruction"] += _RUNTIME_DIRECTIVE
        # SP3 (#1411): the §8b Target REST API directive (only when a target is set).
        if target_directive:
            item["instruction"] += target_directive
        # Track A (#1428): the HLA grounding directive (only when HLA docs exist).
        if hla_directive:
            item["instruction"] += hla_directive
        # Track C (#1429): the BRD business-narrative directive (always present — a
        # BRD.md is emitted for every context; empty fields render a sparser BRD).
        item["instruction"] += _BRD_DIRECTIVE
        spec_items.append(item)
    adr_items = [
        {
            "id": sk["id"],
            "kind": "adr",
            "title": sk["title"],
            "instruction": (
                "Author a standalone ADR: status, context, decision, consequences, "
                "alternatives, the R- ids it affects, and path:line citations."
            ),
        }
        for sk in adr_skeletons
    ]
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "context": {"nodes": context_nodes},
        "specs": spec_items,
        "adrs": adr_items,
    }
    if fr_distribution is not None:
        payload["fr_coverage"] = fr_distribution.get("coverage", {})
        payload["fr_deferred"] = fr_distribution.get("deferred", [])
    return payload


# ---------------------------------------------------------------------------
# Rendering (SP2.3 deconstruction + SP2.4 specs/ADRs/roadmap)
# ---------------------------------------------------------------------------

_UNVERIFIED = "⚠️ UNVERIFIED"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fr_coverage_section(fr_distribution: dict[str, Any]) -> str:
    """A "Functional Requirement Coverage" block for MODERNIZATION.md (#1356).

    Mirrors functional-map's own "N of M FRs covered" audit (#1342): a deferred FR
    is visible and counted, not silently absent from the specs.
    """
    cov = fr_distribution.get("coverage") or {}
    total = cov.get("total", 0)
    if not total:
        return ""
    distributed = cov.get("distributed", 0)
    deferred = fr_distribution.get("deferred") or []
    lines = [
        "",
        "## Functional Requirement Coverage",
        "",
        f"**{distributed} of {total} functional requirements distributed to a bounded context.**",
        "",
    ]
    if deferred:
        ids = ", ".join(str(d.get("id")) for d in deferred)
        lines.append(f"> ⚠️ Deferred (no matching context): {ids}")
        lines.append("")
    return "\n".join(lines)


def _legacy_stack_label(system_map: SystemMap) -> str:
    """A human legacy-stack label from the system-map's flavors (node kinds).

    Deterministic: the distinct node ``kind`` values (each a discovery flavor),
    sorted, joined. Empty when the map carries no flavored nodes — the target ADR
    then falls back to its generic "the current legacy stack" phrasing (no
    fabrication).
    """
    flavors = sorted({n.kind for n in system_map.nodes if n.kind})
    return " + ".join(flavors)


def _adr_index_section(adr_skeletons: list[dict[str, Any]], *, has_target: bool) -> str:
    """The "Modernization Decisions" ADR index for MODERNIZATION.md (#1463).

    Lists **every** ADR :func:`render_specs_and_adrs` writes — one per skeleton at
    ``ADR-{i:04d}-{slug(id)}`` for ``i`` in ``1..len(adr_skeletons)`` (byte-identical
    numbering + filename slug), plus the standalone target-stack ADR at
    ``len+1`` when *has_target* — so every synthesized decision is discoverable from
    the whole-system deconstruction, not just the target one (the pre-#1463 bug listed
    only the target ADR). Renders whenever ≥1 ADR exists; an empty set → empty string
    (byte-identical to prior behavior for a map with no ADRs, which never happens in
    practice since the strangler-fig ADR is always present).
    """
    rows: list[str] = []
    for i, skel in enumerate(adr_skeletons, start=1):
        fname = f"ADR-{i:04d}-{_slug(str(skel['id']).removeprefix('adr-'))}.md"
        rows.append(f"- **ADR-{i:04d}** — {skel['title']} — [`adr/{fname}`](adr/{fname})")
    if has_target:
        n = len(adr_skeletons) + 1
        fname = f"ADR-{n:04d}-target-stack.md"
        rows.append(f"- **ADR-{n:04d}** — Target stack — [`adr/{fname}`](adr/{fname})")
    if not rows:
        return ""
    return "\n" + heading("Modernization Decisions", level=2) + "\n\n" + "\n".join(rows) + "\n"


def render_deconstruction_artifacts(
    system_map: SystemMap,
    out_dir: Path,
    *,
    title: str,
    fr_distribution: dict[str, Any] | None = None,
    hla: HlaSource | None = None,
    target: TargetStack | None = None,
    runtime_journeys: dict[str, Any] | None = None,
) -> list[Path]:
    """Write MODERNIZATION.md + system-map.json durably. Returns written paths.

    When ``fr_distribution`` is supplied (#1356), a Functional Requirement Coverage
    section is appended to MODERNIZATION.md so a deferred FR is visible and counted.

    When ``hla`` is supplied AND carries documents (Track A, #1428), an Architecture
    Conformance section is appended — each stated HLA element reconciled against the
    discovered system-map with a verdict + evidence. No HLA → section omitted
    (byte-identical backward compatibility).

    A "Modernization Decisions" section is appended (#1463) indexing **every** ADR
    :func:`render_specs_and_adrs` writes — one per skeleton (``ADR-0001…``) plus the
    standalone target-stack ADR (``len + 1``) when ``target`` carries ≥1 dimension.
    It renders whenever ≥1 ADR exists (the strangler-fig ADR is always present), so a
    reader can discover all synthesized decisions from the deconstruction — not just
    the target one (the pre-#1463 behavior listed only the target ADR and omitted the
    whole section on a no-target run).
    """
    markdown, machine = render_deconstruction(system_map, title=title, target=target)
    if fr_distribution is not None:
        markdown = markdown + _fr_coverage_section(fr_distribution)
    conformance = render_architecture_conformance(hla, system_map)
    if conformance:
        markdown = markdown + "\n" + conformance
    # Modernization Decisions — index EVERY ADR render_specs_and_adrs writes (#1463),
    # not just the target one, and render whenever ≥1 ADR exists (not only on a
    # --target run). The strangler-fig ADR is always present, so this always renders.
    adr_skeletons = build_adr_skeletons(system_map)
    has_target = target is not None and bool(target.dimensions)
    markdown = markdown + _adr_index_section(adr_skeletons, has_target=has_target)
    # Runtime-Observed Journeys (#1478, additive): when a /mod-runtime-explore
    # runtime-journeys.json is present, append the runtime journeys section.
    # Absent → empty string → MODERNIZATION.md byte-identical (the #1408 invariant).
    if runtime_journeys is not None:
        section = runtime_journeys_section(runtime_journeys)
        if section:
            markdown = markdown + "\n" + section
    written = [
        _write(out_dir / "MODERNIZATION.md", markdown),
        _write(out_dir / "system-map.json", json.dumps(machine, indent=2, default=str)),
    ]
    return written


def _citations_block(citations: list[str]) -> str:
    """Render a body-authored citation list as a Markdown Evidence section."""
    if not citations:
        return ""
    lines = ["", "## Evidence", ""]
    lines.extend(f"- `{c}`" for c in citations)
    return "\n".join(lines)


# The flat ``functional-map.json`` collections that live under ``digest`` in the
# nested :class:`FunctionalMapReport` shape (see functional_map ``render.render_json``).
_DIGEST_COLLECTIONS = (
    "endpoints",
    "guards",
    "capabilities",
    "nav_edges",
    "form_fields",
    "validation_rules",
    "business_contexts",
    "i18n",
    "webflow_flows",
)


def _reshape_flat_functional_map(raw: dict[str, Any]) -> dict[str, Any]:
    """Reconstitute the nested :class:`FunctionalMapReport` shape from the flat one.

    The functional-map primitive emits a **flat** ``functional-map.json`` (top-level
    ``endpoints`` / ``roles`` / ``nav_edges`` / ``flows`` / … — the inverse of
    ``render.render_json``), but :class:`FunctionalMapReport` nests the nine digest
    collections under ``digest`` and the roles under ``reconciliation.reconciled``.
    Without this reshape, ``model_validate`` on a real artifact raises and every BRD
    slice silently drops (#1456 — the web-context BRD renders 1 of 4 diagrams).

    A dict that already carries a ``digest`` key is returned unchanged (a caller
    handing us the nested shape directly). Missing collections default empty, so a
    ``--no-synthesis`` map (endpoints only) still reshapes cleanly.
    """
    if "digest" in raw:
        return raw
    roles = raw.get("roles") or []
    digest = {name: raw.get(name) or [] for name in _DIGEST_COLLECTIONS}
    digest["roles"] = roles
    return {
        "digest": digest,
        "reconciliation": {"reconciled": roles},
        "flows": raw.get("flows") or [],
        "journeys": raw.get("journeys") or [],
        "business_rules": raw.get("business_rules") or [],
        "fr_statements": raw.get("fr_statements") or [],
        "acceptance_criteria": raw.get("acceptance_criteria") or [],
        "synthesized": bool(raw.get("synthesized", False)),
    }


def _resolve_functional_map(context_root: Path, functional_map: Path | None) -> Path | None:
    """Resolve the functional-map artifact path, falling back to discovery (#1460).

    The **continue** leg runs before the default leg's :func:`discover_artifacts`
    call and used the raw ``functional_map`` ``run()`` parameter directly. When the
    CLI omits ``--functional-map`` that parameter is ``None``, so ``build_slices``
    (and the FR-distribution) silently got no artifact → every per-context BRD lost
    its slice and rendered 1 of 4 diagrams. This resolves the conventional location
    under *context_root* when the explicit path is absent, mirroring
    :func:`discover_artifacts` but **tolerantly**: an absent functional-map returns
    ``None`` (specs still render from the authored judgments) instead of raising.
    """
    if functional_map is not None and functional_map.is_file():
        return functional_map
    candidate = context_root / _DISCOVERY["functional_map"][0]
    return candidate if candidate.is_file() else None


def build_slices(
    functional_map_path: Path | None,
    system_map: SystemMap,
) -> dict[str, FunctionalMapReport]:
    """Slice the whole-app functional-map into one report per bounded context (SP-A).

    Reads the functional-map artifact, derives the bounded contexts from the
    *system_map* (node ``domains``, the same join ``distribute_frs`` uses), and
    partitions the report with :func:`slice_functional_map`. Falls back to the
    functional-map's own path-prefix clustering when the system-map exposes no
    contexts. Returns ``{context-slug: FunctionalMapReport}``; an empty dict when
    the artifact is absent or does not parse to the full report shape (a thin
    functional-map.json that only carries the deterministic-inventory subset — the
    golden fixtures — has no full report to slice, so specs render from the
    authored :class:`SpecJudgment` fields alone).

    The functional-map primitive emits a **flat** artifact; :func:`_reshape_flat_
    functional_map` reconstitutes the nested :class:`FunctionalMapReport` shape
    before validation so the per-context BRD diagrams render from a real artifact
    (#1456).
    """
    if functional_map_path is None or not functional_map_path.is_file():
        return {}
    raw = _load(functional_map_path)
    try:
        report = FunctionalMapReport.model_validate(_reshape_flat_functional_map(raw))
    except ValueError:
        # A subset/inventory-shaped functional-map.json (no full report) — nothing
        # to slice; the specs still render from the authored SpecJudgment fields.
        return {}
    endpoints = report.digest.endpoints
    contexts = contexts_from_system_map(system_map.model_dump(mode="json"), endpoints)
    if not contexts:
        contexts = fallback_contexts(endpoints)
    if not contexts:
        return {}
    return slice_functional_map(report, contexts)


def build_parity_oracles(
    test_inventory_path: Path | None,
    system_map: SystemMap,
) -> dict[str, list[dict[str, Any]]]:
    """Slice ``test-inventory.json`` (SP-G, #1387) into ``{context-slug: [test,...]}``.

    Additive: returns ``{}`` when the artifact is absent or unparseable, so the
    caller threads no ``parity_oracles`` and §11 renders exactly as before. When
    present, derives the bounded contexts from *system_map* (node ``domains`` — the
    same source :func:`build_slices` uses) and partitions the tests with
    :func:`slice_test_inventory` (the shared ``context_resolver`` join). The
    returned keys are slugged (via :func:`_slug`) so they match a skeleton's
    ``spec-<domain-slug>`` id, exactly like the functional-map slices.
    """
    if test_inventory_path is None or not test_inventory_path.is_file():
        return {}
    try:
        inventory = _load(test_inventory_path)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(inventory, dict) or not inventory.get("tests"):
        return {}
    # Contexts = the system-map node domains (raw labels).
    domains: list[str] = []
    for node in system_map.model_dump(mode="json").get("nodes") or []:
        if not isinstance(node, dict) or node.get("kind") in {"external", "shared_lib"}:
            continue
        for domain in node.get("domains") or []:
            if domain and domain not in domains:
                domains.append(domain)
    if not domains:
        return {}
    # A test is matched against a context by that context's own label + slug.
    contexts = {d: [d, _slug(d)] for d in domains}
    single = domains[0] if len(domains) == 1 else None
    by_domain = slice_test_inventory(inventory, contexts, single_context=single)
    # Re-key by slug so it matches the skeleton's spec-<slug> id.
    return {_slug(domain): tests for domain, tests in by_domain.items() if tests}


def _screenshot_prefix(context_root: Path, output_dir: Path) -> str:
    """Relative path from the BRD dir (``output/scope/``) to the runtime artifact dir
    (``context_root/runtime/``), so a screen's ``screenshots/<name>.png`` ref embedded
    in a BRD resolves. E.g. default layout → ``../../runtime/``.
    """
    brd_dir = output_dir / "scope"
    runtime_dir = context_root / "runtime"
    try:
        rel = os.path.relpath(runtime_dir, brd_dir)
    except ValueError:
        # On Windows a relpath across different drives raises — fall back to the
        # absolute posix path so the image link still resolves (never break the run).
        return runtime_dir.as_posix() + "/"
    return rel.replace(os.sep, "/") + "/"


def _endpoint_contexts_for_runtime(
    context_root: Path,
    system_map: SystemMap,
) -> dict[str, list[str]] | None:
    """Build ``{context: [endpoint http_iface, ...]}`` from the functional map (#1516).

    This is the authoritative screen→context join for the runtime slicer: the SAME
    ``contexts_from_system_map`` map the endpoint slicer uses, so a runtime screen whose
    URL templatizes to ``GET /owners/find`` routes to whatever context owns that
    endpoint. Returns ``None`` when the functional map is absent or does not parse to the
    full report shape (the slicer then falls back to the token-substring join)."""
    fm_path = context_root / _DISCOVERY["functional_map"][0]
    if not fm_path.is_file():
        return None
    try:
        report = FunctionalMapReport.model_validate(_reshape_flat_functional_map(_load(fm_path)))
    except ValueError:
        return None
    contexts = contexts_from_system_map(system_map.model_dump(mode="json"), report.digest.endpoints)
    return contexts or None


def build_runtime_slices(
    context_root: Path,
    system_map: SystemMap,
    output_dir: Path,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Slice ``runtime/runtime-journeys.json`` into ``{context-slug: {journeys, screens}}``.

    Additive (the #1408 invariant): returns ``{}`` when the artifact is absent or
    unparseable, so the caller threads no ``runtime_slices`` and the specs/BRDs render
    byte-identically. When present, derives bounded contexts from *system_map* node
    ``domains`` (the same source :func:`build_parity_oracles` / :func:`build_slices`
    use), partitions screens+journeys with :func:`slice_runtime_journeys`, and builds
    each context's prompt payload (flows + screens with BRD-relative ``image_path``).
    Keys are slugged so they match a skeleton's ``spec-<domain-slug>`` id.
    """
    rj_json = _load_runtime_journeys(context_root)
    if rj_json is None:
        return {}
    domains: list[str] = []
    for node in system_map.model_dump(mode="json").get("nodes") or []:
        if not isinstance(node, dict) or node.get("kind") in {"external", "shared_lib"}:
            continue
        for domain in node.get("domains") or []:
            if domain and domain not in domains:
                domains.append(domain)
    if not domains:
        return {}
    contexts = {d: [d, _slug(d)] for d in domains}
    single = domains[0] if len(domains) == 1 else None
    # #1516: the PRIMARY screen→context join is by endpoint iface — a runtime screen
    # lands wherever its route's endpoint landed. Layer-named app-mod domains
    # (``src``/``Data``/``Web``) never substring-match a route-path URL, so without this
    # the token-only join routes 0 screens and the BRD "Runtime Screens (observed)"
    # section is silently never emitted. base_url app-relativizes absolute crawler URLs.
    endpoint_contexts = _endpoint_contexts_for_runtime(context_root, system_map)
    base_url = str(rj_json.get("base_url") or "")
    by_domain = slice_runtime_journeys(
        rj_json,
        contexts,
        single_context=single,
        endpoint_contexts=endpoint_contexts,
        base_url=base_url,
    )
    prefix = _screenshot_prefix(context_root, output_dir)
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for domain, slice_data in by_domain.items():
        if not (slice_data["journeys"] or slice_data["screens"]):
            continue
        out[_slug(domain)] = _runtime_slice_payload(slice_data, rj_json, screenshot_prefix=prefix)
    return out


def _slice_key_for_skeleton(
    skel: dict[str, Any],
    slices: dict[str, FunctionalMapReport] | None,
) -> FunctionalMapReport | None:
    """Match a spec skeleton to its per-context slice by domain slug.

    A skeleton id is ``spec-<domain-slug>`` and a slice key is ``<context-slug>``
    (both produced by :func:`_slug`), so the domain of the skeleton maps to the
    slice for the same context. Returns ``None`` when no slice matches.
    """
    if not slices:
        return None
    domain = str(skel.get("domain", ""))
    return slices.get(_slug(domain))


def _prepend_unverified_banner(
    body: str, skel: dict[str, Any], unverified_units: frozenset[str]
) -> str:
    """Insert a ⚠️ UNVERIFIED banner after the spec's H1 title (MR5, #1554).

    A spec carries the banner when any of its bounded context's member components
    (``skel["components"]`` — the unit node ids) has an UNVERIFIED migration state.
    The banner is inserted after the first ``# `` heading line so it reads at the top
    of the spec, distinguishing "planned + verified" from "planned but state unknown".
    Returns *body* unchanged when the spec has no unverified member (byte-identical)."""
    if not unverified_units:
        return body
    members = {str(c) for c in skel.get("components", [])}
    if not (members & unverified_units):
        return body
    banner = (
        "\n> ⚠️ **Modernization-state UNVERIFIED.** This context includes a unit whose "
        "migrated-vs-legacy state was never confirmed. It is scoped IN and planned "
        "normally — treat as legacy work until verified; it is never dropped.\n"
    )
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("# "):
            lines.insert(i + 1, banner)
            return "\n".join(lines)
    # No H1 found (defensive): prepend the banner.
    return banner + body


def render_specs_and_adrs(
    synthesis: ScopeSynthesis,
    spec_skeletons: list[dict[str, Any]],
    adr_skeletons: list[dict[str, Any]],
    system_map: SystemMap,
    out_dir: Path,
    *,
    generated_at: datetime | None = None,
    slices: dict[str, FunctionalMapReport] | None = None,
    target: TargetStack | None = None,
    legacy_stack: str = "",
    unverified_units: frozenset[str] | None = None,
) -> list[Path]:
    """Render each authored spec (``scope/<item>.spec.md``) + ADR (``adr/…``).

    ``generated_at`` is threaded through to :func:`render_adr` so the Date row
    in every ADR shows the pipeline run timestamp instead of the literal
    ``YYYY-MM-DD`` placeholder.

    ``slices`` (SP-A, #1381) maps a bounded-context slug → the per-context
    functional-map :class:`FunctionalMapReport` slice. The slug matches a
    skeleton's ``spec-<domain-slug>`` id (via :func:`_slice_key_for_skeleton`);
    the matching slice is passed to :func:`render_spec` so §5 business rules, §6
    flows/nav, and §7 form-fields render from the extracted functional detail
    (SP-C, #1383), and its controller endpoint inventory merges into §8 Current
    API Surface (SP3, #1411). When ``slices`` is ``None`` or a context has no
    slice, those sections fall back to the authored :class:`SpecJudgment` fields
    or omit.

    ``target`` (SP3, #1411) is the resolved :class:`TargetStack` (or ``None``).
    When present, it is threaded into :func:`render_spec` so §8b Target REST API
    renders under the backend label from each spec's ``kind="target"`` endpoints.
    Absent it, §8b is omitted entirely — the render is byte-identical to prior
    behavior.

    ``legacy_stack`` (WS1, #1449) is the legacy stack being strangled (the app-digest
    flavor label), threaded into the target-stack ADR context. Only consumed when
    ``target`` carries ≥1 dimension.

    **WS1 (#1449) — target-stack ADR.** When ``target`` carries ≥1 dimension, a
    STANDALONE ``adr/ADR-NNNN-target-stack.md`` is emitted LAST (its ``ADR-`` number
    is ``len(adr_skeletons) + 1``) recording the resolved target as an architectural
    decision — via :func:`target_stack_adr_decision` + the shared :func:`render_adr`.
    Slotting it last keeps every per-spec ADR id (``1..len(adr_skeletons)``)
    byte-identical, so a no-target run is unchanged. No target (or no dimensions) →
    NO target ADR file and the ADR id sequence is untouched.
    """
    # Format once; fall back to empty string so render_adr omits the row when
    # no timestamp is available.
    date_str = generated_at.strftime("%Y-%m-%d") if generated_at is not None else ""

    written: list[Path] = []
    judged_specs = {s.spec_id: s for s in synthesis.specs}
    # MR5 (#1554): a spec whose bounded context includes a unit with an UNVERIFIED
    # migration state carries a ⚠️ banner (scoped IN + planned, but never confirmed).
    unverified = unverified_units or frozenset()

    for skel in spec_skeletons:
        judgment = judged_specs.get(skel["id"])
        slice_report = _slice_key_for_skeleton(skel, slices) if slices else None
        if judgment is not None:
            # SP-C (#1383): pass the rich SpecJudgment straight through — render_spec
            # now renders the full ~20-section structure from the typed fields
            # (multi-AC/multi-T, traceability matrix, honest analyzer stubs).
            body = render_spec(judgment, system_map, slice_report=slice_report, target=target)
            body += _citations_block(judgment.citations)
        else:
            item = {
                "spec_id": skel["id"],
                "title": skel["title"],
                "description": f"{_UNVERIFIED} — no synthesis authored for this context.",
            }
            body = render_spec(item, system_map, slice_report=slice_report, target=target)
        body = _prepend_unverified_banner(body, skel, unverified)
        written.append(_write(out_dir / "scope" / f"{skel['id']}.spec.md", body))

    judged_adrs = {a.adr_id: a for a in synthesis.adrs}
    for i, skel in enumerate(adr_skeletons, start=1):
        adr = judged_adrs.get(skel["id"])
        if adr is not None:
            decision = {
                "title": adr.title or skel["title"],
                "status": adr.status,
                "context": adr.context,
                "decision": adr.decision,
                "consequences": adr.consequences,
                "alternatives": adr.alternatives,
                "affected_requirements": adr.affected_requirements,
            }
            body = render_adr(decision, adr_number=i, date=date_str)
            body += _citations_block(adr.citations)
        else:
            decision = {
                "title": skel["title"],
                "status": "Proposed",
                "context": f"{_UNVERIFIED} — no synthesis authored for this decision.",
                "decision": f"{_UNVERIFIED} decision (author via synthesis round).",
            }
            body = render_adr(decision, adr_number=i, date=date_str)
        fname = f"ADR-{i:04d}-{_slug(skel['id'].removeprefix('adr-'))}.md"
        written.append(_write(out_dir / "adr" / fname, body))

    # WS1 (#1449): the standalone target-stack ADR, slotted LAST so every per-spec
    # ADR id above stays byte-identical. Guarded on target having ≥1 dimension — no
    # target → no file, no id-sequence change (the load-bearing backward-compat
    # invariant). Deterministic: the decision is built purely from the TargetStack.
    if target is not None and target.dimensions:
        target_number = len(adr_skeletons) + 1
        target_decision = target_stack_adr_decision(target, legacy_stack=legacy_stack)
        target_body = render_adr(target_decision, adr_number=target_number, date=date_str)
        target_fname = f"ADR-{target_number:04d}-target-stack.md"
        written.append(_write(out_dir / "adr" / target_fname, target_body))

    return written


def render_brds(
    synthesis: ScopeSynthesis,
    spec_skeletons: list[dict[str, Any]],
    system_map: SystemMap,
    out_dir: Path,
    *,
    slices: dict[str, FunctionalMapReport] | None = None,
    runtime_slices: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
    target: TargetStack | None = None,
) -> list[Path]:
    """Render one business ``BRD.md`` per context + a top-level BRD overview (Track C).

    For every spec skeleton, :func:`render_brd` produces a business-audience BRD from
    the SAME authored :class:`SpecJudgment` + per-context slice + system-map the
    technical spec consumes — sharing the spec's ``R-`` / ``FR-`` traceability ids.
    Each per-context BRD is written to ``scope/<id>.brd.md`` (beside its
    ``scope/<id>.spec.md``); a top-level ``BRD.md`` overview linking every context BRD
    is written to *out_dir*. A skeleton with no authored judgment still gets an honest
    (sparser) BRD from the skeleton title alone (no fabrication).
    """
    written: list[Path] = []
    judged_specs = {s.spec_id: s for s in synthesis.specs}
    overview_rows: list[list[str]] = []

    for skel in spec_skeletons:
        judgment = judged_specs.get(skel["id"])
        slice_report = _slice_key_for_skeleton(skel, slices) if slices else None
        spec_input: Any
        if judgment is not None:
            spec_input = judgment
        else:
            spec_input = {
                "spec_id": skel["id"],
                "title": skel["title"],
                "description": f"{_UNVERIFIED} — no business narrative authored for this context.",
            }
        rslice = (runtime_slices or {}).get(_slug(str(skel.get("domain", ""))))
        runtime_screens = rslice.get("screens") if rslice else None
        body = render_brd(
            spec_input,
            slice_report=slice_report,
            system_map=system_map,
            target=target,
            runtime_screens=runtime_screens,
        )
        brd_path = _write(out_dir / "scope" / f"{skel['id']}.brd.md", body)
        written.append(brd_path)
        overview_rows.append(
            [
                skel["title"],
                f"[`scope/{skel['id']}.brd.md`](scope/{skel['id']}.brd.md)",
                f"[`scope/{skel['id']}.spec.md`](scope/{skel['id']}.spec.md)",
            ]
        )

    written.append(_write(out_dir / "BRD.md", _brd_overview(overview_rows)))
    return written


def _brd_overview(rows: list[list[str]]) -> str:
    """The top-level BRD overview linking every per-context BRD to its spec (Track C)."""
    lines: list[str] = [
        heading("Business Requirements — Overview", level=1),
        "",
        (
            "A per-capability Business Requirements Document (BRD) accompanies each "
            "technical specification. Both share the same requirement ids (`R-`/`FR-`), "
            "so a business reader and an engineer trace the same requirement."
        ),
        "",
    ]
    if rows:
        lines.append(md_table(["Capability", "BRD", "Specification"], rows))
    else:
        lines.append("_No bounded capabilities identified — no per-context BRD emitted._")
    return "\n".join(lines).rstrip("\n")


# Risk-weight thresholds (#1357) — deterministic, tuned for the app-mod chain shape.
_HUB_FAN_IN = 2  # ≥ this many dependents → a hub (+1 risk point)
_HEAVY_MUST_FR_LOAD = 8  # ≥ this many must-priority FRs map-wide → carrier nodes riskier
_HIGH_RISK_POINTS = 2  # ≥ this many points → "high"
# Effort-hint thresholds (map-wide FR count → coarse S/M/L).
_EFFORT_M = 4
_EFFORT_L = 12


def derive_risk_weights(
    system_map: SystemMap,
    fr_distribution: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Per-node ``low|medium|high`` risk weights for the sequencer (#1357).

    Replaces the uniform ``medium`` default with a deterministic signal built from
    the system map (+ optional FR density): a node scores higher for incoming-edge
    fan-in (a hub many units depend on), unresolved/heuristic correlation
    confidence (uncertain boundaries), and a high count of ``must``-priority FRs in
    its bounded context. Two points → ``high``, one → ``medium``, none → ``low``.
    """
    # Fan-in: how many units depend on each node (as the dst of an edge). A hub
    # many units depend on is riskier to extract.
    fan_in: dict[str, int] = {}
    weak_conf: set[str] = set()
    for e in system_map.edges:
        fan_in[e.dst] = fan_in.get(e.dst, 0) + 1
        if e.confidence in {"unresolved", "heuristic"}:
            weak_conf.add(e.src)
    # must-priority FR load across the whole map (the app-mod chain is typically a
    # single "System" context, so per-domain granularity isn't available; a heavy
    # overall must-FR load raises the floor for every carrier node).
    by_spec = (fr_distribution or {}).get("by_spec") or {}
    must_total = sum(
        1
        for frs in by_spec.values()
        for fr in frs
        if str(fr.get("priority") or "").lower() == "must"
    )
    heavy_fr_load = must_total >= _HEAVY_MUST_FR_LOAD

    weights: dict[str, str] = {}
    for node in system_map.nodes:
        points = 0
        if fan_in.get(node.id, 0) >= _HUB_FAN_IN:
            points += 1
        if node.id in weak_conf:
            points += 1
        # A carrier node (has any domain) under a heavy must-FR load is riskier.
        if heavy_fr_load and node.domains:
            points += 1
        weights[node.id] = (
            "high" if points >= _HIGH_RISK_POINTS else "medium" if points == 1 else "low"
        )
    return weights


def _dimension_label(target: TargetStack, key: str) -> str:
    """The render label for a target dimension (WS1/WS2, #1449).

    Returns the resolved registry ``label`` (WS1 honesty — a ``registry_matched``
    ``False`` dimension carries an empty label, so we fall back to the verbatim
    ``key=value`` the operator declared rather than inventing one). Empty when the
    stack declares no such dimension.
    """
    return next(
        (d.label or f"{d.key}={d.value}" for d in target.dimensions if d.key == key),
        "",
    )


def _target_toward_clause(target: TargetStack | None) -> str:
    """The "toward <backend> [+ <frontend>]" clause for the loaded target (WS2, #1449).

    Backend leads (the extraction target is a service); the frontend is appended
    when declared. Empty when no target / no backend+frontend dimension — so a
    no-target roadmap is byte-identical. Deterministic: derived purely from the
    resolved :class:`TargetStack` labels (registry honesty from WS1).
    """
    if target is None or not target.dimensions:
        return ""
    backend = _dimension_label(target, "backend")
    frontend = _dimension_label(target, "frontend")
    parts = [p for p in (backend, frontend) if p]
    if not parts:
        return ""
    return " + ".join(parts)


def _wave_objective(
    item_ids: list[str],
    system_map: SystemMap,
    *,
    target: TargetStack | None = None,
) -> str:
    """A short per-wave objective naming the wave's ACTUAL member units (#1378).

    Derived from the wave's own member nodes: each member contributes its bounded
    contexts (post-#1375 a node carries ``domains: list``), and a member with no
    domain contributes its unit id — so the objective always names the real units
    in *that* wave rather than a domain set that may not cover every member (the
    old "Extract the public context" prose contradicting ``.#api``/``.#ui``
    members). Falls back to a bare unit-count clause only when the wave carries no
    identifiable members at all.

    ``target`` (WS2, #1449) is the resolved :class:`TargetStack` (or ``None``). When
    it carries ≥1 dimension with a backend/frontend label, the objective reads as
    extraction *toward* that target ("… stand up their replacements on Spring Boot
    3.3."). No target (or no backend/frontend dimension) → the clause is empty and
    the objective is byte-identical to prior behavior (the #1408 invariant).
    """
    wanted = set(item_ids)
    labels: list[str] = []
    for node in system_map.nodes:
        if node.id not in wanted:
            continue
        labels.extend(node.domains or [node.id])
    labels = sorted(set(labels))
    toward = _target_toward_clause(target)
    suffix = f" on {toward}" if toward else ""
    if labels:
        return (
            f"Extract the {', '.join(labels)} context(s) and stand up their replacements{suffix}."
        )
    return f"Extract these units and stand up their replacements{suffix}."


def _effort_hint(fr_distribution: dict[str, Any] | None) -> str:
    """Coarse S/M/L effort hint from the FR count the map's contexts carry.

    Without a per-node FR mapping (the app-mod chain is typically a single
    context), the map-wide FR load is a deliberately coarse proxy.
    """
    by_spec = (fr_distribution or {}).get("by_spec") or {}
    fr_count = sum(len(v) for v in by_spec.values()) if by_spec else 0
    if fr_count >= _EFFORT_L:
        return "L"
    if fr_count >= _EFFORT_M:
        return "M"
    return "S"


def _target_endpoints_by_spec(
    synthesis: ScopeSynthesis | None,
) -> dict[str, list[ApiEndpoint]]:
    """Index each spec's §8b target endpoints by ``spec_id`` (WS2, #1449).

    Only ``kind == "target"`` endpoints (the resource-per-aggregate REST design the
    harness synthesized in SP3, #1411) are collected — current-contract endpoints
    are excluded so the roadmap references genuine target API surface. Endpoints are
    kept in authored order (deterministic). Empty when no synthesis / no target
    endpoints.
    """
    by_spec: dict[str, list[ApiEndpoint]] = {}
    if synthesis is None:
        return by_spec
    for spec in synthesis.specs:
        targets = [e for e in spec.api_endpoints if e.kind == "target"]
        if targets:
            by_spec[spec.spec_id] = targets
    return by_spec


def _wave_target_endpoints(
    item_ids: list[str],
    system_map: SystemMap,
    endpoints_by_spec: dict[str, list[ApiEndpoint]],
) -> list[ApiEndpoint]:
    """The §8b target endpoints introduced by a wave (WS2, #1449).

    The join is the SAME domain-slug join the spec renderer uses: a wave item is a
    system-map node id; each node carries ``domains``; a domain maps to its
    ``spec-<domain-slug>`` id; that spec's ``kind="target"`` endpoints belong to the
    wave. Only endpoints reached through a REAL node→domain→spec chain are returned
    (no fabricated link) — a wave whose members carry no domain, or whose domains
    have no target-endpoint spec, yields ``[]`` and is annotated with the backend
    label alone. Deduplicated on (verb, path), stable order.
    """
    if not endpoints_by_spec:
        return []
    wanted = set(item_ids)
    seen: set[tuple[str, str]] = set()
    collected: list[ApiEndpoint] = []
    for node in system_map.nodes:
        if node.id not in wanted:
            continue
        for domain in node.domains:
            spec_id = f"spec-{_slug(domain)}"
            for endpoint in endpoints_by_spec.get(spec_id, []):
                key = (endpoint.verb, endpoint.path)
                if key in seen:
                    continue
                seen.add(key)
                collected.append(endpoint)
    return collected


def _target_endpoints_block(
    item_ids: list[str],
    system_map: SystemMap,
    endpoints_by_spec: dict[str, list[ApiEndpoint]],
) -> list[str]:
    """The "Target endpoints (§8b)" markdown block for *item_ids* (WS2 / #1464).

    Reuses :func:`_wave_target_endpoints` (the same node→domain→``spec-<slug>`` join)
    so a unit that lands in a wave, in Deferred, or in Isolated all list their §8b
    target endpoints identically — the endpoints belong to the *unit*, not to the
    wave, and a web unit is often deferred/isolated (petclinic). Empty item set or no
    mapped endpoints → ``[]`` (no fabricated links; byte-identical to prior behavior
    where a section had no endpoints)."""
    endpoints = _wave_target_endpoints(item_ids, system_map, endpoints_by_spec)
    if not endpoints:
        return []
    block = ["**Target endpoints (§8b):**", ""]
    for endpoint in endpoints:
        verb = endpoint.verb or "—"
        path = endpoint.path or "—"
        trace = f" (from `{endpoint.from_controller}`)" if endpoint.from_controller else ""
        block.append(f"- `{verb} {path}`{trace}")
    block.append("")
    return block


#: MR5 (#1554): the visible ⚠️ marker suffixed to a roadmap item / spec whose
#: migration state is UNVERIFIED (scoped IN + planned, but never confirmed).
_UNVERIFIED_MARK = " ⚠️ UNVERIFIED"


def _unverified_marker(unit_id: str, unverified_units: frozenset[str]) -> str:
    """The ⚠️ suffix for *unit_id* when its migration state is UNVERIFIED, else ``""``."""
    return _UNVERIFIED_MARK if unit_id in unverified_units else ""


def render_roadmap(  # noqa: PLR0912 — the documented roadmap render surface (target + MR5 filter)
    system_map: SystemMap,
    out_dir: Path,
    fr_distribution: dict[str, Any] | None = None,
    *,
    target: TargetStack | None = None,
    synthesis: ScopeSynthesis | None = None,
    skip_units: frozenset[str] | None = None,
    unverified_units: frozenset[str] | None = None,
) -> Path:
    """``sequencer.order`` → ROADMAP.md (waves + deferred + never-extract + cycles).

    Enriched (#1357): the sequencer is fed real per-node ``risk_weights`` (not a
    uniform ``medium``), and each wave renders an objective + per-item effort hint
    and the functional requirements it delivers — so the roadmap reads as a plan,
    not a bare topological sort.

    **WS2 (#1449) — target-aware sequencing.** ``target`` is the resolved
    :class:`TargetStack` (or ``None``); ``synthesis`` carries the authored specs
    whose §8b ``kind="target"`` endpoints (SP3, #1411) link to each wave. When
    ``target`` carries ≥1 dimension, the roadmap reads as extraction *toward* the
    target: a header names the target stack, each wave objective ends "… on <backend>",
    a per-wave "Target:" line names the backend (+ frontend), and — where a unit
    maps through the SAME node→domain→``spec-<slug>`` join the spec renderer uses to
    real §8b endpoints — those endpoints are listed. No fabricated links: a section
    with no clean endpoint mapping is annotated with the backend label alone.

    The §8b endpoints belong to the *unit*, not the wave (#1464): a web tier is often
    **deferred** (depends on a high-risk unit) or **isolated**, and that is the unit
    that owns the target endpoints. So the Deferred and Isolated sections render the
    same "Target endpoints (§8b)" block (:func:`_target_endpoints_block`) as the
    waves — otherwise the roadmap surfaced zero target endpoints on a real web app
    whose web unit was deferred.

    Every target line is guarded on ``target is not None and target.dimensions`` —
    so a no-target run is BYTE-IDENTICAL to prior behavior (the #1408 invariant).
    """
    has_target = target is not None and bool(target.dimensions)
    endpoints_by_spec = _target_endpoints_by_spec(synthesis) if has_target else {}
    # MR5 (#1554): the ⚠️ marker set for units whose migration state is UNVERIFIED —
    # scoped IN (planned) but flagged. Empty → byte-identical (the #1408 invariant).
    unverified = unverified_units or frozenset()
    risk_weights = derive_risk_weights(system_map, fr_distribution)
    # MR5 (#1554): confirmed-modern units are skipped — they plan nothing, they demote
    # to the Boundary Contracts (already-modernized) section. Empty → byte-identical.
    roadmap = order(system_map, risk_weights=risk_weights, skip_units=skip_units)
    lines: list[str] = ["# Modernization Roadmap", ""]
    if has_target:
        assert target is not None  # narrowed by has_target; mypy guard
        toward = _target_toward_clause(target)
        if toward:
            lines.append(f"_Sequenced toward the modernization target: {toward}._")
            lines.append("")
    if unverified:
        # MR5 (#1554): a legend so a reader distinguishes planned (legacy/partial)
        # from ⚠️-marked (migration state UNVERIFIED — scoped IN, never dropped).
        lines.append(
            "> ⚠️ = **modernization-state UNVERIFIED** — this unit is scoped IN and planned "
            "normally, but its migrated-vs-legacy state was never confirmed. Treat as legacy "
            "work until verified; it is never dropped."
        )
        lines.append("")
    if not roadmap.waves:
        lines.append("_No extractable units — see deferred / never-extract below._")
    for wave in roadmap.waves:
        lines.append(f"## Wave {wave.wave}")
        lines.append("")
        item_ids = [it.id for it in wave.items]
        lines.append(f"**Objective:** {_wave_objective(item_ids, system_map, target=target)}")
        lines.append("")
        if has_target:
            assert target is not None  # narrowed by has_target; mypy guard
            backend = _dimension_label(target, "backend")
            target_label = backend or _target_toward_clause(target)
            if target_label:
                lines.append(f"**Target:** toward {target_label}")
                lines.append("")
            lines.extend(_target_endpoints_block(item_ids, system_map, endpoints_by_spec))
        if wave.rationale:
            lines.append(f"_{wave.rationale}_")
            lines.append("")
        for it in wave.items:
            deps = ", ".join(it.depends_on) if it.depends_on else "—"
            effort = _effort_hint(fr_distribution)
            mark = _unverified_marker(it.id, unverified)
            lines.append(
                f"- **{it.id}**{mark} (risk: {it.risk}; effort: {effort}; depends on: {deps})"
            )
        lines.append("")
    if roadmap.deferred:
        lines.append("## Deferred")
        lines.append("")
        for it in roadmap.deferred:
            lines.append(f"- **{it.id}**{_unverified_marker(it.id, unverified)} — {it.rationale}")
        lines.append("")
        if has_target:
            # #1464: a deferred unit still owns its §8b endpoints (the web unit is
            # often deferred). List them so the roadmap surfaces the target surface.
            deferred_ids = [it.id for it in roadmap.deferred]
            lines.extend(_target_endpoints_block(deferred_ids, system_map, endpoints_by_spec))
    if roadmap.never_extract:
        lines.append("## Never Extract (cycle members)")
        lines.append("")
        for it in roadmap.never_extract:
            lines.append(f"- **{it.id}** — {it.rationale}")
        lines.append("")
    if roadmap.isolated:
        lines.append("## Isolated (no dependencies discovered)")
        lines.append("")
        lines.append(
            "_Standalone units — no edges to or from the rest of the system, so they "
            "carry no wave ordering. Extract independently whenever convenient._"
        )
        lines.append("")
        for it in roadmap.isolated:
            lines.append(f"- **{it.id}**{_unverified_marker(it.id, unverified)} — {it.rationale}")
        lines.append("")
        if has_target:
            # #1464: an isolated unit still owns its §8b endpoints.
            isolated_ids = [it.id for it in roadmap.isolated]
            lines.extend(_target_endpoints_block(isolated_ids, system_map, endpoints_by_spec))
    if roadmap.cycles:
        lines.append("## Surfaced Dependency Cycles")
        lines.append("")
        for cyc in roadmap.cycles:
            lines.append(f"- {' → '.join(cyc)}")
        lines.append("")
    return _write(out_dir / "ROADMAP.md", "\n".join(lines).rstrip("\n") + "\n")


# ---------------------------------------------------------------------------
# Refcheck (SP2.4) — validate every spec/ADR citation resolves.
# ---------------------------------------------------------------------------


def refcheck_outputs(paths: list[Path], repo_root: Path) -> RefCheckReport:
    """Concatenate the rendered spec/ADR markdown and refcheck all citations."""
    combined = "\n".join(
        p.read_text(encoding="utf-8") for p in paths if p.suffix == ".md" and p.is_file()
    )
    return refcheck_check(combined, repo_root)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _write_state(run_dir: Path, system_map: SystemMap, prompts: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "system-map.json").write_text(
        json.dumps(system_map.model_dump(mode="json"), separators=(",", ":")),
        encoding="utf-8",
    )
    prompts_path = run_dir / "synthesis_prompts.json"
    prompts_path.write_text(json.dumps(prompts, indent=2), encoding="utf-8")
    return prompts_path


def _load_system_map(run_dir: Path) -> SystemMap:
    obj = json.loads((run_dir / "system-map.json").read_text(encoding="utf-8"))
    return SystemMap.model_validate(obj)


# ---------------------------------------------------------------------------
# Target stack (SP2, #1410) — persisted by the orchestrator, read here.
# ---------------------------------------------------------------------------


def load_target(
    *,
    state_dir: Path,
    run_id: str,
    explicit_path: Path | None,
) -> TargetStack | None:
    """Resolve the optional :class:`TargetStack` for this run (SP2, #1410).

    Resolution order:

    * an ``explicit_path`` (the ``--target`` CLI option) wins — but a path that
      does not exist resolves to ``None`` (a missing target is never an error);
    * otherwise auto-discover ``<state_dir>/<run_id>/target-stack.json`` (the
      artifact the orchestrator persists), mirroring the other run-state files;
    * absent both → ``None`` (no target → fully backward-compatible behavior).

    The file, when present, is parsed via :meth:`TargetStack.model_validate`.
    """
    candidate = (
        explicit_path if explicit_path is not None else state_dir / run_id / "target-stack.json"
    )
    if not candidate.is_file():
        return None
    obj = json.loads(candidate.read_text(encoding="utf-8"))
    return TargetStack.model_validate(obj)


def load_hla(
    *,
    state_dir: Path,
    run_id: str,
    explicit_path: Path | None,
) -> HlaSource | None:
    """Resolve the optional :class:`HlaSource` for this run (Track A, #1428).

    Mirrors :func:`load_target` exactly (same resolution shape as SP2's target):

    * an ``explicit_path`` (the ``--hla`` CLI option) wins — but a path that does
      not exist resolves to ``None`` (a missing HLA is never an error);
    * otherwise auto-discover ``<state_dir>/<run_id>/hla-source.json`` (the artifact
      the orchestrator persists ONLY when ``.foundry/hla/`` docs exist), mirroring
      the other run-state files;
    * absent both → ``None`` (no HLA → fully backward-compatible; the Architecture
      Conformance section is omitted).

    The file, when present, is parsed via :meth:`HlaSource.model_validate`.
    """
    candidate = (
        explicit_path if explicit_path is not None else state_dir / run_id / "hla-source.json"
    )
    if not candidate.is_file():
        return None
    obj = json.loads(candidate.read_text(encoding="utf-8"))
    return HlaSource.model_validate(obj)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run(  # noqa: PLR0913, PLR0915 — the documented pipeline surface (SPEC §5)
    code_root: Path,
    context_root: Path,
    *,
    output_dir: Path,
    state_dir: Path,
    run_id: str = "run",
    functional_map: Path | None = None,
    schema: Path | None = None,
    api: Path | None = None,
    app_digest: Path | None = None,
    test_inventory: Path | None = None,
    target_path: Path | None = None,
    hla_path: Path | None = None,
    no_synthesis: bool = False,
    no_migration_synthesis: bool = False,
    extract_only: bool = False,  # noqa: ARG001 — documented CLI surface; default leg == extract leg
    continue_path: Path | None = None,
    generated_at: datetime | None = None,
    scope_contexts: list[str] | None = None,
    repo_set: RepoSet | None = None,
    modernized_overrides: set[str] | None = None,
) -> PipelineResult:
    """Run the synthesizer.

    Legs:

    * **continue** (``continue_path`` given) — reload the system-map + skeletons,
      merge the authored judgments, render specs/ADRs/roadmap, refcheck. Exit 0
      (or 3 on a schema-invalid synthesis, surfaced as ``ValueError`` to the CLI).
    * **--no-synthesis** — build everything, render deterministic skeletons with
      ``⚠️ UNVERIFIED`` stubs (no synthesis round). Exit 0.
    * **--extract-only** — build the system-map + skeletons, write
      ``synthesis_prompts.json``, exit 7.
    * **default** — same as ``--extract-only`` (a synthesis round is expected).

    In every leg the deconstruction (MODERNIZATION.md + system-map.json) is
    written durably to *output_dir*.

    Multi-repo migration confirmation (MR4, #1553): when a *repo_set* is present
    (and NOT ``no_migration_synthesis``), each repo's tree is scored for migration
    evidence; confident units are recorded ``deterministic``, uncertain
    (``partial``/low-confidence) units get harness-judgment prompts (in
    ``migration_prompts.json``) and an ``unverified`` placeholder in
    ``migration-states.json``. The continue leg merges the harness verdicts (a
    ``migration_states`` section of the ``--judgments`` file) → ``source="harness"``.
    Single-repo runs write NO migration state (byte-identical, #1408).
    """
    run_dir = state_dir / run_id
    title = f"{Path(code_root).name or 'System'} — Modernization"

    # The optional target stack (SP2, #1410): explicit --target wins, else
    # auto-discovered under the run-id state dir; ``None`` → byte-identical
    # descriptive behavior. Resolved once here so every leg sees the same target;
    # downstream synthesis/render (SP3+) consumes it. Resolving it now validates
    # the artifact early — a malformed target-stack.json surfaces as a ValueError
    # (CLI exit 3) rather than silently later — and surfaces the resolved stack to
    # the operator (warn-not-fail: an absent target is silent, not an error).
    target = load_target(state_dir=state_dir, run_id=run_id, explicit_path=target_path)
    if target is not None and target.dimensions:
        labels = ", ".join(d.label or f"{d.key}={d.value}" for d in target.dimensions)
        print(f"mod-modernization-scope: target stack — {labels}", file=sys.stderr)

    # The optional HLA source (Track A, #1428): explicit --hla wins, else
    # auto-discovered under the run-id state dir; ``None`` → no Architecture
    # Conformance section (byte-identical descriptive behavior). Resolved once here
    # so every leg sees the same HLA; a malformed hla-source.json surfaces as a
    # ValueError (CLI exit 3) early rather than silently later.
    hla = load_hla(state_dir=state_dir, run_id=run_id, explicit_path=hla_path)
    if hla is not None and hla.documents:
        print(
            f"mod-modernization-scope: HLA grounding — {len(hla.documents)} document(s)",
            file=sys.stderr,
        )

    # --- Continue leg: judgments already authored -----------------------
    if continue_path is not None:
        try:
            system_map = _load_system_map(run_dir)
        except FileNotFoundError as exc:
            raise ArtifactMissingError(
                f"no system-map state under {run_dir} — run --extract-only first"
            ) from exc
        try:
            raw = json.loads(continue_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ArtifactMissingError(f"judgments file not found: {continue_path}") from exc
        # MR4 (#1553): the migration verdicts ride in an additive ``migration_states``
        # section of the SAME --judgments file (one file for the SKILL to author). Pop
        # it off BEFORE validate_synthesis — ScopeSynthesis is ``extra="forbid"``, so it
        # must not see the extra key.
        migration_verdicts_raw = (
            raw.pop("migration_states", None) if isinstance(raw, dict) else None
        )
        synthesis = validate_synthesis(raw)  # ValueError → CLI exit 3

        # Merge the harness migration verdicts into the extract-leg pending states, then
        # rewrite migration-states.json. No pending state (single-repo or
        # --no-migration-synthesis extract leg) → no-op. An unknown code_root in a
        # verdict raises ValueError (CLI exit 3), mirroring synthesis validation.
        pending_migration = _load_migration_state(run_dir)
        confirmed_states: MigrationStates | None = pending_migration
        if pending_migration is not None:
            verdicts = migration_verdicts_raw if isinstance(migration_verdicts_raw, list) else []
            merged_states = merge_migration_verdicts(
                pending_migration, verdicts, system_map, Path(code_root)
            )
            _write_migration_state(run_dir, merged_states)
            confirmed_states = merged_states

        # MR5 (#1554): the what's-left filter — resolve the confirmed migration states
        # (+ any --modernized overrides) into the modern units to SKIP from the roadmap
        # (demoted to Boundary Contracts) and the UNVERIFIED units to ⚠️-mark (scoped
        # IN, never dropped). No migration state + no overrides → empty WhatsLeft →
        # byte-identical sequencer/roadmap (the #1408 additive invariant).
        whats_left = resolve_whats_left(system_map, confirmed_states, modernized_overrides or set())

        # Per-feature slice scope (#1542): filter the spec fan-out to the requested
        # bounded contexts + document their dependencies as boundary contracts. An inactive
        # scope (no --feature/--context) is byte-identical to a whole-app run (#1408).
        scope = resolve_scope(system_map, scope_contexts)
        all_spec_skeletons = build_spec_skeletons(system_map)
        spec_skeletons = filter_skeletons_to_scope(all_spec_skeletons, scope)
        adr_skeletons = build_adr_skeletons(system_map)
        # Resolve the functional-map path — the continue leg runs before the default
        # leg's discover_artifacts, so fall back to the conventional location when the
        # CLI omitted --functional-map (#1460). Absent → None (specs still render).
        continue_fm = _resolve_functional_map(context_root, functional_map)
        # Re-derive the FR distribution deterministically for the coverage audit
        # (#1356). Same inputs as the extract leg → same distribution.
        continue_dist: dict[str, Any] | None = None
        if continue_fm is not None:
            continue_inventory = functional_map_to_inventory(_load(continue_fm))
            continue_dist = distribute_frs(continue_inventory, spec_skeletons)
        written = render_deconstruction_artifacts(
            system_map,
            output_dir,
            title=title,
            fr_distribution=continue_dist,
            hla=hla,
            target=target,
            runtime_journeys=_load_runtime_journeys(context_root),
        )
        continue_slices = build_slices(continue_fm, system_map)
        continue_runtime = build_runtime_slices(context_root, system_map, output_dir)
        written += render_specs_and_adrs(
            synthesis,
            spec_skeletons,
            adr_skeletons,
            system_map,
            output_dir,
            generated_at=system_map.generated_at,
            slices=continue_slices,
            target=target,
            legacy_stack=_legacy_stack_label(system_map),
            unverified_units=whats_left.unverified_units,
        )
        # Track C (#1429): the business BRD.md per context + the top-level overview,
        # from the SAME authored specs + slices (shared R-/FR- traceability ids).
        written += render_brds(
            synthesis,
            spec_skeletons,
            system_map,
            output_dir,
            slices=continue_slices,
            runtime_slices=continue_runtime,
            target=target,
        )
        # Scoped roadmap (#1542): when a slice is active, wave only the slice + its
        # dependency closure (via the induced subgraph) so ROADMAP.md sequences just this
        # piece + its prerequisites. Inactive → the whole system-map (byte-identical).
        roadmap_map = (
            induced_subgraph(system_map, scope.closure)
            if scope.active and scope.closure is not None
            else system_map
        )
        written.append(
            render_roadmap(
                roadmap_map,
                output_dir,
                continue_dist,
                target=target,
                synthesis=synthesis,
                skip_units=whats_left.modern_units,
                unverified_units=whats_left.unverified_units,
            )
        )
        # Boundary Contracts (#1542): append the slice's dependencies (documented, not
        # re-specced) to MODERNIZATION.md. No-op for a whole-app run (empty section).
        boundary = render_boundary_section(scope)
        # MR5 (#1554): append the already-modernized units as Boundary Contracts —
        # "integrate against, do not rebuild" — distinct from the slice-boundary above.
        # Empty when no modern units (byte-identical, nothing appended).
        modern_boundary = render_modernized_boundary_section(system_map, whats_left.modern_units)
        boundary = boundary + modern_boundary
        if boundary:
            mod_path = output_dir / "MODERNIZATION.md"
            if mod_path.is_file():
                mod_path.write_text(
                    mod_path.read_text(encoding="utf-8") + boundary, encoding="utf-8"
                )
        report = refcheck_outputs(written, code_root)
        exit_code = 0 if report.ok else 6
        return PipelineResult(
            exit_code,
            written=written,
            deconstruction_path=output_dir / "MODERNIZATION.md",
        )

    # --- Build the system-map from the discovered artifacts --------------
    # The single-repo ``artifacts`` bundle is ALWAYS resolved — the FR
    # inventory / slices downstream read ``artifacts.functional_map`` — so a
    # modernization run still requires a functional-map for its FR layer.
    artifacts = discover_artifacts(
        context_root,
        functional_map=functional_map,
        schema=schema,
        api=api,
        app_digest=app_digest,
    )
    if generated_at is None:
        generated_at = datetime.now(UTC)
    # MR4 (#1553): the confirmed migration-states + any harness-judgment prompts
    # for the uncertain units. Multi-repo-only + gated by --no-migration-synthesis;
    # single-repo runs leave both empty so NO migration-states.json / prompts are
    # written (the #1408 additive invariant).
    migration_states: list[ConfirmedMigrationState] = []
    migration_prompts: list[dict[str, Any]] = []
    if repo_set is not None and repo_set.entries:
        # Multi-repo (MR2, #1551): the system-map correlates every repo's
        # per-repo artifacts into cross-repo edges. The FR bundle above still
        # feeds the FR inventory / slices (the UI repo's functional-map).
        system_map = build_system_map_from_repo_set(
            repo_set, context_root, generated_at=generated_at
        )
        # MR4 (#1553): score each repo's tree for migration evidence and split into
        # confident deterministic states + uncertain (partial/low) harness prompts.
        evidence = score_repo_set_evidence(repo_set, context_root)
        if no_migration_synthesis:
            # L3 skipped: every band stays as MR3 scored it, source=deterministic;
            # no prompts, no exit-7-for-migration handshake.
            migration_states = [deterministic_state(ev) for ev in evidence.values()]
        else:
            migration_states, migration_prompts = confirm_migration_states(evidence)
    else:
        # Additive invariant (#1408): no repo-set → byte-identical single-repo map,
        # and NO migration scoring / prompts / migration-states.json at all.
        system_map = build_system_map_from_artifacts(
            artifacts, code_root=str(code_root), generated_at=generated_at
        )
    spec_skeletons = build_spec_skeletons(system_map)
    adr_skeletons = build_adr_skeletons(system_map)
    # The functional-map FR layer, threaded into the spec-synthesis prompts (#1356).
    inventory = functional_map_to_inventory(_load(artifacts.functional_map))
    fr_distribution = distribute_frs(inventory, spec_skeletons)

    # Deconstruction is always written durably (SP2.3), with the FR-coverage audit
    # and (Track A, #1428) the Architecture Conformance section when HLA is present.
    written = render_deconstruction_artifacts(
        system_map,
        output_dir,
        title=title,
        fr_distribution=fr_distribution,
        hla=hla,
        target=target,
        runtime_journeys=_load_runtime_journeys(context_root),
    )

    # --- --no-synthesis: deterministic skeletons with UNVERIFIED stubs ---
    if no_synthesis:
        empty = ScopeSynthesis()
        no_synth_slices = build_slices(artifacts.functional_map, system_map)
        no_synth_runtime = build_runtime_slices(context_root, system_map, output_dir)
        written += render_specs_and_adrs(
            empty,
            spec_skeletons,
            adr_skeletons,
            system_map,
            output_dir,
            generated_at=generated_at,
            slices=no_synth_slices,
            target=target,
            legacy_stack=_legacy_stack_label(system_map),
        )
        # Track C (#1429): the business BRD.md per context + the top-level overview.
        written += render_brds(
            empty,
            spec_skeletons,
            system_map,
            output_dir,
            slices=no_synth_slices,
            runtime_slices=no_synth_runtime,
            target=target,
        )
        written.append(
            render_roadmap(
                system_map,
                output_dir,
                fr_distribution,
                target=target,
                synthesis=empty,
            )
        )
        # MR4 (#1553): persist the deterministic migration-states (multi-repo only).
        # --no-synthesis carries no synthesis round, so there is no continue leg to
        # confirm uncertain units — they stay as the scorer banded them. When
        # --no-migration-synthesis is ALSO set, migration_states is the pure MR3
        # deterministic set (built above). Single-repo → migration_states empty → no file.
        if migration_states:
            _write_migration_state(run_dir, MigrationStates(states=tuple(migration_states)))
        return PipelineResult(
            0, written=written, deconstruction_path=output_dir / "MODERNIZATION.md"
        )

    # --- extract-only / default: write prompts, exit 7 -------------------
    # SP-D (#1384): thread the full per-context functional-map slice into each
    # spec prompt (reusing SP-C's build_slices — the same slug join the continue
    # leg's renderer uses) so the harness authors from the full slice.
    extract_slices = build_slices(artifacts.functional_map, system_map)
    # SP-G (#1387): thread this repo's legacy tests as per-context parity oracles.
    # Additive — an absent test-inventory.json yields {}, so §11 is unchanged.
    resolved_test_inventory = test_inventory or _first_test_inventory(context_root)
    extract_oracles = build_parity_oracles(resolved_test_inventory, system_map)
    extract_runtime = build_runtime_slices(context_root, system_map, output_dir)
    prompts = build_synthesis_prompts(
        system_map,
        spec_skeletons,
        adr_skeletons,
        fr_distribution,
        slices=extract_slices,
        parity_oracles=extract_oracles,
        runtime_slices=extract_runtime,
        target=target,
        hla=hla,
    )
    prompts_path = _write_state(run_dir, system_map, prompts)
    # MR4 (#1553): persist migration-states.json (deterministic bands + unverified
    # placeholders) + migration_prompts.json (uncertain units) for the SKILL body to
    # author verdicts, which the continue leg merges. Multi-repo only; single-repo →
    # migration_states empty → no file (byte-identical additive invariant, #1408).
    if migration_states:
        _write_migration_state(
            run_dir, MigrationStates(states=tuple(migration_states)), migration_prompts
        )
    return PipelineResult(
        7,
        written=written,
        prompts_path=prompts_path,
        deconstruction_path=output_dir / "MODERNIZATION.md",
    )
