"""M-EPIC-3 polyglot flavor stub: Visual Basic 6 (M5-FLAVOR-STUBS, #528).

This module reserves the public surface for a future v1.1+ VB6
flavor pack of the M5 ``--platform-flavor`` dispatcher. Per the
M-EPIC-3 design spec
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

The VB6 flavor would parse **Microsoft Visual Basic 6
application sources** — the COM-era RAD platform whose runtime
shipped through Windows XP / 7 / 8.1 / 10 and remains a workhorse
in BFSI desk-side automation and SCADA control rooms. The
following extractions are deferred to a future v1.1+ sub-issue
under EPIC #520:

* **Project inventory.** ``*.vbp`` (project descriptor) and
  ``*.vbw`` (workspace) declare the application unit graph. Each
  project produces one ``ExecutionUnit`` record; referenced
  ``*.dll`` / ``*.ocx`` COM components produce ``SharedComponent``
  records.
* **Form / class / module catalog.** ``*.frm`` (form),
  ``*.cls`` (class module), and ``*.bas`` (standard module) files
  carry the application code. Forms map to §7 (Screen Navigation &
  UI Layer); classes + standard modules feed §3 (Execution Unit
  Inventory) and §5 (Call Chain Maps).
* **Event-handler binding.** ``Private Sub <Control>_<Event>``
  procedure names declare event handlers — the §5 (Call Chain Maps)
  primary edge source for VB6 applications.
* **ADO / DAO recordset extraction.** ``Recordset.Open`` /
  ``Connection.Execute`` carry embedded SQL — parsed into
  ``DataStore`` records for §6 (Data Architecture). Connection
  strings hardcoded into ``*.bas`` modules are a §10 (Security
  Architecture) finding source.
* **COM reference graph.** ``Object`` / ``Reference`` declarations
  in the ``*.vbp`` produce the COM-component dependency graph —
  the VB6 analogue of Java's classpath that §14 (Technical Debt
  Inventory) uses to flag end-of-life dependencies (e.g. ``MSADO15``,
  ``MSCOMCTL`` versions known to be 32-bit-only).
* **Late-binding ``CreateObject`` detection.** Dynamic COM
  instantiation via ``CreateObject("Excel.Application")`` etc. is
  a frequent VB6 pattern; surfacing these in §15 (Modernization
  Analysis) is critical because they break under strangler-fig
  rewrites that assume static call graphs.

## Why ship the stub now

Same rationale as :mod:`.powerbuilder`: BFSI modernization
assessments routinely encounter VB6 codebases (Microsoft ended
mainstream support in 2008, extended in 2018; the runtime ships
through Windows 10 LTSC 2025), and the extension surface should
advertise that path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import Confidence


#: File-extension probe set. ``*.vbp`` and ``*.vbw`` are the project
#: + workspace descriptors; ``*.frm`` (form) and ``*.cls`` (class
#: module) are the per-unit source files. Any one of these flags
#: VB6 presence.
_VB6_EXTENSIONS: Final[frozenset[str]] = frozenset({".vbp", ".vbw", ".frm", ".cls"})

_PARENT_EPIC: Final[str] = "#520"
_SUB_ISSUE: Final[str] = "#528"


class _VB6FlavorPack:
    """Stub :class:`FlavorParser` for the VB6 flavor.

    Implements the full Protocol surface; ``parse()`` raises
    :class:`NotImplementedError` pointing at parent EPIC #520. The
    ``detect()`` heuristic returns ``"low"`` (never ``"medium"`` /
    ``"high"``) — see :mod:`.powerbuilder` rationale.
    """

    name: Final[str] = "vb6"

    def detect(self, code_root: Path) -> Confidence:
        """Probe ``code_root`` for VB6 file extensions.

        Returns ``"low"`` always; presence of ``*.vbp`` / ``*.vbw`` /
        ``*.frm`` / ``*.cls`` is still probed so future telemetry can
        distinguish "VB6-shaped tree, parser not shipped" from "no
        VB6 signal at all".

        Args:
            code_root: Repository root the auto-detect path is
                inspecting.

        Returns:
            ``"low"`` always.
        """
        for ext in _VB6_EXTENSIONS:
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
            "M-EPIC-3 vb6 flavor not yet implemented — "
            f"tracked under parent EPIC {_PARENT_EPIC} (sub-issue {_SUB_ISSUE}; "
            "deferred scope: project inventory, form/class/module catalog, "
            "event-handler binding, ADO/DAO recordset extraction, "
            "COM reference graph, late-binding CreateObject detection). See "
            "primitives/_shared/discovery/flavor/vb6.py "
            "module docstring for the full deferred-scope contract."
        )

    def recommended_section_branches(self) -> list[str]:
        """Return ``["vb6"]`` — the flavor's section branch."""
        return ["vb6"]


FLAVOR_PACK: Final[_VB6FlavorPack] = _VB6FlavorPack()


__all__ = ["FLAVOR_PACK"]
