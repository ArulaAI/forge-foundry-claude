"""M4 Stage C — the FLEX-3 per-target skeleton renderer registry.

Each built-in target ships as a flat ``shared/skeleton_<target>.py`` module that
self-registers a ``render(plan) -> list[ConversionArtifact]`` callable here at
import time, mirroring the :mod:`primitives._renderer_registry` FLEX-3 pattern.
``--target`` validates against :func:`registered_targets`, which overlays
entry-point plugins (``forge_foundry.renderers.sp_converter_targets``) on the
built-ins so a consumer can ship a Quarkus / .NET-worker renderer without
forking (plugin wins on same name).

Three targets are built here (``plan-only``, ``spring-batch``, ``fastapi``); the
remaining two persisted targets (``celery``, ``spring-boot``) are documented
FLEX-3 extension points — they are valid persisted ``Target`` Literals but ship
their renderers via entry point rather than in-tree.

Renderers are deterministic: the same :class:`ConversionPlan` always renders the
same artifact set.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from primitives.sp_converter.shared.models import ConversionArtifact, ConversionPlan

#: Entry-point group third parties declare to register additional targets.
ENTRY_POINT_GROUP = "forge_foundry.renderers.sp_converter_targets"

#: Built-in target name → renderer callable. Populated at import time by each
#: ``skeleton_<target>.py`` module via :func:`register`.
_TARGET_REGISTRY: dict[str, Callable[[ConversionPlan], list[ConversionArtifact]]] = {}


def register(name: str, fn: Callable[[ConversionPlan], list[ConversionArtifact]]) -> None:
    """Register built-in renderer *fn* under target *name* (last-writer-wins)."""
    _TARGET_REGISTRY[name] = fn


def _discover() -> dict[str, Callable[[ConversionPlan], list[ConversionArtifact]]]:
    """Built-ins overlaid with entry-point plugins (plugin wins on same name)."""
    _ensure_builtins_loaded()
    merged = dict(_TARGET_REGISTRY)
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        merged[ep.name] = ep.load()
    return merged


def registered_targets() -> list[str]:
    """Return every registered target name (built-in + entry-point), sorted."""
    return sorted(_discover())


def render(plan: ConversionPlan) -> list[ConversionArtifact]:
    """Render *plan*'s skeleton artifacts via its target's registered renderer.

    Raises:
        ValueError: ``plan.target`` is not a registered target (the CLI
            validates ``--target`` against :func:`registered_targets` upstream,
            so this only fires on a hand-edited plan).
    """
    renderers = _discover()
    fn = renderers.get(plan.target)
    if fn is None:
        raise ValueError(
            f"unknown conversion target {plan.target!r}; available: {sorted(renderers)}. "
            f'Install a plugin that declares `[project.entry-points."{ENTRY_POINT_GROUP}"]`.'
        )
    return fn(plan)


def _ensure_builtins_loaded() -> None:
    """Import the built-in skeleton modules so they self-register (idempotent)."""
    # Imported for their import-time ``register(...)`` side effect.
    from primitives.sp_converter.shared import (  # noqa: F401, PLC0415
        skeleton_fastapi,
        skeleton_plan_only,
        skeleton_spring_batch,
    )


__all__ = [
    "ENTRY_POINT_GROUP",
    "register",
    "registered_targets",
    "render",
]
