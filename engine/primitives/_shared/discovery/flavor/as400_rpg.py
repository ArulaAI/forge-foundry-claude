"""M-EPIC-3 polyglot flavor stub: AS/400 RPG (M5-FLAVOR-STUBS, #528).

This module reserves the public surface for a future v1.1+
AS/400 RPG flavor pack of the M5 ``--platform-flavor`` dispatcher.
Per the M-EPIC-3 design spec
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

The AS/400 RPG flavor would parse **IBM i (AS/400) RPG application
sources** — RPG II / RPG III / RPG/400 / RPG IV (RPGLE) / SQLRPGLE,
plus the surrounding CL command-language tooling. AS/400 systems
remain a massive BFSI / insurance / manufacturing workload (IBM i
runs on Power hardware through current Power10 / Power11 systems).
The following extractions are deferred to a future v1.1+ sub-issue
under EPIC #520:

* **Program / module inventory.** ``*.rpg`` (fixed-format RPG II /
  III / 400), ``*.rpgle`` (free-form RPG IV), and ``*.sqlrpgle``
  (SQLRPGLE — embedded SQL preprocessor) source members produce
  ``ExecutionUnit`` records with ``kind="rpg_program"`` (already
  provisioned in :data:`ExecutionUnitKind`). ``*.cl`` /
  ``*.clle`` (CL / CLLE command-language source members) produce
  the batch / job-control records that feed §9 (Batch Processing).
* **/COPY / /INCLUDE inventory.** ``/COPY <library>/<member>``
  directives in RPGLE source produce ``SharedComponent`` records
  — the RPG analogue of COBOL copybooks and C/C++ includes.
* **File-spec / F-spec inventory.** Fixed-format ``F`` specs and
  free-form ``DCL-F`` declarations bind RPG programs to PF
  (physical file) / LF (logical file) / DSPF (display file) /
  PRTF (printer file) records. PF/LF bindings feed §6 (Data
  Architecture); DSPF bindings feed §7 (Screen Navigation & UI
  Layer) — the classic 5250 green-screen UI surface.
* **DDS source extraction.** Display files + printer files +
  physical/logical files are typically defined in DDS source
  (``*.dds`` / ``*.pf`` / ``*.lf`` / ``*.dspf`` / ``*.prtf``
  conventions vary by shop). The DDS DCMNS / R / A specs carry
  the field-level layout that §6 + §7 render.
* **CL command call graph.** ``CALL PGM(LIB/PGM)`` /
  ``CALLPRC PRC(PROC)`` / ``SBMJOB CMD(CALL …)`` in CL source
  produce the batch-side call graph for §5 (Call Chain Maps) and
  §9 (Batch Processing).
* **Embedded SQL extraction.** ``EXEC SQL`` blocks in
  ``*.sqlrpgle`` carry embedded SQL — each statement produces a
  ``DataStore`` access record for §6 (Data Architecture). SQL
  cursor declarations + FETCH loops are common RPG patterns the
  parser would surface as cursor-management records.
* **Subfile / message subfile patterns.** Subfile-based UI
  (``SFL`` / ``SFLCTL``) is the AS/400 standard for list-display
  green screens; recognizing the pattern feeds §7 + §15
  (Modernization Analysis) — subfile rewrites to web are a
  large chunk of AS/400 modernization work.

## Why ship the stub now

Same rationale as :mod:`.powerbuilder`: AS/400 / IBM i is *not*
end-of-life (IBM ships current OS releases on current Power
hardware), but BFSI modernization frequently targets RPG
replacement programs. Advertising the extension surface signals
"AS/400 modernization is on the roadmap".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import Confidence


#: File-extension probe set. ``*.rpg`` is fixed-format RPG;
#: ``*.rpgle`` is free-form RPG IV; ``*.cl`` / ``*.clle`` are CL
#: command-language sources. ``*.sqlrpgle`` is the SQL preprocessor
#: variant — covered via the ``*.rpgle`` glob suffix match since
#: ``Path.rglob("*.rpgle")`` also picks up ``*.sqlrpgle`` (suffix
#: tail match) — but we include it explicitly for clarity.
_AS400_RPG_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".rpg", ".rpgle", ".sqlrpgle", ".cl", ".clle"}
)

_PARENT_EPIC: Final[str] = "#520"
_SUB_ISSUE: Final[str] = "#528"


class _As400RpgFlavorPack:
    """Stub :class:`FlavorParser` for the AS/400 RPG flavor.

    Implements the full Protocol surface; ``parse()`` raises
    :class:`NotImplementedError` pointing at parent EPIC #520. The
    ``detect()`` heuristic returns ``"low"`` (never ``"medium"`` /
    ``"high"``) — see :mod:`.powerbuilder` rationale.
    """

    name: Final[str] = "as400_rpg"

    def detect(self, code_root: Path) -> Confidence:
        """Probe ``code_root`` for AS/400 RPG file extensions.

        Returns ``"low"`` always; presence of ``*.rpg`` / ``*.rpgle`` /
        ``*.sqlrpgle`` / ``*.cl`` / ``*.clle`` is still probed so
        future telemetry can distinguish "RPG-shaped tree, parser
        not shipped" from "no RPG signal at all".

        Args:
            code_root: Repository root the auto-detect path is
                inspecting.

        Returns:
            ``"low"`` always.
        """
        for ext in _AS400_RPG_EXTENSIONS:
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
            "M-EPIC-3 as400_rpg flavor not yet implemented — "
            f"tracked under parent EPIC {_PARENT_EPIC} (sub-issue {_SUB_ISSUE}; "
            "deferred scope: program/module inventory (RPG/RPGLE/SQLRPGLE/CL/CLLE), "
            "/COPY-/INCLUDE inventory, file-spec / F-spec inventory, "
            "DDS source extraction, CL command call graph, "
            "embedded SQL extraction, subfile/message subfile patterns). See "
            "primitives/_shared/discovery/flavor/as400_rpg.py "
            "module docstring for the full deferred-scope contract."
        )

    def recommended_section_branches(self) -> list[str]:
        """Return ``["as400_rpg"]`` — the flavor's section branch."""
        return ["as400_rpg"]


FLAVOR_PACK: Final[_As400RpgFlavorPack] = _As400RpgFlavorPack()


__all__ = ["FLAVOR_PACK"]
