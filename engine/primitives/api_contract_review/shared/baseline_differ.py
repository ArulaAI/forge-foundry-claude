"""S40 /api-contract-review — Baseline differ (Stage D).

Reads a prior version of the contract file via `git show <ref>:<path>`
and computes a backward-compatibility diff:
  - removed endpoints (paths/methods)
  - type-narrowed fields
  - new required fields

Uses subprocess with shell=False for security.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from primitives.api_contract_review.shared.models import BaselineDiff

logger = logging.getLogger(__name__)

__all__ = ["BaselineDiff", "BaselineDiffError", "diff_baseline"]


class BaselineDiffError(Exception):
    """Raised when the baseline cannot be read or diffed."""


def _scrubbed_git_env() -> dict[str, str]:
    """Return ``os.environ`` with ``GIT_*`` keys stripped.

    Without this, subprocess git calls inherit ``GIT_DIR`` /
    ``GIT_INDEX_FILE`` / ``GIT_WORK_TREE`` when this module is invoked
    from inside another ``git commit`` (e.g. pytest running under a
    lefthook pre-commit hook). The inherited vars cause the inner ``git``
    to look at the outer repo regardless of the ``cwd=`` we pass.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _get_git_root(path: Path) -> Path:
    """Return the root of the git repository containing *path*.

    Raises:
        BaselineDiffError: If not in a git repository.
    """
    repo_dir = path if path.is_dir() else path.parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=_scrubbed_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BaselineDiffError(f"Could not find git root: {exc}") from exc
    if result.returncode != 0:
        raise BaselineDiffError(f"Not in a git repository: {repo_dir}")
    return Path(result.stdout.strip())


def _run_git_show(git_ref: str, file_path: Path) -> str:
    """Run `git show <git_ref>:<relative_path>` and return stdout.

    The path passed to git show must be relative to the git repo root.

    Args:
        git_ref: Git ref (branch, tag, SHA, or expression like HEAD~1).
        file_path: Absolute path to the file in the repo.

    Returns:
        File content at the given git ref.

    Raises:
        BaselineDiffError: If git show fails.
    """
    abs_path = file_path.resolve()
    git_root = _get_git_root(abs_path)
    try:
        relative_path = abs_path.relative_to(git_root)
    except ValueError as exc:
        raise BaselineDiffError(f"File {abs_path} is not under git root {git_root}") from exc

    cmd = ["git", "show", f"{git_ref}:{relative_path}"]
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            cwd=git_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_scrubbed_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BaselineDiffError(f"git show failed for ref={git_ref!r}: {exc}") from exc

    if result.returncode != 0:
        raise BaselineDiffError(
            f"git show {git_ref}:{relative_path} returned exit {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def _load_yaml_or_json(text: str, source: str) -> dict[str, Any]:
    """Parse text as YAML or JSON; return dict or empty dict on error."""
    try:
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except yaml.YAMLError:
        pass
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    logger.warning(
        "baseline_differ: could not parse %s — both YAML and JSON failed; "
        "returning empty baseline (no breaking-change diff)",
        source,
    )
    return {}


def _extract_openapi_endpoints(data: dict[str, Any]) -> dict[str, set[str]]:
    """Extract {path: set(methods)} from an OpenAPI data dict."""
    result: dict[str, set[str]] = {}
    paths = data.get("paths") or {}
    _methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        methods = {m.upper() for m in item if m.lower() in _methods}
        result[str(path)] = methods
    return result


def _extract_openapi_required_fields(data: dict[str, Any]) -> dict[str, set[str]]:
    """Extract {schema_name: set(required_fields)} from components/schemas."""
    result: dict[str, set[str]] = {}
    schemas = (data.get("components") or {}).get("schemas") or {}
    for name, schema in schemas.items():
        if isinstance(schema, dict):
            required = schema.get("required") or []
            if isinstance(required, list):
                result[str(name)] = set(str(r) for r in required)
    return result


def _extract_openapi_field_types(data: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Extract {schema_name: {field_name: type}} from components/schemas."""
    result: dict[str, dict[str, str]] = {}
    schemas = (data.get("components") or {}).get("schemas") or {}
    for name, schema in schemas.items():
        if not isinstance(schema, dict):
            continue
        props = schema.get("properties") or {}
        field_types: dict[str, str] = {}
        for fname, fschema in props.items():
            if isinstance(fschema, dict):
                field_types[str(fname)] = str(fschema.get("type", "unknown"))
        result[str(name)] = field_types
    return result


def _extract_asyncapi_channels(data: dict[str, Any]) -> set[str]:
    """Extract channel names from an AsyncAPI data dict."""
    channels = data.get("channels") or {}
    return set(str(c) for c in channels)


def _extract_graphql_types(text: str) -> set[str]:
    """Extract type names from a GraphQL SDL text."""
    return set(re.findall(r"\btype\s+(\w+)\s*(?:implements\s+\S+\s*)?\{", text))


def diff_baseline(
    current_path: Path,
    baseline_ref: str,
) -> BaselineDiff:
    """Compute backward-compatibility diff against a baseline git ref.

    Reads the prior version via `git show <baseline_ref>:<current_path>`.

    Args:
        current_path: Path to the current contract file (must be in a git repo).
        baseline_ref: Git ref for the baseline (e.g., "HEAD~1", "main", "v1.0.0").

    Returns:
        BaselineDiff with removed, type_narrowed, new_required_fields lists.

    Raises:
        BaselineDiffError: If baseline cannot be read.
    """
    # Read baseline content
    baseline_text = _run_git_show(baseline_ref, current_path)
    current_text = current_path.read_text(encoding="utf-8")

    suffix = current_path.suffix.lower()

    if suffix in (".graphql", ".gql"):
        return _diff_graphql(current_text, baseline_text)

    baseline_data = _load_yaml_or_json(baseline_text, f"baseline:{current_path}")
    current_data = _load_yaml_or_json(current_text, f"current:{current_path}")

    # Detect type
    if "openapi" in baseline_data or "openapi" in current_data:
        return _diff_openapi(current_data, baseline_data)
    if "asyncapi" in baseline_data or "asyncapi" in current_data:
        return _diff_asyncapi(current_data, baseline_data)

    # Fallback: empty diff
    return BaselineDiff()


def _diff_openapi(
    current: dict[str, Any],
    baseline: dict[str, Any],
) -> BaselineDiff:
    """Compute backward-compat diff for OpenAPI contracts."""
    base_eps = _extract_openapi_endpoints(baseline)
    curr_eps = _extract_openapi_endpoints(current)

    removed: list[str] = []
    for path, methods in base_eps.items():
        if path not in curr_eps:
            for m in sorted(methods):
                removed.append(f"{m} {path}")
        else:
            for m in sorted(methods):
                if m not in curr_eps[path]:
                    removed.append(f"{m} {path}")

    # Check required fields
    base_req = _extract_openapi_required_fields(baseline)
    curr_req = _extract_openapi_required_fields(current)
    new_required_fields: list[str] = []
    for schema_name, curr_fields in curr_req.items():
        base_fields = base_req.get(schema_name, set())
        for field in sorted(curr_fields - base_fields):
            new_required_fields.append(f"{schema_name}.{field}")

    # Check type changes
    base_types = _extract_openapi_field_types(baseline)
    curr_types = _extract_openapi_field_types(current)
    type_narrowed: list[str] = []
    for schema_name, curr_field_types in curr_types.items():
        base_field_types: dict[str, str] = base_types.get(schema_name, {})
        for field_name, curr_type in curr_field_types.items():
            base_type = base_field_types.get(field_name)
            if base_type is not None and base_type != curr_type:
                type_narrowed.append(f"{schema_name}.{field_name}: {base_type} → {curr_type}")

    return BaselineDiff(
        removed=removed,
        type_narrowed=type_narrowed,
        new_required_fields=new_required_fields,
    )


def _diff_asyncapi(
    current: dict[str, Any],
    baseline: dict[str, Any],
) -> BaselineDiff:
    """Compute backward-compat diff for AsyncAPI contracts."""
    base_channels = _extract_asyncapi_channels(baseline)
    curr_channels = _extract_asyncapi_channels(current)
    removed = sorted(base_channels - curr_channels)
    return BaselineDiff(removed=removed)


def _diff_graphql(current_text: str, baseline_text: str) -> BaselineDiff:
    """Compute backward-compat diff for GraphQL SDL."""
    base_types = _extract_graphql_types(baseline_text)
    curr_types = _extract_graphql_types(current_text)
    removed = sorted(f"type:{t}" for t in base_types - curr_types)
    return BaselineDiff(removed=removed)
