"""Cone-emphasis realization: emit N representative samples per layer (#1060).

The cone engine resolves a per-layer *emphasis* (1-3) — the inverted snow cone's
wide middle (integration / container / contract / API e2e) gets a higher weight
than the sparing unit + UI-e2e tips (#1032). Previously only the report surfaced
that number; every renderer emitted exactly one sample regardless. This helper
*realizes* the emphasis: a layer renderer emits a self-contained companion file of
``emphasis - 1`` additional representative test stubs, so the wide middle genuinely
receives proportionally more scaffolding.

A companion file (rather than injecting into the base sample's class/describe body)
keeps the mechanism uniform across Python / TS / Java and trivially idempotent — the
sink skips an existing companion, so re-runs never duplicate. Every stub is
**collectable-green**: Python carries the layer mark + ``pytest.skip``, TS uses
``it.skip``, Java uses ``@Disabled`` — it lists clean and never errors until filled.

A renderer asks for the companion via :func:`emphasis_companion`, which returns a
``(path, content)`` pair or ``None`` when ``emphasis <= 1`` (no extra samples).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primitives.test_scaffold.shared.registry import ScaffoldContext, ScaffoldFile

#: Layer key -> the pytest mark the Python stubs carry (hyphen -> underscore).
_PY_MARK = {
    "integration": "integration",
    "container": "container",
    "contract": "contract",
    "api-e2e": "api_e2e",
    "ui-e2e": "ui_e2e",
}


def _slug(layer: str) -> str:
    return layer.replace("-", "_")


def _py_companion(layer: str, emphasis: int) -> tuple[str, str]:
    mark = _PY_MARK.get(layer, _slug(layer))
    slug = _slug(layer)
    cases = "\n\n".join(
        f"@pytest.mark.{mark}\n"
        f"def test_{slug}_representative_case_{n}() -> None:\n"
        f'    """Cone-emphasis sample {n}/{emphasis} for the {layer} layer (#1060).\n\n'
        f"    The inverted snow cone weights this layer at emphasis {emphasis}; flesh\n"
        f"    this out into a distinct scenario (a different boundary / payload /\n"
        f"    failure mode), then drop the skip.\n"
        f'    """\n'
        f'    pytest.skip("emphasis sample {n}: add a distinct {layer} scenario")'
        for n in range(2, emphasis + 1)
    )
    content = (
        f'"""Cone-emphasis companion for the {layer} layer ({emphasis - 1} extra '
        f"samples; #1060).\n\n"
        f"The inverted snow cone weights this layer at emphasis {emphasis}, so it gets\n"
        f"proportionally more scaffolding than the cone's sparing tips. Each sample is\n"
        f"collectable-green (skips until you flesh out a distinct scenario).\n"
        f'"""\n\n'
        f"from __future__ import annotations\n\n"
        f"import pytest\n\n\n"
        f"{cases}\n"
    )
    return (f"tests/{slug}/test_{slug}_emphasis.py", content)


def _ts_companion(layer: str, emphasis: int) -> tuple[str, str]:
    slug = _slug(layer)
    cases = "\n\n".join(
        f"  it.skip('{layer} representative case {n}', async () => {{\n"
        f"    // Cone-emphasis sample {n}/{emphasis}: add a distinct {layer} scenario.\n"
        f"    // Fail-until-wired (#1059): enabling this before adding the scenario\n"
        f"    // fails red, not green-on-a-tautology.\n"
        f"    expect.fail('add a distinct {layer} scenario, then remove this line');\n"
        f"  }});"
        for n in range(2, emphasis + 1)
    )
    content = (
        f"import {{ describe, it, expect }} from 'vitest';\n\n"
        f"// Cone-emphasis companion for the {layer} layer ({emphasis - 1} extra samples;\n"
        f"// #1060). The inverted snow cone weights this layer at emphasis {emphasis}.\n"
        f"describe('{layer} representative cases', () => {{\n"
        f"{cases}\n"
        f"}});\n"
    )
    return (f"tests/{slug}/{slug}.emphasis.test.ts", content)


def _java_companion(layer: str, emphasis: int) -> tuple[str, str]:
    slug = _slug(layer)
    pkg = slug.replace("_", "")
    cls = pkg.title() + "EmphasisTest"
    methods = "\n\n".join(
        f"    // Cone-emphasis sample {n}/{emphasis}: add a distinct {layer} scenario.\n"
        f"    @Test\n"
        f'    @Disabled("emphasis sample {n}: add a distinct {layer} scenario")\n'
        f"    void representativeCase{n}() {{\n"
        f"        // TODO: a different boundary / payload / failure mode.\n"
        f"        // Fail-until-wired (#1059): enabling this before adding the\n"
        f"        // scenario fails red, not green-on-an-empty-body.\n"
        f'        fail("add a distinct {layer} scenario, then remove this line");\n'
        f"    }}"
        for n in range(2, emphasis + 1)
    )
    content = (
        f"package {pkg};\n\n"
        f"import static org.junit.jupiter.api.Assertions.fail;\n\n"
        f"import org.junit.jupiter.api.Disabled;\n"
        f"import org.junit.jupiter.api.Test;\n\n"
        f"/**\n"
        f" * Cone-emphasis companion for the {layer} layer ({emphasis - 1} extra samples;\n"
        f" * #1060). The inverted snow cone weights this layer at emphasis {emphasis}.\n"
        f" */\n"
        f"class {cls} {{\n\n"
        f"{methods}\n"
        f"}}\n"
    )
    return (f"src/test/java/{pkg}/{cls}.java", content)


_COMPANIONS = {
    "python": _py_companion,
    "nodejs": _ts_companion,
    "java": _java_companion,
}


def emphasis_companion(stack: str, layer: str, emphasis: int) -> tuple[str, str] | None:
    """A ``(path, content)`` companion file of ``emphasis - 1`` extra samples, or None.

    Returns ``None`` when ``emphasis <= 1`` (the cone's sparing tips get just the
    base sample) or the stack has no companion shape.
    """
    if emphasis <= 1:
        return None
    builder = _COMPANIONS.get(stack)
    if builder is None:
        return None
    return builder(layer, emphasis)


def with_emphasis(
    files: tuple[ScaffoldFile, ...], context: ScaffoldContext
) -> tuple[ScaffoldFile, ...]:
    """Append the cone-emphasis companion file to *files* for *context* (#1060).

    A renderer calls this on its assembled ``files`` just before returning, so the
    inverted snow cone's wide middle (emphasis 2-3) gets proportionally more
    scaffolding. A no-op when emphasis <= 1 or the stack has no companion shape.

    Only the three built-in stacks (python / nodejs / java) have a companion shape;
    an extension stack registered via ``register_scaffold`` gets a no-op here and
    should emit its own ``emphasis``-scaled samples from its renderer if it wants
    proportional scaffolding (the report still surfaces the emphasis regardless).
    """
    from primitives.test_scaffold.shared.registry import ScaffoldFile  # noqa: PLC0415

    companion = emphasis_companion(context.stack, context.layer, context.emphasis)
    if companion is None:
        return files
    path, content = companion
    return (*files, ScaffoldFile(path=path, content=content))
