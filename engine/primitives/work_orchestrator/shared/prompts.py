"""Build ``round-N-prompts.json`` for the current frontier node+stage (spec §3).

The control plane walks the tree to the next frontier node + stage needing model
work (:func:`stages.next_stage`), then emits **one** structured prompt for that
stage via the shared :mod:`primitives._shared.protocol` envelope. The prompt
carries **curated inputs only** — the node's spec path, the diff, the upstream
reports — and **never** this session's history. Each model turn is exactly one
compressed structured artifact; the control plane never narrates.

A Mode-2 stage's prompt carries a ``response_format`` JSON schema (loaded from
``schemas/<name>.schema.json``); a Mode-3 stage delegates to a downstream skill
and the subagent returns that skill's own structured report (free-form to this
layer, validated by the downstream skill).

Pure construction: no LLM, no clock. Schema files are read as plain dicts and
embedded in the prompt — this module never imports ``jsonschema`` (a dev-only
dependency; see ``_shared/modernization/schemas.py``).
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from primitives._shared.protocol import (
    PromptEnvelope,
    PromptItem,
    ResponseFormat,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from primitives.work_orchestrator.shared.stages import Stage
    from primitives.work_orchestrator.shared.tree import Node, WorkTree

_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

#: The primitive name carried in every envelope (matches the slash command).
PRIMITIVE = "work-orchestrator"


@cache
def load_schema(schema_id: str) -> dict[str, object]:
    """Load the committed JSON Schema whose ``$id`` is *schema_id*.

    Schemas live in ``schemas/`` and are named ``<short>.schema.json`` where
    ``<short>`` is the ``$id`` with the ``work-orchestrator-`` prefix stripped
    (e.g. ``work-orchestrator-spec-review`` -> ``spec-review.schema.json``).
    Returns the parsed schema dict for embedding in a ``ResponseFormat``.
    """
    short = schema_id.removeprefix(f"{PRIMITIVE}-")
    path = _SCHEMA_DIR / f"{short}.schema.json"
    if not path.is_file():
        msg = f"no schema file for id {schema_id!r} (expected {path!s})"
        raise FileNotFoundError(msg)
    schema: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return schema


# ---------------------------------------------------------------------------
# Curated stage inputs
# ---------------------------------------------------------------------------

#: System-prompt fragments per stage. Each tells a *fresh-context* subagent
#: exactly its one job, with no orchestration narrative.
_SYSTEM: Mapping[str, str] = {
    "spec": (
        "You are C1 /spec-write. Produce a C1-shaped spec for the work unit "
        "described below. Output only the spec."
    ),
    "spec-review": (
        "You are a fresh-context spec reviewer. Judge the spec below BEFORE any building: is it "
        "coherent, faithful to the issue intent, and neither over- nor under-scoped? Return a "
        "verdict (pass|revise|fail) and the issues you found, per the response schema."
    ),
    "plan": (
        "You are C2 /planner. Produce a TDD plan for the spec below AND a "
        "decomposition verdict: 'atomic' if this is one independent unit, "
        "'split' (with children[]) if it is more than one. Return the verdict "
        "per the response schema."
    ),
    "tests": (
        "You are S3 /test-from-spec. Write failing tests that drive the plan "
        "below. Output only the tests + their paths."
    ),
    "code-gen": (
        "You are C3 /code-gen. Implement the plan for this node (read the plan + "
        "the spec at the paths below), driving your OWN internal TDD loop to green "
        "on the build branch. Commit internally — NO git push. Return your "
        "structured per-task result."
    ),
    "review": (
        "You are the C7 review chain. Read the build diff (path below) and fan out "
        "to: C4 /code-review and S11 /security-review ALWAYS, plus — conditional on "
        "the changed-file types in the diff — S20 /cloud-arch-review (on IaC: "
        "*.tf / *.yaml CloudFormation / k8s manifests), S30 /frontend-design (on "
        "*.tsx / *.jsx), and S40 /api-contract-review (on openapi.* / *.proto). "
        "Each review skill fires its OWN §3.4 C10/C11 (failure-mode / "
        "production-failure audit) internally — do NOT invoke C10/C11 separately. "
        "Return the merged structured findings, and END your report with a single "
        "final line `Verdict: <pass|review|fail>` — `pass` = no blocking findings, "
        "`review` = non-blocking concerns only, `fail` = at least one blocking "
        "finding. The control plane folds that verdict into the node-gate (#1098)."
    ),
    "intent-review": (
        "You are a fresh-context, clean-room intent reviewer. Judge the diff "
        "below AGAINST the issue/spec intent ONLY (not code-style lenses): does "
        "it deliver exactly what was asked? Return a verdict (pass|review|fail) "
        "and findings, per the response schema."
    ),
    "spec-validate": (
        "You are C5 /spec-validate. Validate the diff below against the spec's "
        "acceptance criteria. Return the structured criterion matrix, and END your "
        "report with a single final line `Verdict: <pass|review|fail>` — `pass` = "
        "every acceptance criterion met, `review` = minor gaps only, `fail` = any "
        "criterion unmet. The control plane folds that verdict into the "
        "node-gate (#1098)."
    ),
    "node-gate": (
        "You are C6 /confidence-gate. The node-gate recommendation is computed "
        "DETERMINISTICALLY by the control plane (a fold of the leaf's review, "
        "intent-review, and spec-validate verdicts, shown below). Return ONLY a "
        "short `rationale` for that recommendation, grounded in those findings, "
        "per the response schema — the schema accepts `rationale` and nothing "
        "else (do NOT supply a `recommendation`; it is computed, not judged)."
    ),
}


#: Stages that need to read the build diff (the changes produced by code-gen):
#: review, intent-review, spec-validate, and the node-gate (#988).
_DIFF_CONSUMERS = frozenset({"review", "intent-review", "spec-validate", "node-gate"})


def _curated_user(tree: WorkTree, node: Node, stage: Stage) -> str:
    """Assemble the curated input block for *node* at *stage* (#988).

    Includes only what the stage needs — the node's title/spec-seed, its spec
    path (populated once the spec stage completes), a pointer to the build branch,
    the per-node report dir (where each upstream stage's report is written/read),
    and — for the diff-consuming stages (review / intent-review / spec-validate /
    node-gate) — the diff source so the subagent reads the change fresh. The
    orchestrator never inlines a diff/report *body* or session history — it keeps
    each prompt to compact identity + **paths/refs** so the fresh-context subagent
    reads upstream artifacts fresh (the token-discipline invariant, spec §3).
    """
    branch = f"foundry/{tree.root_slug}"
    report_dir = f".foundry/workflow-state/{tree.run_id}/nodes/{node.path}"
    lines = [
        f"Run: {tree.run_id}",
        f"Node: {node.path}",
        f"Build branch: {branch}",
        f"Title: {node.title}" if node.title else "",
        f"Spec-seed: {node.spec_seed}" if node.spec_seed else "",
        f"Spec path: .foundry/workflow-state/{tree.run_id}/{node.spec_path}"
        if node.spec_path
        else "",
        f"Reports dir: {report_dir} (read upstream stage reports here)",
        # Diff source for the stages that judge the produced change (#988): the
        # build branch's diff vs its merge base. The subagent reads it fresh.
        f"Diff: `git diff main...{branch}` (read the build diff fresh)"
        if stage.name in _DIFF_CONSUMERS
        else "",
        # For the node-gate, show the captured upstream verdicts the control plane
        # folds — so the model's rationale is grounded in the real inputs (#987).
        _node_gate_inputs_line(node) if stage.name == "node-gate" else "",
        f"Stage: {stage.name} (mode {stage.mode})",
    ]
    return "\n".join(line for line in lines if line)


def _node_gate_inputs_line(node: Node) -> str:
    """Render the captured review / intent-review / spec-validate verdicts (#987)."""
    parts = [
        f"{s}={node.stage_verdicts.get(s, 'incomplete')}"
        for s in ("review", "intent-review", "spec-validate")
    ]
    return "Computed node-gate inputs (the control plane folds these): " + ", ".join(parts)


def _response_format(stage: Stage) -> ResponseFormat:
    """Build the ``ResponseFormat`` for *stage*.

    Mode-2 stages with a ``schema_id`` constrain the response to that JSON
    schema. Mode-3 stages (and Mode-2 stages whose downstream skill owns its own
    structured output, e.g. spec/tests/spec-validate) use free-form ``text`` —
    the downstream skill validates its own shape.
    """
    if stage.schema_id is not None:
        return ResponseFormat(type="json", schema=load_schema(stage.schema_id))
    return ResponseFormat(type="text")


def build_prompt(tree: WorkTree, node: Node, stage: Stage, round_no: int) -> PromptEnvelope:
    """Build the single-item prompt envelope for *node* at *stage*.

    The item id is ``<node-path>:<stage-name>`` so the judgment that comes back
    is unambiguously routed to the right node+stage. Exactly one item per round
    (one frontier node, one stage) — the control plane's one-artifact-per-turn
    invariant (spec §3).
    """
    item = PromptItem(
        id=f"{node.path}:{stage.name}",
        system=_SYSTEM[stage.name],
        user=_curated_user(tree, node, stage),
        response_format=_response_format(stage),
    )
    return PromptEnvelope(primitive=PRIMITIVE, round=round_no, items=[item])


def parse_item_id(item_id: str) -> tuple[str, str]:
    """Split a prompt/judgment item id back into ``(node_path, stage_name)``.

    Inverse of :func:`build_prompt`'s id scheme. Raises ``ValueError`` on a
    malformed id so a crafted judgments file cannot smuggle an unroutable id.
    """
    node_path, sep, stage_name = item_id.partition(":")
    if not sep or not node_path or not stage_name:
        msg = f"malformed prompt item id {item_id!r}: expected '<node-path>:<stage-name>'"
        raise ValueError(msg)
    return node_path, stage_name


#: The item-id prefix for an opt-in root-level system review round (#989). The
#: full id is ``root-review:<review-name>``.
ROOT_REVIEW_PREFIX = "root-review"

#: Root-review name -> downstream skill (spec §3.2). The key order is the
#: canonical build-order (S70 → S10 → S60 → C8) — see :data:`ROOT_REVIEW_ORDER`.
_ROOT_REVIEW_SKILLS: Mapping[str, str] = {
    "S70 compliance-map": "/compliance-map",
    "S10 threat-model": "/threat-model",
    "S60 data-flow-map": "/data-flow-map",
    "C8 architecture-review": "/architecture-review",
}

#: Canonical build-order of the opt-in root reviews. The orchestrator orders the
#: batch round by this (#1007) so emission is deterministic and build-ordered
#: regardless of how ``tree.root_reviews`` round-tripped (the state envelope is
#: written with ``sort_keys=True``, which would otherwise alphabetize the keys).
ROOT_REVIEW_ORDER: tuple[str, ...] = tuple(_ROOT_REVIEW_SKILLS)


def order_root_reviews(names: list[str]) -> list[str]:
    """Return *names* sorted into the canonical build-order (#1007).

    Unknown names (defensive) sort last, stably, after the known reviews.
    """
    index = {name: i for i, name in enumerate(ROOT_REVIEW_ORDER)}
    return sorted(names, key=lambda n: index.get(n, len(index)))


def _root_review_item(tree: WorkTree, review_name: str) -> PromptItem:
    """Build the single prompt item for one opt-in root-level system review."""
    branch = f"foundry/{tree.root_slug}"
    skill = _ROOT_REVIEW_SKILLS.get(review_name, "")
    system = (
        f"You are the C7 root-level system review {review_name}"
        + (f" ({skill})" if skill else "")
        + ". Run it ONCE at the root against the WHOLE assembled change on the build "
        "branch (read the full diff fresh) — this is a system-shaped review, not a "
        "per-file lens. Return a verdict (clean | dirty) and a one-line summary, per "
        "the response schema."
    )
    user = "\n".join(
        [
            f"Run: {tree.run_id}",
            f"Build branch: {branch}",
            f"Diff: `git diff main...{branch}` (the whole assembled change)",
            f"Root review: {review_name}",
        ]
    )
    return PromptItem(
        id=f"{ROOT_REVIEW_PREFIX}:{review_name}",
        system=system,
        user=user,
        response_format=ResponseFormat(
            type="json", schema=load_schema("work-orchestrator-root-review")
        ),
    )


def build_root_review_prompt(tree: WorkTree, review_name: str, round_no: int) -> PromptEnvelope:
    """Build a one-item prompt for a single root-level system review (spec §3.2).

    A system-shaped review runs ONCE at the root against the assembled tree (the
    whole change on the build branch), not per-node. The item id is
    ``root-review:<review-name>`` so the judgment routes back to the right review.
    Prefer :func:`build_root_review_batch_prompt` to run all enabled reviews in one
    round (#1007); this single-review builder is retained for callers that emit one
    at a time.
    """
    return PromptEnvelope(
        primitive=PRIMITIVE, round=round_no, items=[_root_review_item(tree, review_name)]
    )


def build_root_review_batch_prompt(
    tree: WorkTree, review_names: list[str], round_no: int
) -> PromptEnvelope:
    """Build ONE prompt carrying every pending root-level system review (#1007).

    The enabled root reviews (S70 / S10 / S60 / C8) are mutually independent — none
    consumes another's output; they all read the same ``git diff main...<branch>``.
    So they batch into a single multi-item round (one ``root-review:<name>`` item
    each) instead of one orchestration round-trip apiece, trimming up to N-1 tail
    rounds. ``review_names`` is emitted in the given (build-order) order.
    """
    items = [_root_review_item(tree, name) for name in review_names]
    return PromptEnvelope(primitive=PRIMITIVE, round=round_no, items=items)
