import subprocess
import sys


def test_engine_imports(repo_root):
    engine = repo_root / "engine"
    r = subprocess.run(
        [sys.executable, "-c",
         "import forge_validate.cli; "
         "import importlib; importlib.import_module('primitives._shared'); "
         "print('ok')"],
        cwd=engine, capture_output=True, text=True,
        env={"PYTHONPATH": str(engine), "PATH": __import__("os").environ["PATH"]})
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_vendor_lock_present(repo_root):
    import json
    lock = json.loads((repo_root / "engine" / "VENDOR_LOCK.json").read_text())
    assert len(lock["sha"]) == 40 and lock["primitive_count"] == 31
