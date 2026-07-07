"""Unit-layer renderer — delegates to S3 ``test_from_spec`` (#1035).

The unit layer does NOT reimplement unit-test generation. S3 ``/test-from-spec``
is the unit-test engine — and it is a **skill-first** primitive (its generation
runs in-session via its SKILL.md body; there is no importable Python generator to
call). So this renderer composes S3 the only honest way: it emits a thin unit-layer
recipe that **routes** unit-test authoring to S3, never duplicating S3's pytest /
vitest / JUnit templates. That keeps the two primitives drift-free — S3 owns the
unit bodies, ``/test-scaffold`` owns the cone orchestration.

The recipe is collectable-green (it is documentation, not a test that could fail
to import) and idempotent (skipped if a unit layout already exists).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives.test_scaffold.shared.registry import (
    ScaffoldFile,
    ScaffoldResult,
    register_scaffold,
)

if TYPE_CHECKING:
    from primitives.test_scaffold.shared.registry import ScaffoldContext

#: Where each stack's unit tests live (S3's own layout conventions).
_UNIT_DIR: dict[str, str] = {
    "python": "tests/unit",
    "nodejs": "tests/unit",
    "java": "src/test/java",
}

#: The S3 generation command per stack (what the recipe routes the engineer to).
_S3_FRAMEWORK: dict[str, str] = {
    "python": "pytest",
    "nodejs": "vitest",
    "java": "JUnit 5",
}


def _recipe(stack: str, emphasis: int) -> str:
    """Render the unit-layer routing recipe (Markdown) for *stack*."""
    framework = _S3_FRAMEWORK.get(stack, "your unit framework")
    unit_dir = _UNIT_DIR.get(stack, "tests/unit")
    sample_word = "a representative unit test" if emphasis <= 1 else f"{emphasis} unit tests"
    return (
        f"# Unit layer (owned by S3 `/test-from-spec`)\n\n"
        f"The unit layer is **not** scaffolded by `/test-scaffold` — it delegates to\n"
        f"**S3 `/test-from-spec`**, the unit-test engine, to avoid template drift.\n\n"
        f"## How to generate the unit tests\n\n"
        f"Run `/test-from-spec` against your C1-shaped spec. It generates {framework}\n"
        f"unit tests (one file per acceptance criterion, happy-path + edge cases) under\n"
        f"`{unit_dir}/`. For this repo's cone shape, seed {sample_word} per criterion.\n\n"
        f"`/test-scaffold` owns the higher cone layers (integration, container, contract,\n"
        f"API e2e, UI e2e); S3 owns unit. See the SDET tier (charter S1-S4).\n"
    )


def render_unit(context: ScaffoldContext) -> ScaffoldResult:
    """Emit the unit-layer S3 routing recipe for *context*'s stack.

    Deliberately thin: a single ``tests/unit/README.test-scaffold.md`` recipe that
    points the engineer at S3. No unit-test bodies are generated here (that is S3's
    job). Idempotency + containment are handled by the write sink.
    """
    unit_dir = _UNIT_DIR.get(context.stack, "tests/unit")
    return ScaffoldResult(
        files=(
            ScaffoldFile(
                path=f"{unit_dir}/README.test-scaffold.md",
                content=_recipe(context.stack, context.emphasis),
            ),
        ),
        note=(
            f"unit layer delegates to S3 /test-from-spec ({_S3_FRAMEWORK.get(context.stack, '?')})"
        ),
    )


# Built-in registration: the unit renderer is the same delegation for every stack.
for _stack in ("python", "nodejs", "java"):
    register_scaffold(_stack, "unit", render_unit)
