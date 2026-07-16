"""Adapter — app-analysis digest → :class:`ModernizationDigest` (SP1.3, #1314).

Normalizes the structured output of ``mod-app-analysis`` (which composes
``mod-playbook-discovery`` + ``repo-digest``) into the token-budgeted
:class:`~primitives._shared.modernization.models.ModernizationDigest` that
``correlate.py`` and ``sequencer.py`` consume.

**Coordination point — SP3 (#1324):** ``mod-app-analysis`` must produce a
dict matching the input shape documented in :func:`to_digest`'s docstring.
This adapter is the single contract point; update it (and its tests) if the
upstream shape changes.

**Out of scope:** HTTP/API interface discovery (that belongs to the
functional-map / API-contract adapters, #1312 / #1313). This adapter handles
the *app/module layer* only: service/module/package decomposition, internal
import graphs, tech-debt inventory, and migration-state signals.
"""

from __future__ import annotations

from typing import Any

from primitives._shared.modernization.models import ModernizationDigest

#: Default caps (mirror ``digest.py`` defaults so the adapter stays in budget).
_MAX_INTERFACES = 20
_MAX_ENTITIES = 20
_MAX_DEBT = 15
_MAX_SHARED = 20

#: Cap on the number of legacy-column → target-field rows the column-detail channel
#: carries into the SP-D prompt slice (§9). Keeps the slice in token budget; the
#: ``data:<table>`` join key rides on ``consumed_interfaces`` via :func:`to_digest`
#: independently and is NEVER affected by this cap.
_MAX_FIELD_MAPPINGS = 200


def to_digest(app_digest: dict[str, Any], *, code_root: str) -> ModernizationDigest:
    """Normalize an app-analysis digest into a :class:`ModernizationDigest`.

    **Expected input shape** (defined here — SP3 / #1324 must produce this):

    .. code-block:: python

        {
            # Stack / flavor identifier, e.g. "java_ee", "python", "generic".
            "flavor": str,                     # optional, default "generic"

            # LOC breakdown by language bucket.
            "loc_by_language": {str: int},     # optional, default {}

            # Top-level modules / services / packages discovered in the repo.
            # Each entry is a dict with at least "name" (str) and optionally
            # "kind" (str) and "weight" (int — lines of code or complexity proxy).
            "modules": [{"name": str, "kind": str, "weight": int}, ...],  # optional

            # Bounded-context / domain labels (e.g. from a context-detector pass).
            "domains": [str, ...],             # optional, default []

            # Internal import edges or call-graph edges from this unit to others.
            # Each entry is a dict with at least "target" (str) — the imported
            # module / consumed service coordinate.  Optionally "kind": str.
            "imports": [{"target": str, "kind": str}, ...],  # optional

            # Shared-library / utility-module coordinates referenced by this unit
            # (e.g. "common-util", "auth-lib").  Kept whole so correlate.py can
            # match shared-lib coordinates across units.
            "shared_libs": [str, ...],         # optional, default []

            # Tech-debt inventory — one-liner strings per finding.
            "debt": [str, ...],                # optional, default []

            # Migration-state summary, e.g. "Struts↔Spring coexistence".
            # None when no coexistence signal was detected.
            "migration_state": str | None,     # optional, default None

            # Modernization-tier hint, e.g. "strangler-fig" / "lift-and-shift".
            "tier": str | None,                # optional, default None
        }

    **Mapping rules:**

    - ``modules`` → ``key_entities`` (names, sorted by descending weight then
      name as tiebreaker) + ``domains`` merge (module names are *not* added to
      domains — only the explicit ``domains`` list is used).
    - ``imports[*].target`` → ``consumed_interfaces`` (the app/module layer
      only; HTTP routes are excluded — those belong to the functional-map
      adapter, #1312).
    - ``shared_libs`` → ``shared_components`` so ``correlate.py`` can match
      shared-lib coordinates across units.
    - ``debt`` → ``debt_summary``.
    - ``migration_state`` → ``migration_state``.
    - ``flavor`` → ``flavor``; ``tier`` → ``tier_hint``.
    - ``loc_by_language`` → ``loc_by_language``.
    - ``unit_count`` is set to ``len(modules)``;
      ``shared_component_count`` to ``len(shared_libs)``.
    - ``exposed_interfaces`` is left empty: the app-analysis layer does not
      discover outward-facing HTTP/API surfaces (use the functional-map adapter,
      #1312, for those).

    A partial digest (missing keys) never raises — all fields default safely.

    Args:
        app_digest: the upstream app-analysis dict (see shape above).
        code_root: absolute path to the analysed source tree.

    Returns:
        A :class:`ModernizationDigest` ready for ``correlate.py``.
    """
    flavor: str = app_digest.get("flavor") or "generic"
    loc_by_language: dict[str, int] = dict(app_digest.get("loc_by_language") or {})
    migration_state: str | None = app_digest.get("migration_state") or None
    tier_hint: str | None = app_digest.get("tier") or None
    domains: list[str] = list(app_digest.get("domains") or [])
    debt_raw: list[str] = list(app_digest.get("debt") or [])
    shared_libs: list[str] = list(app_digest.get("shared_libs") or [])

    # --- modules / services / packages ---
    modules_raw: list[dict[str, Any]] = list(app_digest.get("modules") or [])
    # Sort by descending weight, then name as a stable tiebreaker.
    sorted_modules = sorted(
        modules_raw,
        key=lambda m: (-int(m.get("weight") or 0), str(m.get("name") or "")),
    )
    key_entities: list[str] = [str(m["name"]) for m in sorted_modules if m.get("name")][
        :_MAX_ENTITIES
    ]

    # --- import / call-graph edges → consumed_interfaces ---
    # The ``data:<table>`` edges are the load-bearing UI→API→DB join keys
    # (#1348): ``correlate.build_system_map`` threads the app unit to the DB unit
    # *only* through them. They must NEVER be dropped by the size cap, so we cap
    # only the ORDINARY interfaces and always keep every ``data:`` edge. Ordering
    # is preserved: kept ordinary interfaces first, then the exempt join keys.
    imports_raw: list[dict[str, Any]] = list(app_digest.get("imports") or [])
    all_targets: list[str] = [str(imp["target"]) for imp in imports_raw if imp.get("target")]
    data_targets = [t for t in all_targets if t.startswith("data:")]
    other_targets = [t for t in all_targets if not t.startswith("data:")]
    consumed_interfaces: list[str] = other_targets[:_MAX_INTERFACES] + data_targets

    return ModernizationDigest(
        code_root=code_root,
        flavor=flavor,
        loc_by_language=loc_by_language,
        migration_state=migration_state,
        exposed_interfaces=[],
        consumed_interfaces=consumed_interfaces,
        domains=domains,
        key_entities=key_entities,
        tier_hint=tier_hint,
        debt_summary=debt_raw[:_MAX_DEBT],
        shared_components=shared_libs[:_MAX_SHARED],
        unit_count=len(modules_raw),
        shared_component_count=len(shared_libs),
    )


def _column_notes(col: dict[str, Any], primary_key: set[str], fk_targets: dict[str, str]) -> str:
    """Deterministic migration-notes for one M1 column (nullability / PK / FK).

    Free-text hints the harness can carry into §9's ``notes`` column — never a
    fabricated target mapping (the TARGET field is harness-interpreted; SP-D).
    """
    notes: list[str] = []
    name = str(col.get("name") or "")
    if name in primary_key:
        notes.append("primary key")
    if name in fk_targets:
        notes.append(f"FK → {fk_targets[name]}")
    # ``nullable`` may be absent; only surface the NOT NULL constraint (the
    # load-bearing one for a target-schema author) when the column is explicitly
    # non-nullable.
    if col.get("nullable") is False:
        notes.append("NOT NULL")
    default = col.get("default")
    if default not in (None, ""):
        notes.append(f"default {default}")
    pii = col.get("pii_classification")
    if pii:
        notes.append(f"PII: {pii}")
    return "; ".join(notes)


def extract_data_field_mappings(schema_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Mine M1's per-column detail into ``DataFieldMapping``-shaped rows for §9.

    M1's ``mod-db-schema-analyze`` output already carries full per-column detail
    (``tables[].columns[]`` — ``name`` / ``type`` / ``nullable`` / ``default`` /
    ``pii_classification`` — plus ``primary_key`` and ``foreign_keys``). The
    ``data:<table>`` **join key** the correlator threads on rides on the digest's
    ``consumed_interfaces`` (see :func:`to_digest`) and is produced independently;
    this function is a **parallel channel** that carries the column *detail*
    ALONGSIDE that key — it never touches, reorders, or drops a join key, so the
    #1348 UI→API→DB correlation stays byte-identical.

    Each row is a plain dict matching ``modernization_scope``'s ``DataFieldMapping``
    shape (``legacy`` / ``target`` / ``type`` / ``migration_notes``). The LEGACY
    side is deterministic (``table.column`` + the dialect-preserving M1 type +
    nullability/PK/FK/PII notes); the TARGET side is left empty for the harness to
    author in-session (SP-D — the deterministic extractor cannot name the target
    schema). Deterministic, no wall-clock / LLM.

    Args:
        schema_json: an M1 ``schema.json`` dict (``{"tables": [{"name", "columns",
            "primary_key", "foreign_keys"}, ...]}``). A partial / empty dict never
            raises — missing keys yield an empty list.

    Returns:
        Up to ``_MAX_FIELD_MAPPINGS`` mapping rows, in table then column order.
    """
    rows: list[dict[str, Any]] = []
    for tbl in schema_json.get("tables") or []:
        if not isinstance(tbl, dict):
            continue
        table_name = str(tbl.get("name") or "").strip()
        if not table_name:
            continue
        primary_key = {str(c) for c in (tbl.get("primary_key") or [])}
        # Map single-column FK sources → "ref_table.ref_column" (multi-column FKs
        # zip their parallel columns / referenced_columns lists).
        fk_targets: dict[str, str] = {}
        for fk in tbl.get("foreign_keys") or []:
            if not isinstance(fk, dict):
                continue
            cols = list(fk.get("columns") or [])
            ref_table = str(fk.get("referenced_table") or "").strip()
            ref_cols = list(fk.get("referenced_columns") or [])
            for i, src_col in enumerate(cols):
                ref_col = ref_cols[i] if i < len(ref_cols) else ""
                fk_targets[str(src_col)] = f"{ref_table}.{ref_col}" if ref_col else ref_table
        for col in tbl.get("columns") or []:
            if not isinstance(col, dict):
                continue
            col_name = str(col.get("name") or "").strip()
            if not col_name:
                continue
            rows.append(
                {
                    "legacy": f"{table_name}.{col_name}",
                    "target": "",
                    "type": str(col.get("type") or ""),
                    "migration_notes": _column_notes(col, primary_key, fk_targets),
                }
            )
            if len(rows) >= _MAX_FIELD_MAPPINGS:
                return rows
    return rows
