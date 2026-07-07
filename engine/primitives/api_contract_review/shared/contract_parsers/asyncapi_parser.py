"""S40 /api-contract-review — AsyncAPI 2.x / 3.x parser.

Parses YAML or JSON files with an `asyncapi:` key.
Extracts channel/operation pairs as Endpoint objects with file:line information.

Supports:
  - AsyncAPI 2.x (asyncapi: "2.x.x") — channels with subscribe/publish
  - AsyncAPI 3.x (asyncapi: "3.x.x") — channels + operations (separate)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import yaml

from primitives.api_contract_review.shared.models import ContractFormat, Endpoint

if TYPE_CHECKING:
    from pathlib import Path


class AsyncAPIParseError(Exception):
    """Raised when an AsyncAPI file cannot be parsed."""


def _detect_format(version_str: str) -> ContractFormat:
    """Map an asyncapi version string to ContractFormat."""
    vs = str(version_str)
    if vs.startswith("2."):
        return "asyncapi-2"
    if vs.startswith("3."):
        return "asyncapi-3"
    raise AsyncAPIParseError(f"Unsupported AsyncAPI version {version_str!r}; expected 2.x or 3.x")


def _load_document(file_path: Path) -> dict[str, Any]:
    """Load a YAML or JSON file. Raises AsyncAPIParseError on failure."""
    text = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()
    try:
        if suffix == ".json":
            data: dict[str, Any] = json.loads(text)
        else:
            data = yaml.safe_load(text) or {}
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise AsyncAPIParseError(f"Failed to parse {file_path}: {exc}") from exc
    return data


def _find_line_for(text: str, token: str) -> int:
    """Heuristically find the 1-indexed line where *token* appears."""
    for i, line in enumerate(text.splitlines(), 1):
        if token in line:
            return i
    return 1


def _parse_v2_channels(
    channels: dict[str, Any],
    text: str,
    file_str: str,
) -> list[Endpoint]:
    """Extract endpoints from AsyncAPI 2.x channels block."""
    endpoints: list[Endpoint] = []
    for channel_name, channel_item in channels.items():
        if not isinstance(channel_item, dict):
            continue
        channel_line = _find_line_for(text, str(channel_name))
        # AsyncAPI 2.x operations: subscribe, publish
        for op in ("subscribe", "publish"):
            if op in channel_item:
                endpoints.append(
                    Endpoint(
                        path=str(channel_name),
                        method_or_op=op,
                        format="asyncapi-2",
                        file=file_str,
                        line=channel_line,
                    )
                )
    return endpoints


def _parse_v3_channels_and_ops(
    channels: dict[str, Any],
    operations: dict[str, Any],
    text: str,
    file_str: str,
) -> list[Endpoint]:
    """Extract endpoints from AsyncAPI 3.x channels + operations blocks."""
    endpoints: list[Endpoint] = []
    # Add one endpoint per channel (send/receive split into operations)
    for channel_name in channels:
        channel_line = _find_line_for(text, str(channel_name))
        endpoints.append(
            Endpoint(
                path=str(channel_name),
                method_or_op="channel",
                format="asyncapi-3",
                file=file_str,
                line=channel_line,
            )
        )
    # Add one endpoint per operation
    for op_name, op_item in operations.items():
        op_line = _find_line_for(text, str(op_name))
        action = "send"
        if isinstance(op_item, dict):
            action = op_item.get("action", "send")
        endpoints.append(
            Endpoint(
                path=str(op_name),
                method_or_op=str(action),
                format="asyncapi-3",
                file=file_str,
                line=op_line,
            )
        )
    return endpoints


def parse_asyncapi(file_path: Path) -> tuple[list[Endpoint], ContractFormat]:
    """Parse an AsyncAPI 2.x or 3.x contract file and extract endpoints.

    Args:
        file_path: Path to the AsyncAPI YAML or JSON file.

    Returns:
        (endpoints, format) where format is "asyncapi-2" or "asyncapi-3".

    Raises:
        AsyncAPIParseError: If the file lacks an `asyncapi:` key or bad version.
    """
    text = file_path.read_text(encoding="utf-8")
    data = _load_document(file_path)

    if "asyncapi" not in data:
        raise AsyncAPIParseError(f"{file_path} does not have an 'asyncapi' key")

    contract_format = _detect_format(data["asyncapi"])
    file_str = str(file_path)

    channels = data.get("channels") or {}
    operations = data.get("operations") or {}

    if contract_format == "asyncapi-2":
        endpoints = _parse_v2_channels(channels, text, file_str)
    else:
        endpoints = _parse_v3_channels_and_ops(channels, operations, text, file_str)

    return endpoints, contract_format
