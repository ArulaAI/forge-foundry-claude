# tests/test_lock.py
import subprocess
from pathlib import Path
import pytest
from scripts._vendor.lock import (
    append_vendor_history,
    read_lock,
    resolve_source,
    write_lock,
)


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True)


def _make_repo(tmp_path) -> Path:
    r = tmp_path / "src"
    r.mkdir()
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
    eng = tmp_path / "engine"
    eng.mkdir()
    d = write_lock(eng, info, primitive_count=31, version="0.1.0")
    assert d["primitive_count"] == 31 and d["version"] == "0.1.0"
    assert read_lock(eng)["sha"] == info.sha


def test_append_vendor_history_writes_line(tmp_path):
    cl = tmp_path / "CHANGELOG-VENDOR.md"
    cl.write_text("# Changelog\n\n## Vendor history\n\n<!-- placeholder -->\n")
    lock = {
        "vendored_at": "2026-07-07T12:22:05Z",
        "ref": "v5.0.2",
        "sha": "0f5f563588b21e0f5aef7e5a277d60a1e8f8b583",
        "primitive_count": 31,
    }
    line = append_vendor_history(cl, lock)
    assert line == "- 2026-07-07T12:22:05Z v5.0.2@0f5f563588b2 — 31 primitives"
    assert line + "\n" in cl.read_text()


def test_append_vendor_history_creates_section_when_missing(tmp_path):
    cl = tmp_path / "CHANGELOG-VENDOR.md"
    cl.write_text("# Changelog\n\nSome prose, no history section.\n")
    lock = {
        "vendored_at": "2026-07-07T00:00:00Z",
        "ref": "v6.0.0",
        "sha": "abcdef012345aaaa",
        "primitive_count": 30,
    }
    line = append_vendor_history(cl, lock)
    text = cl.read_text()
    assert "## Vendor history" in text
    assert line == "- 2026-07-07T00:00:00Z v6.0.0@abcdef012345 — 30 primitives"
    assert line + "\n" in text
