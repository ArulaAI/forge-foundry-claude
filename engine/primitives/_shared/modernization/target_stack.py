"""Target-stack model for target-aware modernization (SP1, foundation).

A first-class, multi-dimensional **target stack** the app-mod chain designs
*toward* — parsed once by the orchestrator, persisted as ``target-stack.json``,
and read by the scope synthesizer and each upstream stage. Offline + deterministic:
version resolution consults ONLY the bundled checked-in registry
(:mod:`primitives._shared.modernization.target_registry`), never a live lookup.

Design: ``docs/superpowers/specs/2026-07-10-target-stack-modernization-design.md`` §1.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from primitives._shared.modernization.target_registry import latest_known


class TargetDimension(BaseModel):
    """One dimension of a declared target stack (e.g. ``backend=spring-boot@3.3``).

    Every field defaults empty/``False`` so the model is inert when no target is
    supplied. ``registry_matched=False`` means the ``value`` was not found in the
    bundled registry — it is carried verbatim (nothing silently dropped) and the
    caller is warned.
    """

    key: str = ""  # frontend | backend | database | cloud | messaging | api_style
    value: str = ""  # "angular", "spring-boot", "oracle", "aws", …
    version: str = ""  # "18", "3.3", "23ai", "" (→ resolved to latest-known)
    version_source: str = ""  # "explicit" | "latest-known"
    registry_matched: bool = False  # False → value not in registry (carried verbatim)
    label: str = ""  # canonical render label, e.g. "Spring Boot 3.3"

    model_config = ConfigDict(extra="forbid")


class TargetStack(BaseModel):
    """The resolved multi-dimensional modernization target.

    Empty (``dimensions == []``) means no target was supplied — the chain falls
    back to its byte-identical descriptive behavior.
    """

    dimensions: list[TargetDimension] = Field(default_factory=list)
    notes: str = ""

    model_config = ConfigDict(extra="forbid")


def parse_target(pairs: list[str]) -> tuple[TargetStack, list[str]]:
    """Parse repeatable ``KEY=VALUE[@version]`` target pairs into a :class:`TargetStack`.

    Warn-not-fail throughout — a malformed pair or an unknown key/value never
    raises; it produces a warning and (for unknown values) carries the value
    verbatim with ``registry_matched=False``. Version resolution is offline: an
    explicit ``@version`` wins (``version_source="explicit"``); an omitted version
    resolves to the registry's latest-known (``version_source="latest-known"``).

    Returns the resolved stack plus the accumulated warnings.
    """
    dimensions: list[TargetDimension] = []
    warnings: list[str] = []

    for raw in pairs:
        pair = raw.strip()
        if "=" not in pair:
            warnings.append(
                f"malformed target pair (expected KEY=VALUE[@version]): {raw!r}; skipping"
            )
            continue

        key, _, rest = pair.partition("=")
        key = key.strip()
        rest = rest.strip()
        if not key or not rest:
            warnings.append(f"malformed target pair (empty key or value): {raw!r}; skipping")
            continue

        value, sep, explicit_version = rest.partition("@")
        value = value.strip()
        explicit_version = explicit_version.strip()

        known_version, known_label = latest_known(key, value)
        # A registry match means the entry EXISTS (label resolved) — independent of
        # whether the value carries a meaningful version. Versionless targets like
        # api_style=rest / orchestration=eks are registered with latest: "" but still
        # render a clean label, so they are matched (not treated as "unknown").
        registry_matched = bool(known_label)

        if not registry_matched:
            warnings.append(
                f"unknown target {key}={value!r}: not in the bundled registry; "
                "carried verbatim (verify at implementation time)"
            )

        if sep and explicit_version:
            version = explicit_version
            version_source = "explicit"
            label = _relabel(known_label, known_version, version) if registry_matched else ""
        elif registry_matched:
            version = known_version
            version_source = "latest-known"
            label = known_label
        else:
            version = ""
            version_source = ""
            label = ""

        dimensions.append(
            TargetDimension(
                key=key,
                value=value,
                version=version,
                version_source=version_source,
                registry_matched=registry_matched,
                label=label,
            )
        )

    return TargetStack(dimensions=dimensions), warnings


def _relabel(known_label: str, known_version: str, version: str) -> str:
    """Re-point a registry label at an explicit version.

    The registry label was formatted with the latest-known version; swap in the
    operator's explicit version so the rendered label is honest.
    """
    if known_version and known_version in known_label:
        return known_label.replace(known_version, version)
    return known_label
