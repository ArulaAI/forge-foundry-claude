"""Validate + apply ``round-N-judgments.json``, advancing node/stage state (spec §3).

After the harness writes a judgment for the one prompted stage, the control
plane validates it against the stage's schema (dependency-free — this module
never imports ``jsonschema``), then **applies** it: records the stage complete,
drives the split-vs-build decision off the ``plan`` verdict, enforces the
``spec-review`` one-retry bound, and sets the node's terminal status when the
node-gate lands.

Pure application: no LLM, no clock. Filesystem writes are delegated to the
caller (the CLI persists via :mod:`state_store` after each apply).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from primitives.work_orchestrator.shared.prompts import ROOT_REVIEW_PREFIX, parse_item_id
from primitives.work_orchestrator.shared.stages import (
    MAX_SPEC_REVISE_RETRIES,
    next_stage,
    stage_for,
)
from primitives.work_orchestrator.shared.tree import frontier

if TYPE_CHECKING:
    from primitives._shared.protocol import JudgmentEnvelope
    from primitives.work_orchestrator.shared.tree import Node, WorkTree


class JudgmentError(ValueError):
    """A judgment failed structural validation or could not be applied."""


# ---------------------------------------------------------------------------
# Dependency-free structural validators (mirror the committed JSON schemas)
# ---------------------------------------------------------------------------

_SPEC_REVIEW_VERDICTS = frozenset({"pass", "revise", "fail"})
_INTENT_VERDICTS = frozenset({"pass", "review", "fail"})
_SEVERITIES = frozenset({"blocker", "warning", "info"})


def _require_obj(response: Any, stage_name: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        msg = f"{stage_name}: response must be a JSON object"
        raise JudgmentError(msg)
    return response


def _reject_unknown_keys(obj: dict[str, Any], allowed: frozenset[str], stage_name: str) -> None:
    """Enforce ``additionalProperties: false`` to match the committed schemas (#992).

    The dependency-free validators must encode the SAME rules as their JSON
    schemas, all of which set ``additionalProperties: false``. A smuggled extra
    key is not exploitable (the apply functions ignore unknown keys), but it
    breaks the schema↔validator fidelity discipline, so reject it here.
    """
    extra = set(obj) - allowed
    if extra:
        msg = (
            f"{stage_name}: unexpected key(s) {sorted(extra)!r} "
            "(schema is additionalProperties:false)"
        )
        raise JudgmentError(msg)


def _validate_issue_list(items: Any, key: str, stage_name: str) -> None:
    if not isinstance(items, list):
        msg = f"{stage_name}: {key!r} must be an array"
        raise JudgmentError(msg)
    for i, entry in enumerate(items):
        if not isinstance(entry, dict):
            msg = f"{stage_name}: {key}[{i}] must be an object"
            raise JudgmentError(msg)
        _reject_unknown_keys(
            entry, frozenset({"severity", "description"}), f"{stage_name}: {key}[{i}]"
        )
        sev = entry.get("severity")
        if sev not in _SEVERITIES:
            msg = f"{stage_name}: {key}[{i}].severity {sev!r} not in {sorted(_SEVERITIES)}"
            raise JudgmentError(msg)
        if not isinstance(entry.get("description"), str):
            msg = f"{stage_name}: {key}[{i}].description must be a string"
            raise JudgmentError(msg)


def validate_spec_review(response: Any) -> dict[str, Any]:
    """Validate a spec-review verdict; return it. Raises :class:`JudgmentError`."""
    obj = _require_obj(response, "spec-review")
    _reject_unknown_keys(obj, frozenset({"verdict", "issues"}), "spec-review")
    if obj.get("verdict") not in _SPEC_REVIEW_VERDICTS:
        msg = f"spec-review: verdict {obj.get('verdict')!r} not in {sorted(_SPEC_REVIEW_VERDICTS)}"
        raise JudgmentError(msg)
    _validate_issue_list(obj.get("issues"), "issues", "spec-review")
    return obj


def validate_plan_verdict(response: Any) -> dict[str, Any]:
    """Validate a plan decomposition verdict; return it. Raises :class:`JudgmentError`."""
    obj = _require_obj(response, "plan")
    _reject_unknown_keys(obj, frozenset({"decomposition", "children", "plan_path"}), "plan")
    decomposition = obj.get("decomposition")
    if decomposition not in {"atomic", "split"}:
        msg = f"plan: decomposition {decomposition!r} must be 'atomic' or 'split'"
        raise JudgmentError(msg)
    children = obj.get("children")
    if decomposition == "split":
        if not isinstance(children, list) or not children:
            msg = "plan: 'children' must be a non-empty array when decomposition is 'split'"
            raise JudgmentError(msg)
        for i, child in enumerate(children):
            if not isinstance(child, dict):
                msg = f"plan: children[{i}] must be an object"
                raise JudgmentError(msg)
            _reject_unknown_keys(
                child, frozenset({"title", "spec_seed", "rationale"}), f"plan: children[{i}]"
            )
            for field in ("title", "spec_seed", "rationale"):
                if not isinstance(child.get(field), str):
                    msg = f"plan: children[{i}].{field} must be a string"
                    raise JudgmentError(msg)
    elif isinstance(children, list) and children:
        msg = "plan: 'children' must be absent or empty when decomposition is 'atomic'"
        raise JudgmentError(msg)
    return obj


def validate_intent_review(response: Any) -> dict[str, Any]:
    """Validate an intent-review verdict; return it. Raises :class:`JudgmentError`."""
    obj = _require_obj(response, "intent-review")
    _reject_unknown_keys(obj, frozenset({"verdict", "findings"}), "intent-review")
    if obj.get("verdict") not in _INTENT_VERDICTS:
        msg = f"intent-review: verdict {obj.get('verdict')!r} not in {sorted(_INTENT_VERDICTS)}"
        raise JudgmentError(msg)
    _validate_issue_list(obj.get("findings"), "findings", "intent-review")
    return obj


def validate_gate_verdict(response: Any) -> dict[str, Any]:
    """Validate a node-gate rationale; return it. Raises :class:`JudgmentError`.

    The node-gate recommendation is computed deterministically by the control plane
    (#987), so the model contributes ONLY a ``rationale``. The ``recommendation``
    field was removed from the schema (#1009): a model-supplied ``recommendation``
    is now rejected as an unexpected key — the enum is no longer embedded in the
    node-gate prompt, saving the tokens it cost on every leaf.
    """
    obj = _require_obj(response, "node-gate")
    _reject_unknown_keys(obj, frozenset({"rationale"}), "node-gate")
    if not isinstance(obj.get("rationale"), str):
        msg = "node-gate: rationale must be a string"
        raise JudgmentError(msg)
    return obj


def validate_root_review(response: Any) -> dict[str, Any]:
    """Validate a root-level system-review verdict (#989); return it."""
    obj = _require_obj(response, "root-review")
    _reject_unknown_keys(obj, frozenset({"verdict", "summary"}), "root-review")
    if obj.get("verdict") not in {"clean", "dirty"}:
        msg = f"root-review: verdict {obj.get('verdict')!r} must be 'clean' or 'dirty'"
        raise JudgmentError(msg)
    if "summary" in obj and not isinstance(obj["summary"], str):
        msg = "root-review: summary must be a string"
        raise JudgmentError(msg)
    return obj


#: Stage name -> structural validator (only the schema-constrained Mode-2 stages).
_VALIDATORS = {
    "spec-review": validate_spec_review,
    "plan": validate_plan_verdict,
    "intent-review": validate_intent_review,
    "node-gate": validate_gate_verdict,
}


# ---------------------------------------------------------------------------
# Apply a single round's judgment
# ---------------------------------------------------------------------------


def _mark_done(node: Node, stage_name: str) -> None:
    if stage_name not in node.completed_stages:
        node.completed_stages = [*node.completed_stages, stage_name]


def _apply_spec_review(node: Node, verdict: dict[str, Any]) -> None:
    """Apply a spec-review verdict, enforcing the one-retry bound (spec §2.1).

    ``pass`` proceeds (spec-review marked done). ``fail`` halts the node. A first
    ``revise`` re-opens the ``spec`` stage for one bounded retry (spec-review is
    NOT marked done, and ``spec`` is removed from completed so it re-runs). A
    second consecutive non-``pass`` (tracked via ``spec_revise_count``) is
    treated as ``fail``.
    """
    decision = verdict["verdict"]
    if decision == "pass":
        _mark_done(node, "spec-review")
        return
    # revise or fail
    retries = node.spec_revise_count
    if decision == "fail" or retries >= MAX_SPEC_REVISE_RETRIES:
        node.status = "failed"
        _mark_done(node, "spec-review")
        return
    # First revise: bounce back to a bounded re-spec.
    node.spec_revise_count = retries + 1
    node.completed_stages = [s for s in node.completed_stages if s != "spec"]


def _apply_plan(tree: WorkTree, node: Node, verdict: dict[str, Any]) -> None:
    """Apply the plan decomposition verdict — the split-vs-build decision (spec §3.1)."""
    _mark_done(node, "plan")
    if verdict["decomposition"] == "split":
        children = [(c["title"], c["spec_seed"]) for c in verdict["children"]]
        tree.split(node.path, children)
    else:
        # atomic -> this node is a build leaf. The verdict's optional plan_path is
        # consumed downstream by the code-gen stage (resolved at the CLI layer);
        # the node model carries no separate plan-path field.
        tree.mark_atomic(node.path)


#: Stages whose verdict the node-gate folds. Each is captured onto
#: ``node.stage_verdicts`` when its round is applied (#987).
_NODE_GATE_INPUTS: tuple[str, ...] = ("review", "intent-review", "spec-validate")


def _capture_stage_verdict(node: Node, stage_name: str, verdict: str) -> None:
    """Record an upstream stage's verdict for the deterministic node-gate (#987)."""
    node.stage_verdicts = {**node.stage_verdicts, stage_name: verdict}


def _apply_node_gate(node: Node, verdict: dict[str, Any]) -> None:
    """Apply the node-gate — DETERMINISTICALLY fold the upstream verdicts (#987).

    Spec §3.1: the node-gate recommendation is the deterministic fold (Python) of
    the leaf's ``review`` + ``intent-review`` + ``spec-validate`` verdicts; **only
    C6's short rationale is a model judgment**. The model's ``recommendation`` in
    the response is therefore IGNORED — the control plane computes it from the
    captured ``node.stage_verdicts`` via :func:`gating.node_gate`. The model's
    ``rationale`` is retained as the human-facing explanation.
    """
    from primitives.work_orchestrator.shared.gating import (  # noqa: PLC0415
        GateRecommendation,
        node_gate,
    )

    def _v(stage: str) -> GateRecommendation:
        # An un-produced upstream verdict contributes `incomplete` (the fold then
        # never reads as a clean pass unless every input genuinely passed).
        raw = node.stage_verdicts.get(stage, "incomplete")
        return raw if raw in ("pass", "review", "fail", "incomplete") else "incomplete"  # type: ignore[return-value]

    rec = node_gate(
        review=_v("review"),
        intent_review=_v("intent-review"),
        spec_validate=_v("spec-validate"),
    )
    node.gate_verdict = rec
    node.status = "clean" if rec == "pass" else "not-clean"
    # Retain the model's rationale (the one model judgment the node-gate consumes)
    # under a reserved key so the report can surface it; the recommendation itself
    # is the Python-computed `rec`, not anything the model supplied.
    rationale = verdict.get("rationale")
    if isinstance(rationale, str) and rationale:
        node.stage_verdicts = {**node.stage_verdicts, "node-gate-rationale": rationale}
    _mark_done(node, "node-gate")


def _apply_root_review(tree: WorkTree, item_id: str, response: Any) -> Node:
    """Apply a root-level system-review verdict (#989); return the root node.

    The id is ``root-review:<name>``. The captured ``clean``/``dirty`` verdict is
    stored on ``tree.root_reviews`` so the final tree-gate folds the REAL outcome
    (not an auto-pass placeholder). Rejects a review name that was not enabled, or
    one already run (replay guard).
    """
    name = item_id[len(ROOT_REVIEW_PREFIX) + 1 :]
    if name not in tree.root_reviews:
        msg = f"root-review judgment for {name!r} which is not an enabled root review"
        raise JudgmentError(msg)
    if tree.root_reviews[name] is not None:
        msg = f"stale root-review judgment for {name!r}: it has already run"
        raise JudgmentError(msg)
    verdict = validate_root_review(response)
    tree.root_reviews = {**tree.root_reviews, name: verdict["verdict"]}
    return tree.root()


def apply_round(tree: WorkTree, judgments: JudgmentEnvelope) -> Node:
    """Validate + apply one round's judgment; return the touched (or root) node.

    The per-node lifecycle emits exactly one prompt item per round, so a well-formed
    node-lifecycle envelope carries exactly one item whose id is
    ``<node-path>:<stage-name>``. The one exception is the root-level system-review
    **batch** (#1007): every enabled review is emitted together in one round (they
    are independent), so an envelope whose items are *all* ``root-review:<name>`` is
    applied as a batch and returns the root. This validates each response against
    its schema and advances state. Raises :class:`JudgmentError` on a malformed
    envelope, an unknown node/stage, a response that fails structural validation, or
    a **stale/replayed** judgment whose node+stage is not the one currently expected
    (#990).
    """
    if not judgments.items:
        msg = "expected at least one judgment item per round, got 0"
        raise JudgmentError(msg)

    # Root-level system review BATCH (spec §3.2, #989/#1007): every item id is
    # ``root-review:<name>``. Apply each and return the root. This is the ONLY
    # multi-item round; the per-node lifecycle stays strictly one item per round.
    root_prefix = f"{ROOT_REVIEW_PREFIX}:"
    if all(it.id.startswith(root_prefix) for it in judgments.items):
        for it in judgments.items:
            _apply_root_review(tree, it.id, it.response)
        return tree.root()

    if len(judgments.items) != 1:
        msg = f"expected exactly one judgment item per round, got {len(judgments.items)}"
        raise JudgmentError(msg)
    item = judgments.items[0]

    try:
        node_path, stage_name = parse_item_id(item.id)
    except ValueError as exc:
        raise JudgmentError(str(exc)) from exc

    if node_path not in tree.nodes:
        msg = f"judgment references unknown node {node_path!r}"
        raise JudgmentError(msg)
    try:
        stage_for(stage_name)
    except KeyError as exc:
        msg = f"judgment references unknown stage {stage_name!r}"
        raise JudgmentError(msg) from exc

    node = tree.nodes[node_path]

    # Replay guard (#990): reject a stale/replayed judgment. The judgment must be
    # for the node+stage the control plane currently expects — i.e. *this* node is
    # the frontier and *this* stage is its next stage. Without this, re-submitting
    # an old `spec-review: revise` would re-increment spec_revise_count and re-strip
    # `spec`, and re-submitting any completed stage would re-run its apply.
    expected_node = frontier(tree)
    if expected_node is None or expected_node.path != node_path:
        msg = (
            f"stale judgment for {node_path!r}: the control plane is not awaiting a "
            f"judgment for that node (frontier is "
            f"{expected_node.path if expected_node else None!r})"
        )
        if expected_node is None:
            # The tree has already settled — there is nothing left to judge. A
            # user reaching here is usually trying to (re)open/push the PR on a
            # finished run; point them at the resume path rather than leaving the
            # bare "frontier is None" (#1194 3b).
            msg += (
                ". The run has settled; re-invoke WITHOUT --continue (optionally "
                "with --force-pr / --push) to re-run the finalize + PR stage."
            )
        raise JudgmentError(msg)
    expected_stage = next_stage(node)
    if expected_stage is None or expected_stage.name != stage_name:
        msg = (
            f"stale judgment for stage {stage_name!r} on {node_path!r}: the node's "
            f"next stage is {expected_stage.name if expected_stage else None!r}"
        )
        raise JudgmentError(msg)

    validator = _VALIDATORS.get(stage_name)
    if validator is not None:
        verdict = validator(item.response)
        if stage_name == "spec-review":
            _apply_spec_review(node, verdict)
        elif stage_name == "plan":
            _apply_plan(tree, node, verdict)
        elif stage_name == "node-gate":
            _apply_node_gate(node, verdict)
        else:  # intent-review: capture its verdict for the deterministic node-gate.
            _capture_stage_verdict(node, "intent-review", str(verdict.get("verdict", "incomplete")))
            _mark_done(node, stage_name)
    else:
        # Free-form (Mode-2 spec/tests/spec-validate, Mode-3 code-gen/review):
        # the downstream skill owns its shape; the control plane just records it.
        if stage_name == "spec":
            # Record the conventional per-node spec path so downstream stages
            # (plan/tests/intent-review/spec-validate) can reference it (#988).
            node.spec_path = _node_spec_path(node.path)
        elif stage_name in _NODE_GATE_INPUTS:
            # review / spec-validate: capture the response's top-level `verdict`
            # (pass|review|fail) so the node-gate can fold it deterministically
            # (#987). A response without a recognized verdict contributes
            # `incomplete` (read by _apply_node_gate's default).
            _capture_stage_verdict(node, stage_name, _extract_verdict(item.response))
        _mark_done(node, stage_name)

    return node


#: Matches a labelled verdict line in a free-form text report, e.g.
#: ``Verdict: pass`` / ``**Verdict:** FAIL`` / ``verdict = review`` (#1098). Only a
#: *labelled* verdict counts — arbitrary prose mentioning "pass"/"fail" does not.
_TEXT_VERDICT = re.compile(r"verdict\s*[:=]\s*\*{0,2}\s*(pass|review|fail)\b", re.IGNORECASE)


def _extract_verdict(response: Any) -> str:
    """Pull a ``pass|review|fail`` verdict out of a free-form stage response (#987).

    The ``review`` and ``spec-validate`` stages return a downstream skill's
    structured report. They are emitted with ``response_format.type: "text"``, so
    the subagent's report is a **string**, not a dict — their prompts therefore
    require a final ``Verdict: <pass|review|fail>`` line (#1098). The control plane
    reads that verdict (from a dict's top-level ``verdict`` key OR a text report's
    labelled verdict line) to fold into the node-gate. Anything unrecognized →
    ``incomplete`` (never silently pass an unparseable report).
    """
    if isinstance(response, dict):
        v = response.get("verdict")
        if isinstance(v, str) and v.lower() in ("pass", "review", "fail"):
            return v.lower()
        return "incomplete"
    if isinstance(response, str):
        match = _TEXT_VERDICT.search(response)
        if match:
            return match.group(1).lower()
    return "incomplete"


def _node_spec_path(node_path: str) -> str:
    """Conventional per-node spec path under the run's node dir (#988).

    The spec stage's subagent writes the C1 spec here; the path is referenced by
    later stages so they read the spec fresh (never inlined). Relative to the run
    dir: ``nodes/<node-path>/spec.md``.
    """
    return f"nodes/{node_path}/spec.md"
