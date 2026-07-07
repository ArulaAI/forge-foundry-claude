#!/usr/bin/env python3
"""Deterministic template-fill, manifest-scan, and static self-review for ``/planner``.

Covers Stages A-det, D-stat.

Token-offload: the model supplies its Stage B/C *judgment* — task decomposition
(ordered TDD units) and the fleshed task code — as a compact JSON payload.
This stdlib-only script does the deterministic remainder:

* **Stage A-det — manifest stack-hint scan.** If ``--codebase-root`` is given,
  scan its top-level manifests (``pyproject.toml``, ``package.json``, ``pom.xml``)
  and top directories to derive a ``tech_stack`` hint that the model uses in Stage B.
* **Stage T — template rendering.** Fill ``references/plan_template.md``'s
  ``{{title}}``, ``{{goal}}``, ``{{architecture}}``, ``{{tech_stack}}``,
  ``{{file_structure}}``, and ``{{tasks}}`` slots from the model's structured
  JSON payload; each task renders as a ``- [ ]`` step sequence.
* **Stage D-stat — static self-review checks.**
  - ``placeholder``: flags any TBD / TODO / ``...`` literal in task code or prose.
  - ``missing_section``: flags any required template section absent from the
    rendered plan (``## File structure``, ``## Tasks``).

The model keeps: Stage B task decomposition (judging 2-5 minute TDD units from the
spec) and Stage C fleshing (writing real test + implementation code per task).  The
model emits the structured JSON payload; this script renders + checks.

Runnable with a bare ``python3`` (stdlib only — no PyYAML, no ``forge-validate``
dependency), which is why the skill body can invoke it in any harness session.

Usage::

    python3 plan_render.py --payload plan_payload.json --output plan.md

    python3 plan_render.py --payload plan_payload.json --dry-run

    python3 plan_render.py --codebase-root /path/to/repo --scan-only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

#: Slots the template expects.
REQUIRED_SLOTS: tuple[str, ...] = (
    "{{title}}",
    "{{goal}}",
    "{{architecture}}",
    "{{tech_stack}}",
    "{{file_structure}}",
    "{{tasks}}",
)

#: Sections the rendered plan must contain (static check).
REQUIRED_SECTIONS: tuple[str, ...] = ("## File structure", "## Tasks")

#: Path to the plan template, relative to this script.
_TEMPLATE_PATH = Path(__file__).parent.parent / "references" / "plan_template.md"


def load_template() -> str:
    """Load the plan template from ``references/plan_template.md``."""
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def render_template(
    template: str,
    title: str,
    goal: str,
    architecture: str,
    tech_stack: str,
    file_structure: str,
    tasks: str,
) -> str:
    """Fill all ``{{slot}}`` placeholders in *template* and return the rendered plan."""
    result = template
    result = result.replace("{{title}}", title)
    result = result.replace("{{goal}}", goal)
    result = result.replace("{{architecture}}", architecture)
    result = result.replace("{{tech_stack}}", tech_stack)
    result = result.replace("{{file_structure}}", file_structure)
    result = result.replace("{{tasks}}", tasks)
    return result


# ---------------------------------------------------------------------------
# Manifest scanner (Stage A-det)
# ---------------------------------------------------------------------------

#: Manifests we inspect at the codebase root.
_MANIFEST_FILES = ("pyproject.toml", "package.json", "pom.xml")

#: Directories that signal specific stacks (heuristic, first-match).
_DIR_HINTS: tuple[tuple[str, str], ...] = (
    ("src/main/java", "Java (Maven/Gradle)"),
    ("src/main/kotlin", "Kotlin (Maven/Gradle)"),
    ("node_modules", "Node.js"),
)


def scan_stack_hint(codebase_root: Path) -> str:
    """Scan *codebase_root* for manifest files and top directories.

    Returns a short human-readable stack hint string such as
    ``"Python (pyproject.toml detected)"`` or ``"unknown"`` when nothing
    recognisable is found.
    """
    hints: list[str] = []

    if (codebase_root / "pyproject.toml").is_file():
        hints.append("Python (pyproject.toml detected)")
    if (codebase_root / "package.json").is_file():
        hints.append("Node.js (package.json detected)")
    if (codebase_root / "pom.xml").is_file():
        hints.append("Java/Maven (pom.xml detected)")

    if not hints:
        # Fallback: inspect sub-directories for known layout hints.
        try:
            top_dirs = {p.name for p in codebase_root.iterdir() if p.is_dir()}
        except OSError:
            top_dirs = set()
        for dir_name, hint in _DIR_HINTS:
            # dir_name may contain a separator — only use the first component.
            top = dir_name.split("/")[0]
            if top in top_dirs:
                hints.append(hint)
                break

    return "; ".join(hints) if hints else "unknown"


# ---------------------------------------------------------------------------
# Task rendering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    """A single fleshed implementation task from the model payload."""

    id: str
    title: str
    files: list[str]
    depends_on: list[str]
    steps: list[str]

    def render(self) -> str:
        """Render the task as a Markdown ``### T01: Title`` block with ``- [ ]`` steps."""
        lines = [f"### {self.id}: {self.title}", ""]
        if self.files:
            lines.append(f"**Files:** {', '.join(f'`{f}`' for f in self.files)}")
        if self.depends_on:
            lines.append(f"**Depends on:** {', '.join(self.depends_on)}")
        if self.files or self.depends_on:
            lines.append("")
        for step in self.steps:
            lines.append(f"- [ ] {step}")
        lines.append("")
        return "\n".join(lines)


def _parse_task(raw: object) -> Task | None:
    """Parse one raw task dict from the payload.  Returns ``None`` for non-dicts."""
    if not isinstance(raw, dict):
        print(f"[planner] WARNING: dropping non-dict task item: {raw!r}", file=sys.stderr)
        return None
    task_id = str(raw.get("id") or "T??")
    title = str(raw.get("title") or "untitled")
    files_raw = raw.get("files")
    files = [str(f) for f in files_raw] if isinstance(files_raw, list) else []
    depends_raw = raw.get("depends_on")
    depends_on = [str(d) for d in depends_raw] if isinstance(depends_raw, list) else []
    steps_raw = raw.get("steps")
    steps = [str(s) for s in steps_raw] if isinstance(steps_raw, list) else []
    return Task(id=task_id, title=title, files=files, depends_on=depends_on, steps=steps)


def render_tasks(tasks: list[Task]) -> str:
    """Render all tasks into a single Markdown string for the ``{{tasks}}`` slot."""
    return "\n".join(t.render() for t in tasks).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Payload → document
# ---------------------------------------------------------------------------


@dataclass
class PlanDocument:
    """Normalized plan ready to render."""

    title: str
    goal: str
    architecture: str
    tech_stack: str
    file_structure: str
    tasks: list[Task] = field(default_factory=list)


def build_document(payload: dict[str, object]) -> PlanDocument:
    """Normalize a raw model payload into a :class:`PlanDocument`."""
    raw_tasks = payload.get("tasks")
    task_items = raw_tasks if isinstance(raw_tasks, list) else []
    tasks = [t for t in (_parse_task(item) for item in task_items) if t is not None]
    return PlanDocument(
        title=str(payload.get("title") or "Implementation Plan"),
        goal=str(payload.get("goal") or ""),
        architecture=str(payload.get("architecture") or ""),
        tech_stack=str(payload.get("tech_stack") or ""),
        file_structure=str(payload.get("file_structure") or ""),
        tasks=tasks,
    )


# ---------------------------------------------------------------------------
# Static self-review checks (Stage D-stat)
# ---------------------------------------------------------------------------

#: Patterns that signal a placeholder left in task prose or code.
_PLACEHOLDER_RE = re.compile(r"\bTBD\b|\bTODO\b|(?<!\\.)\.\.\.")


@dataclass(frozen=True)
class ReviewIssue:
    """One static self-review finding."""

    kind: str  # "placeholder" | "missing_section"
    detail: str


def check_placeholders(rendered: str) -> list[ReviewIssue]:
    """Return a :class:`ReviewIssue` for every placeholder match in *rendered*."""
    issues: list[ReviewIssue] = []
    for i, line in enumerate(rendered.splitlines(), start=1):
        m = _PLACEHOLDER_RE.search(line)
        if m:
            issues.append(
                ReviewIssue(
                    kind="placeholder",
                    detail=f"line {i}: {line.strip()!r} contains placeholder {m.group()!r}",
                )
            )
    return issues


def check_missing_sections(rendered: str) -> list[ReviewIssue]:
    """Return a :class:`ReviewIssue` for each required section absent from *rendered*."""
    return [
        ReviewIssue(kind="missing_section", detail=f"required section {section!r} is absent")
        for section in REQUIRED_SECTIONS
        if section not in rendered
    ]


def static_review(rendered: str) -> list[ReviewIssue]:
    """Run all static self-review checks and return the combined issue list."""
    return check_placeholders(rendered) + check_missing_sections(rendered)


# ---------------------------------------------------------------------------
# Decomposition verdict validator
# ---------------------------------------------------------------------------

#: Allowed decomposition values.
_DECOMPOSITION_VALUES: frozenset[str] = frozenset({"atomic", "split"})

#: Required keys for each child entry when decomposition is 'split'.
_CHILD_REQUIRED_KEYS: tuple[str, ...] = ("title", "spec_seed", "rationale")


def validate_decomposition_verdict(verdict: object) -> list[str]:
    """Structural validation for a parsed decomposition-verdict payload.

    Returns a list of error strings.  An empty list means the payload is valid.
    The model supplies the content; this function only checks structural integrity
    (required keys, types, enum values, conditional presence of children) so that
    the C7 work-orchestrator can trust what it reads.

    Rules:
    - Must be a JSON object.
    - ``schema_version`` must equal ``"1.0"``.
    - ``decomposition`` must be ``"atomic"`` or ``"split"``.
    - When ``decomposition`` is ``"split"``, ``children`` must be a non-empty list
      and each element must be an object with string ``title``, ``spec_seed``,
      and ``rationale``.
    - When ``decomposition`` is ``"atomic"``, ``children`` must be absent or empty.
    """
    errors: list[str] = []
    if not isinstance(verdict, dict):
        return ["decomposition-verdict must be a JSON object"]

    # schema_version
    if "schema_version" not in verdict:
        errors.append("missing required key: 'schema_version'")
    elif verdict["schema_version"] != "1.0":
        errors.append("schema_version must be '1.0'")

    # decomposition
    if "decomposition" not in verdict:
        errors.append("missing required key: 'decomposition'")
    elif verdict["decomposition"] not in _DECOMPOSITION_VALUES:
        errors.append(
            f"decomposition {verdict['decomposition']!r} must be one of: "
            + ", ".join(sorted(_DECOMPOSITION_VALUES))
        )

    if errors:
        return errors

    decomposition = verdict["decomposition"]
    children_raw = verdict.get("children")

    if decomposition == "split":
        # children must be a non-empty list
        if not isinstance(children_raw, list):
            errors.append("'children' must be an array when decomposition is 'split'")
        elif len(children_raw) == 0:
            errors.append("'children' must not be empty when decomposition is 'split'")
        else:
            for i, child in enumerate(children_raw):
                if not isinstance(child, dict):
                    errors.append(f"children[{i}] must be an object")
                    continue
                for key in _CHILD_REQUIRED_KEYS:
                    if key not in child:
                        errors.append(f"children[{i}] missing required key: {key!r}")
                    elif not isinstance(child[key], str):
                        errors.append(f"children[{i}][{key!r}] must be a string")
    # atomic: children must be absent or empty
    elif isinstance(children_raw, list) and len(children_raw) > 0:
        errors.append("'children' must be absent or empty when decomposition is 'atomic'")

    return errors


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _cmd_scan_only(codebase_root: str | None) -> int:
    """Implement the ``--scan-only`` CLI mode.

    Prints ``{"tech_stack": "..."}`` to stdout and returns 0, or returns 2 when
    *codebase_root* is not provided.
    """
    if not codebase_root:
        print("Error: --scan-only requires --codebase-root", file=sys.stderr)
        return 2
    hint = scan_stack_hint(Path(codebase_root))
    print(json.dumps({"tech_stack": hint}))
    return 0


def _cmd_validate_verdict(path: str) -> int:
    """Implement the ``--validate-verdict`` CLI mode.

    Reads a decomposition-verdict JSON file (or stdin when *path* is ``"-"``),
    validates it, prints ``{"verdict_valid": bool, "errors": [...]}`` to stdout,
    and returns 0 (valid), 1 (invalid), or 2 (unreadable / bad JSON).
    """
    if path == "-":
        raw_text = sys.stdin.read()
    else:
        try:
            raw_text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error: cannot read --validate-verdict: {exc}", file=sys.stderr)
            return 2
    try:
        verdict_data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid --validate-verdict JSON: {exc}", file=sys.stderr)
        return 2
    verdict_errors = validate_decomposition_verdict(verdict_data)
    print(json.dumps({"verdict_valid": len(verdict_errors) == 0, "errors": verdict_errors}))
    return 0 if not verdict_errors else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for the plan_render script."""
    parser = argparse.ArgumentParser(
        description="Deterministic plan render + static self-review for /planner."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--payload",
        help="Path to the model's structured JSON payload, or - for stdin.",
    )
    group.add_argument(
        "--scan-only",
        action="store_true",
        help="Only run the manifest stack-hint scan; print the hint and exit.",
    )
    group.add_argument(
        "--validate-verdict",
        metavar="PATH",
        help=(
            "Path to a decomposition-verdict JSON file, or - for stdin. "
            "Validates the verdict structure and prints "
            '{"verdict_valid": bool, "errors": [...]} to stdout. '
            "Exits 0 if valid, 1 if invalid, 2 on unreadable/unparseable input."
        ),
    )
    parser.add_argument(
        "--codebase-root",
        help="Codebase root for the manifest stack-hint scan (Stage A-det).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write the rendered plan to this file.  Omit to print to stdout.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the rendered plan; do not write to disk.",
    )
    args = parser.parse_args(argv)

    # --validate-verdict: validate a decomposition-verdict JSON file.
    if args.validate_verdict:
        return _cmd_validate_verdict(args.validate_verdict)

    # --scan-only: emit just the stack hint.
    if args.scan_only:
        return _cmd_scan_only(args.codebase_root)

    if not args.payload:
        print("Error: --payload is required (or use --scan-only)", file=sys.stderr)
        return 2

    # Load payload.
    if args.payload == "-":
        raw_text = sys.stdin.read()
    else:
        try:
            raw_text = Path(args.payload).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error: cannot read --payload: {exc}", file=sys.stderr)
            return 2

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid --payload JSON: {exc}", file=sys.stderr)
        return 2

    if not isinstance(payload, dict):
        print("Error: --payload must be a JSON object", file=sys.stderr)
        return 2

    # If --codebase-root given and tech_stack absent/empty, fill from scan.
    if args.codebase_root and not payload.get("tech_stack"):
        payload["tech_stack"] = scan_stack_hint(Path(args.codebase_root))

    doc = build_document(payload)

    # Render.
    template = load_template()
    tasks_md = render_tasks(doc.tasks) if doc.tasks else "(no tasks)"
    rendered = render_template(
        template=template,
        title=doc.title,
        goal=doc.goal,
        architecture=doc.architecture,
        tech_stack=doc.tech_stack,
        file_structure=doc.file_structure,
        tasks=tasks_md,
    )

    # Static self-review.
    issues = static_review(rendered)

    if args.dry_run or not args.output:
        sys.stdout.write(rendered)
    else:
        out_path = Path(args.output)
        # The plan is written only to the explicit --output path (or stdout); no
        # filename is derived from the payload. Still reject a '..' segment in the
        # supplied path as a defensive traversal guard.
        if ".." in out_path.parts:
            print("Error: --output must not contain '..' segments", file=sys.stderr)
            return 2
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")

    # Emit review summary as JSON to stdout when writing to file, else to stderr.
    summary = {
        "title": doc.title,
        "task_count": len(doc.tasks),
        "review_issues": [{"kind": i.kind, "detail": i.detail} for i in issues],
    }
    if not args.dry_run and args.output:
        print(json.dumps(summary, indent=2))
    else:
        print(json.dumps(summary, indent=2), file=sys.stderr)

    # Non-zero exit when static issues are found so the skill body can surface them.
    return 1 if issues else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
