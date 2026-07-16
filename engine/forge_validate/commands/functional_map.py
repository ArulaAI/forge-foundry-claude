"""forge-validate functional-map subcommand (S61)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from primitives.functional_map.shared.pipeline import SliceArtifact, run


@click.command("functional-map")
@click.option("--code-root", required=True, type=click.Path(path_type=Path))
@click.option(
    "--state-dir",
    default=".foundry/context/functional-map",
    type=click.Path(path_type=Path),
)
@click.option("--stack-flavor", default="auto")
@click.option("--spec", "spec", default=None, type=click.Path(path_type=Path))
@click.option("--run-id", default="run")
@click.option("--format", "fmt", type=click.Choice(["markdown", "json"]), default="markdown")
@click.option("--output", default=None, type=click.Path(path_type=Path))
@click.option("--no-synthesis", is_flag=True, default=False)
@click.option("--extract-only", is_flag=True, default=False)
@click.option("--continue", "continue_flag", is_flag=True, default=False)
@click.option("--judgments", default=None, type=click.Path(path_type=Path))
@click.option(
    "--system-map",
    "system_map",
    default=None,
    type=click.Path(path_type=Path),
    help="App-mod system-map.json; activates per-bounded-context slicing (SP-A).",
)
@click.option(
    "--contexts",
    "contexts_path",
    default=None,
    type=click.Path(path_type=Path),
    help="Inline {context: [endpoint-key]} JSON; activates slicing (SP-A).",
)
def functional_map(  # noqa: PLR0913 — the documented CLI surface (spec §5)
    code_root: Path,
    state_dir: Path,
    stack_flavor: str,
    spec: Path | None,
    run_id: str,
    fmt: str,
    output: Path | None,
    no_synthesis: bool,
    extract_only: bool,
    continue_flag: bool,
    judgments: Path | None,
    system_map: Path | None,
    contexts_path: Path | None,
) -> None:
    """S61 — reverse-engineer a functional-requirements map from a web-app tree."""
    if continue_flag and judgments is None:
        raise click.UsageError("--continue requires --judgments <path>")
    if not Path(code_root).is_dir():
        raise SystemExit(2)

    contexts: dict[str, list[str]] | None = None
    if contexts_path is not None:
        try:
            contexts = json.loads(Path(contexts_path).read_text())
        except FileNotFoundError as exc:
            raise SystemExit(2) from exc

    try:
        result = run(
            Path(code_root),
            Path(state_dir),
            stack_flavor=stack_flavor,
            spec=spec,
            run_id=run_id,
            no_synthesis=no_synthesis,
            extract_only=extract_only,
            continue_path=Path(judgments) if (continue_flag and judgments is not None) else None,
            system_map_path=Path(system_map) if system_map is not None else None,
            contexts=contexts,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(3) from exc

    _warn_on_slice_gaps(result)

    if result.exit_code in (0, 6):
        payload = result.output_json if fmt == "json" else result.output_markdown
        if output:
            # Explicit destination — write the --format-selected payload there.
            if payload is not None:
                out_path = Path(output)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(payload)
        else:
            # No --output: persist the final artifacts to durable named files under the
            # state dir (default .foundry/context/functional-map), matching the
            # db-schema-analyze convention — the map + JSON FR-inventory sidecar, so the
            # result is discoverable in the repo rather than lost to stdout. Both are
            # always produced regardless of --format (--format only picks which the user
            # asked for; we persist both). stdout gets a short "wrote <paths>" summary.
            #
            # In sliced mode (SP-A) the whole-app doc is still written (as the
            # index/overview) AND one FUNCTIONAL-MAP.md + functional-map.json is written
            # per bounded context under functional-map/<slice>/.
            _persist_durable_artifacts(
                Path(state_dir),
                result.output_markdown,
                result.output_json,
                result.slices,
                slice_provenance=result.slice_provenance,
            )
    raise SystemExit(result.exit_code)


def _warn_on_slice_gaps(result: object) -> None:
    """Surface the two silent-slicing hazards of the sliced pass (#1406).

    A sliced pass (``--system-map``/``--contexts``) is easy to mis-run silently:

    * **No slices emitted.** The bare ``--system-map`` form re-enters the extract leg
      and exits 7 (or a completed synthesis is absent), so NO ``functional-map/<slice>/``
      dirs are written. Auto-resume (pipeline) handles the common case, but if a
      completed synthesis is genuinely missing we WARN rather than leave the operator
      with an empty result and no diagnostic.
    * **Path-prefix fallback.** When a coarse app-mod system-map's nodes carry no
      domain labels (every endpoint collapses to one architectural component), slicing
      degrades to path-prefix clustering. We say so, so operators don't believe the
      system-map drove the partition.
    """
    from primitives.functional_map.shared.pipeline import (  # noqa: PLC0415
        PipelineResult,
        is_fallback_provenance,
    )

    if not isinstance(result, PipelineResult) or not result.sliced_requested:
        return
    if not result.slices:
        click.echo(
            "functional-map: WARNING — a sliced pass was requested (--system-map/"
            "--contexts) but NO slices were emitted. The whole-app synthesis round must "
            "complete first; re-invoke as the continue leg once "
            f"{result.state_dir / 'synthesis.json'} exists:\n"
            "  forge-validate functional-map --code-root <repo> --continue \\\n"
            "      --judgments <state-dir>/run/synthesis.json \\\n"
            "      --state-dir <state-dir> --system-map <system-map.json>",
            err=True,
        )
        return
    if is_fallback_provenance(result.slice_provenance):
        if result.slice_provenance == "system-map-coarse":
            detail = (
                "the --system-map's nodes carry no business-domain labels, so every "
                "endpoint collapsed into a single bucket"
            )
        else:
            detail = (
                "no usable system-map/contexts were supplied, so endpoints were "
                "clustered by their first path segment"
            )
        click.echo(
            "functional-map: NOTE — slicing fell back to path-prefix clustering "
            f"({detail}). The --system-map input did NOT drive the partition; the "
            "slices are path-prefix buckets. This is expected with a coarse app-mod "
            "component system-map.",
            err=True,
        )


def _persist_durable_artifacts(
    state_dir: Path,
    output_markdown: str | None,
    output_json: str | None,
    slices: tuple[SliceArtifact, ...] = (),
    *,
    slice_provenance: str | None = None,
) -> None:
    """Write FUNCTIONAL-MAP.md + functional-map.json into *state_dir*; summarize to stdout.

    In sliced mode, also write ``functional-map/<slice>/FUNCTIONAL-MAP.md`` +
    ``functional-map/<slice>/functional-map.json`` per bounded context (SP-A, #1381).
    When slicing fell back to path-prefix (a coarse app-mod system-map, #1406), a
    ``functional-map/SLICING-NOTE.md`` records *how* the partition was derived so the
    provenance is discoverable in the repo, not only on stderr.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if output_markdown is not None:
        md_path = state_dir / "FUNCTIONAL-MAP.md"
        md_path.write_text(output_markdown)
        written.append(md_path)
    if output_json is not None:
        json_path = state_dir / "functional-map.json"
        json_path.write_text(output_json)
        written.append(json_path)
    for artifact in slices:
        slice_dir = state_dir / "functional-map" / artifact.slug
        slice_dir.mkdir(parents=True, exist_ok=True)
        slice_md = slice_dir / "FUNCTIONAL-MAP.md"
        slice_md.write_text(artifact.output_markdown)
        written.append(slice_md)
        slice_json = slice_dir / "functional-map.json"
        slice_json.write_text(artifact.output_json)
        written.append(slice_json)
    from primitives.functional_map.shared.pipeline import (  # noqa: PLC0415
        is_fallback_provenance,
    )

    if slices and is_fallback_provenance(slice_provenance):
        if slice_provenance == "system-map-coarse":
            cause = (
                "The supplied app-mod system-map's nodes carry no business-domain labels "
                "(its components are coarse architectural tiers — `.#ui` / `.#app` / "
                "`.#api` / `.#db`), so every application endpoint collapsed into a single "
                "bucket and the deterministic endpoint→context resolver had nothing finer "
                "to partition on."
            )
        else:
            cause = (
                "No usable `--system-map` bounded contexts or explicit `--contexts` were "
                "supplied, so the deterministic endpoint→context resolver had no external "
                "partition to slice on."
            )
        note_path = state_dir / "functional-map" / "SLICING-NOTE.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "# Slicing provenance: path-prefix fallback\n\n"
            "These slices were derived by **path-prefix clustering**, NOT by "
            "domain-driven bounded contexts.\n\n"
            f"{cause} Slicing therefore fell back to clustering endpoints by their first "
            "path segment. Treat these buckets as resource-prefix groupings, not "
            "domain-driven bounded contexts.\n"
        )
        written.append(note_path)
    for path in written:
        click.echo(f"functional-map: wrote {path}")


cli = functional_map
