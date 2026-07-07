import re

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
