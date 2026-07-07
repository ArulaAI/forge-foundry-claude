"""Forge Foundry — `forge-validate test-from-spec` subcommand.

Extract + continue legs for the S3 test-from-spec PLB conversion (#1114, #1115).
D1 adds opt-in ``--collection-green`` mode (#1121): the extract leg emits an extra
stub-planning prompt item; the continue leg writes containment-guarded, never-overwriting,
marked stub files so the suite collects/compiles while tests still fail at runtime.
"""

from __future__ import annotations

import json
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

#: Reserved prompt-item id for the collection-green stub-planning round.
_COLLECTION_GREEN_STUB_ID = "__collection_green_stub__"

#: Header comment text prepended to every auto-generated stub file.
_STUB_HEADER_TEXT = (
    "AUTO-GENERATED collection-green stub — replace with real implementation (#1027)"
)

#: Map from file extension to the appropriate comment syntax for the stub header.
_COMMENT_PREFIX: dict[str, str] = {
    ".py": "#",
    ".java": "//",
    ".cs": "//",
    ".ts": "//",
    ".tsx": "//",
    ".js": "//",
    ".jsx": "//",
}


def _stub_header(path: Path) -> str:
    """Return a stack-appropriate single-line header comment for a stub file."""
    prefix = _COMMENT_PREFIX.get(path.suffix.lower(), "#")
    return f"{prefix} {_STUB_HEADER_TEXT}\n"


def _is_containment_safe(raw_path: str, project_root: Path) -> bool:
    """Return True iff *raw_path* is safe to write under *project_root*.

    Rejects:
    - absolute paths
    - any path part that is ``..``
    - resolved paths that escape *project_root*
    """
    p = Path(raw_path)
    if p.is_absolute():
        return False
    if ".." in p.parts:
        return False
    try:
        resolved = (project_root / p).resolve()
        resolved.relative_to(project_root.resolve())
    except ValueError:
        return False
    return True


def _pascal(slug: str) -> str:
    """Convert an underscore slug to PascalCase."""
    return "".join(word.capitalize() for word in slug.split("_"))


def _output_filename(stack: str | None, criterion_id: str) -> str:
    """Return the target test filename for the given stack and criterion slug."""
    if stack == "react":
        return f"{criterion_id}.test.tsx"
    if stack == "angular":
        return f"{criterion_id}.spec.ts"
    if stack in ("nodejs", "js"):
        return f"{criterion_id}.test.ts"
    if stack == "java":
        return f"{_pascal(criterion_id)}Test.java"
    if stack == "dotnet":
        return f"{_pascal(criterion_id)}Tests.cs"
    # python or unknown: fall back to pytest convention
    return f"test_{criterion_id}.py"


@click.command("test-from-spec")
@click.argument("path", metavar="SPEC", type=click.Path(), required=False)
@click.option("--spec", "spec_opt", type=click.Path(), default=None, help="Path to the spec file.")
@click.option(
    "--codebase-root",
    "codebase_root",
    type=click.Path(),
    default=None,
    help="Codebase root for stack detection (default: cwd).",
)
@click.option(
    "--stack",
    default=None,
    help="Stack override (python / js / java / dotnet / react / angular / nodejs).",
)
@click.option("--framework", default=None, help="Framework override.")
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(),
    default=None,
    help="Output dir for generated test files (default: tests/<spec_dir>/).",
)
@click.option(
    "--collection-green",
    "collection_green",
    is_flag=True,
    default=False,
    help=(
        "Opt-in: emit a stub-planning prompt so the suite collects/compiles "
        "while tests still fail at runtime (default: off). "
        "Must be passed on both the --extract-only and --continue invocations."
    ),
)
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(  # noqa: PLR0912, PLR0913, PLR0915
    path: str | None,
    spec_opt: str | None,
    codebase_root: str | None,
    stack: str | None,
    framework: str | None,
    output_dir: str | None,
    collection_green: bool,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """Generate failing unit tests from a C1 spec's Acceptance criteria.

    Exit codes:

    \b
    0  Success — test files written.
    2  Input error — spec file not found or unsafe path.
    3  Spec has no Acceptance section, or unknown --stack.
    7  Awaiting judgments (--extract-only wrote a prompts file).
    """
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )

    spec_raw = path or spec_opt
    if spec_raw is None:
        click.echo("Error: missing spec path (provide as positional argument or --spec).", err=True)
        sys.exit(2)

    # Reject .. path traversal in the raw spec path string.
    if ".." in Path(spec_raw).parts:
        click.echo(f"Error: spec path must not contain '..': {spec_raw!r}", err=True)
        sys.exit(2)

    spec = Path(spec_raw)
    if not spec.is_file():
        click.echo(f"Error: spec file not found: {spec_raw!r}", err=True)
        sys.exit(2)

    root = Path(codebase_root).resolve() if codebase_root else Path.cwd()
    resolved_state_dir = Path(state_dir) if state_dir else root / ".state"

    # =========================================================================
    # Branch: --extract-only  (emit prompts, exit 7)
    # =========================================================================
    if extract_only:
        from primitives._shared.protocol import (  # noqa: PLC0415
            PromptEnvelope,
            PromptItem,
            ResponseFormat,
            write_prompts,
        )
        from primitives.test_from_spec.shared import engine  # noqa: PLC0415

        result = engine.parse_spec(spec, root, stack)
        if not result.criteria:
            click.echo(
                "Error: spec has no Acceptance section (no `- [ ]` criteria found).",
                err=True,
            )
            sys.exit(3)

        stack_name, meta = engine.resolve_target(root, stack=stack, framework=framework)

        if stack is not None and meta is None:
            click.echo(
                f"Error: unknown --stack {stack!r} (no unit-test metadata registered; "
                "valid values: python, js, nodejs, java, dotnet, react, angular).",
                err=True,
            )
            sys.exit(3)

        framework_str = (meta.test_framework if meta else None) or stack_name or "unknown"
        template_id = meta.template_id if meta else "generic"
        api_summary = ", ".join(result.public_api) or "none listed"

        items: list[PromptItem] = []
        for criterion in result.criteria:
            system = (
                f"You are generating a stack-native failing unit test for an acceptance criterion. "
                f"Stack: {stack_name or 'unknown'}. "
                f"Test framework: {framework_str}. "
                f"Template: {template_id}. "
                f"Rules:\n"
                f"- Use ONLY symbols from the Public API: {api_summary}.\n"
                f"- Write ≥2 assertions covering the happy path AND at least one edge case.\n"
                f"- The test MUST be in a failing (red) state against a stub implementation — "
                f"do not write a trivially passing test.\n"
                f"- Do not use `assert True` or `assert False` as assertions.\n"
                f"Return only the raw test file body (no markdown fences, no explanation)."
            )
            user = (
                f"Criterion: {criterion.text}\n\n"
                f"Public API: {api_summary}\n"
                f"Stack: {stack_name or 'unknown'}\n"
                f"Framework: {framework_str}\n"
                f"Spec dir: {result.spec_dir}\n"
            )
            items.append(
                PromptItem(
                    id=criterion.id,
                    system=system,
                    user=user,
                    response_format=ResponseFormat(type="text"),
                )
            )

        # --collection-green: add a stub-planning item so the model generates
        # stub source files for every Public-API symbol.  The model returns a
        # JSON array of {"path": "<relative>", "content": "<source>"} objects.
        if collection_green:
            stub_raise = meta.stub_raise if meta else "raise NotImplementedError"
            cg_system = (
                f"You are planning collection-green stub files for a '{stack_name or 'unknown'}' "
                f"project so that generated tests can import and collect without an "
                f"ImportError, while every stub function/method body uses the stack-native "
                f"'not implemented' statement so tests FAIL at runtime (red preserved).\n"
                f"Stack: {stack_name or 'unknown'}.\n"
                f"Stub statement: {stub_raise!r}.\n"
                f"Rules:\n"
                f"- Output a JSON array (no markdown fences, no explanation) of objects, "
                f'one per file needed, each shaped: {{"path": "<project-relative path>", '
                f'"content": "<full file source>"}}.\n'
                f"- Every Public-API symbol must be DEFINED (class, function, or method) "
                f"so that `from <module> import <symbol>` resolves.\n"
                f"- Every callable body must be exactly the stub statement: {stub_raise!r}.\n"
                f"- Paths must be project-relative (no leading /, no '..' segments).\n"
                f"- Do NOT create test files — only source/implementation stubs."
            )
            cg_user = (
                f"Public API symbols to stub: {api_summary}\n"
                f"Stack: {stack_name or 'unknown'}\n"
                f"Stub statement: {stub_raise!r}\n"
                f"Spec dir hint: {result.spec_dir}\n"
            )
            items.append(
                PromptItem(
                    id=_COLLECTION_GREEN_STUB_ID,
                    system=cg_system,
                    user=cg_user,
                    response_format=ResponseFormat(type="json"),
                )
            )

        envelope = PromptEnvelope(primitive="test-from-spec", round=1, items=items)
        prompts_path = resolved_state_dir / "round-1-prompts.json"
        write_prompts(envelope, prompts_path)
        click.echo(f"WROTE {prompts_path}")
        sys.exit(7)

    # =========================================================================
    # Branch: --continue --judgments  (resume after in-session judgment)
    # =========================================================================
    if continue_:
        from primitives._shared.protocol import read_judgments  # noqa: PLC0415
        from primitives.test_from_spec.shared import engine  # noqa: PLC0415

        assert judgments is not None  # guaranteed by validate_skill_first_flags
        judgment_envelope = read_judgments(Path(judgments))

        result = engine.parse_spec(spec, root, stack)
        stack_name, _ = engine.resolve_target(root, stack=stack, framework=framework)

        spec_dir = result.spec_dir
        base_output = Path(output_dir) if output_dir else root / "tests" / spec_dir
        base_output.mkdir(parents=True, exist_ok=True)

        written: list[str] = []
        rejected: list[tuple[str, str]] = []
        stubs_written: list[str] = []
        stubs_skipped: list[str] = []
        stubs_rejected: list[tuple[str, str]] = []

        for item in judgment_envelope.items:
            criterion_id = item.id

            # ------------------------------------------------------------------
            # Handle the collection-green stub-planning item separately.
            # Only processed when --collection-green is active on this invocation.
            # ------------------------------------------------------------------
            if criterion_id == _COLLECTION_GREEN_STUB_ID:
                if not collection_green:
                    # Flag not active — silently ignore the judgment.
                    continue
                # The response must be a JSON array of {"path", "content"} objects.
                raw_response = item.response
                if isinstance(raw_response, str):
                    try:
                        raw_response = json.loads(raw_response)
                    except json.JSONDecodeError:
                        click.echo(
                            "  WARNING: collection-green stub judgment is not valid JSON;"
                            " skipping.",
                            err=True,
                        )
                        continue
                if not isinstance(raw_response, list):
                    # Wrap a single dict in a list for convenience.
                    raw_response = [raw_response] if isinstance(raw_response, dict) else []
                for entry in raw_response:
                    if not isinstance(entry, dict):
                        click.echo(
                            f"  WARNING: stub entry is not a dict ({entry!r}); skipping.",
                            err=True,
                        )
                        continue
                    stub_path_raw = entry.get("path", "")
                    stub_content = entry.get("content", "")
                    if not stub_path_raw:
                        click.echo(
                            "  WARNING: stub entry missing 'path'; skipping.",
                            err=True,
                        )
                        continue
                    # Safety rail 1 — containment guard.
                    if not _is_containment_safe(stub_path_raw, root):
                        click.echo(
                            f"  WARNING: stub path {stub_path_raw!r} is unsafe "
                            "(absolute or contains '..'); rejected.",
                            err=True,
                        )
                        stubs_rejected.append((stub_path_raw, "unsafe path"))
                        continue
                    stub_dest = root / Path(stub_path_raw)
                    # Safety rail 2 — NEVER overwrite an existing file.
                    if stub_dest.exists():
                        click.echo(
                            f"  SKIPPED stub {stub_path_raw!r}: file already exists.",
                        )
                        stubs_skipped.append(stub_path_raw)
                        continue
                    # Safety rail 3 — prepend marked header comment.
                    header = _stub_header(stub_dest)
                    stub_dest.parent.mkdir(parents=True, exist_ok=True)
                    stub_dest.write_text(header + stub_content, encoding="utf-8")
                    stubs_written.append(stub_path_raw)
                    click.echo(f"  STUB  {stub_dest}")
                continue  # done with the collection-green item

            content = item.response if isinstance(item.response, str) else str(item.response)

            filename = _output_filename(stack_name, criterion_id)

            # Containment guard — the filename must not escape base_output.
            if ".." in Path(filename).parts or Path(filename).is_absolute():
                click.echo(
                    f"  WARNING: rejected criterion {criterion_id!r}: "
                    f"unsafe filename {filename!r}.",
                    err=True,
                )
                rejected.append((criterion_id, "unsafe filename"))
                continue

            dest = base_output / filename
            try:
                dest.resolve().relative_to(base_output.resolve())
            except ValueError:
                click.echo(
                    f"  WARNING: rejected criterion {criterion_id!r}: path escapes output dir.",
                    err=True,
                )
                rejected.append((criterion_id, "path escapes output dir"))
                continue

            # Write the candidate file, run Stage-D, then accept or reject.
            dest.write_text(content, encoding="utf-8")
            check = engine.check_test_files([dest], result.public_api)

            theater_findings = [f for f in check.findings if f.kind == "test_theater"]
            unknown_findings = [f for f in check.findings if f.kind == "unknown_symbol"]

            if theater_findings:
                dest.unlink()
                detail = "; ".join(f.detail for f in theater_findings)
                click.echo(f"  REJECTED {criterion_id}: {detail}", err=True)
                rejected.append((criterion_id, detail))
            else:
                for uf in unknown_findings:
                    click.echo(f"  WARNING {criterion_id}: {uf.detail}", err=True)
                written.append(str(dest))
                click.echo(f"  WROTE {dest}")

        click.echo(f"\ntest-from-spec: {len(written)} written, {len(rejected)} rejected.")
        if rejected:
            for cid, reason in rejected:
                click.echo(f"  rejected: {cid} — {reason}")
        if collection_green:
            click.echo(
                f"collection-green stubs: {len(stubs_written)} written, "
                f"{len(stubs_skipped)} skipped (exists), "
                f"{len(stubs_rejected)} rejected."
            )
            for sp in stubs_skipped:
                click.echo(f"  skipped (exists): {sp}")
        return
