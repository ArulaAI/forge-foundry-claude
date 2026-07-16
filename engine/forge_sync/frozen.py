"""Frozen-binary (PyInstaller) detection + embedded-primitives location.

When ``forge-foundry-sync`` is packaged as a PyInstaller one-file binary (the
air-gapped distribution, #1419), the primitive library is **bundled inside the
binary** rather than git-cloned at runtime. PyInstaller sets ``sys.frozen`` and
extracts the bundled ``datas`` to a temp root exposed as ``sys._MEIPASS``.

The T1 PyInstaller spec roots the embedded ``primitives/`` tree at the bundle
root, so the embedded library lives at ``sys._MEIPASS/primitives`` — mirroring
the repo layout. This module is the single source of truth for that embed
location, so T1 (the spec) and T2 (the resolver) agree on exactly one path.

The embedded tree is read-only and vetted at build time — there is no
attacker-controlled remote in this path, which is the whole point of the
air-gapped build. See ``docs/superpowers/specs/2026-07-10-airgap-binary-packaging-design.md``.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running as a PyInstaller-frozen binary.

    PyInstaller sets ``sys.frozen = True`` on the interpreter it embeds. A normal
    pip-installed / source-checkout run leaves the attribute unset, so this
    returns ``False`` there.
    """
    return bool(getattr(sys, "frozen", False))


def frozen_primitives_root() -> Path | None:
    """Location of the embedded ``primitives/`` tree, or ``None`` when not frozen.

    Returns ``Path(sys._MEIPASS) / "primitives"`` when running frozen and
    ``sys._MEIPASS`` is set (the PyInstaller extraction root). This is the
    embed location the T1 ``.spec`` must materialize: the full non-``.py``
    payload plus the ``.py`` submodules, rooted at ``primitives/`` under the
    bundle root, so the emitter sees a complete library.

    Returns ``None`` when not frozen (the pip/source path) or when — defensively
    — ``sys._MEIPASS`` is somehow absent, so callers fall through to the
    git-clone default unchanged.
    """
    if not is_frozen():
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is None:  # pragma: no cover — frozen without _MEIPASS is pathological
        return None
    return Path(meipass) / "primitives"
