# tests/test_parity_cli_help.py
"""Strong parity guard: `forge-validate --help` imports EVERY registered command
module, so a green exit proves no primitive is missing a runtime-loaded reference
file (skill/references data catalogs, JSON schemas, etc.) across ALL primitives —
not just the two (C10/C11) we know crashed. Skips only if the venv genuinely
can't build in the sandbox (same discipline as test_smoke_plb.py)."""
import os
import subprocess

import pytest


@pytest.fixture(scope="module")
def bootstrapped(repo_root, tmp_path_factory):
    """Emulate the bootstrap hook into a scratch CLAUDE_PLUGIN_ROOT."""
    scratch = tmp_path_factory.mktemp("plugin")
    for name in ("engine", "bin", "hooks"):
        os.symlink(repo_root / name, scratch / name)
    env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(scratch))
    r = subprocess.run(["bash", str(repo_root / "hooks" / "bootstrap.sh")],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0 or not (
            scratch / "engine" / ".venv" / "bin" / "forge-validate").exists():
        pytest.skip(f"bootstrap could not build venv in this env: {r.stderr}")
    return scratch, env


def test_forge_validate_help_imports_every_command(repo_root, bootstrapped):
    """`--help` loads all command modules; if any primitive's Python fails to
    import a missing runtime data file, this exits nonzero."""
    scratch, env = bootstrapped
    shim = repo_root / "bin" / "forge-validate"
    r = subprocess.run([str(shim), "--help"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        f"forge-validate --help failed (exit {r.returncode}); a primitive is "
        f"likely missing a runtime-loaded reference file:\n{r.stderr}")


def test_failure_mode_audit_help(repo_root, bootstrapped):
    """C10 specifically loads failure_mode_rules.yaml from skill/references."""
    scratch, env = bootstrapped
    shim = repo_root / "bin" / "forge-validate"
    r = subprocess.run([str(shim), "failure-mode-audit", "--help"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
