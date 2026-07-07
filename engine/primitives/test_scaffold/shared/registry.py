"""The ``register_scaffold`` extension seam + the containment-guarded write sink (#1031).

A *layer renderer* produces the files for one (stack, layer) pair - e.g. the
Playwright UI-e2e renderer for the ``nodejs`` stack. Built-in renderers
self-register; a consumer ships a renderer for a new stack/framework (Go, .NET,
...) by calling :func:`register_scaffold` from their own import path, mirroring
:func:`primitives._sdet.stacks.register_stack`. This is how "all stacks" is
delivered as a tested seam rather than an unbounded set of hand-written
templates.

Every file a renderer emits flows through :func:`write_scaffold`, the single
**idempotent, containment-guarded** sink: it refuses to overwrite an existing
file (augment-or-skip, never clobber) and rejects any path that escapes the
output root (absolute, ``..``-bearing, or resolving outside the root - the
SEC-10 ``..``-before-``.resolve()`` discipline).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

#: The six canonical cone layers (design §4). ``unit`` delegates to S3; the rest
#: are scaffolded from per-stack template renderers.
LAYERS: tuple[str, ...] = (
    "unit",
    "integration",
    "container",
    "contract",
    "api-e2e",
    "ui-e2e",
)

#: Opt-in data-test layers (DT-3..DT-6, #1138-#1141) — test the *data* / data-system,
#: not the app code. They are **not** part of the app-code cone (no cone emphasis, not
#: in the default plan); a ``--data-tests`` flag (or naming them in ``--layers``) opts
#: in. They reuse the DT-2 ``_sdet`` data-backend registry + the container
#: ``db_container`` fixture, so ``--data-tests`` implies ``container``.
DATA_TEST_LAYERS: tuple[str, ...] = (
    "data-structural",
    "data-quality",
    "data-seed",
    "data-parity",
)

#: Every registerable layer (cone + data-test). ``register_scaffold`` validates against
#: this; the cone machinery (``cone._PROFILES`` / ``layer_emphasis``) only knows
#: :data:`LAYERS`, so a data-test layer defaults to emphasis 1.
ALL_LAYERS: tuple[str, ...] = LAYERS + DATA_TEST_LAYERS


@dataclass(frozen=True)
class ScaffoldFile:
    """One file a renderer wants written, relative to the output root.

    *path* is POSIX-relative to the project/output root (never absolute, never
    ``..``-bearing - enforced by :func:`write_scaffold`). *executable* marks a
    script that should get the executable bit.
    """

    path: str
    content: str
    executable: bool = False


@dataclass(frozen=True)
class ScaffoldResult:
    """What a renderer produced for one (stack, layer): files + a human note."""

    files: tuple[ScaffoldFile, ...] = ()
    note: str = ""


class ScaffoldRenderer(Protocol):
    """Render the scaffold for one (stack, layer).

    Implementations are pure - they return :class:`ScaffoldFile` descriptors and
    do not touch the filesystem (the sink owns I/O + idempotency). *context*
    carries the resolved stack profile, the cone emphasis for this layer, and the
    model-supplied fill-ins (service base-URL, container image, ...).
    """

    def __call__(self, context: ScaffoldContext) -> ScaffoldResult: ...


@dataclass(frozen=True)
class ScaffoldContext:
    """The resolved inputs a renderer needs (design §5).

    Kept deliberately small + serialisable: the stack name, the per-layer
    emphasis (how many samples to emit, from the cone engine #1032), and the
    model fill-ins. Renderers must not reach outside this for project state.
    """

    stack: str
    layer: str
    project_root: Path
    emphasis: int = 1
    fillins: dict[str, str] = field(default_factory=dict)
    #: The S1 ``test-architecture.md`` tooling-pick text for THIS layer, parsed from
    #: the architecture (#1064). Empty when no S1 input named this layer. Renderers
    #: surface it (e.g. as a header note) so the engineer sees what S1 recommended;
    #: the deterministic template choice doesn't otherwise depend on it.
    tooling: str = ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: ``(stack, layer) -> renderer``. Built-ins self-register on import of their
#: module; the layer-renderer packages call ``register_scaffold`` at import time.
_REGISTRY: dict[tuple[str, str], ScaffoldRenderer] = {}


def register_scaffold(stack: str, layer: str, renderer: ScaffoldRenderer) -> None:
    """Register *renderer* for ``(stack, layer)``.

    The most recent registration wins so a consumer plugin can override a
    built-in. *layer* must be one of :data:`ALL_LAYERS` (the cone layers plus the
    opt-in data-test layers); an unknown layer is a programming error and raises
    ``ValueError`` (renderers are registered by foundry code or a deliberate
    plugin, never from untrusted input).
    """
    if layer not in ALL_LAYERS:
        msg = f"unknown layer {layer!r}; must be one of {ALL_LAYERS}"
        raise ValueError(msg)
    _REGISTRY[(stack, layer)] = renderer


def get_renderer(stack: str, layer: str) -> ScaffoldRenderer | None:
    """Return the registered renderer for ``(stack, layer)``, or ``None``."""
    return _REGISTRY.get((stack, layer))


def registered_pairs() -> list[tuple[str, str]]:
    """Return all registered ``(stack, layer)`` pairs (sorted, for tests/reports)."""
    return sorted(_REGISTRY)


# ---------------------------------------------------------------------------
# The containment-guarded, idempotent write sink
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteOutcome:
    """The result of writing one scaffold file."""

    path: str
    #: ``"written"`` (new file created) or ``"skipped"`` (already existed -
    #: idempotent, never clobbered).
    status: str


def _safe_dest(root: Path, relative: str, root_resolved: Path | None = None) -> Path:
    """Resolve *relative* under *root*, rejecting any escape (SEC-10 discipline).

    Rejects an absolute path, a ``..`` segment, or a NUL byte on the **raw** value
    *before* ``.resolve()`` (so a symlink can't smuggle an escape through resolution
    and a NUL gets the clean ``ValueError``, not an ``OSError`` from ``.resolve()``),
    then confirms the resolved path stays under the resolved root. *root_resolved*
    may be passed by a batch caller so the root is resolved once, not per file
    (#1068); when omitted it is resolved here.
    """
    if not relative or relative != relative.strip():
        msg = f"invalid scaffold path {relative!r}: empty or untrimmed"
        raise ValueError(msg)
    pure = relative.replace("\\", "/")
    if "\x00" in pure:
        msg = f"invalid scaffold path {relative!r}: NUL byte rejected"
        raise ValueError(msg)
    if pure.startswith("/") or pure.startswith("~"):
        msg = f"invalid scaffold path {relative!r}: must be relative to the output root"
        raise ValueError(msg)
    if ".." in pure.split("/"):
        msg = f"invalid scaffold path {relative!r}: '..' segments are rejected"
        raise ValueError(msg)
    dest = (root / pure).resolve()
    resolved_root = root_resolved if root_resolved is not None else root.resolve()
    if not dest.is_relative_to(resolved_root):
        msg = f"invalid scaffold path {relative!r}: escapes the output root {root!s}"
        raise ValueError(msg)
    return dest


def write_scaffold(root: Path, files: Iterable[ScaffoldFile]) -> list[WriteOutcome]:
    """Idempotently write *files* under *root*; never clobber, never escape.

    Each file is path-validated (:func:`_safe_dest`). An existing destination is
    **skipped** (augment-or-skip - the primitive never overwrites a hand-edited
    test setup). Parent dirs are created. Returns one :class:`WriteOutcome` per
    file in input order.

    All paths are validated up-front, before any file is written, so a single
    invalid path in the batch raises before touching disk - a layer's write is
    all-or-nothing and can't leave a within-layer partial scaffold (#1057).
    """
    # Validate every destination first; an escaping path raises here, before any
    # write, so the batch is atomic w.r.t. on-disk effects. The root is resolved
    # once and reused per file (#1068), keeping the ..-before-.resolve() discipline
    # on each raw path.
    root_resolved = root.resolve()
    resolved = [(f, _safe_dest(root, f.path, root_resolved)) for f in files]
    outcomes: list[WriteOutcome] = []
    for f, dest in resolved:
        if dest.exists():
            outcomes.append(WriteOutcome(path=f.path, status="skipped"))
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f.content, encoding="utf-8")
        if f.executable:
            dest.chmod(0o755)
        outcomes.append(WriteOutcome(path=f.path, status="written"))
    return outcomes
