"""R1 /rules-gen — catalog YAML loader.

Loads + Pydantic-validates the curated YAML catalogs that drive R1's
dual-harness rule generation. The schema mirrors SPEC.md §"Catalog YAML
schema": ``Catalog`` carries a name, description, version, an optional
``paths`` list (empty → always-on rule), and an ordered list of
``RuleSection`` blocks.

Two failure modes are surfaced as distinct exceptions so callers can
distinguish "the catalog you asked for does not exist" from "the catalog
YAML is structurally broken":

* :class:`CatalogNotFoundError` — the requested name has no matching
  ``<name>.yaml`` (or ``<name>.yml``) under ``shared/catalogs/``.
* :class:`CatalogParseError` — the file exists but is not valid YAML, is
  not a YAML mapping at the top level, or fails ``Catalog`` schema
  validation.

YAML is loaded via ``yaml.safe_load`` so a malicious catalog cannot
instantiate arbitrary Python objects. The ``Catalog`` model has
``extra="forbid"`` so unexpected keys (typos, schema drift) raise
:class:`CatalogParseError` rather than silently surviving the load.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "Catalog",
    "CatalogError",
    "CatalogNotFoundError",
    "CatalogParseError",
    "RuleSection",
    "load_catalog",
    "load_catalogs",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CatalogError(Exception):
    """Base class for catalog-loading errors."""


class CatalogNotFoundError(CatalogError):
    """Raised when a requested catalog name has no matching YAML file."""


class CatalogParseError(CatalogError):
    """Raised when a catalog YAML file is malformed or fails schema validation."""


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class RuleSection(BaseModel):
    """A single rule section: a heading plus the bullet-style rules under it.

    ``heading`` becomes a ``##`` markdown heading in the rendered output;
    ``rules`` becomes the bullet list under that heading. Both fields are
    required and non-empty — an empty section makes no sense in either
    harness and almost always signals a typo in the YAML.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    heading: str = Field(min_length=1)
    rules: list[str] = Field(min_length=1)


class Catalog(BaseModel):
    """A curated rule catalog (BFSI, language, database, cloud, …).

    ``paths`` is the file-glob list both harnesses key off: Claude Code
    serialises it as a YAML list under the ``paths:`` frontmatter key;
    Copilot collapses it to a comma-separated string under ``applyTo:``.
    An empty ``paths`` list (the explicit canonical value, ``[]``) means
    "always-on rule" — every file in the consuming repo gets the rule
    applied. See SPEC.md §"Output" for the renderer contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    version: str = Field(min_length=1)
    paths: list[str] = Field(default_factory=list)
    sections: list[RuleSection] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_catalog(path: Path) -> Catalog:
    """Load + Pydantic-validate a single catalog YAML.

    Args:
        path: Filesystem path to the catalog YAML file.

    Returns:
        The validated :class:`Catalog` instance.

    Raises:
        CatalogNotFoundError: ``path`` does not exist or is not a file.
        CatalogParseError: ``path`` is not valid YAML, is not a mapping at
            the top level, or fails ``Catalog`` schema validation.
    """
    path = Path(path)
    if not path.is_file():
        raise CatalogNotFoundError(f"Catalog file not found: {path}")

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogParseError(f"Could not read catalog file {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise CatalogParseError(f"Catalog {path} is not valid YAML: {exc}") from exc

    if data is None:
        raise CatalogParseError(f"Catalog {path} is empty")
    if not isinstance(data, dict):
        raise CatalogParseError(
            f"Catalog {path} top-level must be a YAML mapping, got {type(data).__name__}"
        )

    try:
        return Catalog.model_validate(data)
    except ValidationError as exc:
        raise CatalogParseError(f"Catalog {path} failed schema validation: {exc}") from exc


def load_catalogs(catalog_dir: Path, names: list[str]) -> list[Catalog]:
    """Load multiple catalogs by name from a directory.

    Args:
        catalog_dir: Directory holding ``<name>.yaml`` / ``<name>.yml`` files.
        names: Catalog name slugs to load (e.g. ``["bfsi-core", "java"]``).
            Names are matched against the file stem; ``.yaml`` takes
            precedence over ``.yml`` if both exist.

    Returns:
        The validated catalogs in the same order as ``names``.

    Raises:
        CatalogNotFoundError: ``catalog_dir`` does not exist, or one or
            more requested ``names`` has no matching file under it.
        CatalogParseError: any matched file fails to parse or validate.
    """
    catalog_dir = Path(catalog_dir)
    if not catalog_dir.is_dir():
        raise CatalogNotFoundError(f"Catalog directory not found: {catalog_dir}")

    catalogs: list[Catalog] = []
    for name in names:
        path = _resolve_catalog_path(catalog_dir, name)
        catalogs.append(load_catalog(path))
    return catalogs


def _resolve_catalog_path(catalog_dir: Path, name: str) -> Path:
    """Return the ``<name>.yaml`` / ``<name>.yml`` file in ``catalog_dir``.

    Both ``.yaml`` and ``.yml`` extensions are accepted; ``.yaml`` wins on
    conflict (Pydantic / PyYAML community convention). Raises
    :class:`CatalogNotFoundError` if neither exists.
    """
    for suffix in (".yaml", ".yml"):
        candidate = catalog_dir / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    raise CatalogNotFoundError(
        f"Catalog {name!r} not found under {catalog_dir} (looked for .yaml and .yml extensions)"
    )
