# scripts/_vendor/lock.py
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class SourceInfo:
    repo: str
    ref: str
    sha: str
    dirty: bool


def _run(args: list[str], cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def resolve_source(src: Path, ref: str, *, allow_dirty: bool = False) -> SourceInfo:
    src = Path(src).resolve()
    if not (src / ".git").exists():
        raise RuntimeError(f"source is not a git repo: {src}")
    status = _run(["git", "status", "--porcelain"], src)
    dirty = bool(status)
    if dirty and not allow_dirty:
        raise RuntimeError(f"source tree is dirty; refusing to vendor (use --allow-dirty): {src}")
    sha = _run(["git", "rev-parse", ref], src)
    try:
        origin = _run(["git", "remote", "get-url", "origin"], src)
    except subprocess.CalledProcessError:
        origin = str(src)
    return SourceInfo(repo=origin, ref=ref, sha=sha, dirty=dirty)


def write_lock(engine_dir: Path, info: SourceInfo, *, primitive_count: int,
               version: str) -> dict:
    engine_dir = Path(engine_dir)
    engine_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "source_repo": info.repo,
        "ref": info.ref,
        "sha": info.sha,
        "vendored_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "primitive_count": primitive_count,
        "version": version,
    }
    (engine_dir / "VENDOR_LOCK.json").write_text(json.dumps(data, indent=2) + "\n")
    return data


def read_lock(engine_dir: Path) -> dict:
    return json.loads((Path(engine_dir) / "VENDOR_LOCK.json").read_text())


_HISTORY_HEADING = "## Vendor history"


def append_vendor_history(changelog: Path, lock: dict) -> str:
    """Append one history line to CHANGELOG-VENDOR.md from a VENDOR_LOCK dict.

    Format: ``- <vendored_at> <ref>@<sha[:12]> — <primitive_count> primitives``.

    The history log naturally grows: a new line is appended on every run. If the
    ``## Vendor history`` section is missing, it is created (with the placeholder
    comment) so the file stays well-formed. Returns the line that was appended.
    """
    changelog = Path(changelog)
    line = (
        f"- {lock['vendored_at']} {lock['ref']}@{lock['sha'][:12]} "
        f"— {lock['primitive_count']} primitives"
    )
    text = changelog.read_text() if changelog.exists() else ""
    if _HISTORY_HEADING not in text:
        prefix = text if text.endswith("\n") or text == "" else text + "\n"
        text = (
            prefix
            + f"\n{_HISTORY_HEADING}\n\n"
            + "<!-- vendor.py appends one line per re-vendor: "
            + "<date> <ref>@<sha12> — <primitive_count> primitives -->\n"
        )
    if not text.endswith("\n"):
        text += "\n"
    text += line + "\n"
    changelog.write_text(text)
    return line
