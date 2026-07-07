# tests/test_smoke_plb.py
import os
import subprocess
import pytest


@pytest.fixture(scope="module")
def bootstrapped(repo_root, tmp_path_factory):
    """Emulate the bootstrap hook into a scratch CLAUDE_PLUGIN_ROOT."""
    scratch = tmp_path_factory.mktemp("plugin")
    # symlink engine + bin + hooks into the scratch plugin root
    for name in ("engine", "bin", "hooks"):
        os.symlink(repo_root / name, scratch / name)
    env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(scratch))
    r = subprocess.run(["bash", str(repo_root / "hooks" / "bootstrap.sh")],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0 or not (scratch / "engine" / ".venv" / "bin" / "forge-validate").exists():
        pytest.skip(f"bootstrap could not build venv in this env: {r.stderr}")
    return scratch, env


def test_shim_runs_repo_digest_help(repo_root, bootstrapped):
    scratch, env = bootstrapped
    shim = repo_root / "bin" / "forge-validate"
    r = subprocess.run([str(shim), "repo-digest", "--help"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "repo-digest" in (r.stdout + r.stderr).lower()


def test_shim_runs_data_flow_map_help(repo_root, bootstrapped):
    scratch, env = bootstrapped
    shim = repo_root / "bin" / "forge-validate"
    r = subprocess.run([str(shim), "data-flow-map", "--help"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
