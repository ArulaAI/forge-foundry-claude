"""C7 per-node stage sequencing + the three execution modes (spec §3, §3.1).

A node advances through a fixed lifecycle; the control plane decides, for the
current frontier node, **which stage runs next** and **how** (one of three modes):

* **Mode 1 — Pure Python (zero model tokens):** ``issue``, ``branch``, the
  split-vs-build decision, gate computation, ``pr`` prep, roll-up. These never
  emit a prompt; the control plane executes them directly.
* **Mode 2 — Single structured judgment (one extract/judge/continue round):**
  ``spec`` (C1), ``spec-review``, ``plan`` (C2 + decomposition verdict),
  ``intent-review``, ``spec-validate`` (C5). One prompt -> one compact artifact.
* **Mode 3 — Delegated sub-pipeline (a fresh-context subagent runs a whole
  downstream skill and returns its structured report):** ``code-gen`` (C3 owns
  its internal TDD loop), the ``review`` chain (C4 + S11 + conditional
  S20/S30/S40, each firing its own §3.4 C10/C11).

This module owns only the **sequencing**: given a node's recorded
``completed_stages`` + its planner ``decomposition`` verdict, it names the next
stage. Prompt construction lives in :mod:`prompts`; judgment application +
state advance live in :mod:`judgments`; gate computation lives in ``gating``.

Pure and deterministic: no LLM, no clock, no filesystem.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from primitives.work_orchestrator.shared.tree import Node

#: Execution mode of a stage (spec §3.1).
StageMode = Literal[1, 2, 3]

#: The model-bearing stage names recorded in ``Node.completed_stages``. Mode-1
#: stages (issue/branch/pr/merge, gate computation) are not tracked here — they
#: are driven directly by the CLI control loop, not via a judgment round.
StageName = Literal[
    "spec",
    "spec-review",
    "plan",
    "tests",
    "code-gen",
    "review",
    "intent-review",
    "spec-validate",
    "node-gate",
]


class Stage(BaseModel):
    """Descriptor for one per-node stage (spec §3.1 table).

    ``schema_id`` is the ``$id`` of the JSON Schema the model's response must
    match for a Mode-2 stage (``None`` for Mode-3, whose subagent returns the
    downstream skill's own structured report, and for the thin Mode-1+2
    ``node-gate``). ``skill`` is the downstream foundry skill the stage invokes
    (``None`` for the two new clean-room judges, which have no standalone skill).
    """

    model_config = ConfigDict(frozen=True)

    name: StageName
    mode: StageMode
    skill: str | None = None
    schema_id: str | None = None


# ---------------------------------------------------------------------------
# The per-node lifecycle (spec §2.1, §3.1)
# ---------------------------------------------------------------------------

#: Stages every node runs before the split-vs-build decision (spec §2.1):
#: spec -> spec-review -> plan. The plan stage emits the decomposition verdict.
_PRE_BUILD: tuple[Stage, ...] = (
    Stage(name="spec", mode=2, skill="/spec-write", schema_id=None),
    Stage(name="spec-review", mode=2, skill=None, schema_id="work-orchestrator-spec-review"),
    Stage(name="plan", mode=2, skill="/planner", schema_id="work-orchestrator-plan-verdict"),
)

#: Stages a *leaf* (``atomic``) node runs after plan (spec §2.1, §3.1):
#: tests -> code-gen -> review -> intent-review -> spec-validate -> node-gate.
_LEAF_BUILD: tuple[Stage, ...] = (
    Stage(name="tests", mode=2, skill="/test-from-spec", schema_id=None),
    Stage(name="code-gen", mode=3, skill="/code-gen", schema_id=None),
    Stage(name="review", mode=3, skill="/code-review", schema_id=None),
    Stage(name="intent-review", mode=2, skill=None, schema_id="work-orchestrator-intent-review"),
    Stage(name="spec-validate", mode=2, skill="/spec-validate", schema_id=None),
    Stage(
        name="node-gate",
        mode=2,
        skill="/confidence-gate",
        schema_id="work-orchestrator-gate-verdict",
    ),
)

#: Lookup by stage name (every distinct stage appears once).
_BY_NAME: dict[str, Stage] = {s.name: s for s in (*_PRE_BUILD, *_LEAF_BUILD)}

#: The stage after which the split-vs-build decision is made. Once ``plan`` has a
#: verdict, an ``atomic`` node continues into ``_LEAF_BUILD``; a ``split`` node
#: spawns children and waits.
_DECISION_STAGE: StageName = "plan"

#: spec-review ``revise`` is bounded to ONE retry (spec §2.1): a second
#: consecutive non-``pass`` is treated as ``fail`` and halts the node.
MAX_SPEC_REVISE_RETRIES = 1


def stage_for(name: str) -> Stage:
    """Return the :class:`Stage` descriptor for *name*; ``KeyError`` if unknown."""
    return _BY_NAME[name]


def next_stage(node: Node) -> Stage | None:
    """Return the next stage needing a model round for *node*, or ``None``.

    Drives the per-node lifecycle deterministically from the node's recorded
    ``completed_stages`` and its planner ``decomposition`` verdict:

    * walk ``spec -> spec-review -> plan`` first;
    * after ``plan`` completes, branch on the verdict — a ``split`` node has no
      further per-node stages (its work is its children), so this returns
      ``None``; a ``leaf`` (``atomic``) node continues through
      ``tests -> code-gen -> review -> intent-review -> spec-validate -> node-gate``;
    * ``None`` once the node's last applicable stage is complete.

    A node whose ``decomposition`` is still unknown (plan not yet run) only ever
    advances through the pre-build stages — the split-vs-build decision is made
    when the plan judgment is applied (see :mod:`judgments`), not here.
    """
    done = set(node.completed_stages)

    for stage in _PRE_BUILD:
        if stage.name not in done:
            return stage

    # plan is complete — the verdict decides what (if anything) comes next.
    if node.decomposition is None:
        # Defensive: plan recorded complete but no verdict applied yet. The
        # judgment layer always sets decomposition when it applies the plan
        # round, so this only happens on a malformed resume — surface no stage.
        return None
    if node.is_split():
        # A split node's remaining work is its children; nothing per-node left.
        return None

    for stage in _LEAF_BUILD:
        if stage.name not in done:
            return stage
    return None


def is_decision_stage(stage: Stage) -> bool:
    """Return ``True`` for the stage whose judgment drives split-vs-build."""
    return stage.name == _DECISION_STAGE


def leaf_lifecycle() -> tuple[Stage, ...]:
    """Return the full leaf stage sequence (pre-build + leaf-build), in order."""
    return (*_PRE_BUILD, *_LEAF_BUILD)
