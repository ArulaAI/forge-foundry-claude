"""verify.py — the strict green-gate verify loop (S2 TM-5, #897).

This is the **second gate** of the S2 closed loop (design spec
``docs/superpowers/specs/2026-06-19-test-maintainer-closed-loop-design.md``
§6).  The C10/C11 audit gate (``audit_gate.py``) runs *first*; the proposals
that survive it arrive here.  For each surviving proposal this engine:

1. Writes the proposed edit to the **test file** (reusing ``apply.py``'s
   non-test-path guard — writing a non-test path is a hard error).
2. Runs the **impacted test subset** via ``adapter.run_tests`` (not just the
   one edited test — so an edit that fixes its target but breaks a sibling in
   the same subset is caught).
3. **Green-gate:** green → keep.  Red → if a ``repropose`` callable is wired
   and we are under ``max_refine`` attempts, ask it for a new edit (re-judge
   with the failure text as context), re-write, and re-run.  Still red after
   ``max_refine`` → **revert the edit to its original on-disk content** and
   record ``{test_id, reason, last_failure}`` in the ``needs_human`` list.

The refine step ("re-judge with the failure as context") is an **in-session
model action** — the Python CLI never calls an LLM.  So the loop is a
deterministic Python orchestration with the re-propose step as an **injected
callable** (the ``repropose`` seam):

- In production, ``repropose`` is supplied by the SKILL.md body's re-judge
  (the ``--extract-only`` / ``--continue`` round driven in-session); the real
  multi-round wiring is the SKILL's job (TM-10).
- In tests, a fake ``repropose`` is injected — one that fixes the edit, or one
  that never does — so the green-gate is fully deterministic and headless.

``repropose`` has the signature ``(proposal, last_failure) -> EditProposal |
None``: given the proposal whose edit just failed and the captured failure
text, return a *new* proposal (typically the same ``test_file`` with refined
``after_content``) to retry, or ``None`` to give up immediately.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from primitives.test_maintainer.shared.apply import _is_test_path
from primitives.test_maintainer.shared.edit_proposal import Verdict

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._sdet.stacks.contract import RunResult, TestStackAdapter, TestSubset
    from primitives.test_maintainer.shared.edit_proposal import EditProposal

#: Default cap on bounded refine attempts (design spec §6 — keeps the loop
#: from running away / costing unboundedly).
DEFAULT_MAX_REFINE = 2


class ReproposeFn(Protocol):
    """The injected re-judge seam.

    Given the *proposal* whose edit just verified red and the captured
    *last_failure* text, return a refined :class:`EditProposal` to retry, or
    ``None`` to stop refining immediately.  In production this is the SKILL.md
    body's in-session re-judge; in tests it is a fake.
    """

    def __call__(
        self, proposal: EditProposal, last_failure: str
    ) -> EditProposal | None: ...  # pragma: no cover - protocol stub


@dataclass(frozen=True)
class NeedsHuman:
    """One proposal that could not be made green and was reverted."""

    test_id: str
    reason: str
    last_failure: str


@dataclass(frozen=True)
class EditResult:
    """The verify outcome for a single proposal."""

    test_id: str
    #: ``"kept"`` (verified green, edit left on disk) or ``"reverted"``
    #: (still red after ``max_refine`` — edit rolled back to its original).
    disposition: str
    #: Number of ``run_tests`` attempts made (1 initial + up to ``max_refine``
    #: refines).
    attempts: int
    #: ``"passed"`` when kept; ``"failed"`` when reverted.
    final_status: str
    #: Captured failure text from the last red run (empty when kept).
    failure_text: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "test_id": self.test_id,
            "disposition": self.disposition,
            "attempts": self.attempts,
            "final_status": self.final_status,
            "failure_text": self.failure_text,
        }


@dataclass(frozen=True)
class VerifyOutcome:
    """The full result of a verify-loop run over a batch of proposals."""

    kept: list[EditProposal] = field(default_factory=list)
    results: list[EditResult] = field(default_factory=list)
    needs_human: list[NeedsHuman] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "kept": [str(p.test_file) for p in self.kept],
            "edits": [r.to_dict() for r in self.results],
            "needs_human": [
                {
                    "test_id": nh.test_id,
                    "reason": nh.reason,
                    "last_failure": nh.last_failure,
                }
                for nh in self.needs_human
            ],
        }


def _resolve_target(project_root: Path, test_file: Path) -> Path:
    return test_file if test_file.is_absolute() else project_root / test_file


def _read_original(target: Path) -> str | None:
    """Return the current on-disk content of *target*, or ``None`` if absent.

    ``None`` distinguishes "the test file does not exist yet" (an ADD proposal)
    from "the file exists and is empty" — revert restores the former by
    deleting and the latter by re-writing the original bytes.
    """
    if target.is_file():
        return target.read_text(encoding="utf-8")
    return None


def _write_edit(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _revert(target: Path, original: str | None) -> None:
    """Restore *target* to its pre-edit state.

    If the file did not exist before the edit (``original is None``), delete
    it; otherwise re-write the exact original bytes.
    """
    if original is None:
        if target.is_file():
            target.unlink()
        return
    target.write_text(original, encoding="utf-8")


def _impacted_subset(
    proposal: EditProposal,
    subset_for: dict[str, TestSubset] | None,
    default_subset: TestSubset,
) -> TestSubset:
    """Pick the impacted subset to run for *proposal*.

    A per-test mapping (keyed by the proposal's ``test_file`` string) takes
    precedence; otherwise the shared *default_subset* is used.  Running the
    whole impacted subset (not just the one edited test) is the regression
    guard (design spec §6): an edit that fixes its target but breaks a sibling
    is detected because the sibling is in the subset.
    """
    if subset_for is not None:
        keyed = subset_for.get(str(proposal.test_file))
        if keyed is not None:
            return keyed
    return default_subset


def _run_is_green(run: RunResult) -> bool:
    """Green iff at least one test ran and none failed/errored.

    A run that collected no tests is *not* green — an edit must positively
    demonstrate the impacted subset passes, never vacuously.
    """
    if run.no_tests_collected:
        return False
    return run.passed


def _failure_text(run: RunResult) -> str:
    """Concatenate the failure text of every failed/errored result in *run*."""
    if run.no_tests_collected:
        return "no tests collected for the impacted subset"
    parts = [f"{r.test_id}: {r.failure_text}".rstrip() for r in run.failures()]
    return "\n".join(parts) if parts else "impacted subset did not pass"


def verify_edits(
    adapter: TestStackAdapter,
    root: Path,
    proposals: list[EditProposal],
    *,
    max_refine: int = DEFAULT_MAX_REFINE,
    repropose: ReproposeFn | None = None,
    subset: TestSubset | None = None,
    subset_for: dict[str, TestSubset] | None = None,
) -> VerifyOutcome:
    """Run the strict green-gate verify loop over *proposals*.

    For each proposal (in input order): write the edit to its **test file**
    (non-test paths hard-error), run the impacted subset via
    ``adapter.run_tests``, and apply the green-gate:

    - **green** → keep the edit (left on disk), record ``kept``.
    - **red** and ``repropose`` given and attempts < ``max_refine`` → call
      ``repropose(proposal, last_failure)`` for a refined edit, re-write,
      re-run.
    - still **red** after ``max_refine`` (or ``repropose`` returns ``None`` /
      is absent) → **revert** the edit to its original on-disk content and add
      a :class:`NeedsHuman` entry.

    Args:
        adapter: The stack adapter (its ``run_tests`` executes the subset).
        root: Project root; relative ``test_file`` paths resolve under it.
        proposals: The C10/C11-surviving proposals to verify.
        max_refine: Max bounded refine attempts per proposal (default
            :data:`DEFAULT_MAX_REFINE`).  ``0`` disables refining.
        repropose: The injected in-session re-judge seam (see module
            docstring).  ``None`` → no refining; the first red reverts.
        subset: The impacted test subset to run for every proposal (the
            regression guard runs the whole subset, not just the edited test).
        subset_for: Optional per-test subset override, keyed by the proposal's
            ``test_file`` string.  Falls back to *subset* when a key is absent.

    Returns:
        A :class:`VerifyOutcome` with the kept proposals, per-edit results,
        and the needs-human list.

    Raises:
        ValueError: if any proposal targets a non-test path (the same hard
            rule ``apply.py`` enforces).
    """
    # Hard-rule check FIRST — fail loud before any I/O, mirroring apply.py.
    for p in proposals:
        if not _is_test_path(p.test_file):
            raise ValueError(
                f"Refusing to write non-test path: {p.test_file}. "
                "test_maintainer verify is hard-restricted to test files only."
            )

    from primitives._sdet.stacks.contract import TestSubset as _TestSubset  # noqa: PLC0415

    default_subset = subset if subset is not None else _TestSubset(frozenset())

    outcome = VerifyOutcome()
    for proposal in proposals:
        result = _verify_one(
            adapter,
            root,
            proposal,
            max_refine=max_refine,
            repropose=repropose,
            default_subset=default_subset,
            subset_for=subset_for,
        )
        outcome.results.append(result)
        if result.disposition == "kept":
            outcome.kept.append(proposal)
        else:
            outcome.needs_human.append(
                NeedsHuman(
                    test_id=result.test_id,
                    reason=f"unfixable after {result.attempts} attempt(s)",
                    last_failure=result.failure_text,
                )
            )
    return outcome


def _verify_one(
    adapter: TestStackAdapter,
    root: Path,
    proposal: EditProposal,
    *,
    max_refine: int,
    repropose: ReproposeFn | None,
    default_subset: TestSubset,
    subset_for: dict[str, TestSubset] | None,
) -> EditResult:
    """Verify a single proposal; revert + report on failure."""
    target = _resolve_target(root, proposal.test_file)
    test_id = str(proposal.test_file)
    # Snapshot the ORIGINAL on-disk content ONCE, before any write, so revert
    # restores the true pre-edit state regardless of how many refines run.
    original = _read_original(target)

    current = proposal
    last_failure = ""
    attempts = 0
    # 1 initial attempt + up to max_refine refine attempts.
    for attempt in range(max_refine + 1):
        _apply_one_edit(target, current)
        run = adapter.run_tests(root, _impacted_subset(current, subset_for, default_subset))
        attempts = attempt + 1
        if _run_is_green(run):
            return EditResult(
                test_id=test_id,
                disposition="kept",
                attempts=attempts,
                final_status="passed",
            )
        last_failure = _failure_text(run)
        # Red. Try to refine if we have budget + a seam.
        if attempt >= max_refine or repropose is None:
            break
        refined = repropose(current, last_failure)
        if refined is None:
            break
        current = refined

    # Still red after the bounded attempts → revert to original, report.
    _revert(target, original)
    return EditResult(
        test_id=test_id,
        disposition="reverted",
        attempts=attempts,
        final_status="failed",
        failure_text=last_failure,
    )


def _apply_one_edit(target: Path, proposal: EditProposal) -> None:
    """Materialise *proposal* on disk (write content, or delete for DELETE)."""
    if proposal.verdict == Verdict.DELETE:
        if target.is_file():
            target.unlink()
        return
    _write_edit(target, proposal.after_content)


def write_verify_results(run_dir: Path, outcome: VerifyOutcome) -> Path:
    """Emit ``verify-results.json`` under *run_dir*; return the path.

    Shape::

        {
          "kept": ["tests/test_x.py", ...],
          "edits": [
            {"test_id": "...", "disposition": "kept|reverted",
             "attempts": N, "final_status": "passed|failed",
             "failure_text": "..."},
            ...
          ],
          "needs_human": [
            {"test_id": "...", "reason": "...", "last_failure": "..."}, ...
          ]
        }
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "verify-results.json"
    target.write_text(
        json.dumps(outcome.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return target
