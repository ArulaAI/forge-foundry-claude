"""Forge Foundry - ``forge-validate test-scaffold`` subcommand (S4, PLB, #1031).

The deterministic driver for ``/test-scaffold``: detect the stack, parse the S1
architecture, resolve the cone shape, run the registered per-layer renderers, and
write the scaffold idempotently. The CLI never calls an LLM - model fill-ins
(service base-URL, container image, ...) arrive via the skill-first
``--extract-only`` / ``--continue --judgments`` protocol.

Exit codes (foundry convention): ``0`` success; ``2`` input error (bad path,
unrecognised stack, malformed ``--layers`` / ``--judgments``); ``7`` awaiting
judgments — ``--extract-only`` wrote a prompts file under ``--state-dir`` for the
model fill-ins the selected layers need; the SKILL.md body judges them and
re-invokes with ``--continue --judgments``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import click

from forge_validate._cli_common import (
    continue_option,
    extract_only_option,
    judgments_option,
    state_dir_option,
    validate_skill_first_flags,
)

#: SEC-8: a ``--layers`` token / cone-shape value reaching no subprocess but used
#: as a closed-set key. First char constrained so a leading ``-`` can't be a flag.
_VALID_LAYER = re.compile(r"^[a-z][a-z0-9-]*$")
_VALID_CONE = ("inverted", "pyramid")


def _parse_layers(raw: str | None) -> tuple[str, ...] | None:
    """Parse ``--layers a,b,c`` into a validated tuple, or ``None`` for all.

    Each token must match :data:`_VALID_LAYER` and be a known layer; an unknown
    or malformed token raises ``click.BadParameter`` (mapped to exit 2).
    """
    from primitives.test_scaffold.shared.registry import ALL_LAYERS  # noqa: PLC0415

    if raw is None:
        return None
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        return None
    for t in tokens:
        if not _VALID_LAYER.match(t) or t not in ALL_LAYERS:
            msg = f"--layers: unknown layer {t!r}; choose from {','.join(ALL_LAYERS)}"
            raise click.BadParameter(msg)
    # De-dupe, order-preserving (#1068): `--layers unit,unit` must scaffold + report
    # the layer once, not twice.
    return tuple(dict.fromkeys(tokens))


def _read_fillins(judgments_path: Path) -> dict[str, str]:
    """Parse a ``--judgments`` file into a flat ``{fillin_key: value}`` dict.

    Accepts two shapes (#1054): a flat ``{"container_image": "redis:7", ...}`` JSON
    object, or a skill-first :class:`JudgmentEnvelope` whose single item carries a
    ``response`` object of the same fill-in mapping. A non-object / non-envelope
    file raises ``ValueError`` (mapped to exit 2 by the caller).
    """
    raw = json.loads(judgments_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("judgments must be a JSON object or a JudgmentEnvelope")
    # Envelope shape: {"items": [{"id": ..., "response": {key: value, ...}}, ...]}.
    items = raw.get("items")
    if isinstance(items, list) and items:
        merged: dict[str, str] = {}
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("response"), dict):
                merged.update({str(k): str(v) for k, v in item["response"].items()})
        if merged:
            return merged
    # Flat shape: {key: value, ...} (ignore any envelope metadata keys if present).
    return {
        str(k): str(v)
        for k, v in raw.items()
        if k not in {"schema_version", "primitive", "round", "items", "_schema"}
    }


def _maybe_emit_prompts(
    layers: tuple[str, ...],
    supplied: dict[str, str],
    project_root: Path,
    state_dir: str | None,
) -> None:
    """Skill-first leg 1: emit a prompts file + exit 7 when fill-ins are needed.

    Computes the fill-ins the *selected* layers consume that *supplied* lacks. With
    none missing, returns (the run proceeds single-shot, exit 0). With one or more,
    writes a consolidated ``round-1-prompts.json`` under ``--state-dir`` (default
    ``<project>/.state``) and ``sys.exit(7)`` — awaiting judgments.
    """
    from primitives._shared.protocol import (  # noqa: PLC0415
        PromptEnvelope,
        PromptItem,
        ResponseFormat,
        write_prompts,
    )
    from primitives.test_scaffold.shared.fillins import missing_fillins  # noqa: PLC0415

    missing = missing_fillins(layers, supplied)
    if not missing:
        return

    catalog = {f.key: {"prompt": f.prompt, "default": f.default} for f in missing}
    item = PromptItem(
        id="test-scaffold-fillins",
        system=(
            "You are scaffolding tests with /test-scaffold. Provide concrete values "
            "for the model fill-ins below, inferred from the repository under test."
        ),
        user=(
            "Provide a JSON object mapping each fill-in key to a value. Keys + their "
            "prompts and deterministic defaults:\n"
            + json.dumps(catalog, indent=2)
            + "\n\nReturn only the {key: value} object; omit a key to accept its default."
        ),
        response_format=ResponseFormat(type="json"),
    )
    envelope = PromptEnvelope(primitive="test-scaffold", round=1, items=[item])
    resolved_state_dir = Path(state_dir) if state_dir is not None else project_root / ".state"
    prompts_path = resolved_state_dir / "round-1-prompts.json"
    write_prompts(envelope, prompts_path)
    click.echo(f"WROTE {prompts_path}")
    click.echo(
        f"Awaiting judgments for {len(missing)} fill-in(s): "
        f"{', '.join(f.key for f in missing)}. "
        "Re-invoke with --continue --judgments <round-1-judgments.json>.",
        err=True,
    )
    sys.exit(7)


@click.command("test-scaffold")
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--from-architecture",
    "from_architecture",
    type=click.Path(),
    default=None,
    help="S1 test-architecture.md or s1-scaffold-plan.json to scaffold from.",
)
@click.option(
    "--layers",
    "layers",
    default=None,
    help="Comma-separated subset: unit,integration,container,contract,api-e2e,ui-e2e.",
)
@click.option(
    "--cone-shape",
    "cone_shape",
    type=click.Choice(_VALID_CONE),
    default=None,
    help="Override the cone shape (default: inverted for service repos).",
)
@click.option(
    "--data-backend",
    "data_backend",
    default=None,
    help="Override the container-layer data backend "
    "(postgres/mysql/mssql/oracle/mongodb/dynamodb). "
    "Default: auto-detect from dependency manifests, else postgres.",
)
@click.option(
    "--data-tests",
    "data_tests",
    is_flag=True,
    default=False,
    help="Opt in to the data-test layers (data-structural/data-quality/data-seed/data-parity); "
    "implies the container layer. Off by default (app-code cone only).",
)
@click.option(
    "--output",
    "output",
    default=None,
    help="Write the scaffold report to this path (else stdout).",
)
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(  # noqa: PLR0913 — Click's decorator pattern forces all flags into one function
    path: str,
    from_architecture: str | None,
    layers: str | None,
    cone_shape: str | None,
    data_backend: str | None,
    data_tests: bool,
    output: str | None,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """Scaffold runnable tests for the whole cone from an S1 architecture.

    Default / ``--extract-only`` resolves the plan + (when renderers need them)
    emits model fill-in prompts; ``--continue --judgments`` applies the fill-ins
    and writes the scaffold. With no fill-ins required the run completes in one
    shot. See the module docstring for exit codes.
    """
    validate_skill_first_flags(extract_only=extract_only, continue_=continue_, judgments=judgments)

    # Lazy imports keep CLI cold-start light.
    from primitives.test_scaffold.shared import layers as _builtin_layers  # noqa: F401, PLC0415
    from primitives.test_scaffold.shared.architecture import load_plan  # noqa: PLC0415
    from primitives.test_scaffold.shared.cone import resolve_cone_shape  # noqa: PLC0415
    from primitives.test_scaffold.shared.scaffolder import (  # noqa: PLC0415
        render_report,
        run_scaffold,
    )

    project_root = Path(path)
    try:
        selected_layers = _parse_layers(layers)
    except click.BadParameter as exc:
        click.echo(f"Error: {exc.message}", err=True)
        sys.exit(2)

    # --data-tests opts in to the data-test layers (off by default). It appends them
    # to the selected layers (or to the full cone when no --layers was given) and
    # ensures `container` is present, since the data-test layers reuse its db_container
    # fixture. When the flag is absent but a data backend is detected, suggest it.
    if data_tests:
        from primitives.test_scaffold.shared.registry import (  # noqa: PLC0415
            DATA_TEST_LAYERS,
            LAYERS,
        )

        base = list(selected_layers) if selected_layers is not None else list(LAYERS)
        for lyr in ("container", *DATA_TEST_LAYERS):
            if lyr not in base:
                base.append(lyr)
        selected_layers = tuple(base)
    else:
        from primitives._sdet.stacks import detect_data_backends  # noqa: PLC0415

        if detect_data_backends(project_root):
            click.echo(
                "Note: data backend(s) detected — pass --data-tests to scaffold "
                "schema / referential-integrity / quality / seed / parity tests.",
                err=True,
            )

    arch_path = Path(from_architecture) if from_architecture is not None else None

    # Cone-shape precedence (#1052): explicit --cone-shape flag > S1 architecture >
    # the best-practice detection default (inverted for a service/API repo). The
    # flag is threaded distinctly as cli_override so load_plan applies it LAST,
    # rather than collapsing it into the default where the architecture would win.
    default_shape = resolve_cone_shape(project_root)
    plan = load_plan(
        project_root,
        from_architecture=arch_path,
        layers=selected_layers,
        cone_shape=default_shape,
        cli_override=cone_shape or None,
    )

    # Model fill-ins (service base-URL, container image, ...) via the skill-first
    # protocol. --continue carries the values the SKILL.md body judged; both a flat
    # {key: value} dict and a JudgmentEnvelope shape are accepted (#1054).
    fillins: dict[str, str] = {}
    if continue_ and judgments is not None:
        try:
            fillins = _read_fillins(Path(judgments))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            click.echo(f"Error: --judgments unreadable: {exc}", err=True)
            sys.exit(2)

    # --data-backend steers the container layer's backend selection (a CLI flag,
    # not a model fill-in). Validate against the registry closed set (SEC-8
    # boundary; unknown name → exit 3 input-rejected) and thread it via fillins.
    if data_backend is not None:
        from primitives._sdet.stacks import parse_data_backends  # noqa: PLC0415

        try:
            names = parse_data_backends(data_backend)
        except ValueError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(3)
        if names:
            fillins["data_backend"] = ",".join(names)

    # --extract-only (skill-first leg 1): when the selected layers need fill-ins
    # that have not been supplied, emit a single consolidated prompts file and exit
    # 7 (awaiting judgments). With no fill-ins required the run completes single-shot
    # so a layers-without-fill-ins scaffold (e.g. unit+integration) never blocks.
    if extract_only:
        _maybe_emit_prompts(plan.layers, fillins, project_root, state_dir)

    run = run_scaffold(plan, project_root, fillins)

    # An unrecognised stack with layers to scaffold is an input error (exit 2),
    # with the register_scaffold pointer.
    if plan.stack is None and any(lo.status == "no-stack" for lo in run.layers):
        click.echo(
            "Error: could not detect a supported stack (python / nodejs / java). "
            "Ship a renderer for your stack via register_scaffold "
            "(primitives.test_scaffold.shared.registry).",
            err=True,
        )
        sys.exit(2)

    # A stack was detected but NONE of its planned layers had a registered renderer
    # (every layer `unsupported`, nothing written) is also exit 2 with the seam
    # pointer (#1066) - mirroring the no-stack path. Otherwise a consumer who
    # registers a stack in _sdet.stacks but forgets its register_scaffold renderers
    # would get a silent exit-0 no-op. (When at least one layer scaffolded, the
    # remaining `unsupported` layers are reported, not an error.)
    if (
        plan.stack is not None
        and run.layers
        and all(lo.status == "unsupported" for lo in run.layers)
    ):
        click.echo(
            f"Error: stack {plan.stack!r} was detected but no layer renderer is "
            f"registered for it (nothing to scaffold). Ship renderers via "
            f"register_scaffold (primitives.test_scaffold.shared.registry).",
            err=True,
        )
        sys.exit(2)

    report = render_report(run)
    if output:
        Path(output).write_text(report, encoding="utf-8")
    else:
        click.echo(report)
    sys.exit(0)
