"""M5 Phase B per-flavor parser surface + M-EPIC-3 flavor-pack extension contract.

This package hosts the per-flavor regex / parser packs the M5 Phase B
(``shared/analyze.py``) dispatches over. The original v1 surface
(M5-FLAVOR-1 #446 + M5-FLAVOR-2 #447) shipped three built-in flavors
keyed on the closed :data:`MainframeFlavor` Literal:

* ``cics_cobol`` → :func:`cics_cobol.parse_cobol_source` — v1 regex pack
  covering ``EXEC CICS LINK/XCTL``, ``CALL``, ``COPY``,
  ``EXEC CICS READ/WRITE/REWRITE/DELETE`` (and the browse-cursor verbs
  ``READNEXT``, ``READPREV``, ``STARTBR``, ``ENDBR``, ``UNLOCK``),
  ``EXEC SQL`` (with embedded table-name extraction),
  ``EXEC CICS SEND MAP/RECEIVE MAP``,
  ``EXEC CICS SIGNON/VERIFY PASSWORD``,
  and ``EXEC CICS ABEND`` / ``HANDLE ABEND``.
* ``jcl_batch`` → :func:`jcl_batch.parse_jcl_batch` — **stub** that
  raises ``NotImplementedError`` pointing at parent EPIC #439 for the
  v1.1 deferred-scope work (JCL job inventory, step-level program
  calls, DD statement extraction). See :mod:`.jcl_batch` for the full
  deferred-scope contract.
* ``ims_db`` → :func:`ims_db.parse_ims_db` — **stub** that raises
  ``NotImplementedError`` pointing at parent EPIC #439 for the v1.1
  deferred-scope work (DBD inventory, segment hierarchy, PSB/PCB
  analysis). See :mod:`.ims_db` for the full deferred-scope contract.
* ``python`` → :class:`python.PythonFlavor` /
  :func:`python.parse_python_source` — M-EPIC-3 first non-mainframe
  built-in (M5-FLAVOR-PYTHON / #526). AST-based extractor that
  emits the abstract entity vocabulary (#522): ``ExecutionUnit`` /
  ``SharedComponent`` / ``EntryPoint`` (FastAPI / Flask / Django /
  Click decorators) / ``DataStore`` (SQLAlchemy / Django ORM models) /
  ``BatchUnit`` (Celery / APScheduler decorators). See :mod:`.python`
  for the full extraction matrix.

## M-EPIC-3 flavor-pack extension contract (M5-FLAVOR-REGISTRY-EXTENSION, #524)

M-EPIC-3 reframes M5 from mainframe-only to polyglot. Third-party
flavor packs install via the
``forge_foundry.playbook_discovery.flavors`` entry-point group and
register a :class:`FlavorParser` Protocol implementation that surfaces
three methods:

* ``detect(code_root)`` returns a :data:`Confidence` Literal used by
  the (future) auto-detect path (#525). A no-op stub is acceptable
  pre-#525; the Protocol just carries the signature.
* ``parse(file_path, content)`` returns a category-keyed
  ``dict[str, list]`` of extracted records. Keys may be the v1
  mainframe categories (``programs`` / ``copybooks`` / …) OR the
  M5-MODELS-V2 abstract types (``execution_units`` / ``shared_components`` /
  …) from #522 — flavor packs choose which vocabulary they emit and
  the analyzer treats both vocabularies uniformly.
* ``recommended_section_branches()`` returns a hint list the §12 +
  per-flavor section renderers consult when picking which branch to
  take.

### Two parallel registries

The legacy ``FLAVORS`` dispatch table is **preserved verbatim**: it
maps each flavor name to a *callable* with signature
``(path, content) → dict[str, list[object]]`` so Phase B's aggregator
(``shared/analyze.py``) can continue to call ``FLAVORS[flavor](source,
content)`` without changes. Built-ins keep their existing registration
path: they bind the bare ``parse_*`` function into ``FLAVORS``.

The new :data:`FLAVOR_PACKS` dict is a parallel registry keyed by the
same flavor name whose values are :class:`FlavorParser` Protocol
*instances*. Built-ins get wrapper instances (``_BuiltinFlavorPack``)
that delegate ``parse()`` to the bare function and ship sensible
defaults for ``detect()`` + ``recommended_section_branches()``.
Third-party packs from entry points slot in here directly, AND their
``.parse`` bound method is mirrored into ``FLAVORS`` so the same Phase
B dispatch path sees the new flavors with no extra branching.

### Built-in precedence

Built-in flavor names — ``cics_cobol``, ``jcl_batch``, ``ims_db`` —
are reserved. An entry-point or :func:`register_flavor` call that
tries to register one of these names is **rejected**: the built-in
stays in place and a stderr warning records the attempted override.
The rationale: built-ins ship under foundry's SemVer guarantee, and
silently letting a third party shadow them would let a misbehaving
plug-in mask an audit-relevant parser at runtime. Operators who
genuinely want to override a built-in should fork foundry, not ship a
plug-in.

### Entry-point group: ``forge_foundry.playbook_discovery.flavors``

External flavor packs declare in their own ``pyproject.toml``::

    [project.entry-points."forge_foundry.playbook_discovery.flavors"]
    python = "my_pack.python:Flavor"
    java_ee = "my_pack.java_ee:Flavor"

Each entry-point payload resolves to either a :class:`FlavorParser`
Protocol class (instantiated with no arguments) or an already-built
instance — both shapes are accepted, mirroring the FLEX-3
``TargetRendererRegistry`` convention. A malformed payload (one that
does not satisfy the Protocol at runtime) raises
:class:`InvalidFlavorError` so a typo in ``pyproject.toml`` surfaces
at import time rather than corrupting Phase B mid-walk.

### Reference points

* FLEX-1 / ``primitives._llm``: per-backend entry-point group
  precedent that this contract mirrors.
* FLEX-3 / the M4 ``sp_converter`` ``TargetRendererRegistry`` shape
  (built-ins + entry-point overlay, Protocol-validated payloads) this
  contract mirrors. That registry's Python was archived when M4 became
  skill-first (recover from the ``v1.1.0-pre-archival`` tag).
* FLEX-4 / ``forge_sync.propagator.register_harness``: the
  runtime-register helper pattern this contract mirrors.
* Design spec ``docs/superpowers/specs/2026-06-03-polyglot-discovery-design.md``
  §"The flavor-pack contract" is the canonical reference.
* New consumer guide: :doc:`/docs/legacy-platform-flavors`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from primitives._shared.discovery.flavor import (
    as400_rpg,
    cics_cobol,
    delphi,
    ims_db,
    java_ee,
    jcl_batch,
    oracle_forms,
    powerbuilder,
    python,
    vb6,
)
from primitives._shared.discovery.flavor.generic import GenericFlavor
from primitives._shared.discovery.flavor.java_ee import JavaEEFlavor

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import (
        Confidence,
        MainframeFlavor,
    )


# ---------------------------------------------------------------------------
# Constants — entry-point group + built-in name reservation
# ---------------------------------------------------------------------------

#: Entry-point group third-party flavor packs declare under in their own
#: ``pyproject.toml``. Mirrors the FLEX-1 entry-point precedent and
#: FLEX-3 (``forge_foundry.renderers.<primitive>``) — operators
#: don't have to learn a new namespace per primitive.
ENTRY_POINT_GROUP: Final[str] = "forge_foundry.playbook_discovery.flavors"

#: Names a third-party flavor pack **must not** register. Built-ins ship
#: under foundry's SemVer guarantee; silently letting a plug-in shadow
#: them would let a misbehaving plug-in mask an audit-relevant parser at
#: runtime. An attempted override is logged to stderr (see
#: :func:`_warn_collision`) and the built-in stays in place.
#:
#: The set covers the M5-FLAVOR-1 + M5-FLAVOR-2 built-in mainframe trio
#: (``cics_cobol`` / ``jcl_batch`` / ``ims_db``), the M5-FLAVOR-PYTHON
#: (#526) first non-mainframe built-in (``python``), AND the
#: M5-FLAVOR-STUBS (#528) polyglot stub quintet (``powerbuilder`` /
#: ``delphi`` / ``vb6`` / ``oracle_forms`` / ``as400_rpg``) AND the
#: first non-mainframe built-in flavor pack ``java_ee``
#: (M5-FLAVOR-JAVA-EE / #527). The stubs are reserved-name
#: *placeholders* — a third-party that wants to ship a real
#: PowerBuilder parser today should still file a sub-issue of
#: EPIC #520 rather than silently shadow the stub at registration time,
#: so the foundry-shipped roadmap stays the source of truth.
_BUILTIN_FLAVOR_NAMES: Final[frozenset[str]] = frozenset(
    {
        # M5-FLAVOR-1 + M5-FLAVOR-2 mainframe built-ins
        "cics_cobol",
        "jcl_batch",
        "ims_db",
        # M5-FLAVOR-PYTHON (#526) first non-mainframe built-in
        "python",
        # M5-FLAVOR-JAVA-EE (#527) second non-mainframe full built-in
        "java_ee",
        # The universal language-agnostic fallback flavor
        "generic",
        # M5-FLAVOR-STUBS (#528) polyglot stub quintet
        "powerbuilder",
        "delphi",
        "vb6",
        "oracle_forms",
        "as400_rpg",
    }
)


# ---------------------------------------------------------------------------
# Type aliases — legacy callable shape (kept for backwards compatibility)
# ---------------------------------------------------------------------------

#: Legacy callable shape every flavor's ``parse_*_source`` function
#: implements. Phase B's aggregator drives this signature directly via
#: ``FLAVORS[flavor](path, content)`` and that call path is preserved
#: verbatim across the M-EPIC-3 reframe so a built-in or third-party
#: flavor can populate :data:`FLAVORS` either by registering its bare
#: callable (built-ins) or by exposing a Protocol instance whose
#: ``.parse`` bound method is mirrored into :data:`FLAVORS` by the
#: registry plumbing (entry-point loaded packs + :func:`register_flavor`).
FlavorParserCallable = Callable[["Path", str], dict[str, list[object]]]


# ---------------------------------------------------------------------------
# FlavorParser Protocol — the M-EPIC-3 extension contract surface
# ---------------------------------------------------------------------------


@runtime_checkable
class FlavorParser(Protocol):
    """Protocol every M-EPIC-3 third-party flavor pack implements.

    Three required methods + an optional ``name`` attribute. Each is
    documented in detail in
    ``docs/superpowers/specs/2026-06-03-polyglot-discovery-design.md``
    §"The flavor-pack contract"; the docstrings below paraphrase the
    spec for in-source discoverability.

    A pack that does not satisfy this Protocol at runtime registration
    time raises :class:`InvalidFlavorError`. The
    :func:`runtime_checkable` decoration means an :func:`isinstance`
    check at registration time catches the most common authoring
    error (forgot a method, mistyped a signature).
    """

    def detect(self, code_root: Path) -> Confidence:
        """Return the pack's auto-detection confidence for ``code_root``.

        Implementations should be cheap (manifest sniff, top-level
        file glob). The (future) #525 auto-detect path calls
        :meth:`detect` on every registered pack and picks the
        highest-confidence match — a pack that does not apply should
        return ``"low"`` rather than raise.

        Pre-#525 this method is a no-op stub for built-in packs (they
        carry the signature so the Protocol contract holds, but the
        analyze.py dispatch path doesn't consult it yet).
        """
        ...

    def parse(
        self,
        file_path: Path,
        content: str,
    ) -> dict[str, list[object]]:
        """Extract records from one source file.

        Keys may be either the v1 mainframe category names
        (``programs`` / ``copybooks`` / ``cics_transactions`` /
        ``vsam_files`` / ``bms_maps`` / ``jcl_jobs``) OR the
        M5-MODELS-V2 abstract entity-type names from #522
        (``execution_units`` / ``shared_components`` / ``entry_points`` /
        ``data_stores`` / ``ui_elements`` / ``batch_units``). The
        analyzer treats both vocabularies uniformly; flavor packs are
        free to emit whichever maps most naturally to their source
        material.

        A file that contributes no records to a given category may
        either omit the key entirely or return an empty list — Phase
        B's aggregator treats absence as equivalent to empty.
        """
        ...

    def recommended_section_branches(self) -> list[str]:
        """Hint list of section-branch tokens for the renderers.

        Phase C renderers consult this list when picking which §12
        Build / Automation Layer branch and per-flavor section
        branches to render. Token grammar is defined by the design
        spec §"Per-flavor adaptation of the 16 sections" — at minimum
        the platform name (e.g. ``"python"``, ``"java_ee"``) plus any
        opt-in section overrides the pack ships.

        Returning an empty list means "use the default branches" —
        appropriate for a Protocol-only flavor that doesn't customize
        any rendering paths.
        """
        ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidFlavorError(TypeError):
    """Raised when an entry-point payload does not satisfy :class:`FlavorParser`.

    Carries the entry-point name so the operator can spot which plug-in
    to fix in their own ``pyproject.toml``. Fails registration rather
    than letting a malformed pack survive into Phase B dispatch — a
    pack that lacks one of the Protocol methods would otherwise crash
    with ``AttributeError`` deep inside the aggregator.
    """


# ---------------------------------------------------------------------------
# Built-in Protocol wrappers — give built-in callables a Protocol view
# ---------------------------------------------------------------------------


#: Optional ``detect()`` callable a built-in flavor module can ship for
#: the M5-FLAVOR-AUTO-DETECT (#525) auto-detect path. Mirrors
#: :class:`FlavorParser`'s ``detect`` signature so the
#: :class:`_BuiltinFlavorPack` adapter can route through it when the
#: built-in module ships one. Built-ins that ship without a detector
#: keep the historical ``"low"`` last-resort confidence.
_BuiltinDetector = Callable[["Path"], "Confidence"]


class _BuiltinFlavorPack:
    """Adapter that exposes a built-in bare-callable flavor as a Protocol.

    Built-in flavors (``cics_cobol`` / ``jcl_batch`` / ``ims_db``) ship
    as bare ``parse_*_source`` functions and stay that way in
    :data:`FLAVORS` (so the existing Phase B dispatch path is
    untouched). This wrapper exists so the parallel
    :data:`FLAVOR_PACKS` registry can carry a Protocol-shaped view of
    every flavor — built-in and third-party alike — without forcing
    callers to special-case which kind they're holding.

    The :meth:`detect` no-op stub returns ``"low"`` by default so the
    auto-detect path treats built-ins-without-a-detector as last-resort
    matches; built-ins that ship a real detector (e.g. ``cics_cobol``
    via :func:`cics_cobol.detect`) opt in via the optional ``detector``
    constructor argument.

    The :meth:`recommended_section_branches` returns an empty list —
    built-ins use the default renderer branches; per-flavor overrides
    are an M-EPIC-3 Phase C concern (#526 onwards).
    """

    def __init__(
        self,
        name: str,
        parser: FlavorParserCallable,
        detector: _BuiltinDetector | None = None,
    ) -> None:
        self.name = name
        self._parser = parser
        self._detector = detector

    def detect(self, code_root: Path) -> Confidence:
        """Return the per-built-in detector's confidence, or ``"low"``.

        Built-ins that ship a real ``detect()`` (M5-FLAVOR-AUTO-DETECT
        / #525 wired ``cics_cobol`` first) route through the optional
        ``detector`` constructor argument; stub built-ins keep the
        historical ``"low"`` last-resort confidence so the auto-detect
        path treats them as opt-out unless the consumer ships a real
        signal-extractor.
        """
        if self._detector is None:
            return "low"
        return self._detector(code_root)

    def parse(
        self,
        file_path: Path,
        content: str,
    ) -> dict[str, list[object]]:
        """Delegate to the built-in's bare ``parse_*`` function."""
        return self._parser(file_path, content)

    def recommended_section_branches(self) -> list[str]:
        """Return ``[]`` — built-ins use the default renderer branches."""
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _warn_collision(name: str, source: str) -> None:
    """Emit a stderr warning when a third-party tries to shadow a built-in.

    ``source`` distinguishes the registration path (``"entry-point"``
    vs ``"register_flavor"``) so the operator can find the offending
    declaration faster. Quiet under pytest's captured-stderr by
    default — pytest sees the line via ``capsys`` / ``capfd``.
    """
    sys.stderr.write(
        f"warn: forge_foundry.playbook_discovery.flavors: "
        f"third-party {source} attempted to register reserved "
        f"built-in flavor name {name!r}; built-in wins. "
        f"Reserved names: {sorted(_BUILTIN_FLAVOR_NAMES)}.\n"
    )
    sys.stderr.flush()


def _coerce_to_pack(payload: object, entry_point_name: str) -> FlavorParser:
    """Resolve an entry-point payload to a :class:`FlavorParser` instance.

    Accepts either a Protocol class (instantiated with no arguments,
    mirroring FLEX-3's renderer-class convention) or an already-built
    instance. Raises :class:`InvalidFlavorError` when the resulting
    instance does not satisfy the :class:`FlavorParser` Protocol — the
    runtime-checkable ``isinstance`` covers the common authoring error
    of forgetting a required method.
    """
    instance = payload() if isinstance(payload, type) else payload
    if not isinstance(instance, FlavorParser):
        raise InvalidFlavorError(
            f"entry-point {entry_point_name!r} in group {ENTRY_POINT_GROUP!r} "
            f"does not satisfy primitives._shared.discovery.flavor."
            f"FlavorParser Protocol; type={type(instance).__name__}. "
            f"Required methods: detect(code_root) -> Confidence, "
            f"parse(file_path, content) -> dict[str, list], "
            f"recommended_section_branches() -> list[str]."
        )
    return instance


# ---------------------------------------------------------------------------
# Registries — FLAVORS (callable dispatch) + FLAVOR_PACKS (Protocol view)
# ---------------------------------------------------------------------------

#: v1 dispatch registry. Keys are flavor names (the built-in three are
#: :data:`MainframeFlavor` Literal values; entry-point loaded flavors
#: add arbitrary lowercase ASCII + underscore names). Values are the
#: per-flavor parser callables — bare functions for built-ins, bound
#: ``FlavorParser.parse`` methods for entry-point loaded packs.
#:
#: Built-in registration is preserved verbatim from the M5-FLAVOR-1 +
#: M5-FLAVOR-2 (#446 / #447) wiring: ``FLAVORS["cics_cobol"]`` is the
#: same ``parse_cobol_source`` function it has always been, so Phase
#: B's aggregator (``shared/analyze.py``) and the existing unit tests
#: continue to work unchanged.
#:
#: This dict is no longer ``Final`` because :func:`register_flavor` and
#: the import-time entry-point loader mutate it; treat it as
#: registry-owned and only mutate via the public helpers.
_JAVA_EE_FLAVOR: Final[JavaEEFlavor] = JavaEEFlavor()

#: The universal language-agnostic fallback. Its ``detect()`` always returns
#: ``"low"`` so it never out-ranks a specific flavor, but it is always available
#: — ``recon.detect_platform_flavor`` resolves to ``"generic"`` whenever no
#: specific flavor returns high/medium confidence, so discovery produces a useful
#: inventory for ANY repo (any language/framework/platform) out of the box.
_GENERIC_FLAVOR: Final[GenericFlavor] = GenericFlavor()


FLAVORS: dict[MainframeFlavor | str, FlavorParserCallable] = {
    # Built-in mainframe trio (M5-FLAVOR-1 + M5-FLAVOR-2)
    "cics_cobol": cics_cobol.parse_cobol_source,
    "jcl_batch": jcl_batch.parse_jcl_batch,
    "ims_db": ims_db.parse_ims_db,
    # M5-FLAVOR-PYTHON (#526) — first non-mainframe built-in (real
    # parser, not a stub).
    "python": python.parse_python_source,
    # M5-FLAVOR-JAVA-EE (#527) — second non-mainframe full built-in,
    # ships a real Protocol implementation rather than a stub.
    "java_ee": _JAVA_EE_FLAVOR.parse,
    # M5-FLAVOR-STUBS (#528) polyglot stub quintet — each .parse raises
    # NotImplementedError pointing at EPIC #520.
    "powerbuilder": powerbuilder.FLAVOR_PACK.parse,
    "delphi": delphi.FLAVOR_PACK.parse,
    "vb6": vb6.FLAVOR_PACK.parse,
    "oracle_forms": oracle_forms.FLAVOR_PACK.parse,
    "as400_rpg": as400_rpg.FLAVOR_PACK.parse,
    # The universal language-agnostic fallback (always available; selected when
    # no specific flavor detects). Makes discovery work for ANY repo.
    "generic": _GENERIC_FLAVOR.parse,
}

#: Parallel registry holding the Protocol view of each registered
#: flavor. Built-ins get wrapped via :class:`_BuiltinFlavorPack` so
#: every entry has the same shape; third-party packs (entry-point
#: loaded or registered via :func:`register_flavor`) slot in directly.
#:
#: Consumers that need ``detect()`` or ``recommended_section_branches()``
#: (the auto-detect path at
#: :func:`primitives._shared.discovery.recon.detect_platform_flavor`
#: per M5-FLAVOR-AUTO-DETECT / #525; the future #526 onwards per-flavor
#: section renderers) read from here. The :data:`FLAVORS` dict is the
#: hot-path dispatch table; :data:`FLAVOR_PACKS` is the metadata
#: surface.
FLAVOR_PACKS: dict[str, FlavorParser] = {
    # Built-in mainframe trio (M5-FLAVOR-1 + M5-FLAVOR-2) — wrapped in
    # _BuiltinFlavorPack so the registry holds a Protocol view of every
    # built-in callable. cics_cobol ships a real detect() since
    # M5-FLAVOR-AUTO-DETECT (#525) so ``--platform-flavor auto`` can
    # pick it up against CICS-shaped codebases. jcl_batch / ims_db
    # keep the historical low-confidence stubs until their v1.1
    # parsers ship.
    "cics_cobol": _BuiltinFlavorPack(
        "cics_cobol",
        cics_cobol.parse_cobol_source,
        detector=cics_cobol.detect,
    ),
    "jcl_batch": _BuiltinFlavorPack("jcl_batch", jcl_batch.parse_jcl_batch),
    "ims_db": _BuiltinFlavorPack("ims_db", ims_db.parse_ims_db),
    # M5-FLAVOR-PYTHON (#526) — first non-mainframe built-in. Unlike the
    # mainframe three, the Python pack carries its own ``detect()`` and
    # ``recommended_section_branches()`` (returns ``["python"]``) so it
    # registers its real Protocol instance, NOT a ``_BuiltinFlavorPack``
    # wrapper. The wrapper's no-op ``detect()`` stub would mask the
    # high-confidence pyproject.toml signal the auto-detect path (#525)
    # needs to surface.
    "python": python.PythonFlavor(),
    # M5-FLAVOR-JAVA-EE (#527) — second non-mainframe full built-in.
    # Same Protocol-shaped registration discipline as ``python`` above:
    # ships its own ``JavaEEFlavor`` instance with real ``detect()`` +
    # ``recommended_section_branches()`` rather than going through
    # ``_BuiltinFlavorPack``.
    "java_ee": _JAVA_EE_FLAVOR,
    # M5-FLAVOR-STUBS (#528) polyglot stub quintet — already
    # Protocol-shaped (each module ships ``FLAVOR_PACK`` as a
    # ready-built instance), so the registry holds the instance
    # directly without the _BuiltinFlavorPack wrapper.
    "powerbuilder": powerbuilder.FLAVOR_PACK,
    "delphi": delphi.FLAVOR_PACK,
    "vb6": vb6.FLAVOR_PACK,
    "oracle_forms": oracle_forms.FLAVOR_PACK,
    "as400_rpg": as400_rpg.FLAVOR_PACK,
    # The universal language-agnostic fallback. Real Protocol instance (carries
    # its own ``detect()`` → always ``"low"``, and ``recommended_section_branches``
    # → ``["generic"]``).
    "generic": _GENERIC_FLAVOR,
}


# ---------------------------------------------------------------------------
# Public registration helpers
# ---------------------------------------------------------------------------


def register_flavor(name: str, parser: FlavorParser) -> None:
    """Register a third-party flavor pack programmatically.

    Mirrors :func:`forge_sync.propagator.register_harness` (FLEX-4
    runtime-register helper precedent). Packages distributed as
    installable wheels should declare an entry-point in their own
    ``pyproject.toml`` so discovery is transparent; this helper exists
    for tests and ad-hoc consumer setup.

    Built-in name collisions are rejected with a stderr warning — the
    built-in stays in place. See module docstring §"Built-in
    precedence" for the rationale.

    Raises:
        InvalidFlavorError: ``parser`` does not satisfy the
            :class:`FlavorParser` Protocol at runtime. The check is
            the same one the entry-point loader applies.
    """
    if name in _BUILTIN_FLAVOR_NAMES:
        _warn_collision(name, "register_flavor")
        return
    if not isinstance(parser, FlavorParser):
        raise InvalidFlavorError(
            f"register_flavor({name!r}, ...) — parser does not satisfy "
            f"primitives._shared.discovery.flavor.FlavorParser "
            f"Protocol; type={type(parser).__name__}. "
            f"Required methods: detect(code_root) -> Confidence, "
            f"parse(file_path, content) -> dict[str, list], "
            f"recommended_section_branches() -> list[str]."
        )
    FLAVOR_PACKS[name] = parser
    FLAVORS[name] = parser.parse


def discover_flavors() -> list[str]:
    """Names of every registered flavor — built-ins + entry-point + runtime.

    Sorted for deterministic CLI ``--list-flavors`` output. Mirrors
    :func:`primitives._llm.discover_backends` and
    :func:`forge_sync.propagator.discover_harnesses`.
    """
    return sorted(FLAVOR_PACKS.keys())


# ---------------------------------------------------------------------------
# Import-time entry-point loading
# ---------------------------------------------------------------------------


def _load_entry_point_flavors() -> None:
    """Walk the entry-point group and register every discovered flavor.

    Built-in collisions are skipped with a stderr warning (see
    :func:`_warn_collision`). Malformed payloads raise
    :class:`InvalidFlavorError` — a typo in a plug-in's
    ``pyproject.toml`` is a configuration error the consumer must fix
    rather than something to silently drop.

    The walk is wrapped in ``FORGE_FLAVOR_LOAD_DISABLE`` for tests
    that need to exercise the empty-registry path without
    monkey-patching ``importlib.metadata`` — set the env var to ``1``
    and the import-time discovery is a no-op (built-ins still
    register; only entry-point loading is skipped).
    """
    if os.environ.get("FORGE_FLAVOR_LOAD_DISABLE") == "1":
        return
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name in _BUILTIN_FLAVOR_NAMES:
            _warn_collision(ep.name, "entry-point")
            continue
        if ep.name in FLAVOR_PACKS:
            # Second declaration of the same third-party name — first
            # one wins. Sorted EP iteration would tie-break
            # alphabetically; we keep the import-order tie-break and
            # warn so the operator notices the duplicate.
            sys.stderr.write(
                f"warn: forge_foundry.playbook_discovery.flavors: "
                f"duplicate entry-point name {ep.name!r}; first declaration wins.\n"
            )
            sys.stderr.flush()
            continue
        payload = ep.load()
        instance = _coerce_to_pack(payload, ep.name)
        FLAVOR_PACKS[ep.name] = instance
        FLAVORS[ep.name] = instance.parse


_load_entry_point_flavors()

# ---------------------------------------------------------------------------
# S61 web flavors — added to FLAVOR_PACKS only (not FLAVORS, which is the
# mainframe-closed registry guarded by test_shared_discovery_flavor_cics_cobol).
# FLAVOR_PACKS is the open extension point used by functional_map/recon.py.
# ---------------------------------------------------------------------------

from primitives._shared.discovery.flavor.web_angular import WebAngularFlavor  # noqa: E402
from primitives._shared.discovery.flavor.web_django import WebDjangoFlavor  # noqa: E402
from primitives._shared.discovery.flavor.web_express_node import (  # noqa: E402
    WebExpressNodeFlavor,
)
from primitives._shared.discovery.flavor.web_hybrid_jsp_spa import (  # noqa: E402
    WebHybridJspSpaFlavor,
)
from primitives._shared.discovery.flavor.web_jsf import WebJsfFlavor  # noqa: E402
from primitives._shared.discovery.flavor.web_rails import WebRailsFlavor  # noqa: E402
from primitives._shared.discovery.flavor.web_react_next import WebReactNextFlavor  # noqa: E402
from primitives._shared.discovery.flavor.web_spring import WebSpringFlavor  # noqa: E402
from primitives._shared.discovery.flavor.web_struts import WebStrutsFlavor  # noqa: E402
from primitives._shared.discovery.flavor.web_vue import WebVueFlavor  # noqa: E402

# The three hybrid/SPA packs' detect() returns 'none' (a rank-0 "definitely not me"
# signal, weaker than Confidence's 'low' — recon maps it via _CONFIDENCE_RANK.get(.,0)),
# so their detect() is annotated the wider ``str``, which the FlavorParser Protocol
# (detect -> Confidence) does not structurally accept. The runtime contract is honored
# (the values are still 'high'/'none'); the type: ignore scopes the Protocol-vs-str gap.
FLAVOR_PACKS.setdefault("web_hybrid_jsp_spa", WebHybridJspSpaFlavor())  # type: ignore[arg-type]
FLAVOR_PACKS.setdefault("web_express_node", WebExpressNodeFlavor())
FLAVOR_PACKS.setdefault("web_react_next", WebReactNextFlavor())
FLAVOR_PACKS.setdefault("web_angular", WebAngularFlavor())  # type: ignore[arg-type]
FLAVOR_PACKS.setdefault("web_vue", WebVueFlavor())  # type: ignore[arg-type]
FLAVOR_PACKS.setdefault("web_django", WebDjangoFlavor())
FLAVOR_PACKS.setdefault("web_struts", WebStrutsFlavor())
FLAVOR_PACKS.setdefault("web_jsf", WebJsfFlavor())
FLAVOR_PACKS.setdefault("web_spring", WebSpringFlavor())
FLAVOR_PACKS.setdefault("web_rails", WebRailsFlavor())


__all__ = [
    "ENTRY_POINT_GROUP",
    "FLAVORS",
    "FLAVOR_PACKS",
    "FlavorParser",
    "FlavorParserCallable",
    "InvalidFlavorError",
    "JavaEEFlavor",
    "as400_rpg",
    "cics_cobol",
    "delphi",
    "discover_flavors",
    "ims_db",
    "java_ee",
    "jcl_batch",
    "oracle_forms",
    "powerbuilder",
    "python",
    "register_flavor",
    "vb6",
]
