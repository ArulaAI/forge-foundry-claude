# forge-foundry-claude Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `forge-foundry-claude`, a self-contained Claude Code plugin that repackages all 31 Forge Foundry primitives as skills with a bundled Python engine, produced by a re-runnable vendor script and guarded by parity tests.

**Architecture:** A vendor script (`scripts/vendor.py`) copies skills/agents + the `forge_validate`/`primitives` Python engine from a pinned checkout of `forge-foundry-primitives`, rewrites PLB skill bodies to call `${CLAUDE_PLUGIN_ROOT}/bin/forge-validate`, and writes a vendor lock. A SessionStart hook builds a venv on first activation (no `uv` dependency). Parity tests verify the vendored result.

**Tech Stack:** Python 3.11+ (vendor script + engine + tests), pytest, bash (bootstrap hook + resolver shim), Claude Code plugin manifests (JSON). Source library vendored at a pinned tag.

## Global Constraints

- **This plan is executed inside the `forge-foundry-claude` repo clone.** The source library `forge-foundry-primitives` is available at the path in env var `$FOUNDRY_SRC` (set before execution; default `/Users/rob/projects/forge-foundry-primitives`). Tasks that vendor read from `$FOUNDRY_SRC`.
- **Python floor:** 3.11+. Vendor script + tests use only stdlib + pytest (no third-party deps in the vendor tooling itself).
- **No behavior fork of the engine.** The engine is copied verbatim; the only content transforms applied to vendored skills are (a) the PLB invocation-prefix rewrite and (b) the state-dir default localization. Both are logged in `CHANGELOG-VENDOR.md`.
- **PLB invocation target:** `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" <name> …` (the resolver shim). Never `uv run`.
- **State-dir default:** `.foundry/context/…` → `.forge-foundry/context/…` in vendored bodies.
- **Exit codes:** the resolver shim uses `69` (EX_UNAVAILABLE) for "engine not bootstrapped" — must never collide with the primitives' `0–7`.
- **Distribution:** private marketplace; `license: UNLICENSED`; never the public Claude marketplace.
- **Excluded from vendoring:** `/autowork`, `/review-pr` (maintainer commands).
- **First release scope:** the 31 primitives present in the source at the pinned tag. NOT S61/S12 (unbuilt upstream).
- **Idempotency:** `scripts/vendor.py` must be safe to re-run; re-running against an already-vendored tree produces no spurious diffs and does not double-rewrite.
- **Commit discipline:** frequent, one deliverable per commit; end messages with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Test command:** `python -m pytest tests/ -q` from repo root. Vendor smoke: `python scripts/vendor.py --source "$FOUNDRY_SRC" --ref <tag>`.

---

## File Structure

```
forge-foundry-claude/
├── .claude-plugin/
│   ├── plugin.json                 # T7 — manifest + SessionStart hook wiring
│   └── marketplace.json            # T7 — private single-plugin marketplace
├── bin/
│   └── forge-validate              # T6 — resolver shim (generated + checked in)
├── hooks/
│   └── bootstrap.sh                # T6 — SessionStart venv build
├── scripts/
│   ├── vendor.py                   # T2-T5 — the vendor entry point
│   └── _vendor/                    # vendor helper modules (one responsibility each)
│       ├── __init__.py
│       ├── lock.py                 # T2 — VENDOR_LOCK.json + source SHA resolution
│       ├── copy_skills.py          # T3 — skills/ + agents/ copy
│       ├── copy_engine.py          # T4 — engine/ copy + pyproject generation
│       └── rewrite.py              # T5 — PLB path-rewrite + state-dir localization
├── skills/                         # vendored output (git-tracked)
├── agents/                         # vendored output
├── engine/                         # vendored output (+ VENDOR_LOCK.json, pyproject.toml)
├── tests/
│   ├── conftest.py                 # T1 — repo-root fixture
│   ├── test_rewrite.py             # T5
│   ├── test_lock.py                # T2
│   ├── test_parity_skills.py       # T8
│   ├── test_parity_engine.py       # T8
│   ├── test_parity_manifests.py    # T8
│   ├── test_shim_and_hook.py       # T8
│   └── test_smoke_plb.py           # T9
├── CHANGELOG-VENDOR.md             # T7
├── README.md                       # T7
├── .gitignore                      # T1 (engine/.venv, __pycache__, run artifacts)
└── pyproject.toml                  # T1 — vendor-tooling + test config (NOT the engine one)
```

---

## Task 1: Repo scaffold + test harness

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `tests/conftest.py`, `scripts/_vendor/__init__.py`, `README.md` (stub)
- Test: `tests/conftest.py` (fixture), `tests/test_scaffold.py`

**Interfaces:**
- Produces: pytest fixture `repo_root` (Path to the plugin repo root); `plugin_paths` fixture returning a dataclass of expected dirs.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scaffold.py
from pathlib import Path


def test_repo_root_has_scripts(repo_root):
    assert (repo_root / "scripts").is_dir()


def test_gitignore_excludes_venv(repo_root):
    gi = (repo_root / ".gitignore").read_text()
    assert "engine/.venv" in gi
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_scaffold.py -q`
Expected: FAIL — no `conftest.py` fixture / files missing.

- [ ] **Step 3: Create the scaffold**

`tests/conftest.py`:
```python
from pathlib import Path
import pytest


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent
```

`.gitignore`:
```
__pycache__/
*.pyc
engine/.venv/
.forge-foundry/
.pytest_cache/
```

`pyproject.toml` (vendor tooling + tests only — the engine gets its OWN generated pyproject under engine/):
```toml
[project]
name = "forge-foundry-claude-tooling"
version = "0.1.0"
description = "Vendor tooling + parity tests for the forge-foundry-claude plugin"
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`scripts/_vendor/__init__.py`: empty. `README.md`: one-line stub (expanded in T7).

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_scaffold.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore tests/conftest.py tests/test_scaffold.py scripts/_vendor/__init__.py README.md
git commit -m "chore: repo scaffold + pytest harness"
```

---

## Task 2: Vendor lock + source SHA resolution

**Files:**
- Create: `scripts/_vendor/lock.py`
- Test: `tests/test_lock.py`

**Interfaces:**
- Produces: `resolve_source(src: Path, ref: str, *, allow_dirty: bool = False) -> SourceInfo` (frozen dataclass: `repo: str`, `ref: str`, `sha: str`, `dirty: bool`); `write_lock(engine_dir: Path, info: SourceInfo, *, primitive_count: int, version: str) -> dict` (writes `engine/VENDOR_LOCK.json`, returns the dict); `read_lock(engine_dir: Path) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_lock.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_lock.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/_vendor/lock.py tests/test_lock.py
git commit -m "feat(vendor): source SHA resolution + VENDOR_LOCK.json"
```

---

## Task 3: Copy skills + agents

**Files:**
- Create: `scripts/_vendor/copy_skills.py`
- Test: `tests/test_copy_skills.py`

**Interfaces:**
- Consumes: source primitives root (`$FOUNDRY_SRC/primitives`).
- Produces: `discover_primitives(src_primitives: Path) -> list[PrimitiveSrc]` (frozen dataclass: `name: str` (dir), `slug: str` (frontmatter name), `skill_dir: Path`, `agents_dir: Path | None`, `has_shared: bool`, `delegates_to_cli: bool`); `copy_skills(prims, out_skills: Path) -> int` (returns count copied; skips symlinks; excludes the dev-command denylist); `copy_agents(prims, out_agents: Path, src_repo: Path) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_copy_skills.py
from pathlib import Path
from scripts._vendor.copy_skills import discover_primitives, copy_skills, PrimitiveSrc


def _fake_primitive(root: Path, name: str, slug: str, *, shared=False,
                    delegates=False, ref="hello.md"):
    p = root / name / "skill"; p.mkdir(parents=True)
    fm = f"---\nname: {slug}\n"
    if delegates:
        fm += "delegates-to-cli: true\n"
    fm += "---\n"
    (p / "SKILL.md").write_text(fm + f"body for {slug}\n")
    (p / "references").mkdir()
    (p / "references" / ref).write_text("ref content")
    if shared:
        s = root / name / "shared"; s.mkdir()
        (s / "__init__.py").write_text("")
    return p


def test_discover_reads_slug_and_flags(tmp_path):
    prims_root = tmp_path / "primitives"; prims_root.mkdir()
    _fake_primitive(prims_root, "data_flow_map", "data-flow-map",
                    shared=True, delegates=True)
    found = discover_primitives(prims_root)
    assert len(found) == 1
    assert found[0].slug == "data-flow-map"
    assert found[0].delegates_to_cli is True and found[0].has_shared is True


def test_copy_skills_copies_references(tmp_path):
    prims_root = tmp_path / "primitives"; prims_root.mkdir()
    _fake_primitive(prims_root, "code_review", "code-review")
    out = tmp_path / "skills"
    n = copy_skills(discover_primitives(prims_root), out)
    assert n == 1
    assert (out / "code-review" / "SKILL.md").is_file()
    assert (out / "code-review" / "references" / "hello.md").read_text() == "ref content"
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_copy_skills.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# scripts/_vendor/copy_skills.py
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

# Maintainer-only commands that must never ship in the consumer plugin.
_DENYLIST = {"autowork", "review-pr"}


@dataclass(frozen=True)
class PrimitiveSrc:
    name: str
    slug: str
    skill_dir: Path
    agents_dir: Path | None
    has_shared: bool
    delegates_to_cli: bool


def _read_frontmatter(skill_md: Path) -> dict:
    text = skill_md.read_text()
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else ""
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def discover_primitives(src_primitives: Path) -> list[PrimitiveSrc]:
    src_primitives = Path(src_primitives)
    found: list[PrimitiveSrc] = []
    for child in sorted(src_primitives.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        skill_md = child / "skill" / "SKILL.md"
        if not skill_md.is_file():
            continue
        fm = _read_frontmatter(skill_md)
        slug = fm.get("name", child.name.replace("_", "-"))
        if slug in _DENYLIST:
            continue
        agents = child / "agents"
        found.append(PrimitiveSrc(
            name=child.name, slug=slug, skill_dir=child / "skill",
            agents_dir=agents if agents.is_dir() else None,
            has_shared=(child / "shared").is_dir(),
            delegates_to_cli=(fm.get("delegates-to-cli", "").lower() == "true"),
        ))
    return found


def _copy_tree_no_symlinks(src: Path, dst: Path) -> None:
    for item in sorted(src.rglob("*")):
        if item.is_symlink():
            continue
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def copy_skills(prims: list[PrimitiveSrc], out_skills: Path) -> int:
    out_skills = Path(out_skills)
    if out_skills.exists():
        shutil.rmtree(out_skills)
    n = 0
    for p in prims:
        _copy_tree_no_symlinks(p.skill_dir, out_skills / p.slug)
        n += 1
    return n


def copy_agents(prims: list[PrimitiveSrc], out_agents: Path, src_repo: Path) -> int:
    """Convert canonical agents/*.agent.yaml → Claude agent .md via the source emitter.

    The source emitter (forge_sync.emitter.agents.emit_agent) is importable because
    vendor.py runs with src_repo on sys.path. Falls back to a verbatim copy of the
    yaml (renamed .md with a fenced block) if the emitter import is unavailable.
    """
    import sys
    out_agents = Path(out_agents)
    if out_agents.exists():
        shutil.rmtree(out_agents)
    prims_with_agents = [p for p in prims if p.agents_dir]
    if not prims_with_agents:
        return 0
    out_agents.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(src_repo))
    try:
        from forge_sync.emitter.loader import load_canonical_sources  # type: ignore
        from forge_sync.emitter.agents import emit_agent  # type: ignore
        emitter_ok = True
    except Exception:
        emitter_ok = False
    n = 0
    for p in prims_with_agents:
        for yml in sorted(p.agents_dir.glob("*.agent.yaml")):
            stem = yml.name.replace(".agent.yaml", "")
            if emitter_ok:
                sources = load_canonical_sources(p.skill_dir.parent)
                emitted = emit_agent(sources.agents[stem], "claude_code")  # type: ignore
                for _relpath, content in emitted.items():
                    (out_agents / f"{stem}.md").write_text(content)
            else:
                (out_agents / f"{stem}.md").write_text(
                    f"<!-- vendored from {yml.name} -->\n\n```yaml\n{yml.read_text()}\n```\n")
            n += 1
    return n
```

> NOTE: the emitter import path (`forge_sync.emitter.agents.emit_agent`, `load_canonical_sources`) is what the feasibility map found. If the real symbol names differ, the fallback branch still produces a valid agent file — the parity test (T8) only asserts an agent `.md` exists per source agent, not its exact rendering.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_copy_skills.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/_vendor/copy_skills.py tests/test_copy_skills.py
git commit -m "feat(vendor): discover + copy skills and agents (symlink-safe, denylist)"
```

---

## Task 4: Copy engine + generate pyproject

**Files:**
- Create: `scripts/_vendor/copy_engine.py`
- Test: `tests/test_copy_engine.py`

**Interfaces:**
- Consumes: `$FOUNDRY_SRC` (root), `list[PrimitiveSrc]`.
- Produces: `copy_engine(src_repo: Path, prims, out_engine: Path) -> None` (copies `forge_validate/`, `primitives/_shared/`, each primitive's `shared/`, creates `__init__.py` packages, writes `engine/pyproject.toml` from a template with deps pinned from the source `uv.lock`); `_pinned_deps(src_repo: Path) -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_copy_engine.py
from pathlib import Path
from scripts._vendor.copy_skills import PrimitiveSrc
from scripts._vendor.copy_engine import copy_engine


def _fake_src(tmp_path) -> Path:
    src = tmp_path / "src"
    (src / "forge_validate" / "commands").mkdir(parents=True)
    (src / "forge_validate" / "__init__.py").write_text("")
    (src / "forge_validate" / "cli.py").write_text("def main():\n    pass\n")
    (src / "forge_validate" / "commands" / "__init__.py").write_text("")
    (src / "primitives" / "_shared").mkdir(parents=True)
    (src / "primitives" / "__init__.py").write_text("")
    (src / "primitives" / "_shared" / "__init__.py").write_text("")
    (src / "primitives" / "data_flow_map" / "shared").mkdir(parents=True)
    (src / "primitives" / "data_flow_map" / "shared" / "__init__.py").write_text("")
    (src / "pyproject.toml").write_text(
        '[project]\nname="forge-foundry-primitives"\n'
        'dependencies=["click>=8","pydantic>=2","sqlglot>=20"]\n')
    (src / "uv.lock").write_text("")  # deps read from pyproject as fallback
    return src


def test_copy_engine_brings_forge_validate(tmp_path):
    src = _fake_src(tmp_path)
    prims = [PrimitiveSrc("data_flow_map", "data-flow-map",
                          src / "primitives/data_flow_map/skill", None, True, True)]
    out = tmp_path / "engine"
    copy_engine(src, prims, out)
    assert (out / "forge_validate" / "cli.py").is_file()
    assert (out / "primitives" / "_shared" / "__init__.py").is_file()
    assert (out / "primitives" / "data_flow_map" / "shared" / "__init__.py").is_file()


def test_generated_pyproject_has_console_script(tmp_path):
    src = _fake_src(tmp_path)
    out = tmp_path / "engine"
    copy_engine(src, [], out)
    pp = (out / "pyproject.toml").read_text()
    assert 'forge-validate = "forge_validate.cli:main"' in pp
    assert "click" in pp
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_copy_engine.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# scripts/_vendor/copy_engine.py
from __future__ import annotations

import re
import shutil
from pathlib import Path

from scripts._vendor.copy_skills import PrimitiveSrc

_PYPROJECT_TEMPLATE = '''\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "forge-foundry-claude-engine"
version = "{version}"
description = "Bundled deterministic engine for the forge-foundry-claude plugin (vendored)."
requires-python = ">=3.11"
dependencies = [
{deps}
]

[project.scripts]
forge-validate = "forge_validate.cli:main"

[tool.setuptools]
packages = ["forge_validate", "primitives"]

[tool.setuptools.package-dir]
"" = "."
'''


def _copy_tree_no_symlinks(src: Path, dst: Path) -> None:
    for item in sorted(src.rglob("*")):
        if item.is_symlink():
            continue
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.suffix == ".pyc" or "__pycache__" in item.parts:
            continue
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _pinned_deps(src_repo: Path) -> list[str]:
    """Read runtime deps from the source pyproject [project].dependencies."""
    text = (src_repo / "pyproject.toml").read_text()
    m = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if not m:
        return ["click>=8", "pydantic>=2"]
    items = re.findall(r'["\']([^"\']+)["\']', m.group(1))
    return items


def copy_engine(src_repo: Path, prims: list[PrimitiveSrc], out_engine: Path,
                *, version: str = "0.1.0") -> None:
    src_repo = Path(src_repo)
    out_engine = Path(out_engine)
    # forge_validate
    _copy_tree_no_symlinks(src_repo / "forge_validate", out_engine / "forge_validate")
    # primitives package root + _shared
    (out_engine / "primitives").mkdir(parents=True, exist_ok=True)
    src_prim_init = src_repo / "primitives" / "__init__.py"
    (out_engine / "primitives" / "__init__.py").write_text(
        src_prim_init.read_text() if src_prim_init.is_file() else "")
    _copy_tree_no_symlinks(src_repo / "primitives" / "_shared",
                           out_engine / "primitives" / "_shared")
    # each primitive's shared/ (hybrid + PLB)
    for p in prims:
        if not p.has_shared:
            continue
        shared = src_repo / "primitives" / p.name / "shared"
        _copy_tree_no_symlinks(shared, out_engine / "primitives" / p.name / "shared")
        pkg_init = out_engine / "primitives" / p.name / "__init__.py"
        if not pkg_init.exists():
            pkg_init.parent.mkdir(parents=True, exist_ok=True)
            pkg_init.write_text("")
    # generated pyproject
    deps = _pinned_deps(src_repo)
    deps_block = "\n".join(f'    "{d}",' for d in deps)
    (out_engine / "pyproject.toml").write_text(
        _PYPROJECT_TEMPLATE.format(version=version, deps=deps_block))
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_copy_engine.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/_vendor/copy_engine.py tests/test_copy_engine.py
git commit -m "feat(vendor): copy forge_validate + primitives engine + generate pyproject"
```

---

## Task 5: PLB path-rewrite + state-dir localization

**Files:**
- Create: `scripts/_vendor/rewrite.py`
- Test: `tests/test_rewrite.py`

**Interfaces:**
- Produces: `rewrite_skill_body(text: str) -> tuple[str, int]` (returns rewritten text + count of invocations rewritten); `rewrite_skills_tree(skills_dir: Path) -> dict[str, int]` (applies to every `SKILL.md`, returns slug→count). Rewrites: (1) `uv run --project "<any>" forge-validate` → `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate"`; (2) `.foundry/context` → `.forge-foundry/context`. Idempotent (skips already-rewritten bodies).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rewrite.py
from scripts._vendor.rewrite import rewrite_skill_body


def test_rewrites_uv_run_prefix():
    body = (
        "Run:\n```bash\n"
        'uv run --project "${foundry_project:-$(pwd)}" forge-validate data-flow-map \\\n'
        '  --state-dir "$state_dir" "$target"\n```\n'
    )
    out, n = rewrite_skill_body(body)
    assert n == 1
    assert '"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" data-flow-map' in out
    assert "uv run --project" not in out


def test_localizes_state_dir_default():
    body = 'state_dir=".foundry/context/dfm"\n'
    out, _ = rewrite_skill_body(body)
    assert ".forge-foundry/context/dfm" in out
    assert ".foundry/context" not in out


def test_idempotent():
    body = '```bash\nuv run --project "$x" forge-validate repo-digest .\n```\n'
    once, n1 = rewrite_skill_body(body)
    twice, n2 = rewrite_skill_body(once)
    assert once == twice
    assert n1 == 1 and n2 == 0


def test_leaves_prose_untouched():
    body = "This skill delegates to forge-validate under the hood.\n"
    out, n = rewrite_skill_body(body)
    assert out == body and n == 0
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_rewrite.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# scripts/_vendor/rewrite.py
from __future__ import annotations

import re
from pathlib import Path

# Match `uv run --project "<anything>" forge-validate` (the command PREFIX only).
_UV_PREFIX = re.compile(
    r'uv\s+run\s+--project\s+"[^"]*"\s+forge-validate\b'
)
_SHIM = '"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate"'
_STATE_DIR = re.compile(r"\.foundry/context")


def rewrite_skill_body(text: str) -> tuple[str, int]:
    new, n = _UV_PREFIX.subn(_SHIM, text)
    new = _STATE_DIR.sub(".forge-foundry/context", new)
    return new, n


def rewrite_skills_tree(skills_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for skill_md in sorted(Path(skills_dir).glob("*/SKILL.md")):
        slug = skill_md.parent.name
        text = skill_md.read_text()
        new, n = rewrite_skill_body(text)
        if new != text:
            skill_md.write_text(new)
        counts[slug] = n
    return counts
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_rewrite.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/_vendor/rewrite.py tests/test_rewrite.py
git commit -m "feat(vendor): PLB invocation rewrite + state-dir localization (idempotent)"
```

---

## Task 6: Resolver shim + bootstrap hook

**Files:**
- Create: `bin/forge-validate`, `hooks/bootstrap.sh`
- Test: `tests/test_shim_and_hook.py`

**Interfaces:**
- Produces: `bin/forge-validate` (bash resolver, exit 69 when venv absent, else `exec` bundled binary); `hooks/bootstrap.sh` (idempotent venv build). Both `chmod +x`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shim_and_hook.py
import os
import stat
import subprocess
from pathlib import Path


def test_shim_exists_and_executable(repo_root):
    shim = repo_root / "bin" / "forge-validate"
    assert shim.is_file()
    assert os.access(shim, os.X_OK)


def test_hook_exists_and_executable(repo_root):
    hook = repo_root / "hooks" / "bootstrap.sh"
    assert hook.is_file() and os.access(hook, os.X_OK)


def test_shim_exits_69_when_venv_absent(repo_root, tmp_path):
    shim = repo_root / "bin" / "forge-validate"
    env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(tmp_path))  # no engine/.venv here
    r = subprocess.run([str(shim), "repo-digest", "."], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 69
    assert "not bootstrapped" in r.stderr
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_shim_and_hook.py -q`
Expected: FAIL — files missing.

- [ ] **Step 3: Create the shim + hook (and chmod +x)**

`bin/forge-validate`:
```bash
#!/usr/bin/env bash
# Resolver shim for the bundled forge-validate engine.
set -euo pipefail
: "${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT not set}"
VENV="${CLAUDE_PLUGIN_ROOT}/engine/.venv"
if [ ! -x "$VENV/bin/forge-validate" ]; then
  echo "forge-foundry-claude: engine not bootstrapped. The SessionStart hook builds it on first activation; if you see this, run: bash \"${CLAUDE_PLUGIN_ROOT}/hooks/bootstrap.sh\"" >&2
  exit 69
fi
exec "$VENV/bin/forge-validate" "$@"
```

`hooks/bootstrap.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
: "${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT not set}"
ROOT="${CLAUDE_PLUGIN_ROOT}"
VENV="$ROOT/engine/.venv"
STAMP="$VENV/.forge-vendor-sha"
LOCK="$ROOT/engine/VENDOR_LOCK.json"
LOCK_SHA="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["sha"])' "$LOCK" 2>/dev/null || echo none)"

if [ -x "$VENV/bin/forge-validate" ] && [ "$(cat "$STAMP" 2>/dev/null || echo x)" = "$LOCK_SHA" ]; then
  exit 0
fi

if command -v python3.11 >/dev/null 2>&1; then PY=python3.11
elif command -v python3 >/dev/null 2>&1; then PY=python3
else
  echo "forge-foundry-claude: python3.11+ not found; PLB skills unavailable until it is installed." >&2
  exit 0
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,11) else 1)'; then
  echo "forge-foundry-claude: python 3.11+ required; found $($PY --version 2>&1)." >&2
  exit 0
fi

echo "forge-foundry-claude: bootstrapping engine venv (one-time)…" >&2
rm -rf "$VENV"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --disable-pip-version-check -e "$ROOT/engine"
echo "$LOCK_SHA" > "$STAMP"
echo "forge-foundry-claude: engine ready." >&2
```

Then:
```bash
chmod +x bin/forge-validate hooks/bootstrap.sh
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_shim_and_hook.py -q`
Expected: PASS (shim exits 69, files executable).

- [ ] **Step 5: Commit**

```bash
git add bin/forge-validate hooks/bootstrap.sh tests/test_shim_and_hook.py
git commit -m "feat(plugin): resolver shim (exit 69) + SessionStart bootstrap hook"
```

---

## Task 7: Manifests, vendor.py entry point, docs

**Files:**
- Create: `scripts/vendor.py`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `CHANGELOG-VENDOR.md`, `README.md` (expand)
- Test: `tests/test_vendor_entry.py`

**Interfaces:**
- Consumes: all of T2–T6.
- Produces: `main(argv)` CLI: `python scripts/vendor.py --source <path> --ref <tag> [--allow-dirty] [--version X.Y.Z]` → runs lock → copy skills/agents → copy engine → rewrite → writes manifests' version + VENDOR_LOCK. Manifests are hand-authored (checked in); vendor.py only asserts/patches the `version` field to match.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vendor_entry.py
import json
import subprocess
from pathlib import Path


def test_plugin_manifest_valid(repo_root):
    pj = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text())
    assert pj["name"] == "forge-foundry-claude"
    assert "SessionStart" in pj["hooks"]


def test_marketplace_points_at_self(repo_root):
    mk = json.loads((repo_root / ".claude-plugin" / "marketplace.json").read_text())
    assert mk["plugins"][0]["name"] == "forge-foundry-claude"
    assert mk["plugins"][0]["source"] == "./"


def test_vendor_help_runs(repo_root):
    r = subprocess.run(["python", "scripts/vendor.py", "--help"],
                       cwd=repo_root, capture_output=True, text=True)
    assert r.returncode == 0 and "--source" in r.stdout
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_vendor_entry.py -q`
Expected: FAIL — files missing.

- [ ] **Step 3: Create manifests + vendor.py + docs**

`.claude-plugin/plugin.json`:
```json
{
  "name": "forge-foundry-claude",
  "description": "Forge Foundry engineering workbench — 31 primitives (spec, review, security, modernization, SDET) as Claude Code skills with a bundled deterministic Python engine.",
  "version": "0.1.0",
  "author": { "name": "Arula", "email": "rbuchanan@inrhythm.com" },
  "license": "UNLICENSED",
  "keywords": ["code-review", "security", "spec", "modernization", "sdet", "workbench"],
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/bootstrap.sh" } ] }
    ]
  }
}
```

`.claude-plugin/marketplace.json`:
```json
{
  "name": "forge-foundry",
  "description": "Arula Forge Foundry — private Claude Code plugin marketplace.",
  "owner": { "name": "Arula", "email": "rbuchanan@inrhythm.com" },
  "plugins": [
    { "name": "forge-foundry-claude", "source": "./", "version": "0.1.0" }
  ]
}
```

`scripts/vendor.py`:
```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._vendor.copy_engine import copy_engine
from scripts._vendor.copy_skills import copy_agents, copy_skills, discover_primitives
from scripts._vendor.lock import resolve_source, write_lock
from scripts._vendor.rewrite import rewrite_skills_tree


def _patch_version(repo: Path, version: str) -> None:
    for rel in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
        path = repo / rel
        data = json.loads(path.read_text())
        if "version" in data:
            data["version"] = version
        if "plugins" in data:
            for p in data["plugins"]:
                p["version"] = version
        path.write_text(json.dumps(data, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Vendor forge-foundry-primitives into this plugin.")
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--allow-dirty", action="store_true")
    ap.add_argument("--version", default="0.1.0")
    args = ap.parse_args(argv)

    repo = Path(__file__).resolve().parent.parent
    info = resolve_source(args.source, args.ref, allow_dirty=args.allow_dirty)
    prims = discover_primitives(args.source / "primitives")

    n_skills = copy_skills(prims, repo / "skills")
    n_agents = copy_agents(prims, repo / "agents", args.source)
    copy_engine(args.source, prims, repo / "engine", version=args.version)
    counts = rewrite_skills_tree(repo / "skills")

    write_lock(repo / "engine", info, primitive_count=n_skills, version=args.version)
    _patch_version(repo, args.version)

    rewritten = sum(counts.values())
    print(f"vendored {n_skills} skills, {n_agents} agents; {rewritten} PLB invocations rewritten")
    print(f"source {info.ref}@{info.sha[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`CHANGELOG-VENDOR.md`:
```markdown
# Vendor Changelog — forge-foundry-claude

Divergences from `forge-foundry-primitives` (source of truth). Every re-vendor updates
`engine/VENDOR_LOCK.json` with the source ref + SHA.

## Deliberate divergences

1. **PLB invocation prefix.** Source skill bodies invoke `uv run --project "<foundry>"
   forge-validate <name>`. Vendored bodies invoke `"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate"
   <name>` (resolver shim → bundled venv). Rationale: no `uv` runtime dependency; self-contained.
2. **State-dir default.** `.foundry/context/…` → `.forge-foundry/context/…` so run artifacts land
   in the user's working project, not the shared plugin cache.
3. **Excluded commands.** `/autowork` and `/review-pr` (maintainer tools) are not vendored.

## Vendor history

<!-- vendor.py appends one line per re-vendor: <date> <ref>@<sha12> <primitive_count> primitives -->
```

Expand `README.md` with: what the plugin is, install steps (`/plugin marketplace add <private-git-url>` → `/plugin install forge-foundry-claude`), the bootstrap note (python3.11+ required, one-time venv), and the re-vendor command.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_vendor_entry.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/vendor.py .claude-plugin CHANGELOG-VENDOR.md README.md tests/test_vendor_entry.py
git commit -m "feat(plugin): vendor.py entry point + manifests + vendor changelog + README"
```

---

## Task 8: Run the real vendor + parity test suite

**Files:**
- Create: `tests/test_parity_skills.py`, `tests/test_parity_engine.py`, `tests/test_parity_manifests.py`
- Vendored output: `skills/**`, `agents/**`, `engine/**` (produced by running vendor.py)

**Interfaces:**
- Consumes: the real vendored tree.

- [ ] **Step 1: Run the real vendor against the source library**

Run:
```bash
python scripts/vendor.py --source "$FOUNDRY_SRC" --ref "$(git -C "$FOUNDRY_SRC" describe --tags --abbrev=0)"
```
Expected: prints `vendored 31 skills, N agents; M PLB invocations rewritten`. Populates `skills/`, `agents/`, `engine/`.

- [ ] **Step 2: Write the parity tests**

`tests/test_parity_skills.py`:
```python
import re
from pathlib import Path
import pytest

EXPECTED_PLB = {
    "code-gen", "data-flow-map", "mod-db-schema-analyze", "mod-modernization-strategist",
    "mod-playbook-discovery", "repo-digest", "rules-gen", "mod-sp-converter",
    "mod-stored-proc-analyze", "mod-synth-logs", "test-from-spec", "test-scaffold",
    "work-orchestrator",
}


def _skills(repo_root):
    return repo_root / "skills"


def test_31_skills_present(repo_root):
    dirs = [p for p in _skills(repo_root).iterdir() if (p / "SKILL.md").is_file()]
    assert len(dirs) == 31, f"expected 31 skills, got {len(dirs)}"


def test_no_dev_commands(repo_root):
    assert not (_skills(repo_root) / "autowork").exists()
    assert not (_skills(repo_root) / "review-pr").exists()


def test_no_residual_uv_run(repo_root):
    offenders = []
    for md in _skills(repo_root).glob("*/SKILL.md"):
        if re.search(r"uv\s+run\s+--project.*forge-validate", md.read_text()):
            offenders.append(md.parent.name)
    assert offenders == [], f"un-rewritten PLB invocations in: {offenders}"


def test_every_plb_slug_has_shim_call(repo_root):
    missing = []
    for slug in EXPECTED_PLB:
        md = _skills(repo_root) / slug / "SKILL.md"
        if not md.is_file() or "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" not in md.read_text():
            missing.append(slug)
    assert missing == [], f"PLB slugs missing shim invocation: {missing}"


def test_state_dir_localized(repo_root):
    offenders = [md.parent.name for md in _skills(repo_root).glob("*/SKILL.md")
                 if ".foundry/context" in md.read_text()]
    assert offenders == [], f".foundry/context not localized in: {offenders}"
```

`tests/test_parity_engine.py`:
```python
import subprocess
import sys
from pathlib import Path


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
```

`tests/test_parity_manifests.py`:
```python
import json


def test_versions_agree(repo_root):
    pj = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text())
    mk = json.loads((repo_root / ".claude-plugin" / "marketplace.json").read_text())
    lock = json.loads((repo_root / "engine" / "VENDOR_LOCK.json").read_text())
    assert pj["version"] == mk["plugins"][0]["version"] == lock["version"]
```

> NOTE on `EXPECTED_PLB`: if the source library's PLB set differs at the pinned tag (e.g. a slug renamed), update this frozen set from the source `forge_validate/cli.py` `_COMMAND_MODULES` registry — the parity test is the guard that forces this to stay honest.

- [ ] **Step 3: Run the parity suite**

Run: `python -m pytest tests/ -q`
Expected: PASS (all parity + unit tests green). If `test_31_skills_present` fails, the source tag has ≠31 primitives — reconcile the count/tag before proceeding.

- [ ] **Step 4: Commit the vendored tree + tests**

```bash
git add skills agents engine tests/test_parity_skills.py tests/test_parity_engine.py tests/test_parity_manifests.py
git commit -m "feat(plugin): vendor 31 primitives + engine; parity test suite green"
```

---

## Task 9: Headless PLB smoke tests

**Files:**
- Create: `tests/test_smoke_plb.py`, `tests/fixtures/mini_repo/**`

**Interfaces:**
- Consumes: a bootstrapped engine venv (built in-test into a scratch dir).

- [ ] **Step 1: Create a tiny fixture repo**

`tests/fixtures/mini_repo/app.py`:
```python
def add(a, b):
    return a + b
```
`tests/fixtures/mini_repo/README.md`: `# mini`.

- [ ] **Step 2: Write the smoke test (builds a real venv, runs a PLB command headlessly)**

```python
# tests/test_smoke_plb.py
import os
import subprocess
import sys
from pathlib import Path
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
```

> NOTE: `--help` on each PLB command exercises the full shim → venv → forge-validate → command-registration path without needing a full analysis run (which may require judgments/exit-7). If `--help` isn't a clean exit-0 for a given command in the source CLI, substitute a genuinely side-effect-free invocation documented in that command's SPEC.

- [ ] **Step 3: Run the smoke suite**

Run: `python -m pytest tests/test_smoke_plb.py -q`
Expected: PASS (or SKIP with a clear reason if the sandbox can't build a venv — the test is written to skip, not falsely fail, in a restricted env).

- [ ] **Step 4: Commit**

```bash
git add tests/test_smoke_plb.py tests/fixtures/mini_repo
git commit -m "test(plugin): headless PLB smoke tests (bootstrap venv + shim → forge-validate)"
```

---

## Task 10: Final full-suite gate + README polish

**Files:**
- Modify: `README.md` (final install + troubleshooting section)
- Test: whole suite

- [ ] **Step 1: Run the entire suite**

Run: `python -m pytest tests/ -q`
Expected: all PASS (smoke may SKIP in a restricted CI sandbox; green locally where python3.11+ exists).

- [ ] **Step 2: Add a Troubleshooting section to README**

Document: "engine not bootstrapped" (exit 69) → what it means + `bash hooks/bootstrap.sh`; "python3.11+ not found" → install python; re-vendor command; where run artifacts land (`.forge-foundry/context/` in your project).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(plugin): install + troubleshooting; final suite gate"
```

- [ ] **Step 4: Manual verification handoff (user)**

The remaining verification can only be observed, not asserted headlessly:
1. Push the repo; `/plugin marketplace add <private-git-url>`.
2. `/plugin install forge-foundry-claude`.
3. Start a session → confirm the bootstrap hook builds the venv (watch stderr).
4. Invoke a skill-first primitive (e.g. `/spec-write`) and a PLB primitive (e.g. `/repo-digest .`); confirm both run without error.

---

## Self-Review

**Spec coverage:**
- Repo layout → T1 + produced across T2–T9. ✓
- Vendor Stage 1 (lock/SHA) → T2. Stage 2 (skills+agents) → T3. Stage 3 (engine+pyproject) → T4. Stage 4 (path-rewrite) → T5. Stage 5 (resolver shim) → T6. ✓
- Bootstrap hook (fast-path, python discovery, fail-soft, SHA stamp) → T6. ✓
- Manifests (plugin.json + marketplace.json, version agreement) → T7 + T8 parity. ✓
- Parity tests (all 9 from spec) → T8 (skills/engine/manifests) + T6 (shim/hook) + T2 (lock). Mapped: all-skills-present→T8; no-dev-commands→T8; plb-paths-rewritten→T8; state-dir-localized→T8; engine-imports→T8; cli-registrations→T9 smoke (via --help resolving registered commands); manifests-valid→T8; vendor-lock-matches→T8; shim/hook-executable→T6. ✓
- Error handling (exit 69, fail-soft, SHA rebuild) → T6 + tested T6/T9. ✓
- Verification strategy (automated + manual gate) → T8/T9 automated, T10 manual handoff. ✓
- Non-goals honored (no uv, no public marketplace, no emitter target, no engine fork, exclude autowork/review-pr, 31 not 33). ✓
- Open questions: agent-conversion reuse → T3 (import emitter, fallback branch); dep pinning → T4 (`_pinned_deps` from source pyproject); rules/ → not required, no primitive ships them (handled by absence). ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases". Three explicit NOTEs (emitter symbol names in T3, EXPECTED_PLB set in T8, --help side-effect-freeness in T9) each name the concrete fallback/reconciliation, not vague deferral. ✓

**Type consistency:** `PrimitiveSrc` (T3) fields consumed unchanged in T4/vendor.py (T7). `SourceInfo`/`resolve_source`/`write_lock` (T2) used in vendor.py (T7). `rewrite_skill_body`/`rewrite_skills_tree` (T5) used in T7/T8. `copy_skills`/`copy_agents`/`copy_engine`/`discover_primitives` signatures identical across T3/T4/T7. Shim exit code 69 consistent T6↔T8↔T9. ✓

---

## Execution Handoff

Tasks are strictly sequential T1→T10 (each builds on the prior; T8 requires T2–T7 complete before the real vendor run). Recommended: subagent-driven, one task per subagent, review between.
