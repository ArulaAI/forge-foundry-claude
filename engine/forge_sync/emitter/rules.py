from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from forge_sync.emitter.models import CanonicalRule

# Harnesses that support a path-scope glob field natively.
# Others (codex, antigravity) degrade to a nested AGENTS.md placed at the
# glob's directory prefix. Cursor supports `globs:` in `.mdc` frontmatter (#749).
_GLOB_HARNESS = {"claude_code", "copilot", "cursor"}

# Smallest printable ASCII codepoint; anything below is a control char (SEC-10, A1).
_FIRST_PRINTABLE = 0x20


def _yaml_escape(value: str) -> str:
    """Escape a string for a double-quoted YAML scalar (matches R1 rule_renderer)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _validate_glob(glob: str) -> None:
    """Reject path-scoped globs that are unsafe to turn into filesystem paths.

    SEC-10: for the codex/antigravity degrade path a glob becomes a real
    filesystem prefix (``{prefix}/AGENTS.md``). An absolute glob (``/abs/**``)
    or one containing a ``..`` segment (``../../**``) would escape the project
    root once written by ``propagator.write_emitted``. Claude/Copilot embed the
    glob in frontmatter rather than a path, but validating every path-scoped
    glob at one point keeps the contract uniform and the write-sink guard honest.
    """
    if glob.startswith("/"):
        raise ValueError(f"rule path glob {glob!r} must be relative and contain no '..' (SEC-10)")
    if ".." in PurePosixPath(glob).parts:
        raise ValueError(f"rule path glob {glob!r} must be relative and contain no '..' (SEC-10)")
    # SEC-10 (A1): a glob carrying a control char (newline/CR/tab/NUL/…) would
    # corrupt or inject the claude `paths:` / copilot `applyTo:` frontmatter once
    # _yaml_escape (which escapes only \ and ") embeds it. A legitimate file glob
    # never contains control chars, so reject them outright.
    if any(ord(c) < _FIRST_PRINTABLE for c in glob):
        raise ValueError(f"rule path glob {glob!r} must not contain control characters (SEC-10)")


def _nested_dir_prefix(glob: str) -> str:
    """Directory prefix of a glob, for nested-AGENTS.md degradation.

    'src/**/*.ts' -> 'src';  '**/*.py' -> '.';  'src/*.py' -> 'src';  '*.md' -> '.';
    'src/' -> 'src';  'src' -> 'src'
    """
    if "**" in glob:
        head = glob.split("**", 1)[0].rstrip("/")
        return head or "."
    if not any(c in glob for c in "*?["):
        # No wildcard at all: a bare directory glob ('src/' or 'src'). Treat the
        # whole thing as a directory prefix (B1) — returning '.' here would both
        # be wrong and alias the always-rule AGENTS.md key, silently clobbering it.
        return glob.rstrip("/") or "."
    # A wildcard but no '**' (e.g. 'src/*.py'): use the file's parent directory.
    return str(PurePosixPath(glob).parent)


def _always(rule: CanonicalRule, harness: str) -> dict[str, str]:
    out = {"AGENTS.md": rule.body.rstrip() + "\n"}
    if harness == "claude_code":
        out["CLAUDE.md"] = "@AGENTS.md\n"
    if harness == "cursor":
        # Always-scoped rule for Cursor: alwaysApply: true (no globs needed) (#749).
        fm = f'---\ndescription: "{_yaml_escape(rule.name)}"\nalwaysApply: true\n---'
        out[f".cursor/rules/{rule.name}.mdc"] = fm + "\n\n" + rule.body.rstrip() + "\n"
    return out


def _scoped(rule: CanonicalRule, harness: str) -> dict[str, str]:
    # SEC-10: validate every path-scoped glob before it is embedded in
    # frontmatter or turned into a nested-AGENTS.md filesystem prefix.
    for glob in rule.paths:
        _validate_glob(glob)
    if harness in _GLOB_HARNESS:
        if harness == "claude_code":
            # Frontmatter ends with "---" (no trailing newline); body is separated by a
            # blank line — matching R1's rule_renderer._join_frontmatter_and_body convention.
            fm = "---\npaths:\n" + "".join(f'  - "{_yaml_escape(p)}"\n' for p in rule.paths) + "---"
            return {f".claude/rules/{rule.name}.md": fm + "\n\n" + rule.body.rstrip() + "\n"}
        if harness == "cursor":
            # Cursor path-scoped rule: comma-separated globs in `.mdc` frontmatter (#749).
            globs = ", ".join(_yaml_escape(p) for p in rule.paths)
            fm = f'---\ndescription: "{_yaml_escape(rule.name)}"\nglobs: "{globs}"\n---'
            return {f".cursor/rules/{rule.name}.mdc": fm + "\n\n" + rule.body.rstrip() + "\n"}
        # copilot — same blank-line separator convention as claude_code above.
        apply_to = ",".join(_yaml_escape(p) for p in rule.paths)
        fm = f'---\napplyTo: "{apply_to}"\n---'
        content = fm + "\n\n" + rule.body.rstrip() + "\n"
        return {f".github/instructions/{rule.name}.instructions.md": content}
    # Codex / Antigravity: no glob field -> nested AGENTS.md per scope prefix.
    # Multiple globs sharing a directory prefix intentionally collapse to one
    # nested AGENTS.md at that prefix (same body); dict-key overwrite is the
    # desired dedup, not a bug.
    out: dict[str, str] = {}
    for glob in rule.paths:
        prefix = _nested_dir_prefix(glob)
        # A root prefix ('.') writes the top-level AGENTS.md (the same key the
        # always-rule path uses), not a distinct './AGENTS.md' alias that would
        # split content across two keys (B1).
        key = "AGENTS.md" if prefix == "." else f"{prefix}/AGENTS.md"
        out[key] = rule.body.rstrip() + "\n"
    return out


def emit_rule(rule: CanonicalRule, harness: str) -> dict[str, str]:
    if rule.scope == "path" and rule.paths:
        return _scoped(rule, harness)
    return _always(rule, harness)
