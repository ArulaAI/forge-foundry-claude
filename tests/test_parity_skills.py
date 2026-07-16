import re

# The 15 Python-load-bearing (delegates-to-cli: true) primitives at the vendored
# source version. Their SKILL.md bodies route work through `forge-validate <slug>`,
# which the vendor rewrites to the `${CLAUDE_PLUGIN_ROOT}/bin/forge-validate` shim.
# (`mod-modernization-strategist` was renamed `mod-sql-strategist` upstream; the
# app-tier `mod-modernization-scope` is the M-tier synthesizer added since v5.)
EXPECTED_PLB = {
    "code-gen", "data-flow-map", "functional-map", "mod-db-schema-analyze",
    "mod-modernization-scope", "mod-playbook-discovery", "mod-sp-converter",
    "mod-sql-strategist", "mod-stored-proc-analyze", "mod-synth-logs",
    "repo-digest", "rules-gen", "test-from-spec", "test-scaffold",
    "work-orchestrator",
}


def _skills(repo_root):
    return repo_root / "skills"


def test_all_skills_present(repo_root):
    # 38 canonical primitives vendored from forge-foundry-primitives v8.5.1.
    dirs = [p for p in _skills(repo_root).iterdir() if (p / "SKILL.md").is_file()]
    assert len(dirs) == 38, f"expected 38 skills, got {len(dirs)}"


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


def test_state_dir_preserved(repo_root):
    # The state dir `.foundry/context` must be PRESERVED, not rewritten. The
    # engine's Python defaults and every cross-primitive read use `.foundry/context`
    # (CWD-relative). The earlier `.forge-foundry/context` rewrite broke the M-tier
    # chain (M3 could not find M1's digest), so it was reverted — no skill body may
    # carry the localized form.
    localized = [md.parent.name for md in _skills(repo_root).glob("*/SKILL.md")
                 if ".forge-foundry/context" in md.read_text()]
    assert localized == [], f".foundry/context wrongly localized in: {localized}"
    # And at least one skill must still carry the correct `.foundry/context` path.
    preserved = [md.parent.name for md in _skills(repo_root).glob("*/SKILL.md")
                 if ".foundry/context" in md.read_text()]
    assert preserved, "expected .foundry/context to be present in skill bodies"
