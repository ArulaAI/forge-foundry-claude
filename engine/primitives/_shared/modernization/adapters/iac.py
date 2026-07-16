"""Adapter: Infrastructure-as-Code source → :class:`ModernizationDigest` (MR6, #1555).

Walks an IaC repository (Terraform / CloudFormation / SAM / Helm) and extracts the
**provisioned resource names** as a first-class IaC unit on the modernization
system-map. Unlike the app/api/db adapters (which normalize an upstream *analysis
artifact*), this adapter reads the IaC **source files directly** — an IaC repo has no
prior digest step, so it is parsed here, best-effort and conservatively.

Conventions
-----------
* The returned digest carries ``flavor="iac"`` so
  :func:`~primitives._shared.modernization.correlate.build_system_map` creates a node
  with ``kind="iac"`` for it (the ``kind`` field is free-form; no schema change).
* Each provisioned resource is recorded as an **exposed** interface using the
  ``provisions:<name>`` convention. A post-correlate enrichment
  (:func:`primitives.modernization_scope.shared.pipeline.attach_provision_edges`,
  Option A) matches these against app/db unit ids to draw ``kind="provisions"`` edges.
* A repo with **no** recognizable IaC files yields ``None`` (not an empty digest) so
  the caller can skip it entirely.

Pure + deterministic apart from the filesystem walk: the resource list is sorted and
bounded (top-N) so the same repo always yields the same digest.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from primitives._shared.modernization.models import ModernizationDigest
from primitives._shared.scan_exclusions import walk_source_files

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The exposed-interface convention: a provisioned resource named ``<name>`` is
#: recorded as ``provisions:<name>`` so correlate/enrichment can match it.
PROVISIONS_PREFIX = "provisions:"

#: File suffixes worth opening. ``.tf`` is unambiguously Terraform; the structured
#: template extensions are opened and content-sniffed (only CloudFormation/SAM ones
#: contribute — a plain data ``.yaml`` contributes nothing).
_IAC_EXTENSIONS: set[str] = {".tf", ".yaml", ".yml", ".json"}

#: Cap the recorded resource names to keep the digest within the ≤1.5k-token target.
_MAX_RESOURCES = 60

# Terraform: ``resource "aws_rds_instance" "orders_db" { ... }`` and
# ``module "networking" { ... }``. The type + name are captured; a Terraform
# ``resource`` contributes its NAME (the local logical id) as the provisioned unit.
_TF_RESOURCE_RE = re.compile(
    r'\bresource\s+"([^"]+)"\s+"([^"]+)"',
)
_TF_MODULE_RE = re.compile(
    r'\bmodule\s+"([^"]+)"',
)


# ---------------------------------------------------------------------------
# Terraform
# ---------------------------------------------------------------------------


def _parse_terraform(text: str) -> list[str]:
    """Best-effort: provisioned resource + module names from Terraform source."""
    names: list[str] = []
    for _res_type, res_name in _TF_RESOURCE_RE.findall(text):
        if res_name:
            names.append(res_name)
    for mod_name in _TF_MODULE_RE.findall(text):
        if mod_name:
            names.append(mod_name)
    return names


# ---------------------------------------------------------------------------
# CloudFormation / SAM
# ---------------------------------------------------------------------------


def _looks_like_cfn(text: str) -> bool:
    """True when a structured template is (best-effort) a CloudFormation/SAM one.

    Sniffs the two reliable CFN markers: an ``AWSTemplateFormatVersion`` line, or a
    ``Resources:`` block that carries a ``Type: AWS::`` entry. A plain data YAML/JSON
    (a Helm ``values.yaml``, a config file) matches neither and contributes nothing.
    """
    if "AWSTemplateFormatVersion" in text:
        return True
    return "Resources:" in text and "AWS::" in text


def _cfn_resource_ids_json(obj: Any) -> list[str]:
    """Logical ids under a parsed-JSON CloudFormation ``Resources`` map."""
    if not isinstance(obj, dict):
        return []
    resources = obj.get("Resources")
    if not isinstance(resources, dict):
        return []
    return [str(k) for k in resources if k]


#: A top-level ``Resources:`` YAML block, then each logical id is a 2-or-more-space
#: indented ``LogicalId:`` key directly under it. Best-effort line scan (no YAML dep):
#: we do NOT descend into nested properties — only the direct children of
#: ``Resources:`` are logical ids.
_YAML_LOGICAL_ID_RE = re.compile(r"^(\s+)([A-Za-z0-9]+):\s*$")


def _cfn_resource_ids_yaml(text: str) -> list[str]:
    """Logical ids under a ``Resources:`` block of a CloudFormation/SAM YAML template.

    A deliberately small line scanner (avoids a YAML dependency in Layer-1): find the
    ``Resources:`` line, note its indent, then collect the keys indented exactly one
    level deeper until the block ends (a line at ≤ the ``Resources:`` indent).
    """
    lines = text.splitlines()
    names: list[str] = []
    in_resources = False
    resources_indent = 0
    child_indent: int | None = None
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if not in_resources:
            if stripped == "Resources:":
                in_resources = True
                resources_indent = indent
            continue
        # We are inside the Resources block.
        if indent <= resources_indent:
            break  # dedented out of the block
        m = _YAML_LOGICAL_ID_RE.match(raw)
        if m is None:
            continue
        key_indent = len(m.group(1))
        if child_indent is None:
            child_indent = key_indent
        if key_indent == child_indent:
            names.append(m.group(2))
    return names


def _parse_cloudformation(text: str) -> list[str]:
    """Best-effort logical resource ids from a CloudFormation/SAM template.

    Tries JSON first (a ``.json`` or JSON-style template), then falls back to the
    line-based YAML scan. Returns ``[]`` for a non-CFN structured file.
    """
    if not _looks_like_cfn(text):
        return []
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _cfn_resource_ids_yaml(text)
    return _cfn_resource_ids_json(obj)


# ---------------------------------------------------------------------------
# Helm
# ---------------------------------------------------------------------------


def _helm_chart_names(repo_root: Path) -> list[str]:
    """Chart names for every Helm chart in the repo (``Chart.yaml`` + ``templates/``).

    A Helm chart is a directory containing a ``Chart.yaml`` AND a sibling
    ``templates/`` dir; the provisioned "resource" is the chart itself (its ``name:``
    from ``Chart.yaml``, falling back to the chart directory name).
    """
    names: list[str] = []
    for chart_file in sorted(repo_root.rglob("Chart.yaml")):
        if chart_file.is_symlink() or not chart_file.is_file():
            continue
        chart_dir = chart_file.parent
        if not (chart_dir / "templates").is_dir():
            continue
        name = ""
        try:
            for line in chart_file.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("name:"):
                    name = stripped.split(":", 1)[1].strip().strip("'\"")
                    break
        except OSError:
            name = ""
        names.append(name or chart_dir.name)
    return names


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _dedup_sorted(names: Iterable[str]) -> list[str]:
    """Unique, sorted, non-empty names (deterministic output)."""
    return sorted({n for n in names if n})


def iac_to_digest(repo_root: Path, *, code_root: str) -> ModernizationDigest | None:
    """Walk *repo_root* for IaC files → an ``flavor="iac"`` :class:`ModernizationDigest`.

    Parameters
    ----------
    repo_root:
        The IaC repository root to scan.
    code_root:
        The node id for the IaC unit on the system-map (passed straight through to
        :attr:`~ModernizationDigest.code_root`).

    Returns
    -------
    ModernizationDigest | None
        A digest whose :attr:`~ModernizationDigest.exposed_interfaces` carries each
        provisioned resource as ``provisions:<name>`` (top-:data:`_MAX_RESOURCES`,
        sorted). ``None`` when the repo contains no recognizable IaC files — the
        caller then contributes no IaC node.

    Notes
    -----
    * Terraform ``resource``/``module`` blocks → the block's local name.
    * CloudFormation/SAM ``Resources:`` → the logical ids.
    * Helm charts (``Chart.yaml`` + ``templates/``) → the chart name.
    * Conservative + generic: a value with no backing IaC construct is never invented.
    """
    resources: list[str] = []
    saw_iac = False

    for path in walk_source_files(repo_root, _IAC_EXTENSIONS):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.suffix == ".tf":
            saw_iac = True
            resources.extend(_parse_terraform(text))
        else:  # .yaml / .yml / .json — only contributes if it is a CFN/SAM template
            names = _parse_cloudformation(text)
            if names or _looks_like_cfn(text):
                saw_iac = True
            resources.extend(names)

    helm_names = _helm_chart_names(repo_root)
    if helm_names:
        saw_iac = True
        resources.extend(helm_names)

    if not saw_iac:
        return None

    exposed = [f"{PROVISIONS_PREFIX}{name}" for name in _dedup_sorted(resources)][:_MAX_RESOURCES]
    entities = _dedup_sorted(resources)[:20]

    return ModernizationDigest(
        code_root=code_root,
        flavor="iac",
        exposed_interfaces=exposed,
        domains=["Infrastructure"],
        key_entities=entities,
        unit_count=len(_dedup_sorted(resources)),
    )
