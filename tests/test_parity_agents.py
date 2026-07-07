"""Guards FIX 1 forever: vendored agents must be valid Claude Code agents.

A Claude Code agent is markdown that opens with a `---` YAML frontmatter block
carrying at least `name:` and `description:` fields, followed by the system-prompt
body. The old vendor bug emitted `<!-- vendored ... -->` + a ```yaml``` fence
instead, producing unloadable stubs — this test fails if that regression returns.
"""

from __future__ import annotations

import pytest


def _agent_files(repo_root):
    return sorted((repo_root / "agents").glob("*.md"))


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Minimal top-level `key: value` frontmatter parse (no yaml dep).

    The bare test env has no PyYAML, so read only the top-level scalar keys we
    assert on (name, description) from the leading `---`…`---` block.
    """
    assert text.startswith("---")
    _, _, rest = text.partition("---\n")
    fm_text, sep, _body = rest.partition("\n---\n")
    assert sep, "frontmatter block is not closed with '---'"
    out: dict[str, str] = {}
    for line in fm_text.splitlines():
        # Top-level keys only (no leading whitespace = not a continuation/nested).
        if line[:1].isspace() or ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def test_agents_dir_has_at_least_one_agent(repo_root):
    assert _agent_files(repo_root), "expected at least one vendored agent under agents/"


def test_every_agent_starts_with_frontmatter(repo_root):
    for md in _agent_files(repo_root):
        text = md.read_text()
        assert text.startswith("---"), (
            f"{md.name} must start with '---' YAML frontmatter, not an HTML comment "
            f"or code fence; got: {text[:40]!r}"
        )


def test_every_agent_frontmatter_has_name_and_description(repo_root):
    for md in _agent_files(repo_root):
        fm = _parse_frontmatter(md.read_text())
        assert fm.get("name"), f"{md.name} frontmatter missing 'name'"
        assert fm.get("description"), f"{md.name} frontmatter missing 'description'"


def test_mainframe_recon_compat_shim_not_vendored(repo_root):
    # The mainframe-recon agent is a backwards-compat re-export shim (denied).
    names = {md.stem for md in _agent_files(repo_root)}
    assert "mainframe-recon" not in names, (
        "mainframe-recon is a compat shim and must not be vendored"
    )
    assert "flavor-recon" in names, "the real flavor-recon agent must be vendored"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
