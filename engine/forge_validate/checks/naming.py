"""Spec B naming convention checks.

Vendored from forge-prowess-primitives@v0.4.0 — see CHANGELOG-VENDOR.md for divergence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class NamingViolation(ValueError):
    """A repo slug, sim slug, or display name violates the naming spec."""


VALID_TRACKS: frozenset[str] = frozenset(
    [
        "foundations",
        "engineering",
        "analyst",
        "data",
        "sse",
        "cgse",
        "capstone",
        "meta",
    ]
)

VALID_STACKS: frozenset[str] = frozenset(
    ["java", "angular", "python", "node", "typescript", "dotnet", "kotlin", "rust", "go"]
)

MAX_SLUG_LENGTH = 60
MAX_SIM_SLUG_SEGMENTS = 3
MIN_REPO_SLUG_PARTS = 2  # track + topic minimum
MIN_CGSE_PARTS = 4  # cgse + mod + N + topic minimum


@dataclass(frozen=True)
class ParsedRepoSlug:
    """Components of a forge-prowess repo slug per Spec B §3."""

    track: str
    topic: str
    stack: str | None


def parse_repo_slug(slug: str) -> ParsedRepoSlug:
    """Parse `forge-<track>-<topic>[-<stack>]` per Spec B §3.

    Raises NamingViolation on any rule violation.
    """
    # Lexical guards FIRST — checked before structural parse so case/casing
    # violations surface their specific message instead of "missing forge- prefix".
    if "_" in slug:
        raise NamingViolation(f"slug `{slug}`: snake_case forbidden — use kebab-case (Spec B §3.1)")
    if any(c.isupper() for c in slug):
        raise NamingViolation(
            f"slug `{slug}`: uppercase forbidden — use lowercase only (Spec B §3.1)"
        )
    if len(slug) > MAX_SLUG_LENGTH:
        raise NamingViolation(
            f"slug `{slug}` length {len(slug)} exceeds 60 character limit (Spec B §3.1)"
        )
    if not slug.startswith("forge-"):
        raise NamingViolation(f"slug `{slug}` is missing forge- prefix (Spec B §3.1)")

    body = slug[len("forge-") :]
    parts = body.split("-")
    if len(parts) < MIN_REPO_SLUG_PARTS:
        raise NamingViolation(
            f"slug `{slug}` missing topic — at least one segment after track required (Spec B §3.1)"
        )

    track = parts[0]
    if track not in VALID_TRACKS:
        if track == "arula":
            raise NamingViolation(
                f"slug `{slug}`: redundant `arula-` prefix — "
                "the GitHub org provides that (Spec B §3.1)"
            )
        raise NamingViolation(
            f"slug `{slug}`: unknown track `{track}` — "
            f"must be one of {sorted(VALID_TRACKS)} (Spec B §2)"
        )

    if "lab" in parts[1:]:
        raise NamingViolation(
            f"slug `{slug}`: redundant `-lab` suffix — every repo IS a lab (Spec B §3.1)"
        )

    # CGSE-specific: must start with `mod-N` reserved token + topic
    if track == "cgse":
        cgse_invalid = (
            len(parts) < MIN_CGSE_PARTS or parts[1] != "mod" or not re.fullmatch(r"\d+", parts[2])
        )
        if cgse_invalid:
            raise NamingViolation(
                f"slug `{slug}`: CGSE labs must use `mod-<N>-<topic>` "
                f"reserved token (Spec B §3.1) — missing topic"
            )

    # Foundations edge case: stack-as-topic
    if track == "foundations" and len(parts) == MIN_REPO_SLUG_PARTS and parts[1] in VALID_STACKS:
        return ParsedRepoSlug(track=track, topic=parts[1], stack=None)

    # Generic case: optional stack on tail
    if parts[-1] in VALID_STACKS:
        topic = "-".join(parts[1:-1])
        stack = parts[-1]
    else:
        topic = "-".join(parts[1:])
        stack = None

    if not topic:
        raise NamingViolation(f"slug `{slug}`: missing topic (Spec B §3.1)")

    return ParsedRepoSlug(track=track, topic=topic, stack=stack)


def validate_sim_slug(sim_slug: str) -> None:
    """Validate a sim_slug per Spec B §5.

    Raises NamingViolation on rule violation.
    """
    if not sim_slug:
        raise NamingViolation("empty sim_slug forbidden (Spec B §5.1)")
    if any(c.isupper() for c in sim_slug):
        raise NamingViolation(f"sim_slug `{sim_slug}`: lowercase only (Spec B §5.1)")
    if "_" in sim_slug:
        raise NamingViolation(
            f"sim_slug `{sim_slug}`: kebab-case only — no underscores (Spec B §5.1)"
        )
    segments = sim_slug.split("-")
    if len(segments) > MAX_SIM_SLUG_SEGMENTS:
        raise NamingViolation(
            f"sim_slug `{sim_slug}`: exceeds 3 segments — got {len(segments)} (Spec B §5.1)"
        )
