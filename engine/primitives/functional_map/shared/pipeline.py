from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from primitives.functional_map.shared.audit import audit_synthesis
from primitives.functional_map.shared.extractor import extract
from primitives.functional_map.shared.models import (
    FunctionalDigest,
    FunctionalMapReport,
    RoleReconciliation,
    thread_business_rules,
)
from primitives.functional_map.shared.recon import run_recon
from primitives.functional_map.shared.render import render_json, render_markdown
from primitives.functional_map.shared.slicing import (
    contexts_from_system_map,
    fallback_contexts,
    slice_functional_map,
)
from primitives.functional_map.shared.synthesis_prompts import (
    build_synthesis_prompts,
    validate_synthesis,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class SliceArtifact:
    """One per-context slice: its rendered markdown + JSON (SP-A, #1381)."""

    slug: str
    output_markdown: str
    output_json: str


@dataclass(frozen=True)
class PipelineResult:
    exit_code: int
    output_markdown: str | None
    output_json: str | None
    state_dir: Path
    #: Per-bounded-context slices, present only in sliced mode (SP-A, #1381).
    slices: tuple[SliceArtifact, ...] = ()
    #: Whether a sliced run was requested (``--system-map``/``--contexts`` supplied).
    #: Lets the CLI distinguish "sliced pass produced no slices" (warn-worthy — #1406)
    #: from "whole-app pass, no slices expected."
    sliced_requested: bool = False
    #: How the slice contexts were resolved: ``"contexts"`` (explicit ``--contexts``),
    #: ``"system-map"`` (system-map node domains drove a genuine multi-context
    #: partition), ``"system-map-coarse"`` (a system-map was supplied but resolved to a
    #: *single* bucket — its nodes carry no business-domain labels, so every endpoint
    #: collapsed to one context and it did NOT meaningfully partition, #1406/#1511), or
    #: ``"path-prefix"`` (no usable system-map/contexts at all — slicing fell back to
    #: path-prefix clustering, #1406). ``None`` for a whole-app run. The CLI surfaces the
    #: coarse / path-prefix fallbacks (see :func:`is_fallback_provenance`) so operators
    #: are not misled into thinking a coarse app-mod system-map drove the slicing.
    slice_provenance: str | None = None


def _write_analysis(run_dir: Path, digest: FunctionalDigest) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "analysis.json").write_text(
        json.dumps({"digest": digest.model_dump(mode="json")}, separators=(",", ":"))
    )


def _load_digest(run_dir: Path) -> FunctionalDigest:
    obj = json.loads((run_dir / "analysis.json").read_text())
    return FunctionalDigest.model_validate(obj["digest"])


def _resolve_contexts(
    report: FunctionalMapReport,
    *,
    system_map_path: Path | None,
    contexts: dict[str, list[str]] | None,
) -> tuple[dict[str, list[str]], str]:
    """Determine the ``{context: [endpoint-key]}`` map + its provenance (SP-A, #1381).

    Priority: an explicit inline ``contexts`` map wins (provenance ``"contexts"``);
    else a ``--system-map`` file supplies the bounded contexts from node ``domains``
    (provenance ``"system-map"``); else — a standalone-but-sliced request, OR a
    system-map whose nodes carry no domain labels (the coarse app-mod component model,
    #1406) — fall back to clustering endpoints by their own path-prefix signals
    (provenance ``"path-prefix"``) so a coherent, non-empty slice set is always
    produced. The provenance lets the caller surface the path-prefix fallback rather
    than silently implying the system-map drove the slicing.
    """
    endpoints = report.digest.endpoints
    if contexts:
        return {c: list(members) for c, members in contexts.items()}, "contexts"
    if system_map_path is not None:
        system_map = json.loads(system_map_path.read_text())
        resolved = contexts_from_system_map(system_map, endpoints)
        if resolved:
            # A *single* resolved context means the system-map's nodes carried no
            # business-domain labels: every endpoint collapsed into one bucket (typically
            # the web-tier catch-all of a coarse app-mod component model). The system-map
            # did NOT meaningfully partition, so this is a fallback the operator must be
            # told about (#1511) — distinct from a genuine multi-context partition.
            provenance = "system-map" if len(resolved) > 1 else "system-map-coarse"
            return resolved, provenance
    return fallback_contexts(endpoints), "path-prefix"


#: Slice provenances that mean "the system-map did NOT drive the partition" — the
#: caller surfaces these (a stderr NOTE + a ``SLICING-NOTE.md``) so operators are not
#: misled (#1406/#1511).
_FALLBACK_PROVENANCES = frozenset({"path-prefix", "system-map-coarse"})


def is_fallback_provenance(provenance: str | None) -> bool:
    """True when *provenance* is a slicing fallback (path-prefix or coarse system-map).

    The single predicate both disclosure sites (the stderr NOTE and the on-disk
    ``SLICING-NOTE.md``) key on, so a coarse app-mod system-map that collapses to one
    bucket is surfaced exactly like a raw path-prefix fallback (#1511)."""
    return provenance in _FALLBACK_PROVENANCES


def _build_slices(
    report: FunctionalMapReport,
    *,
    system_map_path: Path | None,
    contexts: dict[str, list[str]] | None,
) -> tuple[tuple[SliceArtifact, ...], str]:
    """Slice *report* per bounded context; return the artifacts + their provenance."""
    resolved, provenance = _resolve_contexts(
        report, system_map_path=system_map_path, contexts=contexts
    )
    sliced = slice_functional_map(report, resolved)
    artifacts = tuple(
        SliceArtifact(
            slug=slug,
            output_markdown=render_markdown(slice_report),
            output_json=render_json(slice_report),
        )
        for slug, slice_report in sorted(sliced.items())
    )
    return artifacts, provenance


def run(
    code_root: Path,
    state_dir: Path,
    *,
    stack_flavor: str,
    spec: Path | None,
    run_id: str,
    no_synthesis: bool,
    extract_only: bool,
    continue_path: Path | None,
    system_map_path: Path | None = None,
    contexts: dict[str, list[str]] | None = None,
) -> PipelineResult:
    run_dir = state_dir / run_id
    # Slicing activates ONLY when a system-map/contexts input is supplied. When both
    # are absent the run is byte-identical to today's whole-app output (golden-locked).
    sliced_mode = system_map_path is not None or bool(contexts)

    # Auto-resume (#1406): a sliced pass (``--system-map``/``--contexts``, no
    # ``--continue``) dispatched AFTER the whole-app pass already completed its
    # synthesis round finds a durable ``synthesis.json`` in the run dir. Without this,
    # the bare ``--system-map`` re-invocation re-enters the extract leg, re-emits
    # ``synthesis_prompts.json`` and exits 7 — writing NO slice dirs (the operator
    # following the SKILL.md verbatim silently gets nothing). When a completed
    # synthesis exists we transparently resume from it so the documented bare form
    # deterministically emits the slices. An explicit ``--continue`` still wins.
    if continue_path is None and sliced_mode and not extract_only:
        existing_synthesis = run_dir / "synthesis.json"
        if (run_dir / "analysis.json").is_file() and existing_synthesis.is_file():
            continue_path = existing_synthesis

    # Continue leg
    if continue_path is not None:
        try:
            digest = _load_digest(run_dir)
        except FileNotFoundError as e:
            raise SystemExit(2) from e
        try:
            raw = continue_path.read_text()
        except FileNotFoundError as e:
            raise SystemExit(2) from e
        syn = validate_synthesis(json.loads(raw))
        # SP-E (#1385): assign deterministic BR-NNN ids + derive each rule's
        # ``enforced_at`` (file:line) from its ``step_ref``→endpoint mapping, riding
        # ALONGSIDE the harness-authored step_ref/rule/source/rationale. Empty when
        # there are no business rules, so a --no-synthesis run stays byte-identical.
        threaded_rules = thread_business_rules(syn.business_rules, digest.endpoints, syn.flows)
        report = FunctionalMapReport(
            digest=digest,
            reconciliation=syn.reconciliation,
            flows=syn.flows,
            journeys=syn.journeys,
            business_rules=threaded_rules,
            fr_statements=syn.fr_statements,
            acceptance_criteria=syn.acceptance_criteria,
            synthesized=True,
        )
        audit = audit_synthesis(digest, syn)
        md = render_markdown(report)
        code = 6 if audit.below_floor else 0
        slices, provenance = (
            _build_slices(report, system_map_path=system_map_path, contexts=contexts)
            if sliced_mode
            else ((), None)
        )
        return PipelineResult(
            code,
            md,
            render_json(report),
            run_dir,
            slices,
            sliced_requested=sliced_mode,
            slice_provenance=provenance,
        )

    # Phase A + B
    recon = run_recon(code_root, stack_flavor=stack_flavor, spec=spec)
    digest = extract(recon)
    _write_analysis(run_dir, digest)

    if no_synthesis or extract_only:
        if no_synthesis:
            report = FunctionalMapReport(
                digest=digest,
                reconciliation=RoleReconciliation(reconciled=digest.roles),
                synthesized=False,
            )
            slices, provenance = (
                _build_slices(report, system_map_path=system_map_path, contexts=contexts)
                if sliced_mode
                else ((), None)
            )
            return PipelineResult(
                0,
                render_markdown(report),
                render_json(report),
                run_dir,
                slices,
                sliced_requested=sliced_mode,
                slice_provenance=provenance,
            )

        # extract_only: write prompts and exit 7
        (run_dir / "synthesis_prompts.json").write_text(
            json.dumps(build_synthesis_prompts(digest), indent=2)
        )
        return PipelineResult(7, None, None, run_dir, sliced_requested=sliced_mode)

    # Default path (no flags) also writes prompts and exits 7 (synthesis round needed).
    # A sliced request that reaches here had no completed synthesis to auto-resume from
    # (#1406) — the CLI warns that this exit-7 emitted no slices.
    (run_dir / "synthesis_prompts.json").write_text(
        json.dumps(build_synthesis_prompts(digest), indent=2)
    )
    return PipelineResult(7, None, None, run_dir, sliced_requested=sliced_mode)
