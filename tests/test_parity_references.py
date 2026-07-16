from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "skills"

# A `forge-validate <hyphenated-slug>` occurrence that is an executable
# invocation (command value or shell). We fail on any such occurrence that is
# NOT routed through the bundled shim path.
_FORGE_VALIDATE = re.compile(r"forge-validate\s+[a-z][a-z0-9]*-[a-z0-9-]*")
# A `python -m primitives.<...>` occurrence not routed through the engine venv.
_PYTHON_M = re.compile(r"(?<!bin/)python\s+-m\s+primitives\.")


def _all_skill_files() -> list[Path]:
    # Executable invocations live in SKILL.md + reference *markdown*. JSON schema
    # files (references/schemas/*.json) are DATA — their `"description"` fields
    # legitimately mention `forge-validate <slug>` as prose describing the CLI, and
    # the vendor rewrite only rewrites shell invocations, not JSON documentation
    # strings. Excluding them avoids false positives on schema prose.
    return [
        p
        for p in SKILLS.rglob("*")
        if p.is_file() and p.suffix.lower() not in {".json"}
    ]


def test_no_residual_bare_forge_validate_invocations():
    offenders: list[str] = []
    for path in _all_skill_files():
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _FORGE_VALIDATE.search(line) and "${CLAUDE_PLUGIN_ROOT}" not in line:
                offenders.append(f"{path.relative_to(REPO)}:{i}: {line.strip()}")
    assert not offenders, "bare forge-validate invocations found:\n" + "\n".join(offenders)


def test_no_residual_bare_python_m_primitives():
    offenders: list[str] = []
    for path in _all_skill_files():
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _PYTHON_M.search(line) and "${CLAUDE_PLUGIN_ROOT}" not in line:
                offenders.append(f"{path.relative_to(REPO)}:{i}: {line.strip()}")
    assert not offenders, "bare python -m primitives invocations found:\n" + "\n".join(offenders)
