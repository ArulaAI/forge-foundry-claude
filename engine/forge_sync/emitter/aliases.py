"""Deprecation-alias skills — synthetic forwarding shims for renamed slash commands.

When a primitive's slash command is renamed, the OLD command name must keep
resolving for one minor version so consumers who have not re-synced (ClearFlow,
petclinic, …) do not break mid-transition. There is no *primitive directory*
backing the old name any more — the canonical source moved to the new name — so
these aliases are emitted as **synthetic skills** that are not discovered by the
per-primitive emission loop.

Each alias emits a normal per-harness skill whose body says, plainly, that the
command was renamed and forwards the reader to the new ``/mod-…`` command. The
alias is intentionally thin (no delegation, no scripts) and carries no
``delegates-to-cli`` marker — it exists only to be *found* by an agent that
loads the old name and to point it at the new one.

REMOVAL POLICY: these aliases cover exactly ONE minor version. They are removed
in the NEXT major release (see the app-modernization design spec §7 category 6,
docs/superpowers/specs/2026-07-09-app-modernization-design.md). Delete the
corresponding entry from :data:`DEPRECATED_SKILL_ALIASES` at that time; the
emission wiring drops the alias automatically.

Introduced in #1326/#1327 (SP5 of epic #1311) for the DB-chain rename:
``/mod-legacy-modernization`` → ``/mod-sql-modernization`` and
``/mod-modernization-strategist`` → ``/mod-sql-strategist``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from forge_sync.emitter.models import CanonicalSkill
from forge_sync.emitter.skills import emit_skill

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True)
class SkillAlias:
    """One deprecated → new slash-command forwarding shim.

    ``old_name`` / ``new_name`` are bare skill names (no leading ``/``); they are
    validated by the same ``_SAFE_NAME_RE`` guard the loader applies, via
    :func:`emit_skill` → ``skill_dest_dir``, and rendered as ``/<name>`` slash
    commands in the emitted body.
    """

    old_name: str
    new_name: str


#: The live set of deprecation aliases. Remove entries here at the next MAJOR
#: release to drop the forwarding shims (see module docstring — one-minor-version
#: policy). Empty this list and the emitter stops emitting any alias skills.
DEPRECATED_SKILL_ALIASES: tuple[SkillAlias, ...] = (
    # DB-chain rename (#1326/#1327, SP5 of epic #1311). Added: this minor.
    # Remove: next major.
    SkillAlias(old_name="mod-legacy-modernization", new_name="mod-sql-modernization"),
    SkillAlias(old_name="mod-modernization-strategist", new_name="mod-sql-strategist"),
)


def _alias_skill(alias: SkillAlias) -> CanonicalSkill:
    """Build the synthetic :class:`CanonicalSkill` for one forwarding shim."""
    description = (
        f"DEPRECATED alias — renamed to /{alias.new_name}. Forwarding shim kept for "
        "one minor version; removed next major. Invoke /"
        f"{alias.new_name} instead."
    )
    body = (
        f"# /{alias.old_name} (renamed)\n"
        "\n"
        f"**This command was renamed to `/{alias.new_name}`.**\n"
        "\n"
        f"`/{alias.old_name}` is a deprecated forwarding alias. Run "
        f"`/{alias.new_name}` instead — it is the same primitive under its new, "
        "unambiguous name.\n"
        "\n"
        "## When invoked\n"
        "\n"
        f"1. Do NOT proceed under the old name. Re-invoke `/{alias.new_name}` "
        "with the same arguments.\n"
        f"2. If this alias was reached from an emitted CI workflow or a stale "
        f"consumer manifest, update the reference to `/{alias.new_name}` and "
        "re-sync.\n"
        f"3. Report to the user that `/{alias.old_name}` is deprecated and "
        f"`/{alias.new_name}` is the current command.\n"
        "\n"
        "## Output\n"
        "\n"
        f"None — this alias only forwards to `/{alias.new_name}`. It is removed in "
        "the next major release.\n"
    )
    return CanonicalSkill(
        name=alias.old_name,
        description=description,
        body=body,
        references={},
        scripts={},
    )


def emit_alias_skills(
    harness: str, *, aliases: Iterable[SkillAlias] | None = None
) -> dict[str, str]:
    """Emit every deprecation-alias skill for *harness*.

    Returns ``{project-relative path: content}`` — the same shape as
    :func:`forge_sync.emitter.skills.emit_skill`, so the propagator writes it
    through the identical ``write_emitted`` sink (path-traversal-guarded, SEC-10).

    *aliases* defaults to :data:`DEPRECATED_SKILL_ALIASES`; an explicit iterable
    is accepted for testing. The alias body carries no tool intent, so
    ``emit_skill`` is called with an empty ``tools`` tuple (no ``allowed-tools``
    frontmatter is emitted).
    """
    entries = DEPRECATED_SKILL_ALIASES if aliases is None else tuple(aliases)
    out: dict[str, str] = {}
    for alias in entries:
        out.update(emit_skill(_alias_skill(alias), harness, tools=()))
    return out
