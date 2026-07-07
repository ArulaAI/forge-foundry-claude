"""S40 /api-contract-review — Format detector.

Content sniff to determine the API contract format of a file:
  - `openapi: "3.0` → openapi-3.0
  - `openapi: "3.1` → openapi-3.1
  - `asyncapi: "2.` → asyncapi-2
  - `asyncapi: "3.` → asyncapi-3
  - `.graphql` / `.gql` extension or `type Query` / `type Mutation` in content → graphql

Raises ContractFormatUnresolvableError if format cannot be determined.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import yaml

from primitives.api_contract_review.shared.contract_parsers.asyncapi_parser import (
    AsyncAPIParseError,
    parse_asyncapi,
)
from primitives.api_contract_review.shared.contract_parsers.graphql_parser import (
    parse_graphql,
)
from primitives.api_contract_review.shared.contract_parsers.openapi_parser import (
    OpenAPIParseError,
    parse_openapi,
)

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.api_contract_review.shared.models import ContractFormat, Endpoint

# Regex for OpenAPI / AsyncAPI version keys in YAML/JSON text
_OPENAPI_30_RE = re.compile(r"""openapi\s*:\s*["']?3\.0""")
_OPENAPI_31_RE = re.compile(r"""openapi\s*:\s*["']?3\.1""")
_ASYNCAPI_2_RE = re.compile(r"""asyncapi\s*:\s*["']?2\.""")
_ASYNCAPI_3_RE = re.compile(r"""asyncapi\s*:\s*["']?3\.""")
_GRAPHQL_TYPE_RE = re.compile(r"\btype\s+(Query|Mutation|Subscription|[A-Z]\w*)\s*\{")

_GRAPHQL_EXTENSIONS = frozenset({".graphql", ".gql"})


class ContractFormatUnresolvableError(Exception):
    """Raised when contract format cannot be determined from content or extension."""


def detect_contract_format(file_path: Path) -> ContractFormat:
    """Sniff the contract format from file extension and content.

    Priority order:
      1. .graphql / .gql extension → graphql
      2. Content sniff for openapi/asyncapi keys
      3. Content has `type Query` pattern → graphql fallback
      4. Raise ContractFormatUnresolvableError

    Args:
        file_path: Path to the contract file.

    Returns:
        ContractFormat literal.

    Raises:
        ContractFormatUnresolvableError: If format cannot be resolved.
    """
    suffix = file_path.suffix.lower()

    if suffix in _GRAPHQL_EXTENSIONS:
        return "graphql"

    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ContractFormatUnresolvableError(f"Cannot read {file_path}: {exc}") from exc

    fmt = _sniff_all(text, file_path)
    if fmt is None:
        raise ContractFormatUnresolvableError(
            f"Cannot determine API contract format for {file_path}. "
            "Use --format-hint to specify explicitly."
        )
    return fmt


_REGEX_FORMAT_MAP: tuple[tuple[re.Pattern[str], ContractFormat], ...] = (
    (_OPENAPI_31_RE, "openapi-3.1"),
    (_OPENAPI_30_RE, "openapi-3.0"),
    (_ASYNCAPI_3_RE, "asyncapi-3"),
    (_ASYNCAPI_2_RE, "asyncapi-2"),
    (_GRAPHQL_TYPE_RE, "graphql"),
)


def _sniff_all(text: str, file_path: Path) -> ContractFormat | None:
    """Sniff format via structured parse then regex fallbacks."""
    fmt = _sniff_structured(text, file_path)
    if fmt is not None:
        return fmt
    for pattern, contract_fmt in _REGEX_FORMAT_MAP:
        if pattern.search(text):
            return contract_fmt
    return None


def _sniff_structured(text: str, file_path: Path) -> ContractFormat | None:
    """Try to parse as YAML/JSON and check for openapi/asyncapi keys."""
    suffix = file_path.suffix.lower()
    try:
        if suffix == ".json":
            data: dict[str, Any] = json.loads(text)
        else:
            data = yaml.safe_load(text) or {}
    except (yaml.YAMLError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None

    return _fmt_from_dict(data)


def _fmt_from_dict(data: dict[str, Any]) -> ContractFormat | None:
    """Resolve ContractFormat from a parsed YAML/JSON dict."""
    openapi_ver = data.get("openapi")
    if openapi_ver is not None:
        ver_str = str(openapi_ver)
        if ver_str.startswith("3.1"):
            return "openapi-3.1"
        if ver_str.startswith("3.0"):
            return "openapi-3.0"

    asyncapi_ver = data.get("asyncapi")
    if asyncapi_ver is not None:
        ver_str = str(asyncapi_ver)
        if ver_str.startswith("3."):
            return "asyncapi-3"
        if ver_str.startswith("2."):
            return "asyncapi-2"

    return None


def parse_contract(file_path: Path) -> tuple[list[Endpoint], ContractFormat]:
    """Detect format and parse endpoints from a contract file.

    Returns:
        (endpoints, format) — endpoints extracted by the appropriate parser.

    Raises:
        ContractFormatUnresolvableError: If format cannot be resolved or content
            fails format-specific parsing (e.g. malformed YAML, unsupported version).
    """
    fmt = detect_contract_format(file_path)

    try:
        if fmt in ("openapi-3.0", "openapi-3.1"):
            endpoints, resolved_fmt = parse_openapi(file_path)
            return endpoints, resolved_fmt
        if fmt in ("asyncapi-2", "asyncapi-3"):
            endpoints, resolved_fmt = parse_asyncapi(file_path)
            return endpoints, resolved_fmt
        if fmt == "graphql":
            endpoints = parse_graphql(file_path)
            return endpoints, "graphql"
    except (OpenAPIParseError, AsyncAPIParseError) as exc:
        raise ContractFormatUnresolvableError(
            f"Contract detected as {fmt!r} but failed to parse: {exc}"
        ) from exc

    raise ContractFormatUnresolvableError(f"Unknown format: {fmt}")
