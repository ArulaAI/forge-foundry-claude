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
