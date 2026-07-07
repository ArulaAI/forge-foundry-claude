import os
import subprocess


def test_shim_exists_and_executable(repo_root):
    shim = repo_root / "bin" / "forge-validate"
    assert shim.is_file()
    assert os.access(shim, os.X_OK)


def test_hook_exists_and_executable(repo_root):
    hook = repo_root / "hooks" / "bootstrap.sh"
    assert hook.is_file() and os.access(hook, os.X_OK)


def test_shim_exits_69_when_venv_absent(repo_root, tmp_path):
    shim = repo_root / "bin" / "forge-validate"
    # Point the SHA-keyed venv home at an empty tmp dir so the venv is guaranteed
    # absent regardless of the caller's ~/.cache state.
    env = dict(
        os.environ,
        CLAUDE_PLUGIN_ROOT=str(repo_root),
        FORGE_FOUNDRY_VENV_HOME=str(tmp_path / "venv-home"),
    )
    r = subprocess.run([str(shim), "repo-digest", "."], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 69
    assert "not bootstrapped" in r.stderr
