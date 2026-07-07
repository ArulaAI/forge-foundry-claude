"""S40 /api-contract-review — GraphQL SDL parser (regex-based).

Parses .graphql and .gql files using regex.
Extracts top-level type definitions and Query/Mutation/Subscription fields.

Strategy:
  1. Find all `type X { ... }` blocks via regex.
  2. For Query, Mutation, Subscription types: extract each field as an Endpoint.
  3. For all other types: emit one Endpoint per type (path=TypeName, op="type").
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from primitives.api_contract_review.shared.models import Endpoint

if TYPE_CHECKING:
    from pathlib import Path

# Match: type TypeName { ... }
_TYPE_BLOCK_RE = re.compile(
    r"type\s+(\w+)\s*(?:implements\s+\w+(?:\s*&\s*\w+)*)?\s*\{([^}]*)\}",
    re.DOTALL,
)

# Match a field in a type block: fieldName(args): ReturnType
_FIELD_RE = re.compile(r"^\s*(\w+)\s*(?:\([^)]*\))?\s*:", re.MULTILINE)

_OPERATION_TYPES = frozenset({"Query", "Mutation", "Subscription"})


def _find_line_for(text: str, token: str) -> int:
    """Find the 1-indexed line where *token* appears in *text*."""
    for i, line in enumerate(text.splitlines(), 1):
        if token in line:
            return i
    return 1


def parse_graphql(file_path: Path) -> list[Endpoint]:
    """Parse a GraphQL SDL file and extract endpoints.

    Each Query/Mutation/Subscription field becomes an Endpoint with:
      path = TypeName  (e.g., "Query")
      method_or_op = fieldName  (e.g., "getUser")

    Each non-operation type becomes:
      path = TypeName  (e.g., "User")
      method_or_op = "type"

    Args:
        file_path: Path to a .graphql or .gql file.

    Returns:
        List of Endpoint objects extracted from the SDL.
    """
    text = file_path.read_text(encoding="utf-8")
    file_str = str(file_path)
    endpoints: list[Endpoint] = []

    for match in _TYPE_BLOCK_RE.finditer(text):
        type_name = match.group(1)
        body = match.group(2)
        # Find the line for "type TypeName"
        type_line = _find_line_for(text, f"type {type_name}")

        if type_name in _OPERATION_TYPES:
            # Extract each field as an operation endpoint
            for field_match in _FIELD_RE.finditer(body):
                field_name = field_match.group(1)
                # Skip field if it's a built-in directive marker
                if field_name.startswith("__"):
                    continue
                # Find line in full text for this field
                field_line = _find_line_for(text, field_name)
                endpoints.append(
                    Endpoint(
                        path=type_name,
                        method_or_op=field_name,
                        format="graphql",
                        file=file_str,
                        line=field_line,
                    )
                )
        else:
            # Non-operation type: one endpoint per type
            endpoints.append(
                Endpoint(
                    path=type_name,
                    method_or_op="type",
                    format="graphql",
                    file=file_str,
                    line=type_line,
                )
            )

    return endpoints
