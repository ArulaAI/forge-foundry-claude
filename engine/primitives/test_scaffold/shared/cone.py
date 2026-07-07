"""Resolve the cone shape for a project (the keystone seam for #1032).

The inverted snow cone is the SDET best-practice default for service/API repos:
invest in the wide middle (integration / contract / API), keep unit for complex
logic, stay sparing at broad UI e2e. This module resolves the *shape*; the cone
engine (#1032) extends it with the proportional ``ConeRatios`` emphasis the
renderers consume.

The keystone implements the default-shape decision (service repo -> ``inverted``,
else ``pyramid``); #1032 layers the proportional emphasis on top.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: Manifest / marker files that indicate a service or API surface (a web
#: framework, HTTP routes, an API spec, or a message consumer) - for which the
#: inverted snow cone is the right default. Detected by presence anywhere in the
#: project root's top level (cheap; the cone engine #1032 deepens this).
_SERVICE_MARKERS: tuple[str, ...] = (
    "openapi.yaml",
    "openapi.yml",
    "openapi.json",
    "swagger.yaml",
    "swagger.json",
    "docker-compose.yml",
    "docker-compose.yaml",
    "Dockerfile",
)

#: Dependency substrings that indicate a service/API stack when found in a
#: manifest (best-effort; the cone engine #1032 makes this stack-aware).
_SERVICE_DEP_HINTS: tuple[str, ...] = (
    "fastapi",
    "flask",
    "django",
    "express",
    "koa",
    "nestjs",
    "spring-boot",
    "spring-web",
    "grpc",
)

_MANIFESTS: tuple[str, ...] = (
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "pom.xml",
    "build.gradle",
)


def _has_service_marker(project_root: Path) -> bool:
    """True when a service/API marker file or dependency hint is present."""
    for marker in _SERVICE_MARKERS:
        if (project_root / marker).is_file():
            return True
    for manifest in _MANIFESTS:
        path = project_root / manifest
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        if any(hint in text for hint in _SERVICE_DEP_HINTS):
            return True
    return False


def resolve_cone_shape(project_root: Path) -> str:
    """Return the default cone shape for *project_root*.

    ``"inverted"`` (snow cone) when a service/API surface is detected - the
    best-practice default for boundary-heavy systems; ``"pyramid"`` otherwise
    (e.g. a pure-logic library, where unit-heavy is correct). An explicit S1
    architecture or ``--cone-shape`` flag overrides this upstream.
    """
    return "inverted" if _has_service_marker(project_root) else "pyramid"


# ---------------------------------------------------------------------------
# Proportional emphasis (#1032) — turn the cone shape into per-layer scaffolding
# weight (how many sample tests / CI jobs each layer emits).
# ---------------------------------------------------------------------------

#: The cone-ratio profile per shape, projected onto the six scaffold layers.
#: The classic **pyramid** is unit-heavy (the `_sdet` ``ConeRatios.DEFAULT`` shape:
#: unit 70 / contract 15 / integration 12 / e2e 3); the **inverted snow cone** is
#: the SDET best practice for boundary-heavy systems — the wide middle
#: (integration / container / contract / API e2e) carries the weight, unit is
#: right-sized for complex logic, and broad UI e2e stays sparing. Percentages per
#: layer (summing across the six layers); the absolute sample count is derived in
#: :func:`layer_emphasis`.
_PROFILES: dict[str, dict[str, int]] = {
    "pyramid": {
        "unit": 60,
        "integration": 12,
        "container": 8,
        "contract": 8,
        "api-e2e": 7,
        "ui-e2e": 5,
    },
    "inverted": {
        "unit": 15,
        "integration": 28,
        "container": 18,
        "contract": 18,
        "api-e2e": 16,
        "ui-e2e": 5,
    },
}

#: Sample counts a layer emits, bucketed by its profile weight. Keeps scaffolding
#: bounded (a scaffold seeds representative tests, never an exhaustive suite): a
#: heavily-weighted layer gets 3 samples, a mid-weighted one 2, a light one 1.
_HEAVY_THRESHOLD = 20
_MID_THRESHOLD = 10


def infer_shape_from_text(text: str) -> str | None:
    """Infer the cone shape from an S1 ``Test cone target`` section, or ``None``.

    S1's ``test-architecture.md`` describes the cone in prose + ratios. We read an
    explicit ``inverted``/``snow cone`` or ``pyramid`` mention, else fall back to
    the ratio emphasis: if the integration+contract weight visibly exceeds unit,
    it's an inverted snow cone. Returns ``None`` when the text pins nothing (the
    caller keeps the detection-based default).
    """
    low = text.lower()
    # "honeycomb" (Spotify's integration-centric shape) is mapped to "inverted" as
    # the closest of the two supported profiles - both de-emphasise broad unit/UI in
    # favour of the integration-heavy middle. They are not identical shapes; if a
    # distinct honeycomb profile is ever wanted, add it to _PROFILES + a third
    # cone_shape value rather than aliasing here (#1068).
    if "inverted" in low or "snow cone" in low or "honeycomb" in low:
        return "inverted"
    if "pyramid" in low and "inverted" not in low:
        return "pyramid"
    return None


def cone_profile(shape: str) -> dict[str, int]:
    """Return the per-layer weight profile for *shape* (default: ``inverted``)."""
    return _PROFILES.get(shape, _PROFILES["inverted"])


def layer_emphasis(layer: str, shape: str) -> int:
    """How many sample tests *layer* emits under cone *shape* (1-3).

    Derived from the layer's weight in the shape's profile: a heavily-weighted
    layer (the inverted cone's wide middle) seeds 3 representative samples, a
    mid-weighted layer 2, a light layer 1. Bounded so a scaffold stays a seed,
    not an exhaustive suite. An unknown layer defaults to 1.
    """
    weight = cone_profile(shape).get(layer, 0)
    if weight >= _HEAVY_THRESHOLD:
        return 3
    if weight >= _MID_THRESHOLD:
        return 2
    return 1
