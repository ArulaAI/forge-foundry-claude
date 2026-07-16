import json
import re

_SHIM = "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate"

# A minimum sanity floor for the vendored catalog. These tests derive the exact
# expected counts/sets from GROUND TRUTH (the vendor lock + the vendored tree) rather
# than hard-coding a per-version number, so a re-vendor never leaves them stale — this
# floor only guards against a wholly-empty or broken vendor.
_MIN_SKILLS = 30
_MIN_PLB = 10


def _skills(repo_root):
    return repo_root / "skills"


def _skill_dirs(repo_root):
    return [p for p in _skills(repo_root).iterdir() if (p / "SKILL.md").is_file()]


def _plb_slugs(repo_root):
    """The PLB set, DERIVED from the vendored tree: every skill whose SKILL.md routes
    through the ``${CLAUDE_PLUGIN_ROOT}/bin/forge-validate`` shim. Self-updating — no
    hand-maintained list to go stale on the next re-vendor."""
    return {
        p.name for p in _skills(repo_root).iterdir()
        if (p / "SKILL.md").is_file() and _SHIM in (p / "SKILL.md").read_text()
    }


def test_all_skills_present(repo_root):
    # Derive the expected count from the vendor lock the LAST vendor wrote, so the
    # assertion tracks the source version automatically (no hard-coded per-version N).
    lock = json.loads((repo_root / "engine" / "VENDOR_LOCK.json").read_text())
    expected = lock["primitive_count"]
    dirs = _skill_dirs(repo_root)
    assert len(dirs) >= _MIN_SKILLS, f"suspiciously few skills ({len(dirs)}) — broken vendor?"
    assert len(dirs) == expected, (
        f"vendored {len(dirs)} skills but VENDOR_LOCK records {expected}; re-vendor drift"
    )


def test_no_dev_commands(repo_root):
    assert not (_skills(repo_root) / "autowork").exists()
    assert not (_skills(repo_root) / "review-pr").exists()


def test_no_residual_uv_run(repo_root):
    offenders = []
    for md in _skills(repo_root).glob("*/SKILL.md"):
        if re.search(r"uv\s+run\s+--project.*forge-validate", md.read_text()):
            offenders.append(md.parent.name)
    assert offenders == [], f"un-rewritten PLB invocations in: {offenders}"


def test_plb_skills_route_through_shim(repo_root):
    # The real invariant: the shim-bearing (PLB) set is substantial AND no PLB skill
    # body retains a BARE `forge-validate <slug>` invocation line (every invocation was
    # rewritten to the shim). Derived from the tree — immune to upstream renames.
    plb = _plb_slugs(repo_root)
    assert len(plb) >= _MIN_PLB, f"suspiciously few PLB skills ({len(plb)}) — broken rewrite?"
    _bare = re.compile(r"(?<!/)\bforge-validate\s+[a-z][a-z0-9]*-[a-z0-9-]*")
    missing = []
    for slug in plb:
        md = _skills(repo_root) / slug / "SKILL.md"
        text = md.read_text()
        # Every bare `forge-validate <slug>` occurrence on a line must be the shimmed
        # form; a line with a bare invocation and no shim token is an un-rewritten leak.
        for line in text.splitlines():
            if _bare.search(line) and _SHIM not in line:
                missing.append(f"{slug}: {line.strip()}")
    assert missing == [], "un-rewritten bare forge-validate invocations:\n" + "\n".join(missing)


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
