"""M6 ``/mod-synth-logs`` Phase C — render + execute the generator script.

This module is the Phase C orchestrator. It bridges the Phase B output
(:class:`ScenarioPlan` + :class:`EntitiesDigest`) to the on-disk
``generate_logs.py`` script the consumer commits — and (optionally) to a
fresh run of that script so the ten log files land under
``output_dir`` in a single ``forge-validate mod-synth-logs`` invocation.

Three pieces:

1. :func:`render_script` — pure render-and-write. Delegates the template
   work to :func:`primitives.synth_logs.shared.render._render_template`
   (PR #495 + #499 + #503), then writes the rendered source to
   ``output_path / "generate_logs.py"``. The output path is created if
   it doesn't already exist.
2. :func:`execute_script_subprocess` /
   :func:`execute_script_import` — two ways to run the just-rendered
   script. The subprocess form is the default (matches what a consumer
   running ``python generate_logs.py`` on their own machine gets); the
   import form skips ``sys.executable`` startup cost for fast in-process
   tests where subprocess overhead matters.
3. :func:`run_generate` — top-level orchestrator. Renders, then
   dispatches by ``run_script`` mode. ``run_script="skip"`` is the
   render-without-execute branch the CLI offers via ``--run-script
   skip``; it returns an :class:`ExecutionResult` with ``exit_code=-1``
   and an empty ``generated_files`` list so the CLI can still write a
   manifest pointing at the rendered (but unexecuted) script.

The subprocess invocation uses the argv-list form (charter §SEC-8) —
no shell, no string concatenation. The import invocation uses
:func:`importlib.util.spec_from_file_location` so a sibling
``generate_logs.py`` on the consumer's ``sys.path`` cannot shadow the
one we just rendered.

Design spec §M6 Phase C; SPEC.md §"CLI command" ``--run-script``.
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from primitives.synth_logs.shared.render import _render_template

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.synth_logs.shared.models import (
        EntitiesDigest,
        ScenarioPlan,
    )

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

#: The three execution modes ``run_generate`` accepts. Mirrors the
#: ``forge-validate mod-synth-logs --run-script`` Click choices.
RunScriptMode = Literal["subprocess", "import", "skip"]

_VALID_RUN_SCRIPT_MODES: frozenset[str] = frozenset({"subprocess", "import", "skip"})

#: Name of the file :func:`render_script` writes. Pinned as a module
#: constant so a tested `output_path` directory's invariant ("contains
#: exactly one ``generate_logs.py``") stays straightforward to assert.
SCRIPT_FILENAME: str = "generate_logs.py"


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of one Phase C execute step.

    Returned by every ``execute_script_*`` and by :func:`run_generate`.
    Frozen so a caller can stuff the instance directly into the manifest
    without worrying about post-construction mutation.

    Attributes:
        exit_code: ``0`` on success. The subprocess form forwards
            ``returncode`` verbatim. The import form translates a
            ``main()`` return value of ``None`` to ``0``. ``-1`` is
            reserved for the ``run_script="skip"`` branch — the script
            was rendered but never executed, so no real exit code
            exists.
        stdout: Captured ``stdout`` from the script's run. Empty string
            on ``run_script="skip"``.
        stderr: Captured ``stderr`` from the script's run. Empty string
            on ``run_script="skip"``.
        generated_files: Every file the script wrote under
            ``output_dir`` (recursive). Empty list on
            ``run_script="skip"``. Ordering is deterministic (sorted
            lexicographically) so two runs against the same plan + seed
            produce byte-identical manifests.
    """

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    generated_files: list[Path] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Step 1 — render-and-write
# ---------------------------------------------------------------------------


def render_script(
    scenario_plan: ScenarioPlan,
    entities_digest: EntitiesDigest,
    seed: int,
    output_path: Path,
) -> Path:
    """Render ``generate_logs.py`` and write it under ``output_path``.

    The Phase B outputs are baked into the rendered script as
    ``SCENARIO_PLAN`` / ``ENTITIES_DIGEST`` module-level constants (see
    :func:`primitives.synth_logs.shared.render._render_template`); the
    seed becomes the rendered ``--seed`` argparse default. ``output_path``
    is created (``parents=True, exist_ok=True``) so a caller working
    against a fresh ``tmp_path`` doesn't need to mkdir before us.

    The written file uses ``encoding="utf-8"`` and ``newline="\\n"`` so
    the byte stream is identical across Windows and POSIX hosts (the
    determinism test in ``tests/test_synth_logs_generate.py`` relies on
    this).

    Args:
        scenario_plan: Phase B output. Drives the dispatch loop in the
            rendered script.
        entities_digest: Phase A output. Anti-hallucination ground
            truth baked into the script for every renderer.
        seed: Deterministic seed baked as the rendered script's
            ``--seed`` default.
        output_path: Directory the script is written into. Created if
            absent. The script lands at ``output_path / "generate_logs.py"``.

    Returns:
        The absolute path of the written script. A caller can pass this
        verbatim to :func:`execute_script_subprocess` /
        :func:`execute_script_import`.
    """
    output_path.mkdir(parents=True, exist_ok=True)
    rendered = _render_template(scenario_plan, entities_digest, seed)
    script_path = output_path / SCRIPT_FILENAME
    # Pinned newline so the byte stream is host-independent — see the
    # determinism test in tests/test_synth_logs_generate.py.
    script_path.write_text(rendered, encoding="utf-8", newline="\n")
    return script_path


# ---------------------------------------------------------------------------
# Step 2 — execute
# ---------------------------------------------------------------------------


def execute_script_subprocess(
    script_path: Path,
    output_dir: Path,
    seed: int,
) -> ExecutionResult:
    """Run the rendered script as a subprocess.

    Uses ``[sys.executable, str(script_path), "--output-dir",
    str(output_dir), "--seed", str(seed)]`` — argv-list form, no shell,
    no string concatenation (charter §SEC-8). The interpreter that ran
    foundry runs the rendered script too, so virtualenv lookups stay
    consistent.

    Args:
        script_path: Absolute path to ``generate_logs.py`` (the return
            value of :func:`render_script`).
        output_dir: Where the script writes its log files. Created if
            absent so the script's own ``mkdir(parents=True,
            exist_ok=True)`` always sees a writable target.
        seed: Forwarded to the script's ``--seed`` flag. Wins over the
            baked-at-render-time default.

    Returns:
        An :class:`ExecutionResult` with the subprocess's return code,
        captured stdout / stderr, and the sorted list of files the
        script created under ``output_dir`` (recursive).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd: list[str] = [
        sys.executable,
        str(script_path),
        "--output-dir",
        str(output_dir),
        "--seed",
        str(seed),
    ]
    # PYTHONHASHSEED=0 disables the per-process randomisation of
    # ``hash()`` so the rendered script's per-file RNG derivation
    # (``base_rng.randint(...) ^ hash(file_name)``) produces the same
    # byte stream across both subprocess runs and import-mode runs.
    # Cross-mode determinism is part of the M6 contract — see the
    # ``test_subprocess_and_import_modes_produce_same_files`` regression
    # test. Inheriting the rest of the parent's environment keeps PATH
    # and friends intact so the subprocess can still find its
    # interpreter.
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    completed = subprocess.run(  # noqa: S603 (argv-list form, charter §SEC-8)
        cmd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return ExecutionResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        generated_files=_collect_generated_files(output_dir),
    )


def execute_script_import(
    script_path: Path,
    output_dir: Path,
    seed: int,
) -> ExecutionResult:
    """Run the rendered script in-process via :mod:`importlib`.

    Faster than the subprocess form (no ``sys.executable`` startup) so
    tests that exercise the orchestrator hundreds of times pay
    cents-per-run instead of dollars. We import the file directly via
    :func:`importlib.util.spec_from_file_location` so a sibling
    ``generate_logs.py`` on the consumer's ``sys.path`` cannot shadow
    the rendered one.

    The rendered script's ``main()`` accepts ``argv`` as a list, mirroring
    the subprocess form's command-line surface; we hand it
    ``["--output-dir", str(output_dir), "--seed", str(seed)]`` so the
    two paths produce byte-identical output for the same inputs.

    ``stdout`` / ``stderr`` are captured via
    :func:`contextlib.redirect_stdout` / :func:`contextlib.redirect_stderr`
    so the orchestrator can surface them through the same
    :class:`ExecutionResult` field as the subprocess form. Exceptions
    raised by ``main()`` are *not* swallowed — they propagate so the
    caller sees a real Python traceback instead of an opaque non-zero
    exit code.

    Args:
        script_path: Absolute path to ``generate_logs.py`` (the return
            value of :func:`render_script`).
        output_dir: Where the script writes its log files. Created if
            absent.
        seed: Forwarded to the script's ``--seed`` flag.

    Returns:
        An :class:`ExecutionResult`. ``exit_code`` is whatever
        ``main()`` returned (``None`` is normalised to ``0`` — matching
        ``sys.exit(None)`` semantics).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    module_name = "_forge_foundry_synth_logs_generated"

    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"Could not build an import spec for {script_path!r}; "
            "the rendered script is missing or unreadable."
        )

    module = importlib.util.module_from_spec(spec)
    # Drop the module from sys.modules after the run so two back-to-back
    # ``execute_script_import`` calls don't share state. A real failure
    # surfaces an immediate AttributeError on the second call instead of
    # silently re-using stale baked constants.
    spec.loader.exec_module(module)
    main_fn = getattr(module, "main", None)
    if main_fn is None or not callable(main_fn):
        sys.modules.pop(module_name, None)
        raise RuntimeError(
            f"Rendered script at {script_path!r} has no callable `main`; "
            "the M6 template skeleton always emits one — check render.py."
        )

    argv = ["--output-dir", str(output_dir), "--seed", str(seed)]
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    try:
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            raw_exit_code = main_fn(argv)
    finally:
        sys.modules.pop(module_name, None)

    return ExecutionResult(
        exit_code=0 if raw_exit_code is None else int(raw_exit_code),
        stdout=stdout_buf.getvalue(),
        stderr=stderr_buf.getvalue(),
        generated_files=_collect_generated_files(output_dir),
    )


# ---------------------------------------------------------------------------
# Step 3 — top-level orchestrator
# ---------------------------------------------------------------------------


def run_generate(
    scenario_plan: ScenarioPlan,
    entities_digest: EntitiesDigest,
    *,
    seed: int = 42,
    output_dir: Path,
    run_script: RunScriptMode = "subprocess",
) -> ExecutionResult:
    """Render ``generate_logs.py`` then dispatch by ``run_script`` mode.

    ``output_dir`` plays double duty: it is both where the rendered
    script lands *and* where the script writes its log files. This is
    the same convention the BankDemo reference
    ``synth-logs-generate.md`` slash command follows — committing the
    script alongside the logs it produced means a fresh checkout is a
    full reproduction kit.

    Args:
        scenario_plan: Phase B output, passed through to
            :func:`render_script`.
        entities_digest: Phase A output, passed through to
            :func:`render_script`.
        seed: Deterministic seed. Baked as the script's ``--seed``
            default and forwarded as the runtime ``--seed`` so the
            ``ScenarioPlan.seed`` and the orchestrator's ``seed``
            don't drift.
        output_dir: Directory the rendered script lands in *and* the
            directory the script writes its log files to. Created if
            absent.
        run_script: Execution mode. ``"subprocess"`` (default) matches
            what a consumer running the script themselves gets.
            ``"import"`` skips ``sys.executable`` startup for fast
            in-process tests. ``"skip"`` renders without executing — the
            returned :class:`ExecutionResult` has ``exit_code=-1`` and
            no generated files, useful for testing the render in
            isolation.

    Returns:
        An :class:`ExecutionResult`. The ``generated_files`` list does
        *not* include the rendered ``generate_logs.py`` itself — only
        log files the script emitted. (The script path is recoverable
        from ``output_dir / SCRIPT_FILENAME``.)

    Raises:
        ValueError: ``run_script`` is not one of ``"subprocess"``,
            ``"import"``, or ``"skip"``. The CLI layer's Click
            ``choices=`` already gates this at parse time, so a
            ``ValueError`` here means a programmatic caller passed
            something Click would have rejected.
    """
    if run_script not in _VALID_RUN_SCRIPT_MODES:
        raise ValueError(
            f"Unknown run_script mode: {run_script!r}. "
            f"Expected one of {sorted(_VALID_RUN_SCRIPT_MODES)}."
        )

    script_path = render_script(scenario_plan, entities_digest, seed, output_dir)

    if run_script == "skip":
        return ExecutionResult(exit_code=-1)

    if run_script == "subprocess":
        return execute_script_subprocess(script_path, output_dir, seed)

    # run_script == "import"
    return execute_script_import(script_path, output_dir, seed)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _collect_generated_files(output_dir: Path) -> list[Path]:
    """Return every file under ``output_dir`` except the rendered script.

    Used by the two ``execute_script_*`` functions to populate
    :attr:`ExecutionResult.generated_files`. Three classes of file are
    excluded:

    * The rendered ``generate_logs.py`` itself (it's the
      orchestrator's input, not an output the script wrote).
    * Anything under a ``__pycache__`` directory (the import form's
      ``importlib`` machinery writes ``.pyc`` artefacts there; the
      subprocess form doesn't, so excluding them is what keeps the
      cross-mode file-set parity contract clean).
    * The path itself if it is not a regular file.

    Returns the list sorted lexicographically so determinism tests can
    compare two runs byte-for-byte without re-sorting at the call site.
    """
    script_path = output_dir / SCRIPT_FILENAME
    return sorted(
        p
        for p in output_dir.rglob("*")
        if p.is_file() and p.resolve() != script_path.resolve() and "__pycache__" not in p.parts
    )


__all__ = [
    "SCRIPT_FILENAME",
    "ExecutionResult",
    "RunScriptMode",
    "execute_script_import",
    "execute_script_subprocess",
    "render_script",
    "run_generate",
]
