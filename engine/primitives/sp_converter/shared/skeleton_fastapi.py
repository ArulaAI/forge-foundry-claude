"""M4 Stage C — the ``fastapi`` skeleton renderer.

Emits a small FastAPI starter tree for the SP: a ``pyproject.toml`` and an
``app/routers/<sp>.py`` router stub. The renderer is forbidden from inventing
business logic — the route handler body carries an explicit ``# TODO(<sp>): ...``
marker for the engineer to complete, and the data-deps / gotchas come straight
from the (anti-hallucination-guarded) plan.

Deterministic: the same :class:`ConversionPlan` always renders the same tree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives.sp_converter.shared.models import ConversionArtifact
from primitives.sp_converter.shared.skeletons import register

if TYPE_CHECKING:
    from primitives.sp_converter.shared.models import ConversionPlan


def _module_base(sp_name: str) -> str:
    """snake_case a stored-proc name into a Python module basename."""
    base = sp_name.replace("-", "_").lower()
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in base) or "sp"


def render(plan: ConversionPlan) -> list[ConversionArtifact]:
    """Render the ``fastapi`` starter tree for *plan*."""
    base = _module_base(plan.sp_name)
    deps = ", ".join(plan.data_deps) or "(none resolved)"
    gotchas = "\n".join(f"#   - {g}" for g in plan.gotchas) or "#   - (none recorded)"

    pyproject = (
        "[project]\n"
        f'name = "{base}-service"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        'dependencies = ["fastapi", "sqlalchemy", "uvicorn"]\n'
    )

    router = (
        '"""FastAPI router converted from stored procedure ' + plan.sp_name + ".\n\n"
        f"Data dependencies: {deps}\n"
        "Gotchas:\n"
        f"{gotchas}\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "from fastapi import APIRouter\n\n"
        f'router = APIRouter(prefix="/{base}", tags=["{base}"])\n\n\n'
        f'@router.post("/")\n'
        f"async def {base}() -> dict[str, str]:\n"
        f'    """Entry point ported from {plan.sp_name}."""\n'
        f"    # TODO({plan.sp_name}): port the procedure body (txn boundary, side effects).\n"
        '    raise NotImplementedError("generated scaffold — fill in")\n'
    )

    return [
        ConversionArtifact(path="pyproject.toml", content=pyproject, kind="config"),
        ConversionArtifact(path=f"app/routers/{base}.py", content=router, kind="scaffold"),
    ]


register("fastapi", render)

__all__ = ["render"]
