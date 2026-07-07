"""The model fill-in catalog for /test-scaffold's skill-first protocol (#1054).

A *fill-in* is a stack-specific value a layer renderer needs but the deterministic
CLI cannot infer (a container image, a service base-URL, a contract provider). Each
renderer falls back to a sensible default, so a fill-in is *optional* — but when a
consumer wants real values, the SKILL.md body supplies them via the two-leg
``--extract-only`` (emit prompts, exit 7) → ``--continue --judgments`` protocol.

This module is the single source of truth for which fill-ins each layer consumes,
their prompts, and their defaults. The CLI's extract leg reads it to emit a
consolidated prompts file for exactly the fill-ins the *selected* layers need that
are not already supplied; the layer renderers read the same keys via
``context.fillins.get(key, default)``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FillIn:
    """One model fill-in a layer renderer consumes."""

    #: The ``context.fillins`` key the renderer reads.
    key: str
    #: The layer (registry key) that consumes it.
    layer: str
    #: A one-line prompt for the model (what value to provide).
    prompt: str
    #: The deterministic default the renderer uses when the fill-in is absent.
    default: str


#: Every fill-in the built-in layers consume, in layer order. Keep in sync with the
#: ``context.fillins.get(...)`` reads in shared/layers/*.py.
FILLINS: tuple[FillIn, ...] = (
    FillIn(
        key="container_image",
        layer="container",
        prompt=(
            "The container image for the Testcontainers harness "
            "(e.g. 'postgres:16', 'redis:7', 'mysql:8'). Pick the datastore the "
            "service under test actually uses."
        ),
        default="postgres:16",
    ),
    FillIn(
        key="contract_provider",
        layer="contract",
        prompt=(
            "The provider service name for the consumer-driven contract test "
            "(the upstream service this repo calls), e.g. 'billing-api'."
        ),
        default="provider-service",
    ),
    FillIn(
        key="api_base_url",
        layer="api-e2e",
        prompt=(
            "The base URL the API end-to-end journey runs against "
            "(e.g. 'http://localhost:8000' for a local run, or a staging URL)."
        ),
        default="http://localhost:8000",
    ),
    FillIn(
        key="ui_base_url",
        layer="ui-e2e",
        prompt=(
            "The base URL the Playwright UI end-to-end smoke runs against "
            "(e.g. 'http://localhost:3000' for a local dev server)."
        ),
        default="http://localhost:3000",
    ),
)


def fillins_for_layers(layers: tuple[str, ...]) -> tuple[FillIn, ...]:
    """The fill-ins the given layers consume, in catalog order.

    *layers* are registry keys (``container``, ``api-e2e``, ...). A layer with no
    fill-in (``unit``, ``integration``) contributes nothing.
    """
    selected = set(layers)
    return tuple(f for f in FILLINS if f.layer in selected)


def missing_fillins(layers: tuple[str, ...], supplied: dict[str, str]) -> tuple[FillIn, ...]:
    """The fill-ins the given layers need that are not already in *supplied*."""
    return tuple(f for f in fillins_for_layers(layers) if f.key not in supplied)
