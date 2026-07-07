#!/usr/bin/env python3
"""Deterministic IaC parse + filter + dedup + render for ``/cloud-arch-review``.

Token-offload: the model supplies its per-pillar Well-Architected **judgment** —
which resources violate which WAF best practices — as a compact JSON findings
payload. This stdlib-only script does every deterministic remainder:

* **IaC format detection** — content-sniff each file in priority order:
  ``sam`` (``Transform: AWS::Serverless``) → ``cfn``
  (``AWSTemplateFormatVersion`` or ``Resources:`` root key) → ``cdk_python``
  (``from aws_cdk`` / ``import aws_cdk``) → ``cdk_ts``
  (``from 'aws-cdk-lib'``) → ``terraform`` (``resource "aws_*"``).
  For Kubernetes and docker-compose targets: ``kubernetes`` (multi-doc YAML
  with ``apiVersion:`` + ``kind:`` fields) and ``compose``
  (``docker-compose.yml`` / ``compose.yaml`` with a top-level ``services:``
  map) are detected via ``yaml.safe_load`` / ``yaml.safe_load_all``; the file
  name is also used as a tie-breaker for compose files.
  Best-effort heuristic; unusual indentation or quoting styles may not be
  detected.

* **Resource-list parsing** — extract ``(logical_id, type, file, line)`` tuples
  per detected format using regex/line-scanning heuristics.  The model's
  anti-hallucination guards (resource_id and file ground-truth) rely on this
  inventory.  For Kubernetes/compose targets the inventory also captures
  resource-attribute presence data (``resources.limits``, ``resources.requests``,
  ``securityContext``, liveness/readiness ``probes``, ``NetworkPolicy`` presence)
  so that a model finding claiming a resource lacks an attribute can be
  **ground-truth-confirmed** (or flagged as contradicted) by the parser.

* **Confidence mapping + threshold filter** — map each finding's ``confidence``
  float to a level (``> 0.7`` → high, ``0.3-0.7`` → medium, ``< 0.3`` → low)
  and drop findings below ``--confidence-threshold`` (default ``medium``).

* **C10/C11 dedup join** — any C11 FM6/FM7/FM8 finding whose ``(file, line)``
  matches an S20 finding is **moved** from ``c11_findings`` into
  ``ai_quality.deduplicated_findings``; the S20 finding stays canonical.

* **Compliance/security ref validation** — strip any ``compliance_refs`` or
  ``security_refs`` values that are not present in the loaded allowlist files
  (if provided); emit a warning per dropped ref.

* **Markdown, JSON, and SARIF rendering** — produce the Well-Architected review
  report in the requested format.  SARIF uses two runs: one for the S20 pillar
  findings and one for the aggregated C10/C11 AI-quality findings.

The model keeps only the per-pillar WAF judgment: reading each resource's config
and deciding whether it violates a best practice, then emitting compact JSON
findings with the fields described in the ``Payload schema`` section below.

Runnable with a bare ``python3`` (stdlib only — JSON in; Markdown/JSON/SARIF out
via string formatting, no PyYAML, no installed foundry CLI).

Usage::

    python3 cloud_arch_render.py --findings-json findings.json \\
        --format markdown --output report.md

    python3 cloud_arch_render.py --findings-json - --format sarif < findings.json

Payload schema (``findings.json``)::

    {
        "workload_context": {
            "workload_type": "api",
            "criticality": "tier-1",
            "rto_minutes": 15,
            "rpo_minutes": 5,
            "regions": ["us-east-1"]
        },
        "iac_files": ["/path/to/template.yaml"],
        "pillar_findings": [
            {
                "pillar": "security",
                "file": "/path/to/template.yaml",
                "line": 42,
                "resource_id": "MyBucket",
                "severity": "high",
                "confidence": 0.9,
                "title": "S3 bucket lacks encryption",
                "description": "...",
                "suggestion": "...",
                "waf_question": "SEC-8",
                "compliance_refs": ["SOC2-CC6.1"],
                "security_refs": ["CWE-312"]
            }
        ],
        "c10_findings": [...],
        "c11_findings": [...],
        "compliance_allowlist": ["SOC2-CC6.1"],
        "security_allowlist": ["CWE-312"]
    }
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml

    _YAML_AVAILABLE = True
except ImportError:  # pragma: no cover — pyyaml is a repo dep
    _YAML_AVAILABLE = False

# ---------------------------------------------------------------------------
# Security: filename sanitization
# ---------------------------------------------------------------------------

#: Characters disallowed in the artifact filename component (path-injection guard).
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(value: str) -> str:
    """Collapse anything outside ``[A-Za-z0-9._-]`` to ``_`` so a payload-supplied
    value cannot inject path separators into the output path."""
    return _UNSAFE_FILENAME_RE.sub("_", value) or "report"


# ---------------------------------------------------------------------------
# IaC format detection constants
# ---------------------------------------------------------------------------

IAC_FORMATS: tuple[str, ...] = (
    "sam",
    "cfn",
    "cdk_python",
    "cdk_ts",
    "terraform",
    "kubernetes",
    "compose",
)

_SAM_RE = re.compile(r"Transform\s*:\s*AWS::Serverless")
_CFN_RE = re.compile(r"(AWSTemplateFormatVersion|^Resources\s*:)", re.MULTILINE)
_CDK_PY_RE = re.compile(r"(from\s+aws_cdk|import\s+aws_cdk)")
_CDK_TS_RE = re.compile(r"from\s+['\"]aws-cdk-lib['\"]")
_TERRAFORM_RE = re.compile(r'resource\s+"aws_[^"]*"')

_FORMAT_DETECTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sam", _SAM_RE),
    ("cfn", _CFN_RE),
    ("cdk_python", _CDK_PY_RE),
    ("cdk_ts", _CDK_TS_RE),
    ("terraform", _TERRAFORM_RE),
)

# Kubernetes: quick pre-filter before YAML parse (avoids parsing non-YAML).
# A multi-document YAML file or a single YAML file must have both apiVersion: and kind:.
_K8S_QUICK_RE = re.compile(r"(?m)^apiVersion\s*:")
_K8S_KIND_RE = re.compile(r"(?m)^kind\s*:")

# Compose: quick pre-filter — docker-compose files always have a top-level services: key.
_COMPOSE_QUICK_RE = re.compile(r"(?m)^services\s*:")
# Compose file names (exact basenames, case-insensitive)
_COMPOSE_NAMES: frozenset[str] = frozenset(
    {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
)

# Kubernetes workload kinds that we extract resource attributes from
_K8S_WORKLOAD_KINDS: frozenset[str] = frozenset(
    {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod"}
)


def _is_kubernetes_content(content: str) -> bool:
    """Return True when *content* looks like a Kubernetes manifest (multi-doc or single).

    Pre-filters with regex before calling into PyYAML.  Requires both ``apiVersion:``
    and ``kind:`` to appear as top-level keys.
    """
    if not (_K8S_QUICK_RE.search(content) and _K8S_KIND_RE.search(content)):
        return False
    if not _YAML_AVAILABLE:
        # Regex pre-filter passed — accept as k8s even without PyYAML
        return True
    try:
        docs = list(_yaml.safe_load_all(content))
    except Exception:
        return False
    return any(isinstance(doc, dict) and "apiVersion" in doc and "kind" in doc for doc in docs)


def _is_compose_content(content: str, filepath: str = "") -> bool:
    """Return True when *content* looks like a docker-compose file.

    Requires a top-level ``services:`` key.  When *filepath* is provided,
    a matching basename (``docker-compose.yml`` etc.) strengthens the match.
    """
    if not _COMPOSE_QUICK_RE.search(content):
        return False
    if not _YAML_AVAILABLE:
        # Filename tie-breaker when PyYAML unavailable
        return Path(filepath).name.lower() in _COMPOSE_NAMES or True
    try:
        doc = _yaml.safe_load(content)
    except Exception:
        return False
    if not isinstance(doc, dict):
        return False
    return "services" in doc


def detect_iac_format(content: str, filepath: str = "") -> str | None:
    """Detect the IaC format of *content* by heuristic content sniff.

    Returns one of ``"sam"``, ``"cfn"``, ``"cdk_python"``, ``"cdk_ts"``,
    ``"terraform"``, ``"kubernetes"``, ``"compose"``, or ``None`` when the
    content does not match any known pattern.

    Detection order (first match wins):
    1. sam — ``Transform: AWS::Serverless``
    2. cfn — ``AWSTemplateFormatVersion`` or ``Resources:`` root key
    3. cdk_python — ``from aws_cdk`` / ``import aws_cdk``
    4. cdk_ts — ``from 'aws-cdk-lib'``
    5. terraform — ``resource "aws_*"``
    6. kubernetes — multi-doc YAML with ``apiVersion:`` + ``kind:``
    7. compose — YAML with top-level ``services:`` map

    *filepath* is used as a tie-breaker for compose-file name detection;
    it does not affect the AWS formats.

    Best-effort: unusual indentation or quotation styles may yield ``None``.
    """
    for fmt, pattern in _FORMAT_DETECTORS:
        if pattern.search(content):
            return fmt
    if _is_kubernetes_content(content):
        return "kubernetes"
    if _is_compose_content(content, filepath):
        return "compose"
    return None


# ---------------------------------------------------------------------------
# Resource extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IacResource:
    """A resource extracted from an IaC file."""

    logical_id: str
    resource_type: str
    file: str
    line: int


@dataclass
class K8sWorkloadAttrs:
    """Ground-truth attributes for a Kubernetes workload resource.

    These are the dimensions extracted by the parser and used to confirm or
    contradict model findings.  Each Boolean reflects whether the attribute
    is **present** (True) or **absent** (False) in the actual manifest.
    """

    has_resource_limits: bool = False
    has_resource_requests: bool = False
    has_pod_security_context: bool = False
    has_container_security_context: bool = False
    has_liveness_probe: bool = False
    has_readiness_probe: bool = False


#: Attribute dimensions surfaced to the model as ground-truth (k8s/compose).
#: Maps a short token that may appear in a finding title/description to the
#: attribute name on :class:`K8sWorkloadAttrs`.
K8S_VALIDATED_DIMENSIONS: dict[str, str] = {
    "resources.limits": "has_resource_limits",
    "resources.requests": "has_resource_requests",
    "securityContext": "has_pod_security_context",
    "containerSecurityContext": "has_container_security_context",
    "livenessProbe": "has_liveness_probe",
    "readinessProbe": "has_readiness_probe",
}


# CFN/SAM: logical-resource-id lines under Resources: block — heuristic only
_CFN_RESOURCE_TYPE_RE = re.compile(r"^\s{4}Type\s*:\s*(.+)$")
_CFN_LOGICAL_ID_RE = re.compile(r"^  ([A-Za-z][A-Za-z0-9]*)\s*:$")

# Terraform: resource blocks
_TF_RESOURCE_RE = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"')

# CDK Python: rough heuristic — aws_cdk.aws_xxx construct calls
_CDK_PY_RESOURCE_RE = re.compile(r"aws_cdk\.aws_(\w+)\.([A-Z][A-Za-z0-9]+)\(")
_CDK_PY_NEW_RE = re.compile(r"([A-Z][A-Za-z0-9]+)\s*=\s*\S*\.([A-Z][A-Za-z0-9]+)\(")

# CDK TypeScript: new CfnXxx( or new xxx.Yyy( construct
_CDK_TS_RESOURCE_RE = re.compile(r"new\s+(?:\w+\.)*([A-Z][A-Za-z0-9]+)\s*\((?:this|scope|stack)")


def _parse_cfn_resources(content: str, filepath: str) -> list[IacResource]:
    """Extract logical resource IDs from a CFN/SAM YAML template (line scan)."""
    resources: list[IacResource] = []
    lines = content.splitlines()
    in_resources = False
    current_id: str | None = None
    current_line = 0
    for lineno, line in enumerate(lines, start=1):
        if re.match(r"^Resources\s*:", line):
            in_resources = True
            continue
        if in_resources:
            # Top-level resource id under Resources: (2-space indent, no deeper)
            m_id = _CFN_LOGICAL_ID_RE.match(line)
            if m_id:
                current_id = m_id.group(1)
                current_line = lineno
                continue
            m_type = _CFN_RESOURCE_TYPE_RE.match(line)
            if m_type and current_id is not None:
                resources.append(
                    IacResource(
                        logical_id=current_id,
                        resource_type=m_type.group(1).strip(),
                        file=filepath,
                        line=current_line,
                    )
                )
                current_id = None
            # Exit resources block on a new top-level key (no leading spaces)
            if line and line[0].isalpha() and not line.startswith("  "):
                in_resources = False
    return resources


def _parse_terraform_resources(content: str, filepath: str) -> list[IacResource]:
    """Extract resource blocks from Terraform HCL (regex scan)."""
    resources: list[IacResource] = []
    lines = content.splitlines()
    for lineno, line in enumerate(lines, start=1):
        m = _TF_RESOURCE_RE.search(line)
        if m:
            resources.append(
                IacResource(
                    logical_id=m.group(2),
                    resource_type=m.group(1),
                    file=filepath,
                    line=lineno,
                )
            )
    return resources


def _parse_cdk_python_resources(content: str, filepath: str) -> list[IacResource]:
    """Heuristic extraction of CDK Python construct instantiations."""
    resources: list[IacResource] = []
    lines = content.splitlines()
    for lineno, line in enumerate(lines, start=1):
        m = _CDK_PY_RESOURCE_RE.search(line)
        if m:
            resources.append(
                IacResource(
                    logical_id=f"{m.group(1)}_{m.group(2)}_{lineno}",
                    resource_type=f"CDK::{m.group(1)}::{m.group(2)}",
                    file=filepath,
                    line=lineno,
                )
            )
    return resources


def _parse_cdk_ts_resources(content: str, filepath: str) -> list[IacResource]:
    """Heuristic extraction of CDK TypeScript construct instantiations."""
    resources: list[IacResource] = []
    lines = content.splitlines()
    for lineno, line in enumerate(lines, start=1):
        m = _CDK_TS_RESOURCE_RE.search(line)
        if m:
            resources.append(
                IacResource(
                    logical_id=f"{m.group(1)}_{lineno}",
                    resource_type=f"CDK::{m.group(1)}",
                    file=filepath,
                    line=lineno,
                )
            )
    return resources


# ---------------------------------------------------------------------------
# Kubernetes / docker-compose parsers
# ---------------------------------------------------------------------------


def _extract_container_attrs(containers: object) -> tuple[bool, bool, bool, bool, bool, bool]:
    """Extract attribute booleans from a containers list.

    Handles ``spec.containers`` and ``spec.initContainers``.
    Returns ``(has_limits, has_requests, has_csc, has_liveness, has_readiness, _unused)``.
    """
    has_limits = False
    has_requests = False
    has_csc = False
    has_liveness = False
    has_readiness = False
    if not isinstance(containers, list):
        return has_limits, has_requests, has_csc, has_liveness, has_readiness, False
    for c in containers:
        if not isinstance(c, dict):
            continue
        res = c.get("resources")
        if isinstance(res, dict):
            if res.get("limits"):
                has_limits = True
            if res.get("requests"):
                has_requests = True
        if c.get("securityContext"):
            has_csc = True
        if c.get("livenessProbe"):
            has_liveness = True
        if c.get("readinessProbe"):
            has_readiness = True
    return has_limits, has_requests, has_csc, has_liveness, has_readiness, False


def _attrs_from_pod_spec(pod_spec: object) -> K8sWorkloadAttrs:
    """Build :class:`K8sWorkloadAttrs` from a Kubernetes PodSpec dict."""
    attrs = K8sWorkloadAttrs()
    if not isinstance(pod_spec, dict):
        return attrs
    # Pod-level securityContext
    if pod_spec.get("securityContext"):
        attrs.has_pod_security_context = True
    containers = pod_spec.get("containers", [])
    (
        has_limits,
        has_requests,
        has_csc,
        has_liveness,
        has_readiness,
        _,
    ) = _extract_container_attrs(containers)
    attrs.has_resource_limits = has_limits
    attrs.has_resource_requests = has_requests
    attrs.has_container_security_context = has_csc
    attrs.has_liveness_probe = has_liveness
    attrs.has_readiness_probe = has_readiness
    # initContainers contribute to limits/requests/csc too (additive)
    init_containers = pod_spec.get("initContainers", [])
    (
        il,
        ir,
        ic,
        _,
        _,
        _,
    ) = _extract_container_attrs(init_containers)
    attrs.has_resource_limits = attrs.has_resource_limits or il
    attrs.has_resource_requests = attrs.has_resource_requests or ir
    attrs.has_container_security_context = attrs.has_container_security_context or ic
    return attrs


def _pod_spec_from_doc(doc: dict[str, Any]) -> object:
    """Return the PodSpec from a Kubernetes manifest doc (handles all workload kinds)."""
    kind = str(doc.get("kind") or "")
    spec = doc.get("spec") or {}
    if not isinstance(spec, dict):
        return None
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        # spec.jobTemplate.spec.template.spec
        jt = spec.get("jobTemplate") or {}
        if isinstance(jt, dict):
            jspec = jt.get("spec") or {}
            if isinstance(jspec, dict):
                tmpl = jspec.get("template") or {}
                if isinstance(tmpl, dict):
                    return tmpl.get("spec")
        return None
    # Deployment / StatefulSet / DaemonSet / Job: spec.template.spec
    tmpl = spec.get("template") or {}
    if isinstance(tmpl, dict):
        return tmpl.get("spec")
    return None


def _parse_kubernetes_resources(
    content: str, filepath: str
) -> tuple[list[IacResource], dict[str, K8sWorkloadAttrs], bool]:
    """Parse a Kubernetes YAML file (possibly multi-document with ``---`` separators).

    Returns:
        resources: list of :class:`IacResource` (one per workload object).
        attrs_map: mapping of ``logical_id`` → :class:`K8sWorkloadAttrs`.
        has_network_policy: True when at least one ``NetworkPolicy`` object is present
            in the document set.
    """
    resources: list[IacResource] = []
    attrs_map: dict[str, K8sWorkloadAttrs] = {}
    has_network_policy = False

    if not _YAML_AVAILABLE:
        # Fallback: line-scan for kind: XYZ lines
        for lineno, line in enumerate(content.splitlines(), start=1):
            m = re.match(r"^kind\s*:\s*(\S+)", line)
            if m:
                kind = m.group(1)
                logical_id = f"{kind}_{lineno}"
                resources.append(
                    IacResource(
                        logical_id=logical_id,
                        resource_type=f"k8s/{kind}",
                        file=filepath,
                        line=lineno,
                    )
                )
                if kind in _K8S_WORKLOAD_KINDS:
                    attrs_map[logical_id] = K8sWorkloadAttrs()
                if kind == "NetworkPolicy":
                    has_network_policy = True
        return resources, attrs_map, has_network_policy

    try:
        docs = list(_yaml.safe_load_all(content))
    except Exception:
        return resources, attrs_map, has_network_policy

    # Build a line-number index so we can report accurate line numbers.
    # We scan the raw content for "^kind:" lines to record per-doc start lines.
    kind_line_map: list[int] = []
    in_doc_boundary = True
    for lineno, line in enumerate(content.splitlines(), start=1):
        if line.strip() == "---":
            in_doc_boundary = True
            continue
        if in_doc_boundary and re.match(r"^kind\s*:", line):
            kind_line_map.append(lineno)
            in_doc_boundary = False
        elif in_doc_boundary and line.strip() and not line.startswith("#"):
            in_doc_boundary = False

    for doc_idx, doc in enumerate(docs):
        if not isinstance(doc, dict):
            continue
        kind = str(doc.get("kind") or "")
        if not kind:
            continue
        if kind == "NetworkPolicy":
            has_network_policy = True
            continue

        metadata = doc.get("metadata") or {}
        name = str(metadata.get("name") or "") if isinstance(metadata, dict) else ""
        logical_id = name or f"{kind}_{doc_idx}"

        # Pick the closest kind: line we recorded
        line_no = kind_line_map[doc_idx] if doc_idx < len(kind_line_map) else 1

        resources.append(
            IacResource(
                logical_id=logical_id,
                resource_type=f"k8s/{kind}",
                file=filepath,
                line=line_no,
            )
        )

        if kind in _K8S_WORKLOAD_KINDS:
            pod_spec = _pod_spec_from_doc(doc)
            attrs_map[logical_id] = _attrs_from_pod_spec(pod_spec)

    return resources, attrs_map, has_network_policy


def _compose_line_fallback(
    content: str, filepath: str
) -> tuple[list[IacResource], dict[str, K8sWorkloadAttrs]]:
    """Line-scan fallback for compose parsing when PyYAML is unavailable."""
    resources: list[IacResource] = []
    attrs_map: dict[str, K8sWorkloadAttrs] = {}
    in_services = False
    for lineno, line in enumerate(content.splitlines(), start=1):
        if re.match(r"^services\s*:", line):
            in_services = True
            continue
        if in_services:
            m = re.match(r"^  ([A-Za-z][A-Za-z0-9_-]*)\s*:", line)
            if m:
                svc = m.group(1)
                resources.append(
                    IacResource(
                        logical_id=svc,
                        resource_type="compose/service",
                        file=filepath,
                        line=lineno,
                    )
                )
                attrs_map[svc] = K8sWorkloadAttrs()
            elif line and line[0].isalpha():
                in_services = False
    return resources, attrs_map


def _compose_svc_line_map(content: str) -> dict[str, int]:
    """Scan *content* for service-name lines and return ``{name: lineno}``."""
    svc_line_map: dict[str, int] = {}
    in_services_block = False
    for lineno, line in enumerate(content.splitlines(), start=1):
        if re.match(r"^services\s*:", line):
            in_services_block = True
            continue
        if in_services_block:
            m = re.match(r"^  ([A-Za-z][A-Za-z0-9_-]*)\s*:", line)
            if m:
                svc_name = m.group(1)
                if svc_name not in svc_line_map:
                    svc_line_map[svc_name] = lineno
            elif line and line[0].isalpha() and not line.startswith(" "):
                in_services_block = False
    return svc_line_map


def _compose_svc_attrs(svc_cfg: object) -> K8sWorkloadAttrs:
    """Extract :class:`K8sWorkloadAttrs` from a single compose service dict."""
    attrs = K8sWorkloadAttrs()
    if not isinstance(svc_cfg, dict):
        return attrs
    deploy = svc_cfg.get("deploy") or {}
    if not isinstance(deploy, dict):
        return attrs
    deploy_res = deploy.get("resources") or {}
    if not isinstance(deploy_res, dict):
        return attrs
    if deploy_res.get("limits"):
        attrs.has_resource_limits = True
    if deploy_res.get("reservations"):
        attrs.has_resource_requests = True
    return attrs


def _parse_compose_resources(
    content: str, filepath: str
) -> tuple[list[IacResource], dict[str, K8sWorkloadAttrs]]:
    """Parse a docker-compose YAML file.

    Returns:
        resources: list of :class:`IacResource` (one per service).
        attrs_map: mapping of service name → :class:`K8sWorkloadAttrs`
            (compose services only have ``resources.limits``/``resources.requests``
            via ``deploy.resources``).
    """
    if not _YAML_AVAILABLE:
        return _compose_line_fallback(content, filepath)

    resources: list[IacResource] = []
    attrs_map: dict[str, K8sWorkloadAttrs] = {}

    try:
        doc = _yaml.safe_load(content)
    except Exception:
        return resources, attrs_map

    if not isinstance(doc, dict):
        return resources, attrs_map

    services = doc.get("services") or {}
    if not isinstance(services, dict):
        return resources, attrs_map

    svc_line_map = _compose_svc_line_map(content)

    for svc_name, svc_cfg in services.items():
        svc_str = str(svc_name)
        line_no = svc_line_map.get(svc_str, 1)
        resources.append(
            IacResource(
                logical_id=svc_str,
                resource_type="compose/service",
                file=filepath,
                line=line_no,
            )
        )
        attrs_map[svc_str] = _compose_svc_attrs(svc_cfg)
    return resources, attrs_map


def parse_iac_resources(content: str, fmt: str, filepath: str) -> list[IacResource]:
    """Dispatch to the per-format resource parser.

    Returns a (possibly empty) list of :class:`IacResource` items.
    All parsers are heuristic (stdlib regex/line-scan or YAML); unusual formatting
    may yield incomplete results.

    For ``"kubernetes"`` and ``"compose"`` formats, the full attribute map and
    network-policy flag are also available via :func:`parse_k8s_compose_attrs`.
    """
    if fmt in ("cfn", "sam"):
        return _parse_cfn_resources(content, filepath)
    if fmt == "terraform":
        return _parse_terraform_resources(content, filepath)
    if fmt == "cdk_python":
        return _parse_cdk_python_resources(content, filepath)
    if fmt == "cdk_ts":
        return _parse_cdk_ts_resources(content, filepath)
    if fmt == "kubernetes":
        resources, _, _ = _parse_kubernetes_resources(content, filepath)
        return resources
    if fmt == "compose":
        resources, _ = _parse_compose_resources(content, filepath)
        return resources
    return []


def parse_k8s_compose_attrs(
    content: str, fmt: str, filepath: str
) -> tuple[dict[str, K8sWorkloadAttrs], bool]:
    """Parse ground-truth workload attributes for Kubernetes / compose targets.

    Returns:
        attrs_map: ``{logical_id: K8sWorkloadAttrs}`` for workload resources.
        has_network_policy: True when at least one ``NetworkPolicy`` object is
            present in the document set (k8s only; always False for compose).

    For non-k8s/compose formats this returns ``({}, False)`` — callers need not
    guard against format type.
    """
    if fmt == "kubernetes":
        _, attrs_map, has_np = _parse_kubernetes_resources(content, filepath)
        return attrs_map, has_np
    if fmt == "compose":
        _, attrs_map = _parse_compose_resources(content, filepath)
        return attrs_map, False
    return {}, False


# ---------------------------------------------------------------------------
# Confidence mapping + filter
# ---------------------------------------------------------------------------

CONFIDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")
_LEVEL_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

#: Thresholds for confidence level mapping (> HIGH_THRESHOLD = high, >= MED_THRESHOLD = medium).
_CONF_HIGH_THRESHOLD: float = 0.7
_CONF_MED_THRESHOLD: float = 0.3


def confidence_to_level(score: float) -> str:
    """Map a 0-1 confidence score to a named level."""
    if score > _CONF_HIGH_THRESHOLD:
        return "high"
    if score >= _CONF_MED_THRESHOLD:
        return "medium"
    return "low"


def passes_threshold(score: float, threshold: str) -> bool:
    """Return True when *score*'s level is >= *threshold* level."""
    level = confidence_to_level(score)
    return _LEVEL_ORDER.get(level, 0) >= _LEVEL_ORDER.get(threshold, 1)


# ---------------------------------------------------------------------------
# WAF question validation
# ---------------------------------------------------------------------------

_WAF_QUESTION_RE = re.compile(r"^(OPS|SEC|REL|PERF|COST|SUS)-\d+$")


def validate_waf_question(waf_q: object) -> str | None:
    """Return *waf_q* if it matches the canonical pattern, else None (with a warning)."""
    if waf_q is None:
        return None
    s = str(waf_q)
    if _WAF_QUESTION_RE.match(s):
        return s
    print(
        f"[cloud-arch-review] WARNING: clearing invalid waf_question {s!r} "
        "(expected ^(OPS|SEC|REL|PERF|COST|SUS)-\\d+$)",
        file=sys.stderr,
    )
    return None


# ---------------------------------------------------------------------------
# Findings model
# ---------------------------------------------------------------------------

VALID_PILLARS: tuple[str, ...] = (
    "operational_excellence",
    "security",
    "reliability",
    "performance_efficiency",
    "cost_optimization",
    "sustainability",
)

VALID_SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")

# FM6/FM7/FM8 are the C11 finding modes subject to dedup with S20 findings.
_DEDUP_FM_CODES: frozenset[str] = frozenset({"FM6", "FM7", "FM8"})


@dataclass(frozen=True)
class PillarFinding:
    """A normalized Well-Architected pillar finding."""

    pillar: str
    file: str
    line: int
    resource_id: str
    severity: str
    confidence: float
    confidence_level: str
    title: str
    description: str
    suggestion: str
    waf_question: str | None
    compliance_refs: list[str]
    security_refs: list[str]
    #: True when the finding was contradicted by the ground-truth manifest
    #: (k8s/compose only — the attribute the model claims is missing is
    #: actually present in the parsed manifest).  Findings that are
    #: contradicted are downgraded to severity ``"low"`` and their
    #: confidence_level is forced to ``"low"``.
    contradicted: bool = False


@dataclass
class AiQualityBucket:
    """C10 + C11 findings with deduplicated entries separated out."""

    c10_findings: list[dict[str, object]] = field(default_factory=list)
    c11_findings: list[dict[str, object]] = field(default_factory=list)
    deduplicated_findings: list[dict[str, object]] = field(default_factory=list)


@dataclass
class ReviewDocument:
    """The full normalized review document."""

    workload_context: dict[str, object]
    pillar_findings: list[PillarFinding]
    ai_quality: AiQualityBucket


# ---------------------------------------------------------------------------
# Ref validation
# ---------------------------------------------------------------------------


def _filter_refs(
    refs: object,
    allowlist: set[str] | None,
    kind: str,
    finding_title: str,
) -> list[str]:
    """Return only allowlisted refs; warn and drop unknown ones."""
    if not isinstance(refs, list):
        return []
    validated: list[str] = []
    for ref in refs:
        r = str(ref)
        if allowlist is not None and r not in allowlist:
            print(
                f"[cloud-arch-review] WARNING: dropping unknown {kind} ref {r!r} "
                f"(not in allowlist) from finding {finding_title!r}",
                file=sys.stderr,
            )
            continue
        validated.append(r)
    return validated


# ---------------------------------------------------------------------------
# K8s/compose ground-truth contradiction check
# ---------------------------------------------------------------------------

# Tokens that, when found in a finding title or description, indicate the
# model is claiming the named attribute is ABSENT ("missing X", "lacks X",
# "no X configured", "without X", "X not set", …).
# Each key is a dimension token; each value is the K8sWorkloadAttrs field name.
_ABSENCE_CLAIM_TOKENS: dict[str, str] = {
    "resources.limits": "has_resource_limits",
    "resource limits": "has_resource_limits",
    "memory limits": "has_resource_limits",
    "cpu limits": "has_resource_limits",
    "resources.requests": "has_resource_requests",
    "resource requests": "has_resource_requests",
    "memory requests": "has_resource_requests",
    "cpu requests": "has_resource_requests",
    "livenessprobe": "has_liveness_probe",
    "liveness probe": "has_liveness_probe",
    "readinessprobe": "has_readiness_probe",
    "readiness probe": "has_readiness_probe",
    "securitycontext": "has_pod_security_context",
    "security context": "has_pod_security_context",
}

# Words/phrases that assert an attribute is ABSENT. Guard 4 must fire only on a
# genuine absence CLAIM bound to a specific attribute — not a mere mention of it
# (#1196). A "Missing livenessProbe" finding whose description also mentions the
# present readinessProbe must NOT be downgraded on the readinessProbe token: the
# old bare `token in text` substring match could not tell "claimed absent" from
# "mentioned present", so it false-contradicted the real gap.
_ABSENCE_WORD = (
    r"missing|absent|without|lacks?|lacking|no|none|omit\w*|undefined|unspecified|"
    r"un(?:configured|set|defined|specified)|"
    # "not <verb>" — both base and past-participle forms so active voice
    # ("does not define X", "not configure X") and passive ("not defined") both
    # count (#1196 review): "does not define X" contains "not define X".
    r"not\s+(?:have|has|having|configure[ds]?|set|defin[e]?[ds]?|specif(?:y|ie[ds])|"
    r"declare[ds]?|present|includ\w*)|"
    r"does\s*n\W?t\s+(?:have|configure|set|define|specify|declare|includ\w*)|"
    r"(?:is|are|was|were)\s*n\W?t"  # isn't / aren't / wasn't (straight or curly apostrophe)
)
#: Filler words that may sit between the absence word and the attribute token.
_ABSENCE_FILLER = (
    r"(?:a|an|any|the|explicit(?:ly)?|configured|defined|proper|valid|working|specified|set)"
)


def _claims_absent(text: str, token: str) -> bool:
    """Return True if *text* asserts *token* is ABSENT (not merely mentions it).

    Matches an absence word bound to the token in either order:
    ``<absence>[ filler]* <token>`` (e.g. "missing livenessProbe", "no resource
    limits") or ``<token>[ is/are] <absence>`` (e.g. "readinessProbe is absent").
    The absence word must be adjacent to *this* token, so "has readinessProbe but
    missing livenessProbe" binds "missing" to livenessProbe only (#1196).
    """
    t = re.escape(token)
    before = re.search(rf"(?:{_ABSENCE_WORD})(?:\s+{_ABSENCE_FILLER}){{0,3}}\s+{t}", text)
    after = re.search(rf"{t}(?:\s+(?:is|are|was|were|being))?\s+(?:{_ABSENCE_WORD})", text)
    return bool(before or after)


def _check_k8s_contradiction(
    title: str,
    description: str,
    resource_id: str,
    k8s_attrs_by_file: dict[str, dict[str, K8sWorkloadAttrs]],
    finding_file: str,
) -> bool:
    """Return True when the finding claims an attribute is absent but the manifest shows it present.

    This is the anti-hallucination guard 4 for k8s/compose targets.  A finding is
    *contradicted* when:
    - The combined ``title + description`` text CLAIMS a token from
      :data:`_ABSENCE_CLAIM_TOKENS` is absent (an absence word bound to that
      token — not a bare mention, see :func:`_claims_absent`, #1196), AND
    - The corresponding :class:`K8sWorkloadAttrs` field for ``resource_id`` is
      ``True`` (meaning the attribute IS present in the manifest).

    Only fires when ground-truth attribute data is available for ``(finding_file,
    resource_id)`` — absent data is neutral (no contradiction flagged).
    """
    file_attrs = k8s_attrs_by_file.get(finding_file, {})
    workload_attrs = file_attrs.get(resource_id)
    if workload_attrs is None:
        return False

    combined = (title + " " + description).lower()
    for token, attr_field in _ABSENCE_CLAIM_TOKENS.items():
        # Only contradict when the finding CLAIMS this attribute is absent (not
        # merely mentions it) AND the manifest shows it present (#1196).
        if _claims_absent(combined, token) and getattr(workload_attrs, attr_field, False):
            return True
    return False


# ---------------------------------------------------------------------------
# Finding normalization
# ---------------------------------------------------------------------------


def _normalize_finding(
    raw: object,
    reviewed_files: set[str],
    resource_ids_by_file: dict[str, set[str]],
    compliance_allowlist: set[str] | None,
    security_allowlist: set[str] | None,
    confidence_threshold: str,
    k8s_attrs_by_file: dict[str, dict[str, K8sWorkloadAttrs]] | None = None,
) -> PillarFinding | None:
    """Validate and normalize one model-proposed finding.

    Returns ``None`` (with a stderr warning) when the finding fails any
    anti-hallucination guard or is below the confidence threshold.

    For Kubernetes / docker-compose targets, *k8s_attrs_by_file* carries the
    parsed workload-attribute ground truth keyed by ``{filepath: {resource_id: attrs}}``.
    When a finding claims an attribute is absent but the manifest shows it present,
    the finding is marked ``contradicted=True``, downgraded to ``severity="low"``
    and ``confidence_level="low"``, and a WARNING is emitted — mirroring the
    CFN/Terraform guard-2 downgrade behaviour.
    """
    if not isinstance(raw, dict):
        print(
            f"[cloud-arch-review] WARNING: dropping non-dict finding: {raw!r}",
            file=sys.stderr,
        )
        return None

    # Anti-hallucination guard 1: file must be in the reviewed set
    finding_file = str(raw.get("file") or "")
    if finding_file not in reviewed_files:
        print(
            f"[cloud-arch-review] WARNING: dropping finding with unknown file "
            f"{finding_file!r} (not in reviewed-files set)",
            file=sys.stderr,
        )
        return None

    # Anti-hallucination guard 2: resource_id must be in the parsed resource ids
    resource_id = str(raw.get("resource_id") or "")
    file_resource_ids = resource_ids_by_file.get(finding_file, set())
    if resource_id and file_resource_ids and resource_id not in file_resource_ids:
        print(
            f"[cloud-arch-review] WARNING: dropping finding with unknown resource_id "
            f"{resource_id!r} for file {finding_file!r}",
            file=sys.stderr,
        )
        return None

    # Confidence threshold filter
    raw_conf = raw.get("confidence")
    confidence = float(raw_conf) if isinstance(raw_conf, int | float) else 0.0
    if not passes_threshold(confidence, confidence_threshold):
        return None

    conf_level = confidence_to_level(confidence)

    # Anti-hallucination guard 3: waf_question regex
    waf_q = validate_waf_question(raw.get("waf_question"))

    # Ref validation
    title = str(raw.get("title") or "")
    description = str(raw.get("description") or "")
    comp_refs = _filter_refs(raw.get("compliance_refs"), compliance_allowlist, "compliance", title)
    sec_refs = _filter_refs(raw.get("security_refs"), security_allowlist, "security", title)

    pillar = str(raw.get("pillar") or "")
    severity = str(raw.get("severity") or "low")
    if severity not in VALID_SEVERITIES:
        severity = "low"

    # Anti-hallucination guard 4 (k8s/compose): ground-truth contradiction check.
    # When the manifest shows an attribute IS present but the model claims it is
    # absent, downgrade severity + confidence_level and mark as contradicted.
    contradicted = False
    if (
        k8s_attrs_by_file
        and resource_id
        and _check_k8s_contradiction(
            title, description, resource_id, k8s_attrs_by_file, finding_file
        )
    ):
        print(
            f"[cloud-arch-review] WARNING: finding {title!r} for resource "
            f"{resource_id!r} contradicted by ground-truth manifest "
            f"(attribute claimed absent is actually present); downgrading to low",
            file=sys.stderr,
        )
        contradicted = True
        severity = "low"
        conf_level = "low"

    return PillarFinding(
        pillar=pillar,
        file=finding_file,
        line=int(raw.get("line") or 0),
        resource_id=resource_id,
        severity=severity,
        confidence=confidence,
        confidence_level=conf_level,
        title=title,
        description=description,
        suggestion=str(raw.get("suggestion") or ""),
        waf_question=waf_q,
        compliance_refs=comp_refs,
        security_refs=sec_refs,
        contradicted=contradicted,
    )


# ---------------------------------------------------------------------------
# C10/C11 dedup
# ---------------------------------------------------------------------------


def _dedup_ai_quality(
    c10_raw: list[dict[str, object]],
    c11_raw: list[dict[str, object]],
    s20_keys: set[tuple[str, int]],
) -> AiQualityBucket:
    """Apply the dedup discipline.

    C11 FM6/FM7/FM8 findings whose ``(file, line)`` matches an S20 finding are
    moved to ``deduplicated_findings``; all others stay in ``c11_findings``.
    C10 findings are passed through unchanged.
    """
    bucket = AiQualityBucket(c10_findings=list(c10_raw))
    for finding in c11_raw:
        if not isinstance(finding, dict):
            bucket.c11_findings.append(finding)
            continue
        fm_code = str(finding.get("fm_code") or finding.get("failure_mode") or "")
        # Extract just the code portion (e.g. "FM6" from "FM6: stale patterns")
        fm_match = re.match(r"(FM\d+)", fm_code)
        code = fm_match.group(1) if fm_match else ""
        f_file = str(finding.get("file") or "")
        raw_line = finding.get("line")
        f_line = int(raw_line) if isinstance(raw_line, (int, float)) else 0
        if code in _DEDUP_FM_CODES and (f_file, f_line) in s20_keys:
            bucket.deduplicated_findings.append(finding)
        else:
            bucket.c11_findings.append(finding)
    return bucket


# ---------------------------------------------------------------------------
# Document builder
# ---------------------------------------------------------------------------


def build_document(payload: dict[str, object], confidence_threshold: str) -> ReviewDocument:
    """Normalize a raw model payload into a :class:`ReviewDocument`."""
    workload_context_raw = payload.get("workload_context")
    workload_context: dict[str, object] = (
        dict(workload_context_raw) if isinstance(workload_context_raw, dict) else {}
    )

    iac_files_raw = payload.get("iac_files")
    iac_files: list[str] = (
        [str(f) for f in iac_files_raw] if isinstance(iac_files_raw, list) else []
    )
    reviewed_files = set(iac_files)

    # Detect formats and build resource_ids_by_file
    # For k8s/compose we also build a per-file workload-attribute map for
    # ground-truth contradiction checking (anti-hallucination guard 4).
    resource_ids_by_file: dict[str, set[str]] = {}
    # k8s_attrs_by_file: {filepath: {resource_id: K8sWorkloadAttrs}}
    k8s_attrs_by_file: dict[str, dict[str, K8sWorkloadAttrs]] = {}
    for fpath in iac_files:
        p = Path(fpath)
        if not p.is_file():
            resource_ids_by_file[fpath] = set()
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            resource_ids_by_file[fpath] = set()
            continue
        fmt = detect_iac_format(content, fpath)
        if fmt is not None:
            resources = parse_iac_resources(content, fmt, fpath)
            resource_ids_by_file[fpath] = {r.logical_id for r in resources}
            if fmt in ("kubernetes", "compose"):
                attrs_map, _ = parse_k8s_compose_attrs(content, fmt, fpath)
                k8s_attrs_by_file[fpath] = attrs_map
        else:
            resource_ids_by_file[fpath] = set()

    # Allowlists (None = no allowlist = accept all)
    comp_raw = payload.get("compliance_allowlist")
    compliance_allowlist: set[str] | None = (
        set(str(x) for x in comp_raw) if isinstance(comp_raw, list) else None
    )
    sec_raw = payload.get("security_allowlist")
    security_allowlist: set[str] | None = (
        set(str(x) for x in sec_raw) if isinstance(sec_raw, list) else None
    )

    # Normalize pillar findings
    raw_findings = payload.get("pillar_findings")
    items: list[object] = raw_findings if isinstance(raw_findings, list) else []
    pillar_findings: list[PillarFinding] = []
    for item in items:
        normalized = _normalize_finding(
            item,
            reviewed_files,
            resource_ids_by_file,
            compliance_allowlist,
            security_allowlist,
            confidence_threshold,
            k8s_attrs_by_file=k8s_attrs_by_file or None,
        )
        if normalized is not None:
            pillar_findings.append(normalized)

    # S20 (file, line) keys for dedup
    s20_keys: set[tuple[str, int]] = {(f.file, f.line) for f in pillar_findings}

    # C10/C11 dedup
    c10_raw_list = payload.get("c10_findings")
    c11_raw_list = payload.get("c11_findings")
    c10_raw: list[dict[str, object]] = (
        [d for d in c10_raw_list if isinstance(d, dict)] if isinstance(c10_raw_list, list) else []
    )
    c11_raw: list[dict[str, object]] = (
        [d for d in c11_raw_list if isinstance(d, dict)] if isinstance(c11_raw_list, list) else []
    )
    ai_quality = _dedup_ai_quality(c10_raw, c11_raw, s20_keys)

    return ReviewDocument(
        workload_context=workload_context,
        pillar_findings=pillar_findings,
        ai_quality=ai_quality,
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

_SEVERITY_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _wc_summary(ctx: dict[str, object]) -> str:
    regions_raw = ctx.get("regions")
    regions: list[object] = regions_raw if isinstance(regions_raw, list) else ["us-east-1"]
    return (
        f"**Workload type:** {ctx.get('workload_type', 'api')}  "
        f"**Criticality:** {ctx.get('criticality', 'tier-2')}  "
        f"**RTO:** {ctx.get('rto_minutes', 60)} min  "
        f"**RPO:** {ctx.get('rpo_minutes', 60)} min  "
        f"**Regions:** {', '.join(str(r) for r in regions)}"
    )


def render_markdown(doc: ReviewDocument) -> str:
    """Render a human-readable Well-Architected review Markdown report."""
    lines = [
        "# Well-Architected Review",
        "",
        _wc_summary(doc.workload_context),
        "",
    ]

    # Group by pillar
    by_pillar: dict[str, list[PillarFinding]] = {}
    for f in doc.pillar_findings:
        by_pillar.setdefault(f.pillar, []).append(f)

    for pillar in VALID_PILLARS:
        findings = by_pillar.get(pillar, [])
        lines.append(f"## {pillar.replace('_', ' ').title()} ({len(findings)} findings)")
        lines.append("")
        if not findings:
            lines.append("_No findings._")
            lines.append("")
            continue
        for f in sorted(findings, key=lambda x: -_SEVERITY_ORDER.get(x.severity, 0)):
            contradiction_tag = " ⚠️ CONTRADICTED BY MANIFEST" if f.contradicted else ""
            lines.append(f"### [{f.severity.upper()}] {f.title}{contradiction_tag}")
            lines.append("")
            lines.append(f"- **File:** `{f.file}` line {f.line}")
            if f.resource_id:
                lines.append(f"- **Resource:** `{f.resource_id}`")
            if f.waf_question:
                lines.append(f"- **WAF:** {f.waf_question}")
            lines.append(f"- **Confidence:** {f.confidence_level} ({f.confidence:.2f})")
            if f.contradicted:
                lines.append(
                    "- **NOTE:** This finding was contradicted by the ground-truth manifest "
                    "(the claimed-absent attribute is actually present); "
                    "downgraded to low severity."
                )
            lines.append(f"- {f.description}")
            lines.append(f"- **Suggestion:** {f.suggestion}")
            if f.compliance_refs:
                lines.append(f"- **Compliance refs:** {', '.join(f.compliance_refs)}")
            if f.security_refs:
                lines.append(f"- **Security refs:** {', '.join(f.security_refs)}")
            lines.append("")

    # AI quality section
    ai = doc.ai_quality
    total_ai = len(ai.c10_findings) + len(ai.c11_findings)
    dedup_count = len(ai.deduplicated_findings)
    lines.append(f"## AI Quality Findings ({total_ai} findings, {dedup_count} deduplicated)")
    lines.append("")
    if ai.c10_findings:
        lines.append(f"### C10 FM1-FM5 ({len(ai.c10_findings)} findings)")
        lines.append("")
        for af in ai.c10_findings:
            lines.append(f"- {af.get('title', str(af))}")
        lines.append("")
    if ai.c11_findings:
        lines.append(f"### C11 FM6-FM10 ({len(ai.c11_findings)} findings)")
        lines.append("")
        for af in ai.c11_findings:
            lines.append(f"- {af.get('title', str(af))}")
        lines.append("")
    if ai.deduplicated_findings:
        n_dedup = len(ai.deduplicated_findings)
        lines.append(f"### Deduplicated (moved from C11, (file,line) matched S20) ({n_dedup})")
        lines.append("")
        for af in ai.deduplicated_findings:
            lines.append(f"- {af.get('title', str(af))}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_json(doc: ReviewDocument) -> str:
    """Render a structured JSON report."""
    findings_out: list[dict[str, object]] = []
    for f in doc.pillar_findings:
        findings_out.append(
            {
                "pillar": f.pillar,
                "file": f.file,
                "line": f.line,
                "resource_id": f.resource_id,
                "severity": f.severity,
                "confidence": f.confidence,
                "confidence_level": f.confidence_level,
                "title": f.title,
                "description": f.description,
                "suggestion": f.suggestion,
                "waf_question": f.waf_question,
                "compliance_refs": f.compliance_refs,
                "security_refs": f.security_refs,
                "contradicted": f.contradicted,
            }
        )
    output = {
        "workload_context": doc.workload_context,
        "pillar_findings": findings_out,
        "ai_quality": {
            "c10_findings": doc.ai_quality.c10_findings,
            "c11_findings": doc.ai_quality.c11_findings,
            "deduplicated_findings": doc.ai_quality.deduplicated_findings,
        },
    }
    return json.dumps(output, indent=2) + "\n"


def _sarif_result(finding: PillarFinding, rule_id: str) -> dict[str, object]:
    """Convert a :class:`PillarFinding` to a SARIF result object."""
    return {
        "ruleId": rule_id,
        "level": "error" if finding.severity in ("high", "critical") else "warning",
        "message": {"text": f"[{finding.pillar}] {finding.title}: {finding.description}"},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": finding.file},
                    "region": {"startLine": max(1, finding.line)},
                }
            }
        ],
        "properties": {
            "severity": finding.severity,
            "waf_question": finding.waf_question,
            "confidence": finding.confidence,
            "suggestion": finding.suggestion,
            "contradicted": finding.contradicted,
        },
    }


def _sarif_ai_result(raw_finding: dict[str, object]) -> dict[str, object]:
    """Convert a raw C10/C11 finding dict to a SARIF result object."""
    f_file = str(raw_finding.get("file") or "")
    raw_line = raw_finding.get("line")
    f_line = int(raw_line) if isinstance(raw_line, (int, float)) else 1
    fm_code = str(raw_finding.get("fm_code") or raw_finding.get("failure_mode") or "UNKNOWN")
    title = str(raw_finding.get("title") or fm_code)
    desc = str(raw_finding.get("description") or "")
    return {
        "ruleId": fm_code,
        "level": "note",
        "message": {"text": f"{title}: {desc}"},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": f_file},
                    "region": {"startLine": max(1, f_line)},
                }
            }
        ],
    }


def render_sarif(doc: ReviewDocument) -> str:
    """Render a SARIF 2.1.0 report with two runs: S20 pillar + C10/C11 AI-quality."""
    # Run 1: S20 pillar findings
    s20_rules: list[dict[str, object]] = []
    seen_rules: set[str] = set()
    s20_results: list[dict[str, object]] = []
    for f in doc.pillar_findings:
        rule_id = f.waf_question or f"S20-{f.pillar[:4].upper()}"
        if rule_id not in seen_rules:
            seen_rules.add(rule_id)
            s20_rules.append(
                {
                    "id": rule_id,
                    "name": f.pillar,
                    "shortDescription": {"text": f"WAF {f.pillar} check"},
                }
            )
        s20_results.append(_sarif_result(f, rule_id))

    # Run 2: AI-quality findings (C10 + C11 non-deduplicated)
    ai_all = list(doc.ai_quality.c10_findings) + list(doc.ai_quality.c11_findings)
    ai_results = [_sarif_ai_result(f) for f in ai_all]
    ai_rule_ids = sorted(
        {str(f.get("fm_code") or f.get("failure_mode") or "UNKNOWN") for f in ai_all}
    )
    ai_rules: list[dict[str, object]] = [
        {"id": rid, "shortDescription": {"text": f"AI quality check {rid}"}} for rid in ai_rule_ids
    ]

    sarif: dict[str, object] = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "cloud-arch-review",
                        "version": "1.0.0",
                        "rules": s20_rules,
                    }
                },
                "results": s20_results,
            },
            {
                "tool": {
                    "driver": {
                        "name": "cloud-arch-review/ai-quality",
                        "version": "1.0.0",
                        "rules": ai_rules,
                    }
                },
                "results": ai_results,
            },
        ],
    }
    return json.dumps(sarif, indent=2) + "\n"


_RENDERERS = {
    "markdown": render_markdown,
    "json": render_json,
    "sarif": render_sarif,
}

OUTPUT_FORMATS: tuple[str, ...] = ("markdown", "json", "sarif")


def render(doc: ReviewDocument, fmt: str) -> str:
    """Render *doc* to one output format."""
    return _RENDERERS[fmt](doc)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic IaC filter + dedup + render for /cloud-arch-review."
    )
    parser.add_argument(
        "--findings-json",
        required=True,
        help="Findings payload JSON path, or - for stdin.",
    )
    parser.add_argument(
        "--format",
        default="markdown",
        choices=list(OUTPUT_FORMATS),
        help="Output format: markdown (default), json, or sarif.",
    )
    parser.add_argument(
        "--confidence-threshold",
        default="medium",
        choices=list(CONFIDENCE_LEVELS),
        help="Drop findings below this confidence level (default: medium).",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Output file path, or - for stdout (default).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print to stdout regardless of --output.",
    )
    args = parser.parse_args(argv)

    if args.findings_json == "-":
        raw = sys.stdin.read()
    else:
        try:
            raw = Path(args.findings_json).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error: cannot read --findings-json: {exc}", file=sys.stderr)
            return 2

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid --findings-json JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("Error: --findings-json must be a JSON object", file=sys.stderr)
        return 2

    doc = build_document(payload, args.confidence_threshold)
    rendered = render(doc, args.format)

    if args.dry_run or args.output == "-":
        sys.stdout.write(rendered)
        return 0

    # Sanitize the output path: reject path traversal in --output
    out_path = Path(args.output)
    # Reject absolute-path injection via path traversal in the output filename
    if ".." in out_path.parts:
        print(
            f"Error: --output {args.output!r} contains '..'; rejecting to prevent path traversal",
            file=sys.stderr,
        )
        return 2
    # Sanitize the filename stem only (directory component is user-trusted)
    safe_stem = _safe_filename(out_path.stem)
    safe_out = out_path.parent / f"{safe_stem}{out_path.suffix}"
    safe_out.parent.mkdir(parents=True, exist_ok=True)
    safe_out.write_text(rendered, encoding="utf-8")
    print(json.dumps({"written": str(safe_out), "findings": len(doc.pillar_findings)}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
