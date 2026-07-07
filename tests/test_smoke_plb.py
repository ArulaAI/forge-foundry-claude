# tests/test_smoke_plb.py
import json
import os
import subprocess
import pytest


@pytest.fixture(scope="module")
def bootstrapped(repo_root, tmp_path_factory):
    """Emulate the bootstrap hook into a scratch CLAUDE_PLUGIN_ROOT.

    The venv now lives at a SHA-keyed path under FORGE_FOUNDRY_VENV_HOME (outside
    the plugin root); point that at a scratch dir so the test is hermetic.
    """
    scratch = tmp_path_factory.mktemp("plugin")
    # symlink engine + bin + hooks into the scratch plugin root
    for name in ("engine", "bin", "hooks"):
        os.symlink(repo_root / name, scratch / name)
    venv_home = tmp_path_factory.mktemp("venv-home")
    env = dict(
        os.environ,
        CLAUDE_PLUGIN_ROOT=str(scratch),
        FORGE_FOUNDRY_VENV_HOME=str(venv_home),
    )
    r = subprocess.run(["bash", str(repo_root / "hooks" / "bootstrap.sh")],
                       env=env, capture_output=True, text=True)
    sha = json.loads((repo_root / "engine" / "VENDOR_LOCK.json").read_text())["sha"]
    venv_bin = venv_home / f"venv-{sha}" / "bin" / "forge-validate"
    if r.returncode != 0 or not venv_bin.exists():
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
