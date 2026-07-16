"""Adapter propagator: copies harness-native files from .foundry/primitives/ into the project.

Vendored from forge-prowess-primitives@v0.4.0 — see CHANGELOG-VENDOR.md for divergence.

Design decisions:
  - Skill dirs (adapters/claude_code/ → .claude/skills/<name>/) are clean-replaced
    (destination wiped then copytree), matching copier.py's approach.
  - Prompts dirs (adapters/copilot/github-bundle/prompts/) are merged at the file
    level: individual prompt files are copied into the destination dir (never wipe
    the whole prompts dir because other primitives may already have deposited files).
  - JSON hook snippets (settings-snippet.json, hooks-snippet.json) are deep-merged
    into the target JSON file (additive on hook arrays, never overwrite).
  - journey-curator.md (or any file under adapters/claude_code/agents/) is copied
    to .claude/agents/<name>.md (sub-agent convention).
  - Copilot agents/ files are copied into .github/agents/ individually.
  - Primitives with no adapter of a given type are silently skipped.
  - Calling propagate_adapters twice is idempotent (skills clean-replaced each call;
    JSON merges are additive but duplicate hook entries are not inserted again).
"""

from __future__ import annotations

import json
import logging
import shutil
from importlib.metadata import entry_points
from pathlib import Path
from typing import Protocol, Union, runtime_checkable

from forge_sync.emitter.aliases import emit_alias_skills
from forge_sync.emitter.emit import (
    emit_ci_for_primitive,
    emit_from_sources,
    load_canonical_sources,
)

logger = logging.getLogger(__name__)

# Recursive JSON value type (avoids `Any` while keeping mypy happy).
_JSON = Union[None, bool, int, float, str, list["_JSON"], "dict[str, _JSON]"]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _deep_merge_hooks_json(target: Path, snippet: Path) -> None:
    """Deep-merge *snippet* into *target* JSON (additive on hook arrays).

    Rules:
    - If *target* does not exist, write *snippet* content directly.
    - Top-level "_comment" keys in snippet are dropped (not written to target).
    - For each key in snippet (except "_comment"):
        - If the key maps to a list in both target and snippet, extend the target
          list with snippet items that are not already present (dedup by equality).
        - If the key maps to a dict in both, recurse.
        - Otherwise overwrite.
    """
    if not target.exists():
        # Copy snippet verbatim minus _comment
        raw = json.loads(snippet.read_text(encoding="utf-8"))
        raw.pop("_comment", None)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        logger.debug("Created %s from snippet %s", target, snippet)
        return

    existing: dict[str, _JSON] = json.loads(target.read_text(encoding="utf-8"))
    incoming: dict[str, _JSON] = json.loads(snippet.read_text(encoding="utf-8"))
    incoming.pop("_comment", None)

    _merge_into(existing, incoming)

    target.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    logger.debug("Merged snippet %s → %s", snippet, target)


# SEC-9 (#173): keys whose value reflects user security posture. The propagator
# must never let a primitive snippet override these via deep-merge — a future
# (or tampered) snippet that downgrades `permissions.defaultMode` from `deny` to
# `ask`, or rewrites the model / env / allow-list, would silently lower the
# user's trust boundary. The user's value wins; merges into these keys are
# logged-and-dropped.
_PROTECTED_KEYS: frozenset[str] = frozenset(
    {
        "permissions",
        "model",
        "env",
        "deniedTools",
        "trustedRoots",
        "allowList",
        "denyList",
    }
)


def _merge_into(base: dict[str, _JSON], incoming: dict[str, _JSON], *, path: str = "") -> None:
    """Recursively merge *incoming* into *base* in-place (additive on lists).

    SEC-9 (#173): top-level keys in :data:`_PROTECTED_KEYS` are never merged. A
    snippet that tries is logged at WARNING and skipped so the user's value
    survives. *path* is used only for log context — pass ``""`` at the top
    level; recursive calls thread the dotted JSON path.

    The protection is **top-level only** (``path == ""``) — by design, not an
    oversight (#740). Every current harness config shape (Claude Code, Copilot,
    Codex, Antigravity, Cursor ``settings.json``) places its security-sensitive
    keys at the document root, so a top-level check covers the whole real attack
    surface. It is deliberately *not* recursive because a protected name
    (``env``, ``model``, ``permissions``…) can legitimately appear as a
    non-security key nested inside an unprotected parent (e.g. a tool's own
    ``{"task": {"env": {...}}}`` block); rejecting those at any depth would be a
    false positive that silently drops valid config. If a future harness format
    nests genuine security keys, extend this guard to walk those known parents
    explicitly rather than blanket-rejecting every depth.
    """
    for key, value in incoming.items():
        full_path = f"{path}.{key}" if path else key
        if path == "" and key in _PROTECTED_KEYS and base.get(key) != value:
            logger.warning(
                "Refusing to merge protected key %r at %s — user value preserved (SEC-9)",
                key,
                full_path,
            )
            continue
        if key not in base:
            base[key] = value
        else:
            base_val = base[key]
            if isinstance(base_val, list) and isinstance(value, list):
                # Additive dedup: only append items not already in the list.
                for item in value:
                    if item not in base_val:
                        base_val.append(item)
            elif isinstance(base_val, dict) and isinstance(value, dict):
                _merge_into(base_val, value, path=full_path)
            else:
                # Scalar or type mismatch: overwrite (last writer wins).
                base[key] = value


def write_emitted(project_root: Path, emitted: dict[str, str]) -> list[str]:
    """Write {project-relative path: content} under *project_root*. Returns dest paths.

    The returned list is ordered by relative path (``sorted(emitted.items())``).

    SEC-10: this is the categorical write-sink for emitted artifacts. The ``rel``
    keys originate from attacker-controllable canonical-source fields (a skill /
    agent ``name`` frontmatter, a rule ``paths:`` glob). Mirroring SEC-7's
    ``copier.py::_resolve_source`` ``..``-before-``.resolve()`` discipline, every
    ``rel`` is rejected here BEFORE any write if it is absolute, contains a ``..``
    segment, or escapes ``project_root`` once resolved. Source-side validators
    (loader/rules) give friendlier errors earlier, but this guard closes the
    write-escape categorically regardless of how a path was produced.
    """
    project_root_resolved = project_root.resolve()
    items = sorted(emitted.items())

    # Pass 1: validate EVERY rel before writing anything. A malicious key sorted
    # after benign keys must not cause a partial write before the raise (A3).
    for rel, _content in items:
        if not rel.strip() or rel == ".":
            raise ValueError(
                f"emitted path {rel!r} must be a non-empty project-relative path (SEC-10)"
            )
        rel_path = Path(rel)
        if rel_path.is_absolute():
            raise ValueError(
                f"emitted path {rel!r} must be project-relative, not absolute (SEC-10)"
            )
        if ".." in rel_path.parts:
            raise ValueError(f"emitted path {rel!r} must not contain '..' traversal (SEC-10)")
        dest = project_root / rel
        if not dest.resolve().is_relative_to(project_root_resolved):
            raise ValueError(
                f"emitted path {rel!r} resolves outside the project root {project_root} (SEC-10)"
            )

    # Pass 2: all rels validated — write them all.
    touched: list[str] = []
    for rel, content in items:
        dest = project_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        touched.append(str(dest))
    return touched


def _ignore_symlinks(directory: str, names: list[str]) -> set[str]:
    """``shutil.copytree`` ``ignore`` callback that skips symlinked entries.

    SEC-10 (A2): ``copytree`` dereferences symlinks by default, materializing the
    target's contents into the consumer tree (an arbitrary-host-file exfil vector
    for legacy / half-migrated primitives). Returning the symlinked names — both
    files AND directories — excludes them from the copy.
    """
    base = Path(directory)
    return {name for name in names if (base / name).is_symlink()}


def _copy_dir_clean(src: Path, dest: Path) -> list[str]:
    """Wipe *dest* and copy *src* recursively. Returns list of destination paths.

    Symlinked entries under *src* are skipped (SEC-10, A2) — see _ignore_symlinks.

    File paths are collected during the copy via a custom ``copy_function`` so
    the previous O(n) ``dest.rglob("*")`` re-walk after ``copytree`` is avoided.
    """
    if dest.exists():
        shutil.rmtree(dest)
    copied: list[str] = []

    def _copy_and_track(src_file: str, dst_file: str) -> None:
        shutil.copy2(src_file, dst_file)
        copied.append(dst_file)

    shutil.copytree(src, dest, ignore=_ignore_symlinks, copy_function=_copy_and_track)
    return copied


def _copy_files_into(src_dir: Path, dest_dir: Path) -> list[str]:
    """Copy individual files from *src_dir* into *dest_dir* (no wipe). Returns dest paths."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    for src_file in src_dir.rglob("*"):
        # SEC-10 (A2): skip symlinked files — copying them would dereference the
        # link and write arbitrary host-file contents into the consumer tree.
        if src_file.is_symlink():
            continue
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(src_dir)
        dest_file = dest_dir / rel
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
        created.append(str(dest_file))
    return created


# ---------------------------------------------------------------------------
# Per-primitive propagation helpers (keeps propagate_adapters under PLR0915 limit)
# ---------------------------------------------------------------------------


def _propagate_claude_adapters(
    project_root: Path,
    name: str,
    claude_adapter: Path,
) -> list[str]:
    """Propagate all claude_code adapter artifacts for one primitive. Returns touched paths."""
    touched: list[str] = []

    # Skill dir
    if claude_adapter.is_dir() and (claude_adapter / "SKILL.md").is_file():
        dest_skill = project_root / ".claude" / "skills" / name
        touched.extend(_copy_dir_clean(claude_adapter, dest_skill))
        logger.debug("Copied claude_code adapter for %r → %s", name, dest_skill)

    # settings-snippet.json
    settings_snippet = claude_adapter / "settings-snippet.json"
    if settings_snippet.is_file():
        target_settings = project_root / ".claude" / "settings.json"
        target_settings.parent.mkdir(parents=True, exist_ok=True)
        _deep_merge_hooks_json(target_settings, settings_snippet)
        touched.append(str(target_settings))

    # Sub-agent file (journey-curator.md convention, or agents/ subdir)
    # SEC-10: skip symlinks so a symlinked agent file cannot exfiltrate
    # arbitrary host content into the consumer tree.
    agent_file = claude_adapter / "journey-curator.md"
    if agent_file.is_file() and not agent_file.is_symlink():
        dest_agent = project_root / ".claude" / "agents" / f"{name}.md"
        dest_agent.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(agent_file, dest_agent)
        touched.append(str(dest_agent))
    else:
        agents_src = claude_adapter / "agents"
        if agents_src.is_dir():
            for f in agents_src.rglob("*.md"):
                if f.is_symlink():
                    continue  # SEC-10: skip symlinks
                dest_agent = project_root / ".claude" / "agents" / f.name
                dest_agent.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest_agent)
                touched.append(str(dest_agent))

    return touched


def _propagate_copilot_adapters(
    project_root: Path,
    name: str,
    copilot_adapter: Path,
) -> list[str]:
    """Propagate all copilot adapter artifacts for one primitive. Returns touched paths."""
    touched: list[str] = []
    github_bundle = copilot_adapter / "github-bundle"

    # github-bundle/skills/<skill-dir>/  (dir name may be kebab-case, not underscore)
    skills_parent = github_bundle / "skills"
    if skills_parent.is_dir():
        for skill_dir in sorted(skills_parent.iterdir()):
            if skill_dir.is_dir():
                dest_copilot_skill = project_root / ".github" / "skills" / skill_dir.name
                touched.extend(_copy_dir_clean(skill_dir, dest_copilot_skill))
                logger.debug("Copied copilot skill for %r → %s", name, dest_copilot_skill)

    # github-bundle/prompts/
    prompts_src = github_bundle / "prompts"
    if prompts_src.is_dir():
        dest_prompts = project_root / ".github" / "prompts"
        touched.extend(_copy_files_into(prompts_src, dest_prompts))
        logger.debug("Merged copilot prompts for %r → %s", name, dest_prompts)

    # hooks-snippet.json
    hooks_snippet = copilot_adapter / "hooks-snippet.json"
    if hooks_snippet.is_file():
        target_hooks = project_root / ".github" / "hooks" / "hooks.json"
        target_hooks.parent.mkdir(parents=True, exist_ok=True)
        _deep_merge_hooks_json(target_hooks, hooks_snippet)
        touched.append(str(target_hooks))

    # github-bundle/agents/
    copilot_agents_src = github_bundle / "agents"
    if copilot_agents_src.is_dir():
        dest_github_agents = project_root / ".github" / "agents"
        touched.extend(_copy_files_into(copilot_agents_src, dest_github_agents))
        logger.debug("Merged copilot agents for %r → %s", name, dest_github_agents)

    return touched


# ---------------------------------------------------------------------------
# Harness adapter registry (FLEX-4, #176)
# ---------------------------------------------------------------------------


@runtime_checkable
class PropagatorAdapter(Protocol):
    """A harness adapter knows how to copy a primitive's adapter files into the
    consuming project tree for its harness.

    Built-ins ship for Claude Code and Copilot. A third-party package can add a
    new harness (e.g. Cursor → ``.cursor/rules/``) by registering an entry point
    under ``forge_foundry.propagator_adapters`` whose value points at a class with
    this shape.
    """

    name: str  # "claude_code", "copilot", "cursor", …

    def propagate(self, project_root: Path, primitive_name: str, adapter_dir: Path) -> list[str]:
        """Copy adapter files into *project_root*. Returns destination paths touched."""
        ...


class ClaudeCodeAdapter:
    """Built-in :class:`PropagatorAdapter` for ``adapters/claude_code/``."""

    name = "claude_code"

    def propagate(self, project_root: Path, primitive_name: str, adapter_dir: Path) -> list[str]:
        return _propagate_claude_adapters(project_root, primitive_name, adapter_dir)


class CopilotAdapter:
    """Built-in :class:`PropagatorAdapter` for ``adapters/copilot/``."""

    name = "copilot"

    def propagate(self, project_root: Path, primitive_name: str, adapter_dir: Path) -> list[str]:
        return _propagate_copilot_adapters(project_root, primitive_name, adapter_dir)


class CodexAdapter:
    """Built-in :class:`PropagatorAdapter` for OpenAI Codex."""

    name = "codex"

    def propagate(
        self,
        project_root: Path,  # noqa: ARG002
        primitive_name: str,  # noqa: ARG002
        adapter_dir: Path,  # noqa: ARG002
    ) -> list[str]:
        # Codex has no legacy adapters/<harness>/ tree; canonical emission handles it.
        return []


class AntigravityAdapter:
    """Built-in :class:`PropagatorAdapter` for Antigravity (succeeds Gemini CLI)."""

    name = "antigravity"

    def propagate(
        self,
        project_root: Path,  # noqa: ARG002
        primitive_name: str,  # noqa: ARG002
        adapter_dir: Path,  # noqa: ARG002
    ) -> list[str]:
        return []


class CursorAdapter:
    """Built-in :class:`PropagatorAdapter` for Cursor (#749).

    Cursor is a canonical emitter target (``_EMITTER_HARNESSES``); all 30
    primitives reach it via canonical emission (``.cursor/rules/``). This adapter
    exists for protocol compliance and FLEX-4 discoverability — it carries no
    legacy ``adapters/cursor/`` tree, so its legacy-copy path is a no-op.
    """

    name = "cursor"

    def propagate(
        self,
        project_root: Path,  # noqa: ARG002
        primitive_name: str,  # noqa: ARG002
        adapter_dir: Path,  # noqa: ARG002
    ) -> list[str]:
        return []


_HARNESS_REGISTRY: list[PropagatorAdapter] = [
    ClaudeCodeAdapter(),
    CopilotAdapter(),
    CodexAdapter(),
    AntigravityAdapter(),
    CursorAdapter(),
]
_PROPAGATOR_ENTRY_POINT_GROUP = "forge_foundry.propagator_adapters"

# Harnesses the emitter (forge_sync.emitter.targets) knows how to target canonically.
# A third-party harness registered via entry point gets legacy-adapter copy only
# (no canonical emission), failing-safe instead of raising a KeyError inside
# emit_primitive's target dispatch.  Update this set whenever a new built-in
# harness target module is added to forge_sync/emitter/targets/.
_EMITTER_HARNESSES: frozenset[str] = frozenset(
    {"claude_code", "copilot", "codex", "antigravity", "cursor"}
)


def register_harness(adapter: PropagatorAdapter) -> None:
    """Register a :class:`PropagatorAdapter` at runtime (FLEX-4, #176).

    Plugins distributed as packages should prefer declaring the entry point in
    their own ``pyproject.toml`` so discovery is transparent. Programmatic
    registration is useful for tests and ad-hoc consumer setup.
    """
    _HARNESS_REGISTRY.append(adapter)


def _discover_harnesses() -> list[PropagatorAdapter]:
    """Built-ins + entry-point plugins, in order; first declaration wins on name."""
    discovered: list[PropagatorAdapter] = list(_HARNESS_REGISTRY)
    seen = {a.name for a in discovered}
    for ep in entry_points(group=_PROPAGATOR_ENTRY_POINT_GROUP):
        if ep.name in seen:
            continue
        adapter = ep.load()()
        discovered.append(adapter)
        seen.add(ep.name)
    return discovered


def discover_harnesses() -> list[str]:
    """Names of registered harness adapters (FLEX-4, #176)."""
    return [a.name for a in _discover_harnesses()]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def propagate_manifest_env(
    project_root: Path,
    env: dict[str, str],
) -> Path | None:
    """Write ``manifest.env`` to ``.foundry/env`` so SKILL.md / prompts inherit defaults.

    Issue #435 / SPEC §13 OQ7. BFSI consumers (and anyone who wants
    auditable env defaults) declare e.g. ``FORGE_HARNESS_NO_FALLBACK: "1"`` in
    ``.foundry/manifest.yaml::env`` and this propagator writes a
    ``KEY=VALUE`` envfile that downstream primitives source. SKILL.md is
    expected to add a one-line ``set -a; source .foundry/env; set +a`` step
    before invoking ``forge-validate`` (that wiring is Phase C; this function
    is the upstream mechanism Phase C will rely on).

    Note (#551 Part A, 2026-06-03): ``FORGE_HARNESS_NO_FALLBACK=1`` is now
    redundant under hard-error-by-default semantics — propagation is kept for
    manifest back-compat only. New consumers should use
    ``FORGE_HARNESS_ALLOW_FALLBACK=1`` (default unset = hard-error) instead.

    Behavior:

    - Empty / missing ``env`` → no file written, ``None`` returned.
    - Otherwise → write deterministic ``KEY=VALUE`` lines (sorted by key) to
      ``project_root/.foundry/env``; the file is overwritten on every run so
      a key the consumer removed from the manifest disappears from the file.

    Args:
        project_root: Consuming-project root (must already contain
            ``.foundry/manifest.yaml``).
        env: Mapping of env-var name → string value from
            :class:`SyncManifest.env`.

    Returns:
        The path the file was written to, or ``None`` if ``env`` was empty.

    Raises:
        ValueError: if a key is not a valid env-var identifier
            (``[A-Z_][A-Z0-9_]*``), or if a value contains any C0 control
            character (``\\x00``-``\\x1f``, including newline/carriage return/NUL),
            ``$`` or backtick — see the SEC-11 note below.
    """
    if not env:
        return None

    import re  # noqa: PLC0415

    key_pattern = re.compile(r"^[A-Z_][A-Z0-9_]*$")
    # SEC-11: .foundry/env is consumed via `set -a; source .foundry/env`, so the
    # shell *interprets* each value. A newline/CR would inject an extra KEY=VALUE
    # line; ``$`` triggers variable/command expansion and a backtick triggers
    # command substitution (``KEY=$(whoami)`` / ``KEY=`whoami` `` would execute on
    # source). A NUL (``\x00``) can truncate the emitted line in some POSIX shells
    # (string terminator), and no other C0 control character (tab, ESC, …) is a
    # legitimate config value either — so the whole C0 range ``\x00-\x1f`` (which
    # subsumes ``\n``/``\r``) is rejected alongside ``$`` and backtick. A manifest
    # env value is config, not arbitrary data, so any of these is rejected loudly
    # at sync time. Surviving values are additionally single-quoted on output
    # (below) so spaces, ``;``, globs, etc. stay literal — defense in depth, and it
    # lets legitimate values with those characters pass.
    bad_value_pattern = re.compile(r"[\x00-\x1f$`]")
    for key, value in env.items():
        if not key_pattern.match(key):
            raise ValueError(
                f"manifest.env key {key!r} is not a valid env-var identifier; "
                "must match ^[A-Z_][A-Z0-9_]*$"
            )
        if bad_value_pattern.search(value):
            raise ValueError(
                f"manifest.env value for {key!r} contains a forbidden character "
                "(a C0 control character such as NUL/newline/carriage return, "
                "'$', or backtick); .foundry/env is sourced, so these would "
                "inject lines, truncate the line, or trigger shell expansion "
                "(SEC-11)"
            )

    env_path = project_root / ".foundry" / "env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    # Deterministic ordering so consumers can diff between runs.
    lines = [
        "# Auto-generated by forge-foundry-sync from .foundry/manifest.yaml::env.",
        "# Source this file before invoking forge-validate to pick up env defaults",
        "# (e.g. FORGE_HARNESS_ALLOW_FALLBACK=1; FORGE_HARNESS_NO_FALLBACK=1 is a",
        "# back-compat no-op under #551 Part A).  SPEC §13 OQ7.",
    ]
    for key in sorted(env):
        # Single-quote (POSIX): everything between single quotes is literal, so
        # spaces / ``;`` / globs can't run a command. Values are pre-validated to
        # contain no newline/$/backtick; an embedded ``'`` is escaped as ``'\''``.
        quoted = "'" + env[key].replace("'", "'\\''") + "'"
        lines.append(f"{key}={quoted}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.debug("Wrote manifest.env defaults → %s (%d keys)", env_path, len(env))
    return env_path


def propagate_adapters(
    project_root: Path,
    primitive_names: list[str],
    enabled_harnesses: tuple[str, ...] | None = None,
) -> dict[str, list[str]]:
    """Copy harness-native adapter files from .foundry/primitives/ into the project tree.

    For each primitive in ``project_root/.foundry/primitives/<name>/``:

    - ``adapters/claude_code/`` → ``project_root/.claude/skills/<name>/``  (clean replace)
    - ``adapters/copilot/github-bundle/skills/<name>/``
        → ``project_root/.github/skills/<name>/``  (clean replace)
    - ``adapters/copilot/github-bundle/prompts/``
        → ``project_root/.github/prompts/``  (per-file merge, no wipe)
    - ``adapters/claude_code/settings-snippet.json``
        → deep-merged into ``project_root/.claude/settings.json``
    - ``adapters/copilot/hooks-snippet.json``
        → deep-merged into ``project_root/.github/hooks/hooks.json``
    - ``adapters/claude_code/journey-curator.md`` (or any ``agents/`` file)
        → ``project_root/.claude/agents/<name>.md``
    - ``adapters/copilot/github-bundle/agents/`` contents
        → ``project_root/.github/agents/``  (per-file merge)

    Args:
        project_root: Root of the target consuming project.
        primitive_names: Names of primitives to propagate (must already exist under
            ``project_root/.foundry/primitives/``).
        enabled_harnesses: Optional filter of harness names to emit. When ``None``
            (the default), all discovered harnesses are used — preserving backward
            compatibility for callers that do not pass this argument. When provided
            (e.g. from ``manifest.harnesses``), only harnesses whose ``name``
            appears in the tuple are emitted.

    Returns:
        Mapping of primitive name → sorted list of destination paths created/modified.
    """
    primitives_root = project_root / ".foundry" / "primitives"
    result: dict[str, list[str]] = {}

    # Compute the filtered harness list once; used by both emission branches.
    harnesses = [
        h for h in _discover_harnesses() if enabled_harnesses is None or h.name in enabled_harnesses
    ]
    if enabled_harnesses is not None and not harnesses:
        logger.warning(
            "No registered harness matched enabled_harnesses=%r; no adapters will be "
            "emitted. Known harnesses: %s.",
            enabled_harnesses,
            [h.name for h in _discover_harnesses()],
        )

    for name in primitive_names:
        prim_dir = primitives_root / name
        if not prim_dir.is_dir():
            logger.warning(
                "Primitive %r not found under %s — skipping propagation.", name, primitives_root
            )
            continue

        touched: list[str] = []
        has_canonical_skill = (prim_dir / "skill" / "SKILL.md").is_file()
        # Parse the canonical skill/agents/rules ONCE per primitive (harness-independent).
        # The loaders do the has-any check internally (early return [] / None when the
        # directory/file is absent), so we call them exactly once and derive has_canonical
        # from the result — avoiding the double-glob pattern where any(glob) probed for
        # existence and then load_canonical_* globbed again.
        sources = load_canonical_sources(prim_dir)
        has_canonical = sources.skill is not None or bool(sources.agents) or bool(sources.rules)
        if has_canonical:
            # Emit canonical sources (skill/agents/rules) per ENABLED, EMITTER-KNOWN harness.
            # A third-party registered harness the emitter can't target is skipped here
            # and falls through to the legacy-adapter copy below (_EMITTER_HARNESSES guard).
            for harness in harnesses:
                if harness.name not in _EMITTER_HARNESSES:
                    continue
                touched.extend(
                    write_emitted(project_root, emit_from_sources(sources, harness.name))
                )
            # CI is harness-independent — emit it once per primitive (not per harness)
            # from the canonical ci.yaml (Phase 2). Empty for skill-first primitives.
            touched.extend(write_emitted(project_root, emit_ci_for_primitive(prim_dir)))
        # Legacy adapter copy: still applies UNLESS a canonical skill already supplies the
        # skill-dir delivery (avoids wiping the emitted .../skills/<name>/ via clean-replace).
        # A half-migrated primitive (canonical agents/rules + legacy adapters, no canonical
        # skill — e.g. M5 today) emits its canonical agents AND keeps its legacy skill
        # delivery; there is no dest collision because emit_primitive emits no skill dir
        # for a skill-less primitive.
        if not has_canonical_skill:
            # Legacy: copy checked-in adapters/<harness>/ (FLEX-4 #176 unchanged behavior).
            # A consumer can register `cursor` / `jetbrains` / etc. and this loop picks
            # them up without a propagator rewrite.
            for harness in harnesses:
                adapter_dir = prim_dir / "adapters" / harness.name
                if adapter_dir.is_dir():
                    touched.extend(harness.propagate(project_root, name, adapter_dir))
        result[name] = sorted(set(touched))

    # Deprecation-alias skills (#1326/#1327): synthetic forwarding shims for
    # renamed slash commands. These are NOT backed by a primitive directory, so
    # they are emitted once here (after the per-primitive loop) — per enabled,
    # emitter-known harness — through the same path-guarded write_emitted sink.
    # They cover exactly one minor version and are removed next major (see
    # forge_sync/emitter/aliases.py::DEPRECATED_SKILL_ALIASES).
    alias_touched: list[str] = []
    for harness in harnesses:
        if harness.name not in _EMITTER_HARNESSES:
            continue
        alias_touched.extend(write_emitted(project_root, emit_alias_skills(harness.name)))
    if alias_touched:
        result["_deprecation_aliases"] = sorted(set(alias_touched))

    return result
