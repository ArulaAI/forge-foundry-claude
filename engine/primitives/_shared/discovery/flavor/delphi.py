"""M-EPIC-3 polyglot flavor stub: Delphi / Object Pascal (M5-FLAVOR-STUBS, #528).

This module reserves the public surface for a future v1.1+ Delphi
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
:mod:`.ims_db` (M5-FLAVOR-2 / #447) with one critical difference:
M5-FLAVOR-STUBS stubs implement the **full**
:class:`~primitives._shared.discovery.flavor.FlavorParser`
Protocol surface — including a real (cheap) ``detect()`` that
probes the source tree for Delphi-shaped file extensions. Only
``parse()`` raises ``NotImplementedError``.

## Deferred scope (what the future parser will cover)

The Delphi flavor would parse **Embarcadero / Borland Delphi
Object Pascal application sources** — a strongly-typed RAD platform
widely deployed in BFSI / industrial / point-of-sale systems from
the mid-1990s onwards. The following extractions are deferred to a
future v1.1+ sub-issue under EPIC #520:

* **Project / package inventory.** ``*.dpr`` (project source) and
  ``*.dpk`` (package descriptor) declare the application's
  deployable graph. Each project produces one ``ExecutionUnit``
  record; each package produces a ``SharedComponent`` record.
* **Form / DFM hierarchy.** ``*.dfm`` (binary or text form
  resources) carry the visual layout + event-handler binding. Form
  inheritance maps to §7 (Screen Navigation & UI Layer); the
  event-handler → procedure binding feeds §5 (Call Chain Maps).
* **Unit / uses-clause graph.** Pascal ``unit`` declarations with
  ``uses`` clauses produce ``CallEdge`` records — the dependency
  graph powering §5 (Call Chain Maps) and §14 (Technical Debt
  Inventory). Circular uses-clauses are a classic Delphi tech-debt
  signal.
* **Database component extraction.** ``TDataSet`` /
  ``TADOConnection`` / ``TIBQuery`` / ``TFDQuery`` components
  carry embedded SQL — parsed into ``DataStore`` records for §6
  (Data Architecture). The connection-string-on-component pattern
  is also a §10 (Security Architecture) finding source.
* **Component-based architecture metadata.** Custom components
  (registered via ``RegisterComponents`` in package initialization)
  produce ``SharedComponent`` records that span the
  presentation/business/data layers of §4 (Three-Layer
  Architecture).
* **Build automation.** ``*.dproj`` (MSBuild-style project) and
  ``*.cfg`` (compiler config) drive §12 (Build / Automation
  Layer) — Delphi's build chain is MSBuild-based since Delphi 2007
  but historically used IDE-driven builds that don't reproduce
  outside the IDE.

## Why ship the stub now

Same rationale as :mod:`.powerbuilder`: BFSI modernization
assessments routinely encounter Delphi codebases, and the
extension surface should advertise that path. The stub's
``detect()`` flags the presence of Delphi files at ``"low"``
confidence; a real-flavored sibling (``cics_cobol`` / ``python`` /
``java_ee``) wins auto-detect when present.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import Confidence


#: File-extension probe set. ``*.pas`` is the Pascal source unit
#: extension; ``*.dpr`` is the project source; ``*.dfm`` is the form
#: resource. Any one of these in the tree flags Delphi presence.
_DELPHI_EXTENSIONS: Final[frozenset[str]] = frozenset({".pas", ".dpr", ".dfm"})

_PARENT_EPIC: Final[str] = "#520"
_SUB_ISSUE: Final[str] = "#528"


class _DelphiFlavorPack:
    """Stub :class:`FlavorParser` for the Delphi flavor.

    Implements the full Protocol surface; ``parse()`` raises
    :class:`NotImplementedError` pointing at parent EPIC #520. The
    ``detect()`` heuristic returns ``"low"`` (never ``"medium"`` /
    ``"high"``) — see :mod:`.powerbuilder` rationale.
    """

    name: Final[str] = "delphi"

    def detect(self, code_root: Path) -> Confidence:
        """Probe ``code_root`` for Delphi file extensions.

        Returns ``"low"`` always; presence of ``*.pas`` / ``*.dpr`` /
        ``*.dfm`` is still probed so future telemetry can distinguish
        "Delphi-shaped tree, parser not shipped" from "no Delphi
        signal at all".

        Args:
            code_root: Repository root the auto-detect path is
                inspecting.

        Returns:
            ``"low"`` always.
        """
        for ext in _DELPHI_EXTENSIONS:
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
            "M-EPIC-3 delphi flavor not yet implemented — "
            f"tracked under parent EPIC {_PARENT_EPIC} (sub-issue {_SUB_ISSUE}; "
            "deferred scope: project/package inventory, form/DFM hierarchy, "
            "unit/uses-clause graph, database component extraction, "
            "component-based architecture metadata, build automation). See "
            "primitives/_shared/discovery/flavor/delphi.py "
            "module docstring for the full deferred-scope contract."
        )

    def recommended_section_branches(self) -> list[str]:
        """Return ``["delphi"]`` — the flavor's section branch."""
        return ["delphi"]


FLAVOR_PACK: Final[_DelphiFlavorPack] = _DelphiFlavorPack()


__all__ = ["FLAVOR_PACK"]
