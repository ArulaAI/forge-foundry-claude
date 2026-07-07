from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

import yaml

from forge_sync.emitter.models import (
    CI_OUTPUT_FORMATS,
    CI_OUTPUT_MODES,
    MODEL_TIERS,
    NEUTRAL_TOOLS,
    CanonicalAgent,
    CanonicalCI,
    CanonicalCIInput,
    CanonicalRule,
    CanonicalSkill,
)

if TYPE_CHECKING:
    from pathlib import Path

# #631 M1 (SEC): ``deterministic_steps[].run`` is emitted verbatim into a CI
# ``run:`` block. A value carrying these would allow command substitution
# (``$(...)``/backtick), chaining (``;``/``|``/``&``), redirection (``<``/``>``)
# or line injection. Plain ``$VAR`` env references are still allowed.
_BAD_STEP_RE = re.compile(r"\$\(|[`;|&<>\n\r]")

# SEC-10: skill / agent / rule identity fields flow into emitted filesystem
# paths (``.claude/skills/<name>/...``, ``.codex/agents/<name>.toml``, etc.) via
# the emitter. A name carrying a path separator or ``..`` would escape the
# project root once written. Validate identity at the source (defense in depth +
# friendlier errors) against a conservative pattern: must start with an
# alphanumeric and contain only alphanumerics, ``.``, ``_`` and ``-``. This
# admits every real name in the repo (e.g. ``spec-write``, ``flavor-recon``,
# ``mainframe-recon``, ``mod-db-schema-analyze``) while rejecting ``/``, ``..``,
# and leading ``-``/``.``. The categorical write-sink guard in
# ``forge_sync.propagator.write_emitted`` is the backstop.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: The closed set of rule scopes a canonical ``rules/*.rule.md`` may declare.
_VALID_SCOPES: frozenset[str] = frozenset({"always", "path"})

#: ci.yaml ``skill:`` field — the slash-command the CI agent session invokes
#: (e.g. ``/code-review``, ``/mod-db-schema-analyze``). It is interpolated into the
#: emitted shell PROMPT, so it is constrained to a leading ``/`` followed by a safe
#: command name (alphanumeric-led; ``.``/``_``/``-`` only) — no spaces, quotes, or
#: shell metacharacters can reach the ``run:`` block.
_SKILL_RE = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_safe_name(name: str, kind: str) -> str:
    """Return *name* if it matches :data:`_SAFE_NAME_RE`, else raise ``ValueError``."""
    if not _SAFE_NAME_RE.match(name):
        raise ValueError(
            f"invalid {kind} name {name!r}: must match {_SAFE_NAME_RE.pattern} "
            "(no path separators or '..') (SEC-10)"
        )
    return name


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Return (frontmatter_dict, body). Body excludes the leading --- block.

    Normalises CRLF/CR line endings before parsing so Windows-authored files
    are handled identically to LF files (fix #2).  If the parsed frontmatter is
    not a dict (e.g. a YAML list or scalar), the file is treated as having no
    usable frontmatter and ({}, original_text) is returned — the loader is not
    the validation gate (fix #1).
    """
    # Normalise line endings before any partitioning.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---"):
        return {}, text
    _, _, rest = text.partition("---\n")
    # The closing delimiter must be `\n---\n` (own line) or `\n---` at EOF —
    # a bare `\n---` followed by non-newline (e.g. `\n---something`) is a
    # Markdown horizontal rule or other body content, not a frontmatter closer.
    fm_text, sep, body = rest.partition("\n---\n")
    if not sep:
        # Try `\n---` at the very end of the file (no trailing newline).
        if rest.endswith("\n---"):
            fm_text = rest[: -len("\n---")]
            body = ""
        else:
            return {}, text
    fm = yaml.safe_load(fm_text)
    if not isinstance(fm, dict):
        return {}, text
    return fm, body.lstrip("\n")


def _read_tree(root: Path) -> dict[str, str]:
    """relpath -> content for every file under *root* (sorted, deterministic)."""
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for f in sorted(root.rglob("*")):
        # SEC-10: skip symlinked files. ``rglob`` already does not descend into
        # symlinked directories, but a symlinked FILE under references/ or
        # scripts/ would otherwise be read and emitted into the consumer tree —
        # an arbitrary-host-file exfil vector. Skip any symlink entry.
        if f.is_symlink():
            continue
        if f.is_file():
            try:
                out[str(f.relative_to(root))] = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # skip binary files (images, compiled assets)
    return out


def _read_skill_trees(skill_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    """One sorted walk of ``skill/`` partitioned into references and scripts.

    Returns ``(references, scripts)`` where each dict maps paths relative to
    their own subdirectory — identical to calling ``_read_tree`` separately on
    ``skill/references/`` and ``skill/scripts/``, but using a single
    ``rglob("*")`` pass over ``skill/`` for both.

    Files outside the two subdirectories (e.g. ``SKILL.md``) are ignored;
    they are read separately by :func:`load_canonical_skill`.

    SEC-10 invariant preserved: symlinked files are skipped exactly as in
    :func:`_read_tree`.
    """
    refs: dict[str, str] = {}
    scripts: dict[str, str] = {}
    if not skill_dir.is_dir():
        return refs, scripts
    refs_dir = skill_dir / "references"
    scripts_dir = skill_dir / "scripts"
    have_refs = refs_dir.is_dir()
    have_scripts = scripts_dir.is_dir()
    if not have_refs and not have_scripts:
        return refs, scripts
    for f in sorted(skill_dir.rglob("*")):
        if f.is_symlink() or not f.is_file():
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # skip binary files
        if have_refs and f.is_relative_to(refs_dir):
            refs[str(f.relative_to(refs_dir))] = content
        elif have_scripts and f.is_relative_to(scripts_dir):
            scripts[str(f.relative_to(scripts_dir))] = content
    return refs, scripts


def load_canonical_skill(primitive_dir: Path) -> CanonicalSkill | None:
    skill_md = primitive_dir / "skill" / "SKILL.md"
    if not skill_md.is_file():
        return None
    fm, body = _split_frontmatter(skill_md.read_text(encoding="utf-8"))
    name = _validate_safe_name(str(fm.get("name", primitive_dir.name)), "skill")
    # Parse declared tool intents from frontmatter; fall back to ("shell", "read").
    raw_tools = fm.get("tools")
    if isinstance(raw_tools, list):
        tools = tuple(str(t) for t in raw_tools)
        for tool in tools:
            if tool not in NEUTRAL_TOOLS:
                raise ValueError(
                    f"skill {name!r}: unknown tool {tool!r}; valid tools: {sorted(NEUTRAL_TOOLS)}"
                )
    else:
        tools = ("shell", "read")
    references, scripts = _read_skill_trees(primitive_dir / "skill")
    return CanonicalSkill(
        name=name,
        description=str(fm.get("description", "")),
        body=body,
        references=references,
        scripts=scripts,
        delegates_to_cli=bool(fm.get("delegates-to-cli", False)),
        tools=tools,
    )


def load_canonical_agents(primitive_dir: Path) -> list[CanonicalAgent]:
    agents_dir = primitive_dir / "agents"
    if not agents_dir.is_dir():
        return []
    agents: list[CanonicalAgent] = []
    for f in sorted(agents_dir.glob("*.agent.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if "name" not in data:
            raise ValueError(f"agent file {f} is missing required field 'name'")
        name = _validate_safe_name(str(data["name"]), "agent")
        # B2: validate tools at load — a bogus tool would otherwise raise a bare
        # KeyError at emit (claude/copilot) or be silently dropped (codex/antigravity).
        tools = tuple(data.get("tools", []))
        for tool in tools:
            if tool not in NEUTRAL_TOOLS:
                raise ValueError(
                    f"agent {name!r}: unknown tool {tool!r}; valid tools: {sorted(NEUTRAL_TOOLS)}"
                )
        # B3: empty/whitespace-only instructions produce a malformed empty-body
        # agent at emit; reject it as an authoring error at load instead.
        instructions = str(data.get("instructions", ""))
        if not instructions.strip():
            raise ValueError(f"agent {name!r}: 'instructions' must be non-empty")
        model_tier = str(data.get("model_tier", "default"))
        if model_tier not in MODEL_TIERS:
            raise ValueError(
                f"agent {name!r}: unknown model_tier {model_tier!r}; "
                f"valid tiers: {sorted(MODEL_TIERS)}"
            )
        agents.append(
            CanonicalAgent(
                name=name,
                description=str(data.get("description", "")),
                instructions=instructions,
                tools=tools,
                model_tier=model_tier,
            )
        )
    return agents


def load_canonical_rules(primitive_dir: Path) -> list[CanonicalRule]:
    rules_dir = primitive_dir / "rules"
    if not rules_dir.is_dir():
        return []
    rules: list[CanonicalRule] = []
    for f in sorted(rules_dir.glob("*.rule.md")):
        fm, body = _split_frontmatter(f.read_text(encoding="utf-8"))
        scope = str(fm.get("scope", "always"))
        rule_name = _validate_safe_name(f.name.removesuffix(".rule.md"), "rule")
        if scope not in _VALID_SCOPES:
            raise ValueError(
                f"rule {rule_name!r}: unknown scope {scope!r}; "
                f"valid scopes: {sorted(_VALID_SCOPES)}"
            )
        rules.append(
            CanonicalRule(
                name=rule_name,
                scope=scope,
                paths=tuple(cast("list[str]", fm.get("paths", []) or [])),
                body=body,
            )
        )
    return rules


def _ci_input(raw: dict[str, object], command: str) -> CanonicalCIInput:
    """Parse + validate one ci.yaml input (raises ``ValueError`` on a bad shape)."""
    name = str(raw.get("name", ""))
    if not _SAFE_NAME_RE.match(name):
        raise ValueError(
            f"ci.yaml [{command}]: input name {name!r} must match {_SAFE_NAME_RE.pattern}"
        )
    itype = str(raw.get("type", "string"))
    if itype not in {"string", "choice", "bool"}:
        raise ValueError(
            f"ci.yaml [{command}]: input {name!r} type {itype!r} not in string/choice/bool"
        )
    validate = raw.get("validate")
    if validate is not None:
        validate = str(validate)
        # The emitter wraps the pattern as ``^(<validate>)$`` — the parens make the
        # anchoring robust regardless of any inner anchor (an inner ``^``/``$`` just
        # makes the pattern un-matchable, i.e. fails closed; ``^`` inside a class
        # like ``[A-Za-z^-]`` for git refs is a legitimate literal). The real risks
        # are a single quote (breaks the shell ``grep -qE '...'`` quoting) or a
        # newline/CR (breaks the single-line grep). Reject those + non-regexes (SEC-8).
        if any(c in validate for c in ("'", "\n", "\r")):
            raise ValueError(
                f"ci.yaml [{command}]: input {name!r} validate must not contain a single quote "
                "or newline (it is embedded in a shell grep pattern)"
            )
        try:
            re.compile(f"^({validate})$")
        except re.error as exc:
            raise ValueError(
                f"ci.yaml [{command}]: input {name!r} validate is not a valid regex: {exc}"
            ) from exc
    elif itype == "string":
        # #806: a free-text ``string`` input flows into the agent PROMPT, so the
        # anchored shell regex is its only prompt-injection barrier — require it.
        # ``choice``/``bool`` are enum-/boolean-constrained at the *GH Actions /
        # Azure* workflow layer (``options:`` / ``values:``), so they need no
        # author-supplied ``validate`` here. CodeBuild has no input layer, so the
        # emitter (``ci.py::_emit_codebuild``) synthesises the anchored shell
        # validation for choice/bool there from the closed option set / ``true|false``
        # (#841 re-audit LOW) — closing that asymmetry without burdening ci.yaml authors.
        raise ValueError(
            f"ci.yaml [{command}]: input {name!r} has type 'string' but no 'validate' regex; "
            "free-text inputs must declare a validate pattern (the anchored shell regex is the "
            "only barrier against prompt injection into the agent PROMPT — #806)"
        )
    return CanonicalCIInput(
        name=name,
        description=str(raw.get("description", "")),
        type=itype,
        default=str(raw.get("default", "")),
        required=bool(raw.get("required", False)),
        choices=tuple(str(c) for c in cast("list[object]", raw.get("choices", []) or [])),
        validate=validate,
        arg=str(raw.get("arg", "positional")),
        omit_when=None if raw.get("omit_when") is None else str(raw.get("omit_when")),
    )


def load_canonical_ci(primitive_dir: Path) -> CanonicalCI | None:
    """Load ``primitives/<name>/ci.yaml`` into a validated :class:`CanonicalCI`.

    Returns ``None`` when the primitive has no ``ci.yaml`` (skill-first primitives).
    All security guards are enforced here at load time so the emitter renders only
    sanitized data: ``command`` identity (SEC-10), ``skill`` slash-command shape,
    ``output_format`` enum, ``schema_ref`` no-traversal (#631 M2), input
    ``validate`` regex anchoring (SEC-8), and ``deterministic_steps`` shell-metachar
    rejection (#631 M1).
    """
    ci_path = primitive_dir / "ci.yaml"
    if not ci_path.is_file():
        return None
    data = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"ci.yaml in {primitive_dir.name}: top level must be a mapping")

    command = _validate_safe_name(str(data.get("command", primitive_dir.name)), "ci command")
    # 2026-06-18 harness-layer-removal §3.1: ``harness:`` is gone — the CI agent session
    # invokes ``skill:`` (the slash-command) and IS the harness. Default to ``/<command>``.
    skill = str(data.get("skill", f"/{command}"))
    if not _SKILL_RE.match(skill):
        raise ValueError(
            f"ci.yaml [{command}]: skill {skill!r} must match {_SKILL_RE.pattern} "
            "(a leading-slash slash-command, e.g. '/code-review')"
        )
    output_mode = str(data.get("output_mode", "file"))
    if output_mode not in CI_OUTPUT_MODES:
        raise ValueError(
            f"ci.yaml [{command}]: output_mode {output_mode!r} not in {sorted(CI_OUTPUT_MODES)}"
        )
    raw_fmt = data.get("output_format")
    output_format = None if raw_fmt is None else str(raw_fmt)
    if output_format is not None and output_format not in CI_OUTPUT_FORMATS:
        valid = sorted(CI_OUTPUT_FORMATS)
        raise ValueError(f"ci.yaml [{command}]: output_format {output_format!r} not in {valid}")
    if output_mode == "file" and output_format is None:
        raise ValueError(f"ci.yaml [{command}]: output_mode 'file' requires output_format")

    schema_ref = data.get("schema_ref")
    if schema_ref is not None:
        schema_ref = str(schema_ref)
        # #631 M2: reject absolute paths and any ``..`` segment on the RAW value
        # (before any resolution), mirroring SEC-7 discipline.
        if schema_ref.startswith("/") or ".." in schema_ref.split("/"):
            raise ValueError(
                f"ci.yaml [{command}]: schema_ref {schema_ref!r} must be a primitive-relative "
                "path with no '..' segment (#631 M2)"
            )

    steps: list[dict[str, str]] = []
    for raw_step in cast("list[dict[str, object]]", data.get("deterministic_steps", []) or []):
        run = str(raw_step.get("run", ""))
        if _BAD_STEP_RE.search(run):
            raise ValueError(
                f"ci.yaml [{command}]: deterministic_steps run contains a forbidden shell "
                "metacharacter ($(, backtick, ;|&<>, newline) (#631 M1)"
            )
        steps.append({"name": str(raw_step.get("name", "step")), "run": run})

    perms = cast("dict[str, object]", data.get("permissions", {}) or {})
    inputs = tuple(
        _ci_input(raw, command)
        for raw in cast("list[dict[str, object]]", data.get("inputs", []) or [])
    )
    return CanonicalCI(
        command=command,
        display_name=str(data.get("display_name", command)),
        skill=skill,
        output_mode=output_mode,
        output_format=output_format,
        schema_ref=schema_ref,
        permissions={str(k): str(v) for k, v in perms.items()},
        inputs=inputs,
        deterministic_steps=tuple(steps),
    )
