"""M-EPIC-3 polyglot flavor stub: PowerBuilder (M5-FLAVOR-STUBS, #528).

This module reserves the public surface for a future v1.1+ PowerBuilder
flavor pack of the M5 ``--platform-flavor`` dispatcher. Per the
M-EPIC-3 design spec
(``docs/superpowers/specs/2026-06-03-polyglot-discovery-design.md``
§"Auto-detection heuristic" + §"Implementation tickets" row #528), the
M-EPIC-3 reframing ships a *shaped* extension surface — five stub
flavors (``powerbuilder``, ``delphi``, ``vb6``, ``oracle_forms``,
``as400_rpg``) registered alongside the built-in three so
``--platform-flavor`` help output advertises the full polyglot
roadmap. The deferred parser work lands as separate sub-issues of
the parent EPIC **#520** (``[M-EPIC-3] Polyglot discovery
reframe``); each stub points at that tracker so future implementers
have a clear hand-off.

This pattern intentionally mirrors :mod:`.jcl_batch` and
:mod:`.ims_db` (M5-FLAVOR-2 / #447) with one critical difference:
M5-FLAVOR-STUBS stubs implement the **full**
:class:`~primitives._shared.discovery.flavor.FlavorParser`
Protocol surface — including a real (cheap) ``detect()`` that probes
the source tree for PowerBuilder-shaped file extensions. Only
``parse()`` raises ``NotImplementedError``. The rationale (per
design spec §"Open Question 1 — detect() vs. parse() in stubs"): an
operator running ``--platform-flavor auto`` on a PowerBuilder repo
gets a useful error message ("PowerBuilder detected; the
powerbuilder flavor pack is registered but its parser is not yet
implemented — see #520") instead of a confusing "no flavors
matched".

## Deferred scope (what the future parser will cover)

The PowerBuilder flavor would parse **Sybase / Appeon PowerBuilder
application sources** — a 4GL window-event-driven RAD platform
heavily deployed in BFSI front-office and back-office systems
through the 1990s-2010s. The following extractions are deferred to
a future v1.1+ sub-issue under EPIC #520:

* **PBL / PBT inventory.** ``*.pbl`` (PowerBuilder Library binary
  containers, holding compiled PowerScript objects) and ``*.pbt``
  (target / workspace) files declare the application's deployable
  unit graph. The parser produces ``ExecutionUnit`` records keyed
  on the library name with ``kind="powerbuilder_userobject"``
  (already provisioned in :data:`ExecutionUnitKind`).
* **Window / DataWindow hierarchy.** Window inheritance + embedded
  DataWindow objects map to the §4 three-layer architecture's
  presentation tier. Window-event scripts populate §5 (Call Chain
  Maps) — the PowerBuilder analogue of CICS LINK / XCTL edges.
* **Embedded SQL extraction.** DataWindow objects carry embedded
  SQL (the "Data Source" tab in the PB IDE serialises to text);
  parsing that SQL produces ``DataStore`` records for §6 (Data
  Architecture). This is the closest analogue PB has to v1's
  ``EXEC SQL`` extraction in CICS COBOL.
* **PowerScript call graph.** Per-window / per-userobject function
  calls (``of_*`` instance functions, global functions) produce
  ``CallEdge`` records — the inheritance tree is non-trivial and
  needs cross-PBL resolution.
* **Database-tier role mapping.** PB application logins to Sybase /
  Oracle / SQL Server (commonly via ``transaction`` objects with
  embedded credentials) drive §10 (Security Architecture) — the
  PB-side analogue of CICS RACF / SIGNON parsing.
* **PowerGen / PBORCA build artefact catalogue.** §12 (Build /
  Automation Layer) renders the build steps; PB historically uses
  the PowerGen orchestrator + PBORCA library for headless builds.

## Why ship the stub now

The shape of M-EPIC-3 is "polyglot discovery". Shipping a flavor
registry that advertises *only* ``cics_cobol`` + ``python`` +
``java_ee`` would understate the modernization surface — BFSI
modernization assessments routinely encounter PB / Delphi / VB6 /
Oracle Forms / AS400 RPG codebases, and an operator opening
``forge-foundry-sync --list-flavors`` should *see* those names in
the registry output so they know the path forward (file a sub-issue
of #520 with their corpus). The stub makes the extension surface
visible without committing to ship parsers for every named flavor
in the current EPIC.

The detect() heuristic stays at ``"low"`` confidence because the
parser isn't implemented — the auto-detect path (#525) will still
pick a real-flavored sibling (``cics_cobol`` / ``python`` /
``java_ee``) when one matches ``"medium"`` or ``"high"`` confidence.
The stub only "wins" auto-detect when nothing else matches, and at
that point the resulting ``NotImplementedError`` from ``parse()``
gives the operator a clean, traceable handoff to EPIC #520.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import Confidence


#: File-extension probe set. PowerBuilder's deployable artefact is the
#: ``*.pbl`` library; ``*.pbt`` is the target / workspace descriptor.
#: A repo carrying either is almost certainly PowerBuilder. The probe
#: is intentionally minimal — a real ``detect()`` would also sniff
#: ``*.pbg`` (project group), ``*.pbr`` (resource), but the two
#: extensions below are sufficient to flag "PB present".
_POWERBUILDER_EXTENSIONS: Final[frozenset[str]] = frozenset({".pbl", ".pbt"})

#: Tracking EPIC for the M-EPIC-3 polyglot reframe. Every stub cites
#: this so a future implementer reading the
#: ``NotImplementedError`` knows where the work tracker lives.
_PARENT_EPIC: Final[str] = "#520"

#: This stub's own sub-issue under :data:`_PARENT_EPIC`. Carried in
#: the error message so the trace surfaces both the EPIC and the
#: specific stub-tracking issue.
_SUB_ISSUE: Final[str] = "#528"


class _PowerBuilderFlavorPack:
    """Stub :class:`FlavorParser` for the PowerBuilder flavor.

    Implements the full Protocol surface but ``parse()`` raises
    :class:`NotImplementedError` pointing at parent EPIC #520. The
    ``detect()`` heuristic returns ``"low"`` (never ``"medium"`` /
    ``"high"``) because the parser isn't implemented — the auto-detect
    path (#525) will still pick a real-flavored sibling when one
    matches ``"medium"`` or higher. The stub "wins" auto-detect only
    when nothing else matches, and at that point the
    ``NotImplementedError`` from ``parse()`` is the right handoff
    (operator knows to file a sub-issue of #520).
    """

    #: Name carried for parity with other flavor packs that surface
    #: their own name (mirrors ``_BuiltinFlavorPack.name``).
    name: Final[str] = "powerbuilder"

    def detect(self, code_root: Path) -> Confidence:
        """Probe ``code_root`` for PowerBuilder file extensions.

        Returns ``"low"`` whenever any ``*.pbl`` or ``*.pbt`` file is
        present in the tree, else ``"low"``. Confidence is **always**
        ``"low"`` for stubs — see class docstring rationale. The
        presence-vs-absence distinction is preserved so future
        instrumentation (telemetry on which flavors were probed) can
        still tell "the tree carries PB-shaped files but the parser
        isn't shipped" from "the tree has nothing PB-shaped".

        Args:
            code_root: Repository root the auto-detect path is
                inspecting. The probe is non-recursive in spirit
                (``rglob`` is used for convenience) — even a single
                ``*.pbl`` anywhere in the tree counts.

        Returns:
            ``"low"`` always. The stub's confidence ceiling is by
            design: it cannot win against a real-flavored sibling
            that reports ``"medium"`` or ``"high"``.
        """
        for ext in _POWERBUILDER_EXTENSIONS:
            try:
                if next(code_root.rglob(f"*{ext}"), None) is not None:
                    return "low"
            except OSError:
                # Unreadable tree (permission error, broken symlink) —
                # treat as "no signal" and keep walking the other
                # extensions. A genuinely missing root would have
                # raised before this method was called.
                continue
        return "low"

    def parse(
        self,
        file_path: Path,
        content: str,
    ) -> dict[str, list[object]]:
        """Raise :class:`NotImplementedError` citing EPIC #520.

        Args:
            file_path: Source file path (placeholder for the future
                v1.1+ signature).
            content: Raw file content (placeholder for the future
                v1.1+ signature).

        Raises:
            NotImplementedError: Always, with a message pointing at
                parent EPIC #520 (M-EPIC-3 polyglot reframe) and
                sub-issue #528 (M5-FLAVOR-STUBS) so the trace surfaces
                both the EPIC and the specific stub-tracking issue.
        """
        del file_path, content  # placeholder for v1.1+ implementation
        raise NotImplementedError(
            "M-EPIC-3 powerbuilder flavor not yet implemented — "
            f"tracked under parent EPIC {_PARENT_EPIC} (sub-issue {_SUB_ISSUE}; "
            "deferred scope: PBL/PBT inventory, window/DataWindow hierarchy, "
            "embedded SQL extraction, PowerScript call graph, database-tier "
            "role mapping, PowerGen/PBORCA build artefact catalogue). See "
            "primitives/_shared/discovery/flavor/powerbuilder.py "
            "module docstring for the full deferred-scope contract."
        )

    def recommended_section_branches(self) -> list[str]:
        """Return ``["powerbuilder"]`` — the flavor's section branch.

        Phase C renderers consult this list when picking which §12
        Build / Automation Layer branch and per-flavor section
        branches to render. The single-element list signals "use the
        powerbuilder-specific branches where available; fall back to
        defaults elsewhere". Per the design spec §"Per-flavor
        adaptation of the 16 sections" table row for ``powerbuilder``,
        §4 / §5 / §6 / §7 / §11 / §12 / §15 all carry per-flavor
        adaptations.
        """
        return ["powerbuilder"]


#: Singleton instance the registry binds. Mirrors the FLEX-3
#: ``TargetRendererRegistry`` shape — registry holds already-built
#: instances so the registration path doesn't have to switch on
#: class-vs-instance.
FLAVOR_PACK: Final[_PowerBuilderFlavorPack] = _PowerBuilderFlavorPack()


__all__ = ["FLAVOR_PACK"]
