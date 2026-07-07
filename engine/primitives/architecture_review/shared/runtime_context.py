"""C8 /architecture-review — Runtime cross-primitive context loader.

Reads upstream artifacts that other foundry primitives emit under
``.foundry/context/`` and exposes them as a typed, graceful-degradation
context object that Stage B (layer_checker) and Stage E (llm_subjective)
can fold into their findings.

Upstream artifacts:

* ``.foundry/context/data-flow.json`` — S60 (data_flow_map) output.
  Used to detect when an abstraction / API / cross-cutting subjective
  issue references a module whose backing file carries PII / PCI / PHI.
* ``.foundry/context/threat-model.json`` — S10 (threat_model) output.
  Used to detect when a Stage B layer-violation crosses a trust boundary
  — defined here as a cross-component edge where both components appear
  in S10's enumerated threats (i.e. STRIDE has already been performed
  against them, so the boundary is known to the security model).

Graceful-degradation contract (charter §7 "compliance-aware by birth"):

* No file present → empty context, no decoration.
* File present but malformed JSON / schema mismatch → log a single
  WARNING to stderr, return empty context for that side.
* Single side present → that side's decoration runs; the other is no-op.

Backward compatibility: every new flag on existing models defaults to
False, so absent context is observationally identical to the previous
behavior.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Default convention: .foundry/context/ relative to code_path
# ---------------------------------------------------------------------------

_DATA_FLOW_FILENAME = "data-flow.json"
_THREAT_MODEL_FILENAME = "threat-model.json"
_DEFAULT_CONTEXT_SUBDIR = Path(".foundry") / "context"


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeContext:
    """Cross-primitive runtime context loaded from ``.foundry/context/``.

    All fields default to empty containers, so an instance constructed
    with no arguments represents the "no upstream artifacts present"
    case. Callers can treat it uniformly; the predicates below are
    falsy in the empty case, which is the correct degrade-gracefully
    behavior.
    """

    #: Absolute / relative file path strings of every PII source file
    #: S60 detected. Used to ask "does this module's file appear here?"
    pii_files: frozenset[str] = field(default_factory=frozenset)

    #: Set of trust-boundary component names enumerated by S10 (i.e.
    #: components that appear as Threat.component in threat-model.json).
    #: An edge whose two endpoints map to *different* components in
    #: this set is treated as a trust-boundary crossing.
    trust_boundary_components: frozenset[str] = field(default_factory=frozenset)

    #: True when neither upstream artifact contributed anything. Useful
    #: for telling Stage B / Stage E to skip the decoration loop entirely.
    @property
    def is_empty(self) -> bool:
        """True when no upstream artifacts contributed any data."""
        return not self.pii_files and not self.trust_boundary_components

    # ------------------------------------------------------------------
    # Predicates the Stage B / Stage E call-sites use.
    # ------------------------------------------------------------------

    def file_touches_pii(self, file_path: str | None) -> bool:
        """True when *file_path* matches one of S60's PII source files.

        Matching strategy is tolerant of relative vs absolute paths:
        an exact match, a suffix match on the basename, or a suffix
        match on the relative path all count. This is intentional
        because S60 may emit either repo-relative or absolute paths
        depending on how it was invoked, and C8's Module.path uses
        the same source.
        """
        if not file_path or not self.pii_files:
            return False
        if file_path in self.pii_files:
            return True
        # Tolerant suffix match: handle "src/auth.py" vs "/abs/src/auth.py".
        normalized = file_path.replace("\\", "/")
        norm_basename = normalized.rsplit("/", 1)[-1]
        for known in self.pii_files:
            known_norm = known.replace("\\", "/")
            if known_norm.endswith("/" + normalized) or normalized.endswith("/" + known_norm):
                return True
            # Plain basename match (last-resort): catches the case where
            # one side has a path and the other only the leaf name.
            if known_norm.rsplit("/", 1)[-1] == norm_basename and norm_basename:
                return True
        return False

    def is_trust_boundary_crossing(
        self,
        from_component: str | None,
        to_component: str | None,
    ) -> bool:
        """True when both endpoints are S10-known components and differ.

        A "trust boundary" here is conservative: it only fires when both
        modules map to components that S10 itself enumerated as bearing
        threats. This avoids inferring boundaries that the security model
        has not actually evaluated.
        """
        if not self.trust_boundary_components:
            return False
        if not from_component or not to_component:
            return False
        if from_component == to_component:
            return False
        return (
            from_component in self.trust_boundary_components
            and to_component in self.trust_boundary_components
        )


# ---------------------------------------------------------------------------
# Loaders — graceful degradation
# ---------------------------------------------------------------------------


def _safe_load_json(path: Path, artifact_label: str) -> dict[str, Any] | None:
    """Load and parse a JSON file. Returns None and logs on any failure."""
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"[architecture-review] WARNING: could not read {artifact_label} at {path}: {exc}",
            file=sys.stderr,
        )
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        print(
            f"[architecture-review] WARNING: could not parse {artifact_label} at {path}: {exc}",
            file=sys.stderr,
        )
        return None
    if not isinstance(data, dict):
        print(
            f"[architecture-review] WARNING: {artifact_label} at {path} is not "
            f"a JSON object (got {type(data).__name__}); ignoring.",
            file=sys.stderr,
        )
        return None
    return data


def _extract_pii_files(data: dict[str, Any]) -> frozenset[str]:
    """Pull PII source file paths out of an S60 data-flow.json document.

    Tolerant of schema drift: any item under ``sources`` that is a dict
    with a string ``file`` key contributes. Anything else is silently
    skipped — S60's own format guarantees stronger shape, but we don't
    want C8 to crash if a sibling repo writes a slightly-old version of
    the schema.
    """
    sources = data.get("sources")
    if not isinstance(sources, list):
        return frozenset()
    files: set[str] = set()
    for entry in sources:
        if not isinstance(entry, dict):
            continue
        file_val = entry.get("file")
        if isinstance(file_val, str) and file_val:
            files.add(file_val)
    return frozenset(files)


def _extract_trust_boundary_components(data: dict[str, Any]) -> frozenset[str]:
    """Pull trust-boundary component names out of an S10 threat-model.json.

    Convention: every distinct ``Threat.component`` value counts as a
    trust-boundary endpoint, because S10 has enumerated at least one
    STRIDE category against it. The set is conservative: components
    referenced *only* in the spec text but never tied to a threat are
    excluded.
    """
    threats = data.get("threats")
    if not isinstance(threats, list):
        return frozenset()
    components: set[str] = set()
    for entry in threats:
        if not isinstance(entry, dict):
            continue
        comp = entry.get("component")
        if isinstance(comp, str) and comp:
            components.add(comp)
    return frozenset(components)


def load_runtime_context(
    code_path: Path | None,
    context_dir: Path | None = None,
) -> RuntimeContext:
    """Load cross-primitive context from ``.foundry/context/``.

    Args:
        code_path: The codebase being reviewed. If *context_dir* is None
            and *code_path* is provided, looks under
            ``code_path / ".foundry" / "context"``.
        context_dir: Explicit override; takes precedence over the
            ``code_path``-derived default.

    Returns:
        A :class:`RuntimeContext`. Always returns a value — missing or
        malformed artifacts degrade to an empty context with a single
        stderr warning per failure mode.
    """
    resolved: Path | None
    if context_dir is not None:
        resolved = context_dir
    elif code_path is not None:
        resolved = code_path / _DEFAULT_CONTEXT_SUBDIR
    else:
        resolved = None

    if resolved is None or not resolved.is_dir():
        # Silent no-op: no .foundry/context/ directory means upstream
        # primitives simply haven't run. This is the common case.
        return RuntimeContext()

    pii_files: frozenset[str] = frozenset()
    trust_boundary_components: frozenset[str] = frozenset()

    df_path = resolved / _DATA_FLOW_FILENAME
    df_data = _safe_load_json(df_path, "data-flow.json (S60)")
    if df_data is not None:
        pii_files = _extract_pii_files(df_data)
        print(
            f"[architecture-review] loaded S60 data-flow.json: "
            f"{len(pii_files)} PII source file(s).",
            file=sys.stderr,
        )

    tm_path = resolved / _THREAT_MODEL_FILENAME
    tm_data = _safe_load_json(tm_path, "threat-model.json (S10)")
    if tm_data is not None:
        trust_boundary_components = _extract_trust_boundary_components(tm_data)
        print(
            f"[architecture-review] loaded S10 threat-model.json: "
            f"{len(trust_boundary_components)} trust-boundary component(s).",
            file=sys.stderr,
        )

    return RuntimeContext(
        pii_files=pii_files,
        trust_boundary_components=trust_boundary_components,
    )
