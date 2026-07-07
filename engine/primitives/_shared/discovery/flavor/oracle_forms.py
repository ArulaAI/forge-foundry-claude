"""M-EPIC-3 polyglot flavor stub: Oracle Forms (M5-FLAVOR-STUBS, #528).

This module reserves the public surface for a future v1.1+ Oracle
Forms flavor pack of the M5 ``--platform-flavor`` dispatcher. Per
the M-EPIC-3 design spec
(``docs/superpowers/specs/2026-06-03-polyglot-discovery-design.md``
§"Auto-detection heuristic" + §"Implementation tickets" row #528),
the M-EPIC-3 reframing ships a *shaped* extension surface — five
stub flavors (``powerbuilder``, ``delphi``, ``vb6``, ``oracle_forms``,
``as400_rpg``) registered alongside the built-in three so
``--platform-flavor`` help output advertises the full polyglot
roadmap. Tracker: parent EPIC **#520** / sub-issue **#528**.

This pattern intentionally mirrors :mod:`.jcl_batch` and
:mod:`.ims_db` (M5-FLAVOR-2 / #447) — see :mod:`.powerbuilder` for
the full pattern rationale. Only ``parse()`` raises
``NotImplementedError``; ``detect()`` is a real cheap probe.

## Deferred scope (what the future parser will cover)

The Oracle Forms flavor would parse **Oracle Forms application
sources** — Oracle's 4GL forms-runner / forms-builder stack widely
deployed in BFSI / government back-office through the 1990s-2010s
(Oracle Forms 6i / 10g / 11g / 12c). The following extractions are
deferred to a future v1.1+ sub-issue under EPIC #520:

* **Module inventory.** ``*.fmb`` (Forms Module Binary, the
  source-of-truth in the Forms Builder IDE) and ``*.fmx`` (Forms
  Module eXecutable, the compiled runtime artefact) declare the
  application's deployable graph. ``*.mmb`` / ``*.mmx`` (menu
  module) and ``*.pll`` / ``*.plx`` (PL/SQL library) round out
  the inventory.
* **Block / item / canvas hierarchy.** Forms organize around
  ``DATA BLOCK`` (cursor over a base table or PL/SQL block) which
  contains ``ITEM`` records, rendered onto a ``CANVAS``. The
  block-base-table binding feeds §6 (Data Architecture); the
  canvas hierarchy feeds §7 (Screen Navigation & UI Layer).
* **Trigger inventory.** Each block / item / form-level trigger
  (``WHEN-BUTTON-PRESSED``, ``WHEN-VALIDATE-ITEM``,
  ``PRE-INSERT``, etc.) carries PL/SQL bodies; the trigger graph
  is the primary §5 (Call Chain Maps) edge source. Form-to-form
  navigation via ``CALL_FORM`` / ``OPEN_FORM`` produces
  cross-module edges.
* **PL/SQL embedded SQL.** Triggers and program units contain
  embedded SQL (anonymous PL/SQL blocks, ``DECLARE``-shaped
  cursors); each ``SELECT`` / ``INSERT`` / ``UPDATE`` /
  ``DELETE`` produces a ``DataStore`` access record for §6 (Data
  Architecture).
* **LOV / record-group catalog.** Lists of Values (``LOV``) and
  record groups encode the application's enum domains; they feed
  the §8 (Business Rules Catalog) "domain constants" section.
* **Library dependency graph.** ``*.pll`` (PL/SQL library)
  ``ATTACH_LIB`` declarations produce ``SharedComponent`` records
  — Forms's analogue of a copybook / shared-library graph for §3
  (Execution Unit Inventory).

## Why ship the stub now

Same rationale as :mod:`.powerbuilder`: Oracle Forms is end-of-life
on most current Oracle releases (12c extended support ended 2024),
and BFSI modernization assessments routinely encounter Forms
codebases. Advertising the extension surface signals "Forms
modernization is on the roadmap".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import Confidence


#: File-extension probe set. ``*.fmb`` is the Forms binary source;
#: ``*.fmx`` is the compiled runtime artefact; ``*.frm`` is the
#: legacy Forms 4/5 text source (also collides with VB6's ``*.frm``,
#: so detection alone cannot disambiguate — the future parser would
#: peek at file headers to discriminate).
_ORACLE_FORMS_EXTENSIONS: Final[frozenset[str]] = frozenset({".fmb", ".fmx", ".frm"})

_PARENT_EPIC: Final[str] = "#520"
_SUB_ISSUE: Final[str] = "#528"


class _OracleFormsFlavorPack:
    """Stub :class:`FlavorParser` for the Oracle Forms flavor.

    Implements the full Protocol surface; ``parse()`` raises
    :class:`NotImplementedError` pointing at parent EPIC #520. The
    ``detect()`` heuristic returns ``"low"`` (never ``"medium"`` /
    ``"high"``) — see :mod:`.powerbuilder` rationale.
    """

    name: Final[str] = "oracle_forms"

    def detect(self, code_root: Path) -> Confidence:
        """Probe ``code_root`` for Oracle Forms file extensions.

        Returns ``"low"`` always; presence of ``*.fmb`` / ``*.fmx`` /
        ``*.frm`` is still probed so future telemetry can
        distinguish "Forms-shaped tree, parser not shipped" from
        "no Forms signal at all".

        Note: ``*.frm`` collides with VB6's form extension. The
        detect heuristic intentionally flags it for both flavors —
        both return ``"low"`` and a future implementation can
        peek file headers to disambiguate.

        Args:
            code_root: Repository root the auto-detect path is
                inspecting.

        Returns:
            ``"low"`` always.
        """
        for ext in _ORACLE_FORMS_EXTENSIONS:
            try:
                if next(code_root.rglob(f"*{ext}"), None) is not None:
                    return "low"
            except OSError:
                continue
        return "low"

    def parse(
        self,
        file_path: Path,
        content: str,
    ) -> dict[str, list[object]]:
        """Raise :class:`NotImplementedError` citing EPIC #520.

        Args:
            file_path: Source file path (placeholder).
            content: Raw file content (placeholder).

        Raises:
            NotImplementedError: Always, citing parent EPIC #520 and
                sub-issue #528.
        """
        del file_path, content
        raise NotImplementedError(
            "M-EPIC-3 oracle_forms flavor not yet implemented — "
            f"tracked under parent EPIC {_PARENT_EPIC} (sub-issue {_SUB_ISSUE}; "
            "deferred scope: module inventory (FMB/FMX/MMB/PLL), "
            "block/item/canvas hierarchy, trigger inventory, "
            "PL/SQL embedded SQL, LOV/record-group catalog, "
            "library dependency graph). See "
            "primitives/_shared/discovery/flavor/oracle_forms.py "
            "module docstring for the full deferred-scope contract."
        )

    def recommended_section_branches(self) -> list[str]:
        """Return ``["oracle_forms"]`` — the flavor's section branch."""
        return ["oracle_forms"]


FLAVOR_PACK: Final[_OracleFormsFlavorPack] = _OracleFormsFlavorPack()


__all__ = ["FLAVOR_PACK"]
