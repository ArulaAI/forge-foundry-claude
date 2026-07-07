"""CI emitter — ``primitives/<name>/ci.yaml`` → GH Actions / Azure DevOps / CodeBuild.

Phase 2 (v3): CI is emitted at sync time from one dialect-neutral source instead
of three hand-maintained templates per primitive. Design:
``docs/superpowers/specs/2026-06-14-phase2-ci-emitter-design.md``.

2026-06-18 harness-layer-removal (§3): the run step no longer shells
``forge-validate <cmd> --harness <name>`` (which spawned an agent session inside
an agent session). Instead it invokes the **agent CLI** (``claude -p ...``,
``copilot-cli -p ...``, …) which loads the primitive's SKILL.md and runs the
extract→judge→continue protocol in-session — the agent IS the harness. The skill
body still drives ``forge-validate`` for its deterministic stages, so the
workflow keeps checkout + Python/uv setup + input validation + SARIF upload. This
module renders that pipeline; it never emits a step whose behaviour the emitter
*itself* depends on model output for, and the inputs it threads were already
sanitised at load time (``loader.load_canonical_ci``).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from forge_sync.emitter.models import (
    CI_AGENT_CLI,
    CI_CONTEXT_DIR,
    CI_DEFAULT_HARNESS,
    CI_OUTPUT_EXT,
    CI_PROVISIONAL_HARNESSES,
)

if TYPE_CHECKING:
    from forge_sync.emitter.models import CanonicalCI, CanonicalCIInput

__all__ = ["emit_ci"]


def _env(name: str) -> str:
    """ci.yaml input name → the UPPER_SNAKE env var the shell references."""
    return name.upper().replace("-", "_")


def _yq(value: str) -> str:
    """Single-quote a scalar for YAML (doubling any embedded single quote)."""
    return "'" + value.replace("'", "''") + "'"


def _output_file(ci: CanonicalCI) -> str:
    """The single output filename (``file`` mode only — caller guards on output_mode)."""
    ext = CI_OUTPUT_EXT[ci.output_format] if ci.output_format else "out"
    return f"{ci.command}.{ext}"


def _is_sarif(ci: CanonicalCI) -> bool:
    return ci.output_mode == "file" and ci.output_format == "sarif"


# ---------------------------------------------------------------------------
# Shared bash — identical across all three dialects.
# ---------------------------------------------------------------------------


def _one_input_validation(env: str, anchored: str, msg: str, nl_msg: str, error: str) -> list[str]:
    """Fail-closed shell lines validating one input value held in ``$<env>``.

    *anchored* is the already-``^(...)$``-wrapped regex; *msg*/*nl_msg* are the
    regex-mismatch and multi-line error messages; *error* is the dialect's
    printf-style error template (one ``%s``).

    #806: a multi-line value whose first line is valid would slip past the
    per-line ``grep -q``, smuggling later lines (newlines, ``;``, ``$(...)``,
    prose) into the PROMPT — a prompt-injection vector. We reject embedded control
    characters *before* the grep so the anchored ``^(...)$`` match governs the
    whole single-line value.

    Portability (#841 re-audit LOW): the control-char rejection is a POSIX
    before/after byte-length compare (``wc -c`` ± ``tr -d '[:cntrl:]'``) — it works
    identically under ``sh``/``dash`` and ``bash``. (The prior ``case "$X" in
    *$'\\n'*`` relied on bash ANSI-C quoting, which under POSIX ``dash`` matches
    neither a real newline nor the literal text — silently no-opping the guard and
    reopening the bypass.)
    """
    return [
        # Reject ANY control character (covers LF, CR, NUL, …) so a multi-line value
        # can't smuggle later lines past the per-line ``grep -q``. We compare the byte
        # length before vs. after deleting control bytes (``tr -d``); a shrink means at
        # least one was present. This is fully POSIX — identical under ``sh``/``dash``
        # and ``bash`` — unlike the prior bash-only ``case "$X" in *$'\\n'*`` ANSI-C glob,
        # and unlike a ``$(… | tr -dc …)`` test (command substitution strips trailing
        # newlines, silently passing a value whose only control char is a trailing LF).
        # ``LC_ALL=C`` pins ``[:cntrl:]`` to the byte class; ``-ne`` is a numeric compare
        # so BSD ``wc``'s leading-space padding is harmless.
        f'if [ "$(printf \'%s\' "${env}" | wc -c)" -ne '
        f"\"$(printf '%s' \"${env}\" | LC_ALL=C tr -d '[:cntrl:]' | wc -c)\" ]; then",
        f"  {error % nl_msg}",
        "  exit 1",
        "fi",
        f"if ! printf '%s' \"${env}\" | grep -qE '{anchored}'; then",
        f"  {error % msg}",
        "  exit 1",
        "fi",
    ]


def _validation_lines(ci: CanonicalCI, error: str, *, validate_choices: bool = False) -> list[str]:
    """Shell lines that fail-closed if any input violates its regex / closed set.

    *error* is a printf-style template with one ``%s`` for the message, rendered
    in the dialect's error syntax (``::error::`` / ``##vso[...]`` / plain stderr).

    By default only inputs carrying a ``validate`` regex are checked — for GH
    Actions / Azure the ``choice``/``bool`` inputs are constrained at the workflow
    layer (``options:`` / ``values:``) so they never reach the shell unconstrained.

    *validate_choices* (CodeBuild only — #841 re-audit LOW): CodeBuild has no input
    layer, so ``choice``/``bool`` values are plain ``env: variables:`` an operator
    can override at ``start-build`` — flowing unvalidated into the PROMPT. When set,
    this also emits anchored validation for ``choice`` (against its closed option
    set) and ``bool`` (against ``true|false``) inputs, closing the GH/Azure↔CodeBuild
    asymmetry. GH/Azure leave it ``False``.

    SEC-8: the inner pattern is anchored ``^(...)$``; the value comes from env and
    is never interpolated, and the (already-rejected) value is deliberately NOT
    echoed into the error — interpolating it into an annotation would be an
    injection vector.
    """
    lines: list[str] = []
    for inp in ci.inputs:
        if inp.validate is not None:
            pattern = inp.validate
        elif validate_choices and inp.type == "choice":
            # Validate against the closed option set (regex-escape each member).
            pattern = "|".join(re.escape(c) for c in inp.choices)
        elif validate_choices and inp.type == "bool":
            pattern = "true|false"
        else:
            continue
        env = _env(inp.name)
        anchored = f"^({pattern})$"
        msg = f"{inp.name} must match {pattern}"
        nl_msg = f"{inp.name} must be single-line"
        lines += _one_input_validation(env, anchored, msg, nl_msg, error)
    return lines


def _run_lines(ci: CanonicalCI, output_path: str, harness: str) -> list[str]:
    """Build the PROMPT + agent-CLI invocation (2026-06-18 harness-layer-removal §3.3).

    The run step assembles a natural-language PROMPT from the workflow inputs, then
    invokes the agent CLI for *harness*. The agent loads the primitive's SKILL.md
    and runs the extract→judge→continue protocol — there is no ``--harness`` flag
    and no ``forge-validate`` invocation in the emitted run line (the skill body
    drives ``forge-validate`` for its deterministic stages, hence the workflow still
    sets up Python + uv).

    What the prompt records about output depends on ``output_mode``:
    - ``file``: ``Output format: <fmt>. Output file: <output_path>.``
    - ``dir``: ``Output format: <fmt>.`` only when an ``output_format`` is set. The
      primitive's output *directory* is supplied through whichever of its own
      declared inputs carries it; the CI publish step always collects artifacts
      from the fixed ``CI_CONTEXT_DIR`` (``.foundry/context``).
    - ``none``: neither — the primitive self-manages format + destination.
    """
    positionals = [f"${{{_env(i.name)}}}" for i in ci.inputs if i.arg == "positional"]
    # ``ci.skill`` is load-time-constrained to ``^/[A-Za-z0-9][A-Za-z0-9._-]*$`` and the
    # env refs are the only interpolation — no shell metacharacters reach this string.
    # Only add the ``on <target>`` clause when the primitive declares a positional input;
    # a target-less primitive (e.g. confidence-gate) just gets ``Run /<skill>.``.
    if positionals:
        lines = [f'PROMPT="Run {ci.skill} on {" ".join(positionals)}."']
    else:
        lines = [f'PROMPT="Run {ci.skill}."']
    for inp in ci.inputs:
        lines += _prompt_lines(inp)
    if ci.output_mode == "file":
        lines.append(
            f'PROMPT="$PROMPT Output format: {ci.output_format}. Output file: {output_path}."'
        )
    elif ci.output_mode == "dir" and ci.output_format is not None:
        lines.append(f'PROMPT="$PROMPT Output format: {ci.output_format}."')
    if harness in CI_PROVISIONAL_HARNESSES:
        lines.append(
            f"# NOTE: the {harness} CLI invocation below is provisional — confirm the "
            f"{harness} agent-CLI binary name and its prompt/headless flags against your "
            f"installed {harness} version before enabling this workflow."
        )
    lines.append(CI_AGENT_CLI[harness].format(prompt='"$PROMPT"'))
    return lines


def _prompt_lines(inp: CanonicalCIInput) -> list[str]:
    """Append one non-positional input to the PROMPT (conditionally, mirroring omit_when)."""
    if inp.arg == "positional":
        return []
    env = _env(inp.name)
    label = inp.name.replace("_", " ").capitalize()
    if inp.arg == "flag-when-true":  # bool → a directive sentence only when true
        flag = f"--{inp.name.replace('_', '-')}"
        return [f'if [ "${env}" = "true" ]; then PROMPT="$PROMPT Pass {flag}."; fi']
    # "--flag-name" with a value → a labelled prompt sentence (the flag name itself is
    # not threaded; the agent maps the label to the slash-command option).
    sentence = f'PROMPT="$PROMPT {label}: ${{{env}}}."'
    if inp.omit_when is None:
        return [sentence]
    if inp.omit_when == "":
        return [f'if [ -n "${env}" ]; then {sentence}; fi']
    return [f'if [ "${env}" != "{inp.omit_when}" ]; then {sentence}; fi']


def _indent(lines: list[str], spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + ln for ln in lines)


# ---------------------------------------------------------------------------
# Dialect renderers.
# ---------------------------------------------------------------------------

_BANNER = (
    "# {name} — EMITTED from the primitive's ci.yaml by forge-foundry-sync. DO NOT EDIT.\n"
    "# DORMANT: manual trigger only; the consumer opts into push/pull_request.\n"
)

#: GitHub Actions banner — adds the concrete enablement recipe (#823).
#: Literal braces are escaped (``{{``/``}}``) because the banner is fed through ``str.format``.
_BANNER_GH = (
    _BANNER + "# To enable on pull requests: under `on:` below, replace the `workflow_dispatch`\n"
    "#   key with a `pull_request` trigger key, and add a top-level `permissions:` block\n"
    "#   with `contents: read` (least privilege — widen only if this primitive writes back).\n"
)

#: Azure DevOps banner — Azure's equivalent of opting into PR triggers (#823).
_BANNER_AZURE = (
    _BANNER + "# To enable on pull requests: replace `trigger: none` below with a `pr:` block\n"
    "#   that lists the branches to build (e.g. a `pr:` -> `branches:` -> `include:` list).\n"
)

#: CodeBuild banner — buildspec triggering is owned by the project/webhook (#823).
_BANNER_CODEBUILD = (
    _BANNER + "# To enable: wire this buildspec to a CodeBuild project with a source webhook\n"
    "#   filtered to PULL_REQUEST events (the buildspec itself carries no trigger).\n"
)


def _gh_type(inp: CanonicalCIInput) -> str:
    return {"string": "string", "choice": "choice", "bool": "boolean"}[inp.type]


def _gh_publish(ci: CanonicalCI) -> str:
    if _is_sarif(ci):
        return (
            "\n      - name: Upload SARIF\n"
            "        uses: github/codeql-action/upload-sarif@v3\n"
            "        if: always()\n        with:\n"
            f"          sarif_file: {_output_file(ci)}\n"
        )
    path = _output_file(ci) if ci.output_mode == "file" else CI_CONTEXT_DIR
    return (
        "\n      - name: Upload artifact\n"
        "        uses: actions/upload-artifact@v4\n"
        "        if: always()\n        with:\n"
        f"          name: {ci.command}-output\n"
        f"          path: {path}\n"
    )


def _azure_publish(ci: CanonicalCI) -> str:
    path = (
        f"$(Build.ArtifactStagingDirectory)/{_output_file(ci)}"
        if ci.output_mode == "file"
        else CI_CONTEXT_DIR
    )
    return (
        "\n  - task: PublishBuildArtifacts@1\n    inputs:\n"
        f"      pathToPublish: '{path}'\n"
        f"      artifactName: {ci.command}-output\n"
    )


def _cb_publish(ci: CanonicalCI) -> str:
    files = _output_file(ci) if ci.output_mode == "file" else f"{CI_CONTEXT_DIR}/**/*"
    return f"\nartifacts:\n  files:\n    - {files}\n  name: {ci.command}-report\n"


def _emit_github(ci: CanonicalCI, harness: str) -> str:
    out_file = _output_file(ci)
    inputs_yaml: list[str] = []
    for inp in ci.inputs:
        inputs_yaml.append(f"      {inp.name}:")
        inputs_yaml.append(f"        description: {_yq(inp.description)}")
        inputs_yaml.append(f"        required: {str(inp.required).lower()}")
        inputs_yaml.append(f"        default: {_yq(inp.default)}")
        inputs_yaml.append(f"        type: {_gh_type(inp)}")
        if inp.type == "choice":
            inputs_yaml.append("        options:")
            inputs_yaml += [f"          - {c}" for c in inp.choices]
    env_yaml = [f"          {_env(i.name)}: ${{{{ inputs.{i.name} }}}}" for i in ci.inputs]
    perms = "\n".join(f"  {k}: {v}" for k, v in ci.permissions.items()) or "  contents: read"
    validate = _indent(_validation_lines(ci, "echo '::error::%s' >&2"), 10)
    run = _indent(_run_lines(ci, out_file, harness), 10)
    publish = _gh_publish(ci)
    return (
        _BANNER_GH.format(name=ci.display_name, cmd=ci.command)
        + f"name: {_yq(ci.display_name)}\n\n"
        "on:\n  workflow_dispatch:\n    inputs:\n"
        + "\n".join(inputs_yaml)
        + f"\n\npermissions:\n{perms}\n\n"
        f"jobs:\n  {ci.command}:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@v4\n        with:\n          fetch-depth: 0\n"
        "      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.12'\n"
        "      - run: pip install uv\n"
        "      - run: uv sync\n"
        "      - name: Validate inputs\n        env:\n"
        + "\n".join(env_yaml)
        + "\n        run: |\n          set -euo pipefail\n"
        + validate
        + f"\n      - name: Run {ci.command}\n        env:\n"
        + "\n".join(env_yaml)
        + "\n        run: |\n          set -euo pipefail\n"
        + run
        + "\n"
        + publish
    )


def _emit_azure(ci: CanonicalCI, harness: str) -> str:
    out_file = f"$(Build.ArtifactStagingDirectory)/{_output_file(ci)}"
    params: list[str] = []
    for inp in ci.inputs:
        params.append(f"  - name: {inp.name}")
        params.append(f"    type: {'string' if inp.type != 'bool' else 'boolean'}")
        params.append(f"    default: {_yq(inp.default)}")
        if inp.type == "choice":
            params.append("    values:")
            params += [f"      - {c}" for c in inp.choices]
    env_yaml = [f"      {_env(i.name)}: ${{{{ parameters.{i.name} }}}}" for i in ci.inputs]
    validate = _indent(_validation_lines(ci, "echo '##vso[task.logissue type=error]%s'"), 6)
    run = _indent(_run_lines(ci, out_file, harness), 6)
    publish = _azure_publish(ci)
    return (
        _BANNER_AZURE.format(name=ci.display_name, cmd=ci.command)
        + "\ntrigger: none\n\nparameters:\n"
        + "\n".join(params)
        + "\n\npool:\n  vmImage: 'ubuntu-latest'\n\nsteps:\n"
        "  - task: UsePythonVersion@0\n    inputs:\n      versionSpec: '3.12'\n"
        "  - script: pip install uv && uv sync\n    displayName: Install dependencies\n"
        "  - script: |\n      set -euo pipefail\n"
        + validate
        + "\n    displayName: Validate inputs\n    env:\n"
        + "\n".join(env_yaml)
        + "\n  - script: |\n      set -euo pipefail\n"
        + run
        + f"\n    displayName: Run {ci.command}\n    env:\n"
        + "\n".join(env_yaml)
        + "\n"
        + publish
    )


def _emit_codebuild(ci: CanonicalCI, harness: str) -> str:
    out_file = _output_file(ci)
    variables = "\n".join(f"    {_env(i.name)}: {_yq(i.default)}" for i in ci.inputs)
    # CodeBuild has no input layer (#841): choice/bool env vars are operator-overridable
    # at start-build, so validate them in-shell too (GH/Azure are workflow-constrained).
    validate = _indent(_validation_lines(ci, 'echo "%s" >&2', validate_choices=True), 8)
    run = _indent(_run_lines(ci, out_file, harness), 8)
    artifacts = _cb_publish(ci)
    return (
        _BANNER_CODEBUILD.format(name=ci.display_name, cmd=ci.command)
        + "\nversion: 0.2\n\nenv:\n  variables:\n"
        + variables
        + "\n\nphases:\n"
        "  install:\n    runtime-versions:\n      python: 3.12\n"
        "    commands:\n      - pip install uv\n      - uv sync\n"
        "  pre_build:\n    commands:\n      - |\n        set -euo pipefail\n"
        + validate
        + "\n  build:\n    commands:\n      - |\n        set -euo pipefail\n"
        + run
        + "\n"
        + artifacts
    )


def emit_ci(ci: CanonicalCI, harness: str = CI_DEFAULT_HARNESS) -> dict[str, str]:
    """Render *ci* to the three CI targets. Returns {project-relative path: content}.

    *harness* selects the agent-CLI invocation the run step emits (2026-06-18
    harness-layer-removal §3.2). It defaults to ``claude_code`` — the only target with
    confirmed CLI flags; the provisional ones (``copilot``/``codex``/``antigravity``)
    additionally emit a consumer-facing ``# NOTE: … provisional …`` comment naming what to
    confirm (the agent-CLI binary + prompt/headless flags). A bad name raises ``KeyError``.
    """
    if harness not in CI_AGENT_CLI:
        raise ValueError(f"emit_ci: unknown harness {harness!r}; valid: {sorted(CI_AGENT_CLI)}")
    return {
        f".github/workflows/forge-{ci.command}.yml": _emit_github(ci, harness),
        f"azure-pipelines/forge-{ci.command}.yml": _emit_azure(ci, harness),
        f"buildspec/forge-{ci.command}.yml": _emit_codebuild(ci, harness),
    }
