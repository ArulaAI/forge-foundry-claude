from pathlib import Path
from scripts._vendor.copy_skills import discover_primitives, copy_skills


def _fake_primitive(root: Path, name: str, slug: str, *, shared=False,
                    delegates=False, ref="hello.md"):
    p = root / name / "skill"
    p.mkdir(parents=True)
    fm = f"---\nname: {slug}\n"
    if delegates:
        fm += "delegates-to-cli: true\n"
    fm += "---\n"
    (p / "SKILL.md").write_text(fm + f"body for {slug}\n")
    (p / "references").mkdir()
    (p / "references" / ref).write_text("ref content")
    if shared:
        s = root / name / "shared"
        s.mkdir()
        (s / "__init__.py").write_text("")
    return p


def test_discover_reads_slug_and_flags(tmp_path):
    prims_root = tmp_path / "primitives"
    prims_root.mkdir()
    _fake_primitive(prims_root, "data_flow_map", "data-flow-map",
                    shared=True, delegates=True)
    found = discover_primitives(prims_root)
    assert len(found) == 1
    assert found[0].slug == "data-flow-map"
    assert found[0].delegates_to_cli is True and found[0].has_shared is True


def test_copy_skills_copies_references(tmp_path):
    prims_root = tmp_path / "primitives"
    prims_root.mkdir()
    _fake_primitive(prims_root, "code_review", "code-review")
    out = tmp_path / "skills"
    n = copy_skills(discover_primitives(prims_root), out)
    assert n == 1
    assert (out / "code-review" / "SKILL.md").is_file()
    assert (out / "code-review" / "references" / "hello.md").read_text() == "ref content"
