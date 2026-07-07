"""Safe XML parsing for peer/federated content (SEC-3, issue #85).

`pom.xml` files reach the foundry primitives from arbitrary sources — local
project trees, federated peer repos, and CI-cloned remote refs. The stdlib
`xml.etree.ElementTree` parser is *not* safe against denial-of-service via
quadratic-blowup, billion-laughs, and deeply-nested-element attacks; it also
keeps full text of expanded entities in memory.

This module centralizes the safe path:

* `parse_pom(text)` — parse a pom.xml from a string, returning the root
  element. Rejects payloads larger than `MAX_POM_BYTES` (5 MB by default;
  pom.xml files in real projects are kilobytes, not megabytes).
* `parse_pom_path(path)` — read the file safely via the same size cap, then
  delegate to `parse_pom`.

Both helpers swallow only `defusedxml.common.DefusedXmlException` and
`xml.etree.ElementTree.ParseError`, returning `None` on either. Callers
explicitly handle `XmlSafetyError` (size violation) to surface the rejection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from xml.etree.ElementTree import Element, ElementTree, ParseError

from defusedxml import ElementTree as _DefusedET
from defusedxml.common import DefusedXmlException

if TYPE_CHECKING:
    from pathlib import Path

MAX_POM_BYTES: Final[int] = 5_000_000


class XmlSafetyError(ValueError):
    """Raised when an XML payload violates the safety policy (size, etc.)."""


def parse_pom(text: str | bytes) -> Element | None:
    """Parse a pom.xml string with defusedxml and a 5 MB size cap.

    Returns the parsed root :class:`Element` on success. Returns ``None`` if
    the input is empty or malformed (matches the prior `ET.fromstring` +
    `except ParseError: return []` pattern). Raises :class:`XmlSafetyError`
    if the payload exceeds :data:`MAX_POM_BYTES`.
    """
    if not text:
        return None

    raw = text.encode("utf-8") if isinstance(text, str) else text
    if len(raw) > MAX_POM_BYTES:
        raise XmlSafetyError(
            f"pom.xml payload is {len(raw):,} bytes — exceeds the "
            f"{MAX_POM_BYTES:,}-byte safety cap."
        )

    try:
        return _DefusedET.fromstring(raw)
    except (ParseError, DefusedXmlException):
        return None


def parse_pom_path(path: Path) -> Element | None:
    """Read a pom.xml from *path* with the size cap, then delegate to
    :func:`parse_pom`. Returns ``None`` for missing files or malformed
    content; raises :class:`XmlSafetyError` for oversized files.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None

    if size > MAX_POM_BYTES:
        raise XmlSafetyError(
            f"{path} is {size:,} bytes — exceeds the {MAX_POM_BYTES:,}-byte safety cap."
        )

    try:
        text = path.read_bytes()
    except OSError:
        return None

    return parse_pom(text)


def parse_pom_tree(path: Path) -> ElementTree | None:
    """Like :func:`parse_pom_path` but returns the full :class:`ElementTree`
    so the caller can mutate-and-write it. Returns ``None`` for missing or
    malformed files; raises :class:`XmlSafetyError` for oversized files.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None

    if size > MAX_POM_BYTES:
        raise XmlSafetyError(
            f"{path} is {size:,} bytes — exceeds the {MAX_POM_BYTES:,}-byte safety cap."
        )

    try:
        return _DefusedET.parse(path)
    except (ParseError, DefusedXmlException, OSError):
        return None


__all__ = [
    "MAX_POM_BYTES",
    "XmlSafetyError",
    "parse_pom",
    "parse_pom_path",
    "parse_pom_tree",
]
