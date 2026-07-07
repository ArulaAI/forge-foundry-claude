"""M6 ``/mod-synth-logs`` — log catalog registry and flavor dispatch.

A **log catalog** is the platform-specific content layer for M6's four-phase
pipeline (Phase A ingest → B plan → C render → D validate).  The pipeline
itself is platform-agnostic; the catalog supplies:

- A closed set of :data:`LogFormat` Literal values + canonical file names.
- A closed set of :data:`IncidentEventKind` Literal values.
- Per-format Jinja2 templates under ``shared/templates/``.

v1 ships exactly **one** registered catalog: ``cics_cobol`` (mainframe
CICS / VSAM / JCL / Enterprise Server).  Additional catalogs (web access
logs, microservice traces, k8s pod events, Kafka topic logs) land as T2
work via the same flavor-pack registry M5 uses — see SPEC.md §"Extension
surface".

This module exposes:

:data:`REGISTERED_CATALOGS`
    Frozenset of platform flavor names that have a registered log catalog.
    Phase A catalog dispatch checks the resolved flavor against this set
    before proceeding to Phase B — an unsupported flavor exits 6 with a
    clear diagnostic rather than crashing at Phase B or emitting
    mainframe-shaped logs for a non-mainframe system.

:func:`detect_flavor_from_playbook`
    Scan a PLAYBOOK.md text and return the most probable platform flavor
    string based on keyword heuristics.  Returns ``None`` when no
    flavor-specific markers are found (caller should fall back to
    ``"generic"``).

:func:`assert_catalog_available`
    Raise :class:`UnsupportedFlavorError` when *flavor* is not in
    :data:`REGISTERED_CATALOGS`.  The CLI catches this and exits 6.

Design spec: ``docs/superpowers/specs/2026-06-02-mainframe-playbook-synth-logs-design.md``
+ SPEC.md §"Extension surface" (T2 catalog road map).
"""

from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
# Registered catalogs
# ---------------------------------------------------------------------------

#: The set of platform flavor names for which M6 has a registered log
#: catalog.  v1 ships only the mainframe (``cics_cobol``) catalog.
#: Additional catalogs land as T2 work (SPEC.md §"Extension surface").
#:
#: The catalog registry is intentionally a ``frozenset`` so membership
#: tests are O(1) and callers cannot mutate it at runtime.
REGISTERED_CATALOGS: Final[frozenset[str]] = frozenset({"cics_cobol"})

#: Human-readable list for diagnostic messages (sorted for determinism).
_REGISTERED_SORTED: Final[list[str]] = sorted(REGISTERED_CATALOGS)


# ---------------------------------------------------------------------------
# Playbook flavor detection
# ---------------------------------------------------------------------------

#: Minimum score (combined keyword hits) before we commit to a flavor
#: rather than returning ``None`` (unknown / generic).
_FLAVOR_MIN_SCORE: Final[int] = 2

#: Per-flavor keyword patterns (case-insensitive).  Each entry is a list
#: of patterns; each *unique pattern* that matches in the playbook text
#: contributes +1 to that flavor's score.
_FLAVOR_PATTERNS: Final[dict[str, list[re.Pattern[str]]]] = {
    "cics_cobol": [
        re.compile(r"\bCICS\b", re.IGNORECASE),
        re.compile(r"\bVSAM\b", re.IGNORECASE),
        re.compile(r"\bCOBOL\b", re.IGNORECASE),
        re.compile(r"\.cbl\b", re.IGNORECASE),
        re.compile(r"\bJCL\b", re.IGNORECASE),
        re.compile(r"\bEnterprise Server\b", re.IGNORECASE),
        re.compile(r"\bEXEC CICS\b", re.IGNORECASE),
        re.compile(r"\bBMS\b"),  # BMS map — mainframe-specific
        re.compile(r"\bASRA\b|\bAEY9\b|\bASRB\b"),  # CICS abend codes
    ],
    "python": [
        re.compile(r"\bPython\b", re.IGNORECASE),
        re.compile(r"\b(?:Django|Flask|FastAPI|Celery|SQLAlchemy)\b", re.IGNORECASE),
        re.compile(r"\.py\b"),
        re.compile(r"\bvirtualenv\b|\bpip\b|\brequirements\.txt\b", re.IGNORECASE),
        re.compile(r"\buvicorn\b|\bgunicorn\b|\bwsgi\b|\basgi\b", re.IGNORECASE),
    ],
    "java_ee": [
        re.compile(r"\bJava\b", re.IGNORECASE),
        re.compile(r"\b(?:EJB|JPA|JMS|CDI|JAX-RS|Servlet|Spring|Hibernate)\b", re.IGNORECASE),
        re.compile(r"\.java\b|\b\.war\b|\b\.ear\b|\b\.jar\b", re.IGNORECASE),
        re.compile(r"\bJBoss\b|\bWebLogic\b|\bWildFly\b|\bTomcat\b", re.IGNORECASE),
    ],
    "jcl_batch": [
        re.compile(r"\bJCL\b"),
        re.compile(r"//[A-Z]{1,8}\s+JOB\b"),
        re.compile(r"\bIEBGENER\b|\bIEBCOPY\b|\bSORT\b.*SYSIN", re.IGNORECASE),
    ],
    "ims_db": [
        re.compile(r"\bIMS\b|\bIMS/DB\b|\bIMS/TM\b", re.IGNORECASE),
        re.compile(r"\bDBD\b|\bPSB\b|\bACB\b"),  # IMS control blocks
        re.compile(r"\bDL/I\b", re.IGNORECASE),
    ],
}


def detect_flavor_from_playbook(playbook_text: str) -> str | None:
    """Scan *playbook_text* and return the most probable platform flavor.

    Scores each registered flavor pattern set against the playbook text
    and returns the highest-scoring flavor when it clears the minimum
    threshold (:data:`_FLAVOR_MIN_SCORE`).  Returns ``None`` when the
    text matches no flavor strongly enough (caller should fall back to
    ``"generic"``).

    The heuristic is intentionally lightweight — a PLAYBOOK.md produced by
    M5 always contains enough platform-specific vocabulary (CICS transaction
    codes, ``.cbl`` source paths, JCL DSN references, Python framework
    imports, Java EE annotations, etc.) to resolve unambiguously.  The
    function is **not** a general-purpose language detector; it only needs
    to distinguish the flavors for which M6 has registered catalogs from
    flavors it does not yet support.

    Args:
        playbook_text: Full text of the PLAYBOOK.md (or any playbook-shaped
            Markdown).  May be empty — returns ``None`` immediately when
            the text has no content.

    Returns:
        A flavor name string (e.g. ``"cics_cobol"``, ``"python"``) or
        ``None`` if the text is empty or no flavor clears the threshold.

    Example:
        >>> text = "## 3. Program Inventory\\n\\nCICS VSAM COBOL\\n"
        >>> detect_flavor_from_playbook(text)
        'cics_cobol'
        >>> detect_flavor_from_playbook("") is None
        True
    """
    if not playbook_text.strip():
        return None

    scores: dict[str, int] = {}
    for flavor, patterns in _FLAVOR_PATTERNS.items():
        score = sum(1 for pat in patterns if pat.search(playbook_text))
        scores[flavor] = score

    best_flavor = max(scores, key=lambda f: scores[f])
    best_score = scores[best_flavor]

    if best_score < _FLAVOR_MIN_SCORE:
        return None

    return best_flavor


# ---------------------------------------------------------------------------
# Catalog availability guard
# ---------------------------------------------------------------------------


class UnsupportedFlavorError(Exception):
    """Raised by :func:`assert_catalog_available` when *flavor* has no
    registered log catalog.

    Attributes:
        flavor: The platform flavor that was requested.
        supported: Sorted list of flavors that do have registered catalogs.
    """

    def __init__(self, flavor: str, *, supported: list[str]) -> None:
        self.flavor = flavor
        self.supported = supported
        super().__init__(
            f"no log catalog registered for flavor {flavor!r}; "
            f"v1 supports: {', '.join(supported) or '(none)'}"
        )


def assert_catalog_available(flavor: str) -> None:
    """Assert that *flavor* has a registered log catalog.

    Args:
        flavor: The resolved platform flavor string (e.g. ``"cics_cobol"``,
            ``"python"``, ``"generic"``).

    Raises:
        :class:`UnsupportedFlavorError`: *flavor* is not in
            :data:`REGISTERED_CATALOGS`.  The CLI catches this and exits
            with code 6 ("capability not available").

    Example:
        >>> assert_catalog_available("cics_cobol")  # no exception
        >>> assert_catalog_available("python")
        Traceback (most recent call last):
            ...
        primitives.synth_logs.shared.catalog.UnsupportedFlavorError: \
no log catalog registered for flavor 'python'; v1 supports: cics_cobol
    """
    if flavor not in REGISTERED_CATALOGS:
        raise UnsupportedFlavorError(flavor, supported=_REGISTERED_SORTED)


__all__ = [
    "REGISTERED_CATALOGS",
    "UnsupportedFlavorError",
    "assert_catalog_available",
    "detect_flavor_from_playbook",
]
