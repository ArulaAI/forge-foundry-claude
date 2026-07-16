"""Feature-name → bounded-context candidate ranker for per-feature slice scoping (#1541).

The operator names a **human feature** — e.g. ``"Account Summary: Contribution History"`` —
which must resolve to the owning bounded context(s) so ``/mod-modernization-scope`` can
deconstruct just that slice (+ its dependency closure). Pure token-matching is too brittle
for real feature names (the business vocabulary rarely matches the code's identifiers), so
resolution is a **harness-synthesis** step (the extract → judge → continue pattern M5 and
the scope synthesizer already use):

    1. **This module (deterministic, Python)** — score every bounded context against the
       feature's tokens using the richest available signals (domain label, endpoint
       path+handler, FR statements, flows, app-digest entities), and emit the **ranked
       candidate contexts + their match evidence**. ``suggestion`` is the deterministic
       best guess; ``candidates`` is the ranked evidence the harness reasons over.
    2. **The harness (in-session judgment)** — reads the candidates + evidence and picks /
       confirms the owning context(s), applying business-vocabulary judgment the token
       matcher cannot (synonyms, renamed concepts, cross-context features).
    3. **Python (validate)** — ``validate_context_selection`` checks the harness's pick is a
       real context in the map; an invalid pick fails loud rather than modernizing nothing.

So Python earns its keep on the deterministic prep + validation; the model does the
semantic mapping. Imports only sibling ``_shared`` modules — QUAL-2 clean.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable

    from primitives._shared.modernization.models import SystemMap

#: Tokens with no discriminating power — dropped from a feature name before scoring so
#: "Account Summary: Contribution History" matches on account/summary/contribution/history.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "for",
        "to",
        "in",
        "on",
        "by",
        "with",
        "management",
        "manage",
        "view",
        "page",
        "screen",
        "feature",
        "module",
        "system",
        "service",
        "summary",
        "detail",
        "details",
        "info",
        "information",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: The deterministic ``suggestion`` is only offered when the top context's score clears
#: this floor (≥1 token hit). Below it, ``low_confidence`` is set and the harness judges
#: from the candidate evidence alone — the deterministic layer never silently guesses.
_SCORE_FLOOR = 1

#: A feature name often spans MORE than one context ("Account Summary: Contribution
#: History" touches both ``account`` and ``contribution``). So ``suggestion`` is not just
#: the single top context — it is every context scoring within this fraction of the top
#: score (and ≥ floor), so a genuinely cross-context feature is suggested in full, while a
#: clear single winner is suggested alone. The harness may still override.
_MATCH_BAND = 0.5


class ContextMatch(BaseModel):
    """One bounded context scored against the feature name, with its match evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context: str
    score: int
    evidence: list[str] = Field(default_factory=list)


class FeatureCandidates(BaseModel):
    """The deterministic candidate ranking for a feature name — the input to harness
    synthesis, NOT the final answer.

    ``suggestion`` is the deterministic best guess: the context(s) scoring within
    ``_MATCH_BAND`` of the top (so a cross-context feature suggests all it strongly names).
    The harness may accept it or override using the ``candidates`` (ranked contexts + match
    evidence) and its own business-vocabulary judgment. ``low_confidence`` is True when even
    the top context did not clear the floor — the harness must then decide from evidence
    alone (and may legitimately conclude the feature maps to no modernizable context).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    feature: str
    suggestion: list[str] = Field(default_factory=list)
    low_confidence: bool = False
    candidates: list[ContextMatch] = Field(default_factory=list)


def _tokens(text: str) -> set[str]:
    """Lowercase alphanumeric tokens of *text*, stopwords removed."""
    return {t for t in _TOKEN_RE.findall(text.lower()) if t and t not in _STOPWORDS}


def _context_signals(
    functional_map: dict[str, Any] | None,
    system_map: SystemMap,
    app_digest: dict[str, Any] | None,
) -> dict[str, list[str]]:
    """Build ``{context(domain): [matchable text signal, ...]}``.

    Signals per context: the domain label itself, its endpoints' ``path`` + ``handler``
    (grouped by the shared endpoint→context join), the FR statements whose ``domain``
    names it, its flow ids / view-state names, and — appended to every context — the
    app-digest entity names (they are not context-scoped in the digest, so they weakly
    boost any context whose feature tokens also name an entity)."""
    signals: dict[str, list[str]] = defaultdict(list)

    domains: list[str] = []
    for node in system_map.nodes:
        if node.kind in {"external", "shared_lib"}:
            continue
        for dom in node.domains:
            if dom and dom not in domains:
                domains.append(dom)
    for dom in domains:
        signals[dom].append(dom)  # the label is itself a signal

    fm = functional_map or {}
    # Endpoints → context via the shared join (contexts_from_system_map), then attach the
    # endpoint's path + handler text to the owning context.
    endpoints = [e for e in fm.get("endpoints", []) if isinstance(e, dict)]
    ep_by_iface = _endpoints_by_context(system_map, endpoints)
    for context, eps in ep_by_iface.items():
        for ep in eps:
            signals[context].append(str(ep.get("path") or ""))
            signals[context].append(str(ep.get("handler") or ""))

    # FR statements → context via their own ``domain`` field.
    for fr in fm.get("fr_statements", []) if isinstance(fm.get("fr_statements"), list) else []:
        if not isinstance(fr, dict):
            continue
        dom = str(fr.get("domain") or "").strip()
        if dom in signals:
            signals[dom].append(str(fr.get("statement") or ""))

    # Flows: ids + view-state names carry feature language (e.g. "contributionHistory").
    for flow in fm.get("webflow_flows", []) if isinstance(fm.get("webflow_flows"), list) else []:
        if not isinstance(flow, dict):
            continue
        # A flow is not directly context-tagged; attach its text to every context that
        # already has a signal (weak, but flows name features). Kept minimal + additive.
        flow_text = (
            str(flow.get("flow_id") or "")
            + " "
            + " ".join(str(v) for v in flow.get("view_states", []) if isinstance(v, str))
        )
        for context in list(signals):
            signals[context].append(flow_text)

    # app-digest entities: append to every context (not context-scoped in the digest).
    entities = _app_digest_entities(app_digest)
    if entities:
        for context in list(signals):
            signals[context].extend(entities)

    return dict(signals)


def _endpoints_by_context(
    system_map: SystemMap, endpoints: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Group endpoints by their owning context, reusing the shared endpoint→context join.

    Delegates to ``functional_map.shared.slicing.contexts_from_system_map`` (the one join)
    via a lazy import — that lives in a sibling ``_shared``-adjacent module; importing it
    here would cross a primitive boundary, so we re-derive the join locally from the same
    ``http_iface`` contract instead (kept in lockstep with #1516's reconciliation join)."""
    from primitives._shared.modernization.iface import http_iface  # noqa: PLC0415

    # {context: {iface strings}} from the system-map node domains + endpoint path tokens.
    domain_ifaces = _contexts_to_ifaces(system_map, endpoints, http_iface)
    iface_to_context: dict[str, str] = {}
    for context, ifaces in domain_ifaces.items():
        for iface in ifaces:
            iface_to_context.setdefault(iface, context)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ep in endpoints:
        iface = http_iface(str(ep.get("method") or "GET"), str(ep.get("path") or ""))
        owner = iface_to_context.get(iface)
        if owner is not None:
            grouped[owner].append(ep)
    return dict(grouped)


def _contexts_to_ifaces(
    system_map: SystemMap, endpoints: list[dict[str, Any]], http_iface: Any
) -> dict[str, set[str]]:
    """``{context: {endpoint iface}}`` — a business-domain token in the endpoint's
    path/handler assigns it to that domain; unmatched endpoints fall to a web-tier
    catch-all domain if one exists (mirrors ``contexts_from_system_map``, #1455/#1516)."""
    domains: list[str] = []
    for node in system_map.nodes:
        if node.kind in {"external", "shared_lib"}:
            continue
        for dom in node.domains:
            if dom and dom not in domains:
                domains.append(dom)
    web = next((d for d in domains if d.lower() in {"web", "webtier", "web-tier"}), None)
    out: dict[str, set[str]] = {d: set() for d in domains}
    for ep in endpoints:
        haystack = f"{ep.get('path') or ''} {ep.get('handler') or ''}".lower()
        iface = http_iface(str(ep.get("method") or "GET"), str(ep.get("path") or ""))
        matched = False
        for dom in domains:
            if dom is web:
                continue
            if dom.lower() in haystack:
                out[dom].add(iface)
                matched = True
                break
        if not matched and web is not None:
            out[web].add(iface)
    return out


def _app_digest_entities(app_digest: dict[str, Any] | None) -> list[str]:
    if not isinstance(app_digest, dict):
        return []
    out: list[str] = []
    for ent in (
        app_digest.get("entities", []) if isinstance(app_digest.get("entities"), list) else []
    ):
        if isinstance(ent, dict) and ent.get("name"):
            out.append(str(ent["name"]))
        elif isinstance(ent, str):
            out.append(ent)
    return out


def rank_feature_candidates(
    feature: str,
    *,
    functional_map: dict[str, Any] | None,
    system_map: SystemMap,
    app_digest: dict[str, Any] | None = None,
) -> FeatureCandidates:
    """Rank bounded contexts for *feature* — the deterministic input to harness synthesis.

    Scores each context by how many of the feature's tokens hit that context's signals
    (domain label / endpoint path+handler / FR statement / flow / entity), each hit
    recorded as evidence. Returns the ranked ``candidates`` + a deterministic ``suggestion``
    (contexts within ``_MATCH_BAND`` of the top). ``low_confidence`` is set when even the
    top context did not clear ``_SCORE_FLOOR`` — the harness then judges from evidence.
    This is NOT the final answer: the harness confirms/overrides the pick.
    """
    feat_tokens = _tokens(feature)
    signals = _context_signals(functional_map, system_map, app_digest)

    scored: list[ContextMatch] = []
    for context, texts in signals.items():
        hit_tokens: set[str] = set()
        evidence: list[str] = []
        for text in texts:
            common = feat_tokens & _tokens(text)
            if common:
                hit_tokens |= common
                snippet = text.strip()
                if snippet and snippet not in evidence:
                    evidence.append(snippet)
        if hit_tokens:
            scored.append(
                ContextMatch(
                    context=context, score=len(hit_tokens), evidence=sorted(set(evidence))[:5]
                )
            )

    scored.sort(key=lambda m: (-m.score, m.context))  # highest score first, stable by name
    if not scored or scored[0].score < _SCORE_FLOOR:
        return FeatureCandidates(
            feature=feature, low_confidence=True, suggestion=[], candidates=scored[:8]
        )
    top = scored[0].score
    # A cross-context feature suggests every context within _MATCH_BAND of the top (and ≥
    # floor): "Account Summary: Contribution History" → both account + contribution; a clear
    # single winner suggests only itself. The harness may still override.
    threshold = max(_SCORE_FLOOR, top * _MATCH_BAND)
    suggestion = sorted(m.context for m in scored if m.score >= threshold)
    return FeatureCandidates(feature=feature, suggestion=suggestion, candidates=scored[:8])


def validate_context_selection(
    selected: Iterable[str], system_map: SystemMap
) -> tuple[list[str], list[str]]:
    """Validate a harness's context selection against the real contexts in *system_map*.

    Returns ``(valid, unknown)``: contexts that exist as a domain label on some unit node
    vs those that do not. The caller keeps ``valid`` and fails loud on any ``unknown`` — a
    harness that names a context the map does not have must not silently modernize nothing.
    Case-insensitive (the shared normalization discipline), returning the map's canonical
    casing for the valid ones.
    """
    canonical: dict[str, str] = {}
    for node in system_map.nodes:
        if node.kind in {"external", "shared_lib"}:
            continue
        for dom in node.domains:
            if dom:
                canonical.setdefault(str(dom).strip().lower(), str(dom))
    valid: list[str] = []
    unknown: list[str] = []
    for sel in selected:
        key = str(sel).strip().lower()
        if key in canonical:
            if canonical[key] not in valid:
                valid.append(canonical[key])
        elif str(sel) not in unknown:
            unknown.append(str(sel))
    return sorted(valid), unknown


__all__ = [
    "ContextMatch",
    "FeatureCandidates",
    "rank_feature_candidates",
    "validate_context_selection",
]
