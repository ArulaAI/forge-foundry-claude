"""Parse the S1 input + detect the stack into a :class:`ScaffoldPlan` (#1031).

The primitive's deterministic front end. It resolves three things the renderers
need: (1) the **stack** (reusing :mod:`primitives._sdet.stacks`), (2) the
**cone shape** S1 chose (or the best-practice default - wired by the cone engine,
#1032), and (3) the per-layer **tooling picks** parsed from S1's
``test-architecture.md`` section bodies. With no S1 input it falls back to
detection + defaults and records a warning.

The model fill-ins (service base-URL, container image, ...) are NOT resolved here -
they arrive via the ``--extract-only`` / ``--continue`` protocol at the CLI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from primitives.test_scaffold.shared.registry import LAYERS

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class ScaffoldPlan:
    """The resolved inputs for a scaffold run.

    *stack* is the detected stack name (``python`` / ``nodejs`` / ``java`` /
    plugin), or ``None`` when unrecognised. *layers* are the layers to scaffold.
    *cone_shape* is ``"inverted"`` (snow cone - integration/contract/API-heavy)
    or ``"pyramid"`` (unit-heavy). *tooling* maps a layer to the S1 tooling-pick
    text (advisory to renderers). *warnings* records non-fatal fallbacks.
    """

    stack: str | None
    layers: tuple[str, ...]
    cone_shape: str
    tooling: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def detect_stack(project_root: Path) -> str | None:
    """Return the detected stack name for *project_root*, or ``None``.

    Reuses the shared ``_sdet`` execution-adapter resolution (Python / JS-TS /
    Java); an unrecognised root returns ``None`` so the CLI can exit 2 with the
    ``register_scaffold`` pointer.
    """
    from primitives._sdet.stacks import resolve_adapter  # noqa: PLC0415

    adapter = resolve_adapter(project_root)
    return getattr(adapter, "name", None) if adapter is not None else None


def _read_architecture_md(text: str) -> dict[str, str]:
    """Extract the S1 per-layer tooling-pick section bodies, keyed by layer.

    Splits on ``## <heading>`` / ``### <heading>`` markdown headings and maps the
    four S1 layer sections to ``unit`` / ``contract`` / ``integration`` / ``e2e``
    body text. Unknown/absent sections are simply omitted (best-effort parse - a
    malformed doc degrades to "no tooling picks", not a crash).
    """
    heading_to_layer = {
        "Layer 1 - Unit": "unit",
        "Layer 2 - Contract": "contract",
        "Layer 3 - Integration": "integration",
        "Layer 4 - E2E": "ui-e2e",
    }
    out: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        # S1 emits em-dash headings ("Layer 1 — Unit", U+2014); normalize the
        # em-dash / en-dash to an ASCII hyphen before the lookup so real S1 docs
        # match the table above (#1055).
        stripped = (
            line.lstrip("#").strip().replace("—", "-").replace("–", "-")  # noqa: RUF001 - intentional em-dash/en-dash normalization
        )
        is_heading = line.lstrip().startswith("#")
        if is_heading and stripped in heading_to_layer:
            if current is not None:
                out[current] = "\n".join(buf).strip()
            current = heading_to_layer[stripped]
            buf = []
        elif is_heading and current is not None:
            out[current] = "\n".join(buf).strip()
            current = None
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf).strip()
    return out


def _read_scaffold_plan_json(data: dict[str, object]) -> tuple[dict[str, str], str | None]:
    """Read the optional machine-readable ``s1-scaffold-plan.json`` contract.

    Returns ``(tooling, cone_shape)``. Tolerant: missing keys degrade to empty /
    ``None`` rather than raising (the JSON contract is optional, design §5).
    """
    tooling_raw = data.get("tooling")
    tooling: dict[str, str] = {}
    if isinstance(tooling_raw, dict):
        tooling = {str(k): str(v) for k, v in tooling_raw.items()}
    cone = data.get("cone_shape")
    cone_shape = cone if isinstance(cone, str) and cone in {"inverted", "pyramid"} else None
    return tooling, cone_shape


def load_plan(
    project_root: Path,
    *,
    from_architecture: Path | None,
    layers: tuple[str, ...] | None,
    cone_shape: str,
    cli_override: str | None = None,
) -> ScaffoldPlan:
    """Resolve a :class:`ScaffoldPlan` from the project + optional S1 input.

    Cone-shape precedence (#1052): an explicit ``cli_override`` (the ``--cone-shape``
    flag) wins over everything; otherwise an S1 architecture that pins a shape wins
    over *cone_shape*, the detection default the caller (the cone engine, #1032)
    resolved. *layers* selects a subset; ``None`` means all of :data:`LAYERS`.
    Returns warnings (not exceptions) for a missing/unparseable architecture so the
    run proceeds on defaults.
    """
    warnings: list[str] = []
    stack = detect_stack(project_root)
    tooling: dict[str, str] = {}
    resolved_shape = cone_shape

    if from_architecture is not None:
        if not from_architecture.is_file():
            warnings.append(f"--from-architecture {from_architecture}: not found; using defaults")
        elif from_architecture.suffix == ".json":
            try:
                data = json.loads(from_architecture.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                warnings.append(f"--from-architecture: unreadable JSON ({exc}); using defaults")
            else:
                if isinstance(data, dict):
                    tooling, json_shape = _read_scaffold_plan_json(data)
                    # The S1 architecture pins the shape only when the caller did
                    # not pass an explicit --cone-shape flag (#1052).
                    if json_shape is not None and cli_override is None:
                        resolved_shape = json_shape
                else:
                    warnings.append("--from-architecture: JSON is not an object; using defaults")
        else:
            try:
                md_text = from_architecture.read_text(encoding="utf-8")
            except OSError as exc:
                warnings.append(f"--from-architecture: unreadable ({exc}); using defaults")
            else:
                tooling = _read_architecture_md(md_text)
                # An explicit cone shape in the S1 "Test cone target" section pins
                # the shape over the detection default (#1032) - but never over an
                # explicit --cone-shape flag (#1052).
                from primitives.test_scaffold.shared.cone import (  # noqa: PLC0415
                    infer_shape_from_text,
                )

                md_shape = infer_shape_from_text(md_text)
                if md_shape is not None and cli_override is None:
                    resolved_shape = md_shape
    else:
        warnings.append("no --from-architecture supplied; using stack detection + cone defaults")

    # The explicit flag wins last, over both the S1 architecture and the default.
    if cli_override is not None:
        resolved_shape = cli_override

    selected = tuple(layers) if layers is not None else LAYERS
    return ScaffoldPlan(
        stack=stack,
        layers=selected,
        cone_shape=resolved_shape,
        tooling=tooling,
        warnings=tuple(warnings),
    )
