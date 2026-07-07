"""R1 /rules-gen — dual-harness rule renderer.

Implements Stage B of the R1 pipeline per
``primitives/rules_gen/SPEC.md`` §"Pipeline stages": converts a validated
:class:`primitives.rules_gen.shared.catalog_loader.Catalog` into the two
output strings R1 ships into a consuming repo:

* :func:`render_claude_rule` — ``.claude/rules/<name>.md`` with a
  ``paths:`` YAML-list frontmatter (Claude Code-native).
* :func:`render_copilot_instruction` — ``.github/instructions/<name>.instructions.md``
  with a comma-separated ``applyTo:`` frontmatter (Copilot-native).

The body of both files is **byte-identical** — only the frontmatter
differs. The body is a markdown document whose first heading is the
catalog ``description`` (so the rendered file is a curator-authored
title rather than a slugified ``name``), followed by one ``##`` section
per ``RuleSection`` with a bullet list of rules.

Empty-``paths`` catalogs render with **no frontmatter at all** — both
``paths:`` (Claude Code) and ``applyTo:`` (Copilot) are omitted, and the
surrounding ``---`` delimiters are dropped too. Claude Code and Copilot
both treat an absent frontmatter as an always-on rule, which is the
SPEC.md §"File contract" v0.1.0 semantics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primitives.rules_gen.shared.catalog_loader import Catalog

__all__ = ["render_claude_rule", "render_copilot_instruction"]


# ---------------------------------------------------------------------------
# Public renderers
# ---------------------------------------------------------------------------


def render_claude_rule(catalog: Catalog) -> str:
    """Render the ``.claude/rules/<name>.md`` content for *catalog*.

    The frontmatter (when emitted) carries ``paths:`` as a YAML list so
    Claude Code's path-scoping engine can match each glob independently.
    A catalog with ``paths == []`` skips the frontmatter entirely; the
    rule is then loaded for every file under the consuming repo.

    Args:
        catalog: A validated :class:`Catalog` instance.

    Returns:
        The full rendered markdown document as a single UTF-8 string with
        ``\\n`` line endings.
    """
    frontmatter = _render_claude_frontmatter(catalog.paths)
    body = _render_body(catalog)
    return _join_frontmatter_and_body(frontmatter, body)


def render_copilot_instruction(catalog: Catalog) -> str:
    """Render the ``.github/instructions/<name>.instructions.md`` content.

    The frontmatter (when emitted) carries ``applyTo:`` as a single
    comma-separated string per the Copilot instruction-file convention.
    A catalog with ``paths == []`` skips the frontmatter entirely.

    Args:
        catalog: A validated :class:`Catalog` instance.

    Returns:
        The full rendered markdown document as a single UTF-8 string with
        ``\\n`` line endings. The body content is **byte-identical** to
        :func:`render_claude_rule`'s body — only the frontmatter differs.
    """
    frontmatter = _render_copilot_frontmatter(catalog.paths)
    body = _render_body(catalog)
    return _join_frontmatter_and_body(frontmatter, body)


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------


def _render_claude_frontmatter(paths: list[str]) -> str:
    """Return the Claude Code ``paths:`` YAML-list frontmatter, or empty string."""
    if not paths:
        return ""
    lines = ["---", "paths:"]
    lines.extend(f'  - "{_yaml_escape(p)}"' for p in paths)
    lines.append("---")
    return "\n".join(lines)


def _render_copilot_frontmatter(paths: list[str]) -> str:
    """Return the Copilot ``applyTo:`` comma-separated frontmatter, or empty string."""
    if not paths:
        return ""
    joined = ",".join(paths)
    return f'---\napplyTo: "{_yaml_escape(joined)}"\n---'


def _yaml_escape(value: str) -> str:
    """Escape a YAML double-quoted scalar.

    R1 catalog paths and applyTo strings are file globs whose realistic
    character set is ``[A-Za-z0-9*?/.{}_-]`` plus the rare embedded
    backslash. We only need to escape the two characters that break a
    double-quoted YAML scalar: ``\\`` and ``"``.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Body rendering
# ---------------------------------------------------------------------------


def _render_body(catalog: Catalog) -> str:
    """Render the markdown body of a catalog.

    The body is byte-identical between the Claude Code and Copilot
    outputs, so it lives in a single helper. Format:

    ``# {description}\\n\\n## {section.heading}\\n\\n- {rule}\\n- ...``

    Sections are separated by a blank line. Within a section, rules are a
    bullet list. No trailing newline beyond the final rule — callers
    that want a terminal newline can append one.
    """
    parts: list[str] = [f"# {catalog.description}"]
    for section in catalog.sections:
        parts.append("")  # blank line between blocks
        parts.append(f"## {section.heading}")
        parts.append("")
        for rule in section.rules:
            parts.append(f"- {rule}")
    return "\n".join(parts)


def _join_frontmatter_and_body(frontmatter: str, body: str) -> str:
    """Concatenate the frontmatter and body with a single blank line between."""
    if not frontmatter:
        return body
    return f"{frontmatter}\n\n{body}"
