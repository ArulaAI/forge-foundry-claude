from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# Shared vocabulary consumed by the emitter modules (targets/skills/agents);
# they live here so every emitter module can import from a single authoritative source.
NEUTRAL_TOOLS = frozenset({"shell", "read", "edit", "search"})
MODEL_TIERS = frozenset({"default", "fast", "deep"})
HARNESSES = ("claude_code", "copilot", "codex", "antigravity", "cursor")


@dataclass(frozen=True)
class CanonicalSkill:
    name: str
    description: str
    body: str
    # Mapping (read-only view) to signal that mutation is not expected; plain
    # dicts from the loader satisfy Mapping[str, str] without any wrapping.
    references: Mapping[str, str]
    scripts: Mapping[str, str]
    # Phase 5: marks Python-load-bearing primitives whose canonical body
    # legitimately delegates to ``forge-validate <name>`` via Bash (their Python
    # is the implementation). The completeness gate exempts such bodies from the
    # no-forge-validate forbid-rule; the emitter passes the marker through to the
    # per-harness frontmatter so it survives a round-trip.
    delegates_to_cli: bool = False
    # Neutral tool intents declared in frontmatter (``tools: [shell, read, edit]``).
    # Defaults to ``("shell", "read")`` when unset — the common case for most skills.
    tools: tuple[str, ...] = ("shell", "read")


@dataclass(frozen=True)
class CanonicalAgent:
    name: str
    description: str
    instructions: str
    tools: tuple[str, ...]
    model_tier: str


@dataclass(frozen=True)
class CanonicalRule:
    name: str
    scope: str
    paths: tuple[str, ...]
    body: str


# --- CI (Phase 2: ci.yaml → GH Actions / Azure DevOps / CodeBuild) -----------
# Design: docs/superpowers/specs/2026-06-14-phase2-ci-emitter-design.md.
CI_OUTPUT_FORMATS = frozenset({"sarif", "json", "markdown"})
#: How a primitive's output is shaped — drives what the emitter records in the agent
#: PROMPT and what the CI publishes. ``file``: prompt records ``Output format: <fmt>.
#: Output file: <cmd>.<ext>.`` → publish that file (SARIF upload for sarif, else an
#: artifact). ``dir``: prompt records ``Output format: <fmt>.`` only (the primitive
#: receives its output directory via its own declared input) → publish
#: ``.foundry/context/``. ``none``: neither recorded (the primitive self-manages both)
#: → publish ``.foundry/context/``.
CI_OUTPUT_MODES = frozenset({"file", "dir", "none"})
#: output_format → emitted artifact extension.
CI_OUTPUT_EXT: Mapping[str, str] = {"sarif": "sarif", "json": "json", "markdown": "md"}
#: Conventional output directory for ``dir``/``none`` primitives (M-tier digests etc.).
CI_CONTEXT_DIR = ".foundry/context"

#: Per-harness agent-CLI invocation template (2026-06-18 harness-layer-removal design §3.2).
#: ``{prompt}`` is substituted with the shell variable reference ``"$PROMPT"``. The agent
#: session loads the primitive's SKILL.md and runs the extract→judge→continue protocol — it
#: *is* the harness, so CI no longer threads ``--harness`` into ``forge-validate``.
#: Only ``claude_code`` has confirmed CLI flags; the others are provisional placeholders the
#: emitter flags with a consumer-facing ``# NOTE: … provisional …`` comment naming what the
#: consumer should confirm (the agent-CLI binary name + prompt/headless flags) before enabling.
#:
#: Least-privilege tool scope: the skill-first CI loop needs ``Bash`` (to run
#: ``forge-validate ... --extract-only`` / ``--continue``), ``Read`` (to read the emitted
#: prompts), and ``Write`` (to write the report artifact). It does NOT need ``Edit`` — every
#: primitive *produces a new report*, none edits existing source — so ``Edit`` is withheld to
#: keep the emitted agent invocation minimally privileged against prompt-injection in reviewed
#: code (least-privilege; guarded by ``tests/test_ci_emitter.py``).
CI_AGENT_CLI: Mapping[str, str] = {
    "claude_code": "claude -p {prompt} --allowedTools Bash,Read,Write --max-turns 50",
    "copilot": "copilot-cli -p {prompt}",
    "codex": "codex exec {prompt}",
    "antigravity": "antigravity run {prompt}",
}
#: Harness targets whose agent-CLI invocation is provisional (flags unverified — see §7).
#:
#: TOOL-SCOPING PARITY REQUIREMENT (closes #821):
#: When confirming a provisional harness's CLI flags and removing it from this set, its
#: ``CI_AGENT_CLI`` template MUST simultaneously gain least-privilege tool-scoping equivalent
#: to the ``claude_code`` line (``--allowedTools Bash,Read,Write`` or the harness-native
#: equivalent).  CI primitives emit new reports — they never edit existing source — so ``Edit``
#: (or any broad "all tools" grant) MUST be withheld.  A partial fix that confirms flags but
#: omits tool-scoping silently ships an over-privileged invocation exposed to prompt-injection in
#: reviewed code.  The guard in ``tests/test_ci_emitter.py::test_claude_code_template_tool_scope``
#: and ``tests/test_ci_emitter.py::test_provisional_harnesses_carry_todo_comment`` enforce
#: these invariants at commit time.
CI_PROVISIONAL_HARNESSES = frozenset({"copilot", "codex", "antigravity"})
#: The default agent-CLI target the CI emitter renders for (the one with confirmed flags).
CI_DEFAULT_HARNESS = "claude_code"


@dataclass(frozen=True)
class CanonicalCIInput:
    """One workflow input, mapped deterministically into the agent PROMPT (a positional
    target, a labelled sentence, or a ``Pass --flag`` directive)."""

    name: str
    description: str
    type: str  # "string" | "choice" | "bool"
    default: str
    required: bool
    choices: tuple[str, ...]  # non-empty only for type == "choice"
    #: Inner regex validating a free-text value (SEC-8). The emitter anchors it as
    #: ``^(<validate>)$``; ``None`` for choice/bool inputs (Click-validated at the CLI).
    validate: str | None
    #: How the input maps to a ``forge-validate`` arg: ``"positional"`` |
    #: ``"--flag-name"`` (value follows) | ``"flag-when-true"`` (bool → bare flag).
    arg: str
    #: For string/choice flags: omit the flag when value equals this sentinel
    #: (e.g. ``"none"``/``""``). ``None`` means always pass it.
    omit_when: str | None


@dataclass(frozen=True)
class CanonicalCI:
    """The dialect-neutral CI source (``primitives/<name>/ci.yaml``).

    Rendered to all three CI targets by ``forge_sync.emitter.ci.emit_ci``. The emitted
    run step invokes the agent CLI for the chosen harness (the agent loads the
    primitive's SKILL.md); this config itself has zero model involvement.
    """

    command: str
    display_name: str
    #: The slash-command the CI agent session invokes (e.g. ``/code-review``). Replaces the
    #: v2 ``harness:`` field (2026-06-18 harness-layer-removal design §3.1): the agent loads
    #: the primitive's SKILL.md and runs the protocol in-session, so the CLI no longer needs
    #: a harness. Validated at load time against ``^/[A-Za-z0-9][A-Za-z0-9._-]*$``.
    skill: str
    output_mode: str  # ∈ CI_OUTPUT_MODES (file | dir | none)
    output_format: str | None  # ∈ CI_OUTPUT_FORMATS; required for file mode, optional for dir
    schema_ref: str | None  # primitive-relative; no ".."/absolute (load-time guard)
    permissions: Mapping[str, str]
    inputs: tuple[CanonicalCIInput, ...]
    #: Optional extra deterministic shell steps ({name, run}); each ``run`` is
    #: load-time-rejected if it carries shell metacharacters (#631 M1).
    deterministic_steps: tuple[Mapping[str, str], ...]
