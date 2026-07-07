# tests/test_lock.py
import json
import subprocess
from pathlib import Path
import pytest
from scripts._vendor.lock import resolve_source, write_lock, read_lock, SourceInfo


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True)


def _make_repo(tmp_path) -> Path:
    r = tmp_path / "src"; r.mkdir()
    _git("init", "-q", cwd=r)
    _git("config", "user.email", "t@t", cwd=r)
    _git("config", "user.name", "t", cwd=r)
    (r / "f.txt").write_text("x")
    _git("add", "-A", cwd=r)
    _git("commit", "-qm", "init", cwd=r)
    _git("tag", "v1.0.0", cwd=r)
    return r


def test_resolve_source_reads_sha(tmp_path):
    r = _make_repo(tmp_path)
    info = resolve_source(r, "v1.0.0")
    assert len(info.sha) == 40 and info.dirty is False and info.ref == "v1.0.0"


def test_resolve_source_rejects_dirty(tmp_path):
    r = _make_repo(tmp_path)
    (r / "f.txt").write_text("dirty")
    with pytest.raises(RuntimeError, match="dirty"):
        resolve_source(r, "v1.0.0")


def test_write_and_read_lock(tmp_path):
    r = _make_repo(tmp_path)
    info = resolve_source(r, "v1.0.0")
    eng = tmp_path / "engine"; eng.mkdir()
    d = write_lock(eng, info, primitive_count=31, version="0.1.0")
    assert d["primitive_count"] == 31 and d["version"] == "0.1.0"
    assert read_lock(eng)["sha"] == info.sha
