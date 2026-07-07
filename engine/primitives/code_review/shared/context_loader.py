"""C4 /code-review — Runtime context loader for S70/S60 artifacts (#425).

Reads upstream-emitted context that lands under ``.foundry/context/`` in a
consuming repository:

* ``compliance.yaml`` — S70 ``/compliance-map`` output (control matrix +
  raw config). Provides concrete framework / control IDs C4 can fold into
  the LLM system prompt instead of the generic "Defer GDPR/PCI scope to
  S70" placeholder, and supplies the candidate-control set used when
  attaching ``linked_controls`` to findings.
* ``data-flow.json`` — S60 ``/data-flow-map`` output (PII sources + sinks
  + retention markers). Tells C4 which source files are "scoped" (touch
  classified data) and what classification each file carries so a finding
  in those files can be tagged with the relevant compliance controls.

Charter §7 binds the order: S70 and S60 land before C4, and downstream
primitives are expected to *consume* their outputs at runtime — not just
reference them by name in prompt text. This module is the runtime hop the
audit (finding 6) called out as missing.

Graceful degradation: both artifacts are optional. When either or both
are absent, the loader returns ``None`` and the orchestrator continues
without enrichment. Malformed payloads also degrade to ``None`` with a
single stderr warning — never a crash.

Mirror pattern: ``security_review.shared.compliance_annotator`` does the
same trick for S70's compliance.yaml. The two loaders deliberately accept
slightly different shapes (S11 only needs a flat set of refs;
C4 also needs a file→control map), but the resilience behavior is
identical.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComplianceContext:
    """Loaded view of ``.foundry/context/compliance.yaml`` (S70 output).

    Attributes:
        frameworks: Framework identifiers present in the config (e.g.
            ``["pci-dss-4.0", "gdpr"]``). Empty list if not declared.
        control_ids: Every control ID we recovered from the document. Used
            both to populate the prompt-side hint and to gate
            ``linked_controls`` against hallucinated values.
        source_path: Where we loaded the file from, for logging.
    """

    frameworks: list[str] = field(default_factory=list)
    control_ids: list[str] = field(default_factory=list)
    source_path: str = ""


@dataclass(frozen=True)
class DataFlowContext:
    """Loaded view of ``.foundry/context/data-flow.json`` (S60 output).

    Attributes:
        scoped_files: Set of *relative or absolute* file paths that S60
            flagged as carrying PII / PCI / PHI / other classified data
            (anything other than ``public``/``internal``).
        file_classifications: Map ``file -> {classification, ...}``
            collected across all sources + sinks pointing at that file.
            ``classification`` is one of S60's ``DataClassification``
            literals (``pii``, ``pci``, ``phi``, ``internal``,
            ``public``).
        source_path: Where we loaded the file from, for logging.
    """

    scoped_files: frozenset[str] = field(default_factory=frozenset)
    file_classifications: dict[str, frozenset[str]] = field(default_factory=dict)
    source_path: str = ""


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_COMPLIANCE_REL = ".foundry/context/compliance.yaml"
_DEFAULT_DATA_FLOW_REL = ".foundry/context/data-flow.json"

# Classifications S60 emits that we treat as "scoped" — anything not in this
# set (``public``, ``internal``) is *not* compliance-relevant.
_SCOPED_CLASSIFICATIONS: frozenset[str] = frozenset({"pii", "pci", "phi"})

# Classification → preferred framework heuristic for the linked_controls
# computation. PCI → pci-dss-4.0; PII/PHI → gdpr first (Article 32 is the
# generic protection control). Pure framework hits beyond this still flow
# through whatever S70 catalog actually shipped — this is just the
# default mapping when S70 surfaces more than one framework.
_CLASSIFICATION_TO_FRAMEWORKS: dict[str, tuple[str, ...]] = {
    "pci": ("pci-dss-4.0",),
    "pii": ("gdpr", "pci-dss-4.0"),
    "phi": ("gdpr",),
}


# ---------------------------------------------------------------------------
# Compliance loader
# ---------------------------------------------------------------------------


def _walk_for_control_ids(data: Any, found: set[str]) -> None:
    """Recursively collect strings that look like compliance control IDs.

    A "control ID" is a string that either:
      * Matches the ``<framework>:<id>`` convention (e.g. ``pci-dss-4.0:3.5.1``).
      * Lives under a ``control_ids``/``controls``/``id`` key.
    Mirrors ``security_review.compliance_annotator._collect_refs`` so the
    two primitives stay consistent on what counts as a valid ref.
    """
    if isinstance(data, str):
        if ":" in data:
            found.add(data)
    elif isinstance(data, list):
        for item in data:
            _walk_for_control_ids(item, found)
    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(key, str) and ":" in key:
                found.add(key)
            if isinstance(key, str) and key in {"id", "control_id"} and isinstance(value, str):
                found.add(value)
            _walk_for_control_ids(value, found)


def load_compliance_context(path: Path | None) -> ComplianceContext | None:
    """Load a ``.foundry/context/compliance.yaml`` if it exists.

    Args:
        path: Explicit path to the compliance.yaml file. May be ``None`` or
            point to a missing file — both yield ``None`` with no warning
            (this is the graceful-degradation path).

    Returns:
        A :class:`ComplianceContext` on success; ``None`` when the file is
        missing, unreadable, or empty. Malformed YAML logs a single stderr
        warning and still returns ``None``.
    """
    if path is None or not path.exists():
        return None

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"[code-review] context_loader: could not read {path}: {exc}",
            file=sys.stderr,
        )
        return None

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        print(
            f"[code-review] context_loader: YAML parse error in {path}: {exc}",
            file=sys.stderr,
        )
        return None

    if not isinstance(data, dict):
        # Empty file or a top-level scalar/list — nothing to enrich with.
        return None

    frameworks_raw = data.get("frameworks", [])
    frameworks: list[str] = (
        [str(fw) for fw in frameworks_raw if isinstance(fw, str)]
        if isinstance(frameworks_raw, list)
        else []
    )

    found: set[str] = set()
    _walk_for_control_ids(data, found)

    if not frameworks and not found:
        # File exists but carries no actionable content.
        return None

    return ComplianceContext(
        frameworks=sorted(frameworks),
        control_ids=sorted(found),
        source_path=str(path),
    )


# ---------------------------------------------------------------------------
# Data-flow loader
# ---------------------------------------------------------------------------


def _collect_file_classifications(payload: dict[str, Any]) -> dict[str, set[str]]:
    """Walk S60's data-flow JSON and build ``file -> {classifications}``."""
    out: dict[str, set[str]] = {}

    sources = payload.get("sources", [])
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            file = source.get("file")
            classification = source.get("classification")
            if isinstance(file, str) and isinstance(classification, str):
                out.setdefault(file, set()).add(classification)

    # Sinks reference PII sources by name; they don't carry the
    # classification directly. We treat every sink-file as scoped if it
    # references at least one PII source so the linked_controls path can
    # surface a finding sitting at the *output* boundary.
    sinks = payload.get("sinks", [])
    if isinstance(sinks, list):
        for sink in sinks:
            if not isinstance(sink, dict):
                continue
            file = sink.get("file")
            refs = sink.get("references_pii", [])
            if isinstance(file, str) and isinstance(refs, list) and refs:
                # Default to ``pii`` — the most defensive classification
                # for a downstream sink whose specific category was lost.
                out.setdefault(file, set()).add("pii")

    return out


def load_data_flow_context(path: Path | None) -> DataFlowContext | None:
    """Load a ``.foundry/context/data-flow.json`` if it exists.

    Args:
        path: Explicit path to the data-flow.json file. May be ``None`` or
            point to a missing file — both yield ``None`` silently.

    Returns:
        A :class:`DataFlowContext` on success; ``None`` when the file is
        missing or malformed.
    """
    if path is None or not path.exists():
        return None

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"[code-review] context_loader: could not read {path}: {exc}",
            file=sys.stderr,
        )
        return None

    try:
        payload = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError) as exc:
        print(
            f"[code-review] context_loader: JSON parse error in {path}: {exc}",
            file=sys.stderr,
        )
        return None

    if not isinstance(payload, dict):
        return None

    classifications_per_file = _collect_file_classifications(payload)
    scoped = {
        file
        for file, classes in classifications_per_file.items()
        if classes & _SCOPED_CLASSIFICATIONS
    }

    if not scoped and not classifications_per_file:
        return None

    return DataFlowContext(
        scoped_files=frozenset(scoped),
        file_classifications={
            file: frozenset(cls) for file, cls in classifications_per_file.items()
        },
        source_path=str(path),
    )


# ---------------------------------------------------------------------------
# Auto-locate (orchestrator convenience)
# ---------------------------------------------------------------------------


def auto_locate_compliance(code_path: Path) -> Path:
    """Return the default ``.foundry/context/compliance.yaml`` path."""
    return code_path / _DEFAULT_COMPLIANCE_REL


def auto_locate_data_flow(code_path: Path) -> Path:
    """Return the default ``.foundry/context/data-flow.json`` path."""
    return code_path / _DEFAULT_DATA_FLOW_REL


# ---------------------------------------------------------------------------
# Prompt enrichment
# ---------------------------------------------------------------------------

_PLACEHOLDER_LINE = "Defer GDPR/PCI scope to S70."


def enrich_prompt_with_compliance(
    prompt: str,
    compliance_ctx: ComplianceContext | None,
    data_flow_ctx: DataFlowContext | None,
) -> str:
    """Replace the prompt-text placeholder with concrete control IDs.

    The default ``security_smell`` and ``BATCHED`` prompts carry the line
    ``Defer GDPR/PCI scope to S70.`` When the consuming repo has actually
    run S70 (and we loaded its compliance.yaml), we swap that placeholder
    for the concrete framework list and a sample of the catalog. When S60
    is also present, we add a short note about which classifications the
    LLM should be alert to.

    If neither context is available, the prompt is returned unchanged so
    the existing behavior is fully preserved.
    """
    if compliance_ctx is None and data_flow_ctx is None:
        return prompt

    if _PLACEHOLDER_LINE not in prompt:
        # The non-security_smell prompts don't carry the placeholder; the
        # orchestrator currently only enriches the prompts that do (we
        # leave the rest alone to avoid surprising the LLM with an
        # unfamiliar control-vocabulary prefix on lenses where compliance
        # is genuinely off-topic — e.g. `documentation`).
        return prompt

    inserts: list[str] = []

    if compliance_ctx is not None:
        if compliance_ctx.frameworks:
            inserts.append(
                "S70 has mapped this codebase against: "
                + ", ".join(compliance_ctx.frameworks)
                + "."
            )
        if compliance_ctx.control_ids:
            # Cap the count so the prompt does not balloon for a real
            # BFSI catalog (PCI-DSS alone has ~280 controls). The model
            # only needs the *shape* — exact attribution is anti-
            # hallucination's job later.
            sample = compliance_ctx.control_ids[:12]
            inserts.append("Known control IDs in scope (sample): " + ", ".join(sample) + ".")

    if data_flow_ctx is not None and data_flow_ctx.scoped_files:
        inserts.append(
            "S60 has flagged "
            + str(len(data_flow_ctx.scoped_files))
            + " file(s) as carrying classified data (PII/PCI/PHI). "
            "Be especially alert to logging, response bodies, and persistence on those files."
        )

    if not inserts:
        return prompt

    replacement = "\n".join(inserts)
    return prompt.replace(_PLACEHOLDER_LINE, replacement)


# ---------------------------------------------------------------------------
# linked_controls computation
# ---------------------------------------------------------------------------


def _matches_file(issue_file: str, scoped_file: str) -> bool:
    """Lenient file-match: equal, or one is a suffix of the other.

    S60 may emit absolute paths while C4 sees relative ones (or vice
    versa) depending on how the consuming pipeline invoked each step.
    Matching on suffix avoids spurious misses without conflating files
    whose names just happen to overlap (we require a path-separator
    boundary).
    """
    if issue_file == scoped_file:
        return True
    if issue_file.endswith("/" + scoped_file):
        return True
    # Symmetric case: C4 saw a relative path, S60 emitted an absolute one.
    return scoped_file.endswith("/" + issue_file)


def compute_linked_controls(
    issue_file: str,
    compliance_ctx: ComplianceContext | None,
    data_flow_ctx: DataFlowContext | None,
) -> list[str]:
    """Return the compliance control IDs that apply to a finding.

    Decision tree:
      1. If S60 has no entry for the issue's file → return ``[]``.
      2. If S70 is missing → return ``[]`` (we have nowhere to map to).
      3. Otherwise, take the data-classifications S60 attached to the
         file, look up the framework heuristic, and return every control
         ID from S70 that starts with one of those framework prefixes.

    The result is sorted and deduplicated so consumers get a stable
    ordering across runs.
    """
    if data_flow_ctx is None or compliance_ctx is None:
        return []
    if not compliance_ctx.control_ids:
        return []

    matched_classifications: set[str] = set()
    for scoped_file, classes in data_flow_ctx.file_classifications.items():
        if _matches_file(issue_file, scoped_file):
            matched_classifications.update(classes)
    if not matched_classifications & _SCOPED_CLASSIFICATIONS:
        return []

    candidate_frameworks: set[str] = set()
    for classification in matched_classifications:
        candidate_frameworks.update(_CLASSIFICATION_TO_FRAMEWORKS.get(classification, ()))
    # Intersect with the frameworks S70 actually maps so we never claim
    # a control belongs to a framework the consumer hasn't run.
    if compliance_ctx.frameworks:
        candidate_frameworks &= set(compliance_ctx.frameworks)
    if not candidate_frameworks:
        return []

    matched: set[str] = set()
    for control_id in compliance_ctx.control_ids:
        prefix = control_id.split(":", 1)[0]
        if prefix in candidate_frameworks:
            matched.add(control_id)

    return sorted(matched)
