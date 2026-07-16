"""High-Level Architecture (HLA) grounding models + conformance render (Track A, #1428).

The operator can drop a stated architecture into ``.foundry/hla/`` (``*.md`` read
directly; ``*.docx`` text extracted *in-session* by the orchestrator SKILL body —
NO Python dependency, honoring the offline invariant). The orchestrator collects
those docs into an :class:`HlaSource` (raw text + provenance captured
deterministically; the *interpretation* of what the HLA asserts is harness-authored
during synthesis, since the HLA is free-form human prose) and persists it as
``hla-source.json`` under the run-id state dir.

The payoff is the **Architecture Conformance** section: for each stated HLA element,
a verdict against the discovered system-map — **confirmed / partial / contradicted /
not-found** — each with evidence (HLA says X → system-map shows Y). The verdicts this
module renders are the ones the system-map can **structurally** confirm (name /
interface matching, reusing the ``context_resolver`` matching discipline); the
interpretive verdicts are harness-authored via a synthesis directive. No HLA → the
section is omitted (no fabrication, backward-compatible).

Design: ``docs/superpowers/specs/2026-07-10-hla-entrypoints-brd-design.md`` §Track A.

Pure and deterministic: no LLM, no clock, no filesystem.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from primitives._shared.modernization.context_resolver import (
    build_member_index,
    resolve_context,
)
from primitives._shared.modernization.render import heading, md_table

if TYPE_CHECKING:
    from primitives._shared.modernization.models import SystemMap


class HlaDocument(BaseModel):
    """One stated-architecture document the operator dropped in ``.foundry/hla/``.

    Every field defaults empty so the model is inert when no HLA is supplied. The
    Python side captures ``source_path`` + raw ``text`` deterministically (``.md``
    read directly; ``.docx`` text extracted in-session by the SKILL body); the
    stated-element lists (``stated_components`` / ``boundaries`` / ``integrations`` /
    ``constraints`` / ``nfrs``) are harness-summarized during synthesis from the raw
    prose.
    """

    model_config = ConfigDict(extra="forbid")

    source_path: str = ""
    text: str = ""
    stated_components: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    nfrs: list[str] = Field(default_factory=list)


class HlaSource(BaseModel):
    """The collected HLA documents for a run.

    Empty (``documents == []``) means no HLA was supplied — the chain falls back to
    its byte-identical descriptive behavior (the Architecture Conformance section is
    omitted).
    """

    model_config = ConfigDict(extra="forbid")

    documents: list[HlaDocument] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Architecture Conformance render
# ---------------------------------------------------------------------------

#: The verdict vocabulary. ``confirmed`` / ``not-found`` are deterministically
#: assignable from the system-map here; ``partial`` / ``contradicted`` are the
#: interpretive verdicts a harness authors against this deterministic baseline.
_CONFIRMED = "confirmed"
_NOT_FOUND = "not-found"

#: Structural-kind synonyms. A discovery pass records a node's technology ``kind`` in
#: its own vocabulary (``postgres``), while a stated HLA names it in the human form
#: (``PostgreSQL``). Each canonical kind maps to the extra normalized aliases under
#: which its node should ALSO be indexed, so either spelling resolves. Kept small and
#: high-confidence — only well-known 1:1 technology equivalences.
_KIND_ALIASES: dict[str, tuple[str, ...]] = {
    "postgres": ("postgresql",),
    "postgresql": ("postgres",),
    "mssql": ("sqlserver", "sql-server", "microsoftsqlserver"),
    "mysql": ("mariadb",),
    "mongodb": ("mongo",),
}


def _system_map_member_keys(system_map: SystemMap) -> dict[str, str]:
    """Invert the system-map into ``normalized-member-key → representative node id``.

    Every structural handle a stated HLA element could match on is a key: each node
    id, each node **kind** (e.g. ``postgres`` on ``.#db`` — plus its well-known aliases
    like ``postgresql``), each domain label, and each edge's ``kind`` / ``evidence`` /
    ``dst`` coordinate. Reuses the ``context_resolver`` normalization discipline
    (lowercased + stripped) so the HLA↔system-map join can never diverge from the
    endpoint↔context join.
    """
    # ``build_member_index`` inverts ``context → members`` into ``member → context``,
    # where the *members* (list values) become the lookup keys resolving to the *context*
    # (dict key) as evidence. So to make a node's kind an additional lookup key that
    # resolves TO that node, kind/alias are appended as members under the node-id context
    # (alongside its id and domains) — the #1514 fix. The edge-coordinate indexing is kept
    # exactly as-is (unchanged blast radius): ``edge.dst`` remains the key.
    contexts: dict[str, list[str]] = {}
    for node in system_map.nodes:
        handles = [node.id]
        if node.kind:
            kind_key = str(node.kind).strip().lower()
            handles.append(kind_key)
            handles.extend(_KIND_ALIASES.get(kind_key, ()))
        handles.extend(str(domain) for domain in node.domains)
        contexts.setdefault(node.id, []).extend(handles)
    for edge in system_map.edges:
        for coord in (edge.kind, edge.evidence, edge.dst):
            if coord:
                contexts.setdefault(str(coord), []).append(edge.dst)
    return build_member_index(contexts)


def _verdict_row(stated: str, kind_label: str, member_index: dict[str, str]) -> list[str]:
    """One conformance row: stated element → verdict + evidence.

    Deterministic: a stated element whose normalized name (or any of its whitespace-
    split tokens, so "Owner Service" matches an ``owner-service`` node) hits the
    system-map member index is ``confirmed`` with the matched node as evidence;
    otherwise ``not-found``. The interpretive ``partial`` / ``contradicted`` verdicts
    are layered on by a harness against this baseline — never fabricated here.
    """
    # Try the full stated name first, then a slugified form, then per-token, reusing
    # resolve_context's single-key normalization.
    candidates = [stated, stated.replace(" ", "-"), stated.replace(" ", "")]
    candidates.extend(tok for tok in stated.split() if tok)
    matched = resolve_context(candidates, member_index, single_context=None)
    if matched is not None:
        return [stated, kind_label, _CONFIRMED, f"system-map node `{matched}`"]
    return [stated, kind_label, _NOT_FOUND, "no matching system-map node/interface"]


def render_architecture_conformance(hla: HlaSource | None, system_map: SystemMap) -> str:
    """Render the Architecture Conformance section from an HLA source + system-map.

    For each stated HLA element (components, boundaries, integrations across every
    document), a conformance verdict against the discovered *system_map*: the
    structurally-confirmable ones (``confirmed`` / ``not-found``) are assigned
    deterministically here; the interpretive verdicts (``partial`` /
    ``contradicted``) are harness-authored via a synthesis directive against this
    baseline.

    Returns the Markdown section, or ``""`` when there is no HLA (``None`` or no
    documents) — the section is omitted entirely (no fabrication, backward-compatible).
    """
    if hla is None or not hla.documents:
        return ""

    member_index = _system_map_member_keys(system_map)
    rows: list[list[str]] = []
    seen: set[tuple[str, str]] = set()
    for doc in hla.documents:
        for stated in doc.stated_components:
            key = ("component", stated.strip().lower())
            if stated.strip() and key not in seen:
                seen.add(key)
                rows.append(_verdict_row(stated, "component", member_index))
        for stated in doc.boundaries:
            key = ("boundary", stated.strip().lower())
            if stated.strip() and key not in seen:
                seen.add(key)
                rows.append(_verdict_row(stated, "boundary", member_index))
        for stated in doc.integrations:
            key = ("integration", stated.strip().lower())
            if stated.strip() and key not in seen:
                seen.add(key)
                rows.append(_verdict_row(stated, "integration", member_index))

    if not rows:
        return ""

    lines: list[str] = [
        "",
        heading("Architecture Conformance", level=2),
        "",
        (
            "Each stated High-Level Architecture element reconciled against the "
            "discovered system-map. `confirmed` / `not-found` are structurally derived; "
            "`partial` / `contradicted` are interpretive verdicts a reviewer refines "
            "against this baseline."
        ),
        "",
        md_table(["Stated element", "Kind", "Verdict", "Evidence"], rows),
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "HlaDocument",
    "HlaSource",
    "render_architecture_conformance",
]
