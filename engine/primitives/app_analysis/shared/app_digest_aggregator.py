"""M-app-analysis post-discovery aggregator — ``app-digest.json`` + ``ANALYSIS.md``.

Invoked by the skill body after all discovery stages have completed::

    python -m primitives.app_analysis.shared.app_digest_aggregator \\
        --repo-root <path> \\
        --context-dir <output-dir>

Reads from on-disk stage artifacts:

* ``PLAYBOOK.md``              — written by ``/mod-playbook-discovery``
* repo-digest text             — written to ``<context-dir>/repo-digest.txt``
                                  by the skill body's ``/repo-digest`` dispatch
* ``functional-map.json``      — written by ``/functional-map`` (optional)

Writes:

* ``<context-dir>/app-digest.json``  — compact application digest in the
                                        contracted SP3 shape (§"Output shape").
* ``<context-dir>/ANALYSIS.md``      — terse narrative analysis with
                                        refcheck-validated ``path:line`` citations.

Anti-hallucination invariant
----------------------------
Every module name, dependency, and debt item in the output is derived from an
on-disk stage artifact.  No content is invented.  Missing optional artifacts
(``functional-map.json``) cause graceful degradation, not fabricated content.

Adapter round-trip
------------------
The written ``app-digest.json`` is validated via
:func:`primitives._shared.modernization.adapters.app_digest.to_digest` before
writing.  A shape mismatch is a hard error (exit 1).

Citation validation
-------------------
Every ``path:line`` citation in ``ANALYSIS.md`` is validated by
:func:`primitives._shared.modernization.refcheck.check` before writing.
Unresolvable citations are removed; the count of dropped citations is reported to
stderr.  The written file has zero unresolved citations.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Literal

from primitives._shared.modernization.adapters.app_digest import to_digest as _adapter_to_digest
from primitives._shared.modernization.refcheck import _REF_RE
from primitives._shared.modernization.refcheck import check as _refcheck

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_MODULES = 30
_MAX_IMPORTS = 20
_MAX_SHARED_LIBS = 20
_MAX_DEBT = 15
_MAX_DOMAINS = 20

_SCHEMA_VERSION = "1.0"

# Artifact file names (relative to context-dir unless noted)
_REPO_DIGEST_TXT = "repo-digest.txt"
_FUNCTIONAL_MAP_JSON = "functional-map.json"

# ---------------------------------------------------------------------------
# PLAYBOOK.md parsing helpers
# ---------------------------------------------------------------------------

# Section heading pattern — matches "## 1. Executive Summary" or "## §14 Tech-debt"
_SECTION_RE = re.compile(r"^##\s+(?:§?\d+\.?\s+|§?\d+\s+)?(.+)$", re.IGNORECASE)

# Patterns used to identify flavors from PLAYBOOK.md section 2
_FLAVOR_SIGNALS: list[tuple[str, str]] = [
    (r"\bjava.?ee\b|\bjakarta\b|\bejb\b|\bjsp\b|\bservlet\b", "java_ee"),
    (r"\bspring\s+(?:boot|mvc|framework)\b|\bspring-boot\b", "java_ee"),
    (r"\bcobol\b|\bcics\b|\bvsam\b|\bjcl\b", "cics_cobol"),
    (r"\bdjango\b|\bflask\b|\bfastapi\b", "python"),
    (r"\bpython\b", "python"),
    (r"\bnode\.?js\b|\bexpress\b|\bnest\.?js\b", "node_js"),
    (r"\bruby.on.rails\b|\bsinatra\b", "ruby"),
    (r"\bdotnet\b|\.net\b|\basp\.net\b|\bc#\b", "dotnet"),
    (r"\bphp\b|\blaravel\b|\bsymfony\b", "php"),
    (r"\brails\b", "ruby"),
]

# Debt markers in PLAYBOOK.md §14
_DEBT_ITEM_RE = re.compile(r"^[-*]\s+(.+)$")

# Migration-state signals in §15
_MIGRATION_STATE_RE = re.compile(
    r"(?:coexistence|migration|transitioning|dual.?stack|hybrid\s+framework|"
    r"(?:struts|spring)\s*[↔←→]\s*(?:struts|spring))",
    re.IGNORECASE,
)

# Tier signals in §15
_TIER_SIGNALS: list[tuple[str, str]] = [
    (r"\bstrangler.?fig\b", "strangler-fig"),
    (r"\blift.and.shift\b|\blift.n.shift\b", "lift-and-shift"),
    (r"\brewrite\b", "rewrite"),
    (r"\bmodular.?monolith\b", "modular-monolith"),
    (r"\bmicroservices?\b", "microservices"),
]

# Shared-lib kind signals — imports with these kinds become shared_libs
_SHARED_LIB_KINDS = frozenset({"shared-lib", "utility", "shared", "internal-lib"})

# Dependency kinds that suggest a web tier
_WEB_TIER_SIGNALS = frozenset(
    {
        "spring-webmvc",
        "spring-web",
        "spring-boot",
        "javax.servlet",
        "jakarta.servlet",
        "django",
        "flask",
        "fastapi",
        "express",
        "nestjs",
        "rails",
        "sinatra",
        "asp.net",
        "react",
        "angular",
        "vue",
    }
)


def _split_playbook_sections(text: str) -> dict[str, str]:
    """Split PLAYBOOK.md into a mapping of section title → section body."""
    sections: dict[str, str] = {}
    current_title = "__preamble__"
    current_lines: list[str] = []

    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            sections[current_title] = "\n".join(current_lines)
            current_title = m.group(1).strip()
            current_lines = []
        else:
            current_lines.append(line)

    sections[current_title] = "\n".join(current_lines)
    return sections


def _detect_flavor_from_text(text: str) -> str:
    """Heuristically detect the platform flavor from arbitrary text."""
    for pattern, flavor in _FLAVOR_SIGNALS:
        if re.search(pattern, text, re.IGNORECASE):
            return flavor
    return "generic"


def _extract_debt_items(section_body: str) -> list[str]:
    """Extract bullet-point debt items from a PLAYBOOK section body."""
    items: list[str] = []
    for line in section_body.splitlines():
        m = _DEBT_ITEM_RE.match(line.strip())
        if m:
            item = m.group(1).strip()
            if item:
                items.append(item)
    return items


def _extract_migration_state(section_body: str) -> str | None:
    """Extract the coexistence signal from the §15 modernization analysis body.

    Returns the CLAUSE around the matched signal — snapped to sentence/clause
    boundaries (``.``/``;``/``:``, or line start/end) rather than a fixed-width
    character window, so the excerpt reads as a whole phrase instead of being cut
    mid-word (#1583).
    """
    for line in section_body.splitlines():
        m = _MIGRATION_STATE_RE.search(line)
        if not m:
            continue
        # Expand left to the previous clause boundary, right to the next one.
        left = max(
            (line.rfind(delim, 0, m.start()) for delim in ".;:"),
            default=-1,
        )
        start = left + 1  # char after the boundary (or 0 when none found → -1+1)
        right_candidates = [pos for delim in ".;:" if (pos := line.find(delim, m.end())) != -1]
        end = min(right_candidates) if right_candidates else len(line)
        excerpt = line[start:end].strip().rstrip(".,:;")
        return excerpt
    return None


def _extract_tier(section_body: str) -> str | None:
    """Extract tier hint from §15 modernization analysis body."""
    for pattern, tier in _TIER_SIGNALS:
        if re.search(pattern, section_body, re.IGNORECASE):
            return tier
    return None


# ---------------------------------------------------------------------------
# PLAYBOOK.md parser
# ---------------------------------------------------------------------------


def _parse_playbook(playbook_path: Path) -> dict[str, Any]:
    """Parse a PLAYBOOK.md written by ``/mod-playbook-discovery``.

    Returns a dict with keys:
        ``flavor``, ``domains``, ``debt``, ``migration_state``, ``tier``,
        ``raw_arch_text`` (for flavor fallback).
    """
    text = playbook_path.read_text(encoding="utf-8")
    sections = _split_playbook_sections(text)

    # §2 System Architecture — flavor detection + domain hints
    arch_text = ""
    for title, body in sections.items():
        if re.search(r"system\s+arch|architecture", title, re.IGNORECASE):
            arch_text = body
            break

    # §8 Business Rules / Bounded Contexts — domain labels
    domains: list[str] = []
    for title, body in sections.items():
        if re.search(r"business\s+rules?|bounded\s+context|domain", title, re.IGNORECASE):
            # Extract capitalized words / noun phrases as domain labels
            for line in body.splitlines():
                # Match "- **Owner** context" or "### Owner"
                dm = re.search(r"(?:\*\*|###?\s+)(\w+)", line)
                if dm:
                    label = dm.group(1).strip().lower()
                    if label and label not in {"the", "a", "an", "is", "are", "has"}:
                        domains.append(label)

    # §14 Tech-debt inventory
    debt: list[str] = []
    for title, body in sections.items():
        if re.search(r"tech.?debt|technical\s+debt", title, re.IGNORECASE):
            debt = _extract_debt_items(body)
            break

    # §15 Modernization analysis
    migration_state: str | None = None
    tier: str | None = None
    for title, body in sections.items():
        if re.search(r"modernization|migration", title, re.IGNORECASE):
            migration_state = _extract_migration_state(body)
            tier = _extract_tier(body)
            break

    flavor = _detect_flavor_from_text(arch_text or text)

    return {
        "flavor": flavor,
        "domains": list(dict.fromkeys(domains))[:_MAX_DOMAINS],  # dedup, preserve order
        "debt": debt[:_MAX_DEBT],
        "migration_state": migration_state,
        "tier": tier,
        "raw_arch_text": arch_text,
    }


# ---------------------------------------------------------------------------
# /repo-digest output parser
# ---------------------------------------------------------------------------

# LOC table pattern: "| Language | Lines |" or "| Java | 12500 |"
_LOC_ROW_RE = re.compile(r"\|\s*([A-Za-z][A-Za-z0-9#+\s._/-]*?)\s*\|\s*(\d[\d,]*)\s*\|")
# Module/package pattern: "- `com.example.owners` (3200 lines)"
_MODULE_LINE_RE = re.compile(
    r"(?:[-*]\s+)?`?([A-Za-z][A-Za-z0-9._/-]*)`?\s+[\(\[]?(\d+)(?:\s*lines?)?[\)\]]?"
)
# Dependency pattern: "- spring-webmvc:5.3.x" or "- spring-webmvc"
_DEP_LINE_RE = re.compile(r"[-*]\s+`?([A-Za-z][A-Za-z0-9._/-]*(?::[A-Za-z0-9._/-]+)?)`?")
# Consumed-table pattern in a data-access section: "- `owners`" or "- owners table"
_TABLE_LINE_RE = re.compile(r"[-*]\s+`?([A-Za-z][A-Za-z0-9_]*)`?(?:\s+table)?\s*$", re.IGNORECASE)

# English stop-words / prose fragments that leak into a repo-digest "## Data Access"
# section as bullet noise and were becoming ``data:<word>`` phantom join keys
# (#1462). A DB table name is essentially never one of these, so dropping them at
# the source keeps the app-digest's ``data:`` edges clean. Non-stop-word noise that
# still slips through (proper nouns, framework names) is dropped downstream by the
# correlate guard, which refuses to promote an unmatched ``data:`` consumed
# interface to an ``external:data:*`` node.
_TABLE_NAME_STOPWORDS: frozenset[str] = frozenset(
    {
        "table",
        "entity",
        "repository",
        "dao",
        # articles / determiners / conjunctions / prepositions / common verbs
        "a",
        "an",
        "the",
        "and",
        "or",
        "not",
        "but",
        "for",
        "with",
        "from",
        "into",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "is",
        "are",
        "be",
        "as",
        "it",
        "this",
        "that",
        "these",
        "those",
        "should",
        "must",
        "may",
        "can",
        "will",
        "would",
        # Framework / ORM / infrastructure / view-technology keywords that a
        # prose-tokenizing repo-digest Data Access extractor leaks as bullet noise
        # (#1510): JPA `fetch = FetchType`, `@Query(nativeQuery=…)`, i18n "localized",
        # the `spring` / `web` / `src` path & framework tokens, and the generic
        # "database" / "datasource" words. None of these is ever a real table name, so
        # dropping them at the source keeps app-digest.json's ``data:`` section
        # trustworthy for a human reader (the correlate guard already drops them
        # downstream, but they still polluted the digest artifact).
        "fetch",
        "native",
        "lazy",
        "eager",
        "localized",
        "http",
        "https",
        "web",
        "spring",
        "src",
        "database",
        "datasource",
        "jpa",
        "hibernate",
        "query",
        # NB: still deliberately NOT including content words that could plausibly name
        # a real table (note, example, use, return, see, order, type, …). The correlate
        # guard (``_is_unmatched_data_key``) remains the real safety net — it drops ANY
        # unmatched ``data:`` token — so this set only adds clear function/framework
        # words that reduce app-digest.json noise without risking a real table name.
    }
)


def _parse_loc_table(text: str) -> dict[str, int]:
    """Parse LOC breakdown from a repo-digest text output.

    Looks for a Markdown table with Language / Lines columns.
    """
    loc: dict[str, int] = {}
    in_table = False
    header_seen = False
    for line in text.splitlines():
        if "|" in line:
            # Detect header row: contains "Language" or "Lines"
            if not header_seen and re.search(r"language|loc|lines", line, re.IGNORECASE):
                header_seen = True
                in_table = True
                continue
            if in_table and re.match(r"^\|\s*[-:]+\s*\|", line):
                # Separator row "| --- |"
                continue
            m = _LOC_ROW_RE.search(line)
            if m and in_table:
                lang = m.group(1).strip()
                count_str = m.group(2).replace(",", "")
                if lang and not re.match(r"^[-:]+$", lang):
                    with contextlib.suppress(ValueError):
                        loc[lang] = int(count_str)
        elif in_table and line.strip() == "":
            # Blank line ends the table
            in_table = False
            header_seen = False
    return loc


def _parse_modules(text: str) -> list[dict[str, Any]]:
    """Parse module/package inventory from repo-digest text.

    Looks for a section heading containing "module" or "package" followed by
    bullet-point entries of the form ``- name (NNN lines)``.
    """
    modules: list[dict[str, Any]] = []
    in_section = False
    for line in text.splitlines():
        if re.search(r"##\s+.*(?:module|package|component|service|directory)", line, re.IGNORECASE):
            in_section = True
            continue
        if in_section and line.startswith("##"):
            in_section = False
        if in_section:
            m = _MODULE_LINE_RE.search(line)
            if m:
                name = m.group(1).strip()
                weight = int(m.group(2))
                if name and weight > 0:
                    modules.append({"name": name, "kind": "module", "weight": weight})

    # Sort by weight descending, name ascending (deterministic)
    return sorted(modules, key=lambda x: (-x["weight"], x["name"]))[:_MAX_MODULES]


def _parse_imports(text: str) -> list[dict[str, Any]]:
    """Parse dependency list from repo-digest text.

    Looks for a "Dependencies" or "Imports" section with bullet-point entries.
    """
    imports: list[dict[str, Any]] = []
    in_section = False
    for line in text.splitlines():
        if re.search(r"##\s+.*(?:dependencies|imports?|libraries|packages?)", line, re.IGNORECASE):
            in_section = True
            continue
        if in_section and line.startswith("##"):
            in_section = False
        if in_section:
            m = _DEP_LINE_RE.match(line.strip())
            if m:
                raw = m.group(1).strip()
                # Normalize "name:version" → "name"
                name = raw.split(":")[0].strip()
                # Classify
                kind: str
                if name.lower() in _WEB_TIER_SIGNALS or any(
                    sig in name.lower() for sig in ("web", "mvc", "http", "rest", "api")
                ):
                    kind = "framework"
                elif any(sig in name.lower() for sig in ("test", "junit", "mockito", "pytest")):
                    kind = "test"
                elif any(sig in name.lower() for sig in ("common", "util", "shared", "core")):
                    kind = "shared-lib"
                else:
                    kind = "dependency"
                if name:
                    imports.append({"target": name, "kind": kind})

    return imports[:_MAX_IMPORTS]


def _extract_shared_libs(imports: list[dict[str, Any]]) -> list[str]:
    """Extract shared-lib names from the import list."""
    return [imp["target"] for imp in imports if imp.get("kind") in _SHARED_LIB_KINDS][
        :_MAX_SHARED_LIBS
    ]


def _parse_consumed_tables(text: str) -> list[dict[str, Any]]:
    """Parse consumed DB tables from a data-access section → ``data:<table>`` imports.

    **SP2↔SP3 join-key contract (#1324 / #1318):** the ``mod-modernization-scope``
    synthesizer joins the app→DB thread by matching an app-digest
    ``consumed_interfaces`` value against a schema-digest ``exposed_interfaces``
    value.  The schema adapter exposes each table as ``data:<name.lower()>`` (via
    ``correlate._normalize``), so app-analysis MUST emit consumed tables with the
    same ``data:`` prefix — a bare table name would degrade the UI→API→DB thread
    silently.  We therefore prefix every parsed table with ``data:`` and lowercase
    it, mirroring the schema side.

    Looks for a section heading containing "data access", "persistence", "tables",
    "entities", or "repositories" followed by bullet-point table names.
    """
    names: list[str] = []
    in_section = False
    for line in text.splitlines():
        if re.search(
            r"##\s+.*(?:data\s*access|persistence|tables?|entities|repositories|dao)",
            line,
            re.IGNORECASE,
        ):
            in_section = True
            continue
        if in_section and line.startswith("##"):
            in_section = False
        if in_section:
            m = _TABLE_LINE_RE.match(line.strip())
            if m:
                name = m.group(1).strip().lower()
                # Skip English stop-words / framework fragments that are not table names
                # (#1462/#1510) — they were leaking through as data:<word> phantom keys.
                if name and name not in _TABLE_NAME_STOPWORDS:
                    names.append(name)

    names = _dedupe_singular_plural(names)
    return [{"target": f"data:{name}", "kind": "data-access"} for name in names]


def _dedupe_singular_plural(names: list[str]) -> list[str]:
    """Drop a singular table token when its plural is also present (#1510).

    A prose-tokenizing Data Access extractor emits both ``owner`` and ``owners`` (and
    ``vet``/``vets``, ``visit``/``visits``): the plural is the canonical JPA-mapped
    table name, so the singular is a phantom duplicate. When BOTH a token and that
    token + ``s``/``es`` appear, the singular is dropped and the plural kept. Order is
    preserved; a lone singular (no plural sibling) is left untouched — it may be the
    real table name."""
    present = set(names)
    kept: list[str] = []
    for name in names:
        if (name + "s") in present or (name + "es") in present:
            continue  # a plural sibling exists → this singular is a phantom dupe
        kept.append(name)
    return kept


# Signals that a repo *has* data access even when no ``## Data Access`` section
# was parsed — used to fire the zero-``data:``-edge hard-warn (#1405). A repo that
# clearly persists to a DB but yields zero ``data:`` edges means the repo-digest
# format drifted out from under the aggregator's parser: warn loudly rather than
# silently threading a half-broken UI→API→DB join.
_DATA_ACCESS_SIGNAL_RE = re.compile(
    r"@Table\b|@Entity\b|@Repository\b|\bJpaRepository\b|\bCrudRepository\b|"
    r"\bspring-jdbc\b|\bspring-data\b|\bhibernate\b|\bjdbcTemplate\b|"
    r"\bCREATE\s+TABLE\b|\bINSERT\s+INTO\b|\bpersistence\b|\bDataSource\b|"
    r"\bsqlalchemy\b|\bactiverecord\b|\bmybatis\b|\bentityManager\b",
    re.IGNORECASE,
)


def _has_data_access_signal(*texts: str) -> bool:
    """Return True if any *texts* carry a clear DB/persistence signal (#1405)."""
    return any(_DATA_ACCESS_SIGNAL_RE.search(t) for t in texts if t)


# Source extensions scanned by the zero-edge-warning fallback probe.
_DATA_ACCESS_PROBE_EXTS = frozenset(
    {".java", ".kt", ".py", ".sql", ".xml", ".rb", ".cs", ".php", ".go"}
)
# Cap the fallback probe so it never dominates aggregation cost.
_DATA_ACCESS_PROBE_FILE_CAP = 2000


def _repo_has_data_access(repo_root: Path) -> bool:
    """Probe *repo_root*'s source files for a DB/persistence signal (#1405).

    A bounded, best-effort fallback used only when the repo-digest text carried
    no ``## Data Access`` section AND no signal — it lets the zero-edge warning
    fire even when the digest text is terse. Returns True on the first hit.
    """
    scanned = 0
    for path in sorted(repo_root.rglob("*")):
        if scanned >= _DATA_ACCESS_PROBE_FILE_CAP:
            break
        if not path.is_file() or path.is_symlink() or path.suffix not in _DATA_ACCESS_PROBE_EXTS:
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _DATA_ACCESS_SIGNAL_RE.search(text):
            return True
    return False


def _parse_repo_digest(repo_digest_path: Path) -> dict[str, Any]:
    """Parse the /repo-digest output text file.

    Returns dict with keys: ``loc_by_language``, ``modules``, ``imports``.
    ``imports`` includes both library dependencies and consumed DB tables
    (the latter prefixed ``data:<table>`` per the SP2↔SP3 join-key contract).
    """
    text = repo_digest_path.read_text(encoding="utf-8")
    # ``_parse_imports`` already caps ORDINARY dependency imports at
    # ``_MAX_IMPORTS``. The ``data:<table>`` join-key edges are contract-critical
    # (#1348): the SP2 correlator threads ``.#app → .#db`` *only* through them, so
    # they must NEVER be dropped by the size cap. We therefore append every
    # ``data:`` edge unconditionally and do NOT re-truncate the combined list —
    # re-applying ``[:_MAX_IMPORTS]`` here is what silently severed the UI→API→DB
    # thread when a repo had ≥20 ordinary imports (the data: edges, appended last,
    # fell off the end).
    imports = _parse_imports(text)
    seen = {imp["target"] for imp in imports}
    for tbl in _parse_consumed_tables(text):
        if tbl["target"] not in seen:
            imports.append(tbl)
            seen.add(tbl["target"])
    return {
        "loc_by_language": _parse_loc_table(text),
        "modules": _parse_modules(text),
        "imports": imports,
    }


# ---------------------------------------------------------------------------
# functional-map.json parser (optional)
# ---------------------------------------------------------------------------


def _parse_functional_map(fm_path: Path) -> dict[str, Any] | None:
    """Parse functional-map.json if present.  Returns None if absent."""
    if not fm_path.is_file():
        return None
    try:
        return json.loads(fm_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return None


# Web-tier detection is an in-session concern of the skill body (advisory: it
# decides whether to dispatch /functional-map), NOT the aggregator's — the
# aggregator only assembles the on-disk artifacts.  ``_WEB_TIER_SIGNALS`` is used
# above by ``_parse_imports`` to classify framework dependencies.


# ---------------------------------------------------------------------------
# ANALYSIS.md renderer
# ---------------------------------------------------------------------------


def _render_loc_section(loc: dict[str, int]) -> list[str]:
    """Render the LOC-by-language table section."""
    if not loc:
        return []
    lines: list[str] = ["## LOC by Language", "", "| Language | Lines |", "|---|---|"]
    for lang, count in sorted(loc.items(), key=lambda x: -x[1]):
        lines.append(f"| {lang} | {count:,} |")
    lines.append("")
    return lines


def _module_citation(name: str, repo_root: Path) -> str:
    """Return a ``path:1`` citation for *name* if a known entry-point file exists."""
    candidate = repo_root / name.replace(".", "/")
    if candidate.is_dir():
        for entry in ("__init__.py", "index.js", "index.ts"):
            f = candidate / entry
            if f.is_file():
                return str(f.relative_to(repo_root)) + ":1"
    return ""


def _render_modules_section(modules: list[dict[str, Any]], repo_root: Path) -> list[str]:
    """Render the module-inventory table."""
    if not modules:
        return []
    lines: list[str] = [
        "## Module Inventory",
        "",
        "| Module | Kind | Weight (LOC) |",
        "|---|---|---|",
    ]
    for mod in modules:
        name = mod.get("name", "?")
        kind = mod.get("kind", "module")
        weight = mod.get("weight", 0)
        cite = _module_citation(name, repo_root)
        if cite:
            lines.append(f"| `{name}` (`{cite}`) | {kind} | {weight:,} |")
        else:
            lines.append(f"| `{name}` | {kind} | {weight:,} |")
    lines.append("")
    return lines


def _render_sources_section(
    repo_root: Path,
    context_dir: Path,
    playbook_path: Path | None,
    *,
    fm_present: bool,
) -> list[str]:
    """Render the 'Analysis Sources' section with path:line citations."""
    lines: list[str] = ["## Analysis Sources", ""]
    if playbook_path and playbook_path.is_file():
        with contextlib.suppress(ValueError):
            rel = playbook_path.relative_to(repo_root)
            lines.append(f"- PLAYBOOK.md: `{rel}:1`")
    if fm_present:
        fm_path = context_dir / _FUNCTIONAL_MAP_JSON
        if not fm_path.exists():
            fm_path = repo_root / ".foundry" / "context" / "functional-map" / "functional-map.json"
        if fm_path.exists():
            with contextlib.suppress(ValueError):
                rel = fm_path.relative_to(repo_root)
                lines.append(f"- functional-map.json: `{rel}:1`")
    lines.append("")
    return lines


def _render_analysis(
    repo_root: Path,
    context_dir: Path,
    digest: dict[str, Any],
    playbook_path: Path | None,
    *,
    fm_present: bool,
) -> str:
    """Render the terse ANALYSIS.md document.

    All citations are relative to *repo_root* so refcheck can validate them.
    Only files that actually exist under repo_root are cited; others are omitted.
    """
    repo_name = repo_root.name
    sections: list[list[str]] = [
        [
            f"# Application Analysis — {repo_name}",
            "",
            (
                f"**Flavor:** `{digest.get('flavor', 'generic')}` "
                f"**Tier:** `{digest.get('tier') or 'unknown'}` "
                f"**Migration state:** {digest.get('migration_state') or 'none detected'}"
            ),
            "",
        ],
        _render_loc_section(dict(digest.get("loc_by_language") or {})),
        _render_modules_section(list(digest.get("modules") or []), repo_root),
    ]

    # Tech-debt inventory
    debt = list(digest.get("debt") or [])
    if debt:
        debt_lines: list[str] = ["## Tech-Debt Inventory", ""]
        debt_lines.extend(f"- {item}" for item in debt)
        debt_lines.append("")
        sections.append(debt_lines)

    # Shared libraries
    shared = list(digest.get("shared_libs") or [])
    if shared:
        shared_lines: list[str] = ["## Shared Libraries", ""]
        shared_lines.extend(f"- `{lib}`" for lib in shared)
        shared_lines.append("")
        sections.append(shared_lines)

    # Domains
    domains = list(digest.get("domains") or [])
    if domains:
        domain_lines: list[str] = ["## Bounded Contexts / Domains", ""]
        domain_lines.extend(f"- {d}" for d in domains)
        domain_lines.append("")
        sections.append(domain_lines)

    # Modernization assessment
    assessment: list[str] = ["## Modernization Assessment", ""]
    tier = digest.get("tier")
    migration = digest.get("migration_state")
    if tier:
        assessment.append(f"**Recommended tier:** {tier}")
    if migration:
        assessment.append(f"**Migration coexistence detected:** {migration}")
    assessment.append("")
    sections.append(assessment)

    # Data sources
    sections.append(
        _render_sources_section(repo_root, context_dir, playbook_path, fm_present=fm_present)
    )

    lines: list[str] = []
    for section in sections:
        lines.extend(section)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Citation scrubbing (post-refcheck)
# ---------------------------------------------------------------------------


def _drop_broken_citations(analysis_md: str, repo_root: Path) -> tuple[str, int, int]:
    """Remove unresolvable ``path:line`` citations from *analysis_md*.

    Returns (cleaned_text, total_citations, dropped_citations).
    """
    report = _refcheck(analysis_md, repo_root)
    if report.ok:
        return analysis_md, report.total_refs, 0

    # Collect broken refs by source line (1-indexed)
    broken_lines = {br.source_line for br in report.broken}
    cleaned_lines = []
    for i, line in enumerate(analysis_md.splitlines(), start=1):
        if i in broken_lines:
            # Drop the citation inline — remove the path:line pattern
            cleaned = _REF_RE.sub("(citation removed)", line)
            cleaned_lines.append(cleaned)
        else:
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines), report.total_refs, len(report.broken)


# ---------------------------------------------------------------------------
# Adapter round-trip validation
# ---------------------------------------------------------------------------


def _validate_digest_roundtrip(digest: dict[str, Any], repo_root: Path) -> None:
    """Validate that *digest* round-trips through the SP1.3 adapter.

    Raises :class:`ValueError` on shape mismatch (hard error, nothing is written).
    """
    try:
        _adapter_to_digest(digest, code_root=str(repo_root))
    except Exception as exc:  # noqa: BLE001 — validation boundary: any adapter failure is
        # re-wrapped (not swallowed) into a clear ValueError so a malformed digest fails hard
        # with context; the round-trip guard must catch every unanticipated shape mismatch.
        raise ValueError(f"app-digest.json failed SP1.3 adapter round-trip: {exc}") from exc


# ---------------------------------------------------------------------------
# Top-level assembly function
# ---------------------------------------------------------------------------


def assemble(
    repo_root: Path,
    context_dir: Path,
) -> tuple[dict[str, Any], str]:
    """Assemble ``app-digest.json`` and ``ANALYSIS.md`` from stage artifacts.

    Args:
        repo_root: absolute path to the analysed source tree.
        context_dir: output directory for ``app-digest.json`` and ``ANALYSIS.md``.

    Returns:
        ``(digest_dict, analysis_md_text)`` — both validated before return.

    Raises:
        FileNotFoundError: if ``repo-digest.txt`` is missing (required).
        ValueError: if the assembled digest fails the SP1.3 adapter round-trip.
    """
    # --- PLAYBOOK.md (optional — graceful degradation if absent) ---
    playbook_path: Path | None = None
    playbook_data: dict[str, Any] = {
        "flavor": "generic",
        "domains": [],
        "debt": [],
        "migration_state": None,
        "tier": None,
        "raw_arch_text": "",
    }
    # Search conventional locations
    for candidate in [
        repo_root / "PLAYBOOK.md",
        repo_root / ".foundry" / "context" / "PLAYBOOK.md",
        context_dir / "PLAYBOOK.md",
    ]:
        if candidate.is_file():
            playbook_path = candidate
            playbook_data = _parse_playbook(candidate)
            break

    # --- /repo-digest output (required) ---
    repo_digest_path = context_dir / _REPO_DIGEST_TXT
    if not repo_digest_path.is_file():
        raise FileNotFoundError(
            f"repo-digest.txt not found at {repo_digest_path}. "
            "Run /repo-digest and save its output to this path before calling the aggregator."
        )
    repo_digest_data = _parse_repo_digest(repo_digest_path)

    # --- functional-map.json (optional) ---
    fm_path = repo_root / ".foundry" / "context" / "functional-map" / "functional-map.json"
    if not fm_path.is_file():
        fm_path = context_dir / _FUNCTIONAL_MAP_JSON
    fm_data = _parse_functional_map(fm_path)
    fm_present = fm_data is not None

    # --- Merge: flavor ---
    flavor = playbook_data["flavor"]
    if flavor == "generic":
        # Fallback: detect from loc_by_language
        loc = repo_digest_data["loc_by_language"]
        if "Java" in loc:
            flavor = "java_ee"
        elif "Python" in loc:
            flavor = "python"
        elif "JavaScript" in loc or "TypeScript" in loc:
            flavor = "node_js"
        elif "Ruby" in loc:
            flavor = "ruby"
        elif "PHP" in loc:
            flavor = "php"
        elif "C#" in loc:
            flavor = "dotnet"
        elif "COBOL" in loc:
            flavor = "cics_cobol"

    # --- Merge: imports + shared_libs ---
    imports: list[dict[str, Any]] = repo_digest_data["imports"]
    shared_libs = _extract_shared_libs(imports)

    # --- Zero-``data:``-edge hard-warn (#1405) ---
    # If the parse yielded ZERO ``data:<table>`` edges but the repo clearly has
    # data access, the repo-digest format drifted out from under this parser and
    # the UI→API→DB join is about to be silently half-threaded. Warn loudly (this
    # is the exact #1291-class silent gap the issue calls out) rather than
    # emitting an empty set with no signal.
    data_edges = [imp for imp in imports if str(imp.get("target", "")).startswith("data:")]
    if not data_edges:
        repo_digest_text = repo_digest_path.read_text(encoding="utf-8")
        if _has_data_access_signal(repo_digest_text, playbook_data.get("raw_arch_text", "")) or (
            _repo_has_data_access(repo_root)
        ):
            print(
                "WARNING (#1405): the repo shows clear data-access signals "
                "(@Table/@Entity/@Repository/JDBC/DDL) but the parsed repo-digest "
                "yielded ZERO data:<table> edges. The SP2 UI→API→DB join will be "
                "silently half-threaded. This usually means repo-digest emitted no "
                "'## Data Access' section (format drift). Regenerate repo-digest so "
                "its architecture output carries the '## Data Access' section, or "
                "add one to repo-digest.txt before re-running the aggregator.",
                file=sys.stderr,
            )

    # --- Merge: modules ---
    modules = repo_digest_data["modules"]

    # --- Merge: domains (playbook first, fallback top-level package names) ---
    domains: list[str] = playbook_data["domains"]
    if not domains and modules:
        # Derive from top-level package names (first segment before ".")
        top_level: dict[str, None] = {}
        for mod in modules[:_MAX_DOMAINS]:
            name = mod["name"]
            top = name.split(".")[0].split("/")[0].lower()
            if top:
                top_level[top] = None
        domains = list(top_level.keys())

    # --- Assemble the contracted digest dict ---
    digest: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "flavor": flavor,
        "loc_by_language": repo_digest_data["loc_by_language"],
        "modules": modules,
        "domains": domains,
        "imports": imports,
        "shared_libs": shared_libs,
        "debt": playbook_data["debt"],
        "migration_state": playbook_data["migration_state"],
        "tier": playbook_data["tier"],
    }

    # --- Validate round-trip (hard error on shape mismatch) ---
    _validate_digest_roundtrip(digest, repo_root)

    # --- Render ANALYSIS.md ---
    analysis_md = _render_analysis(
        repo_root=repo_root,
        context_dir=context_dir,
        digest=digest,
        playbook_path=playbook_path,
        fm_present=fm_present,
    )

    # --- Citation validation ---
    analysis_md, total_cites, dropped = _drop_broken_citations(analysis_md, repo_root)
    if dropped:
        print(
            f"refcheck: {dropped}/{total_cites} citation(s) dropped (unresolvable); "
            f"ANALYSIS.md has zero unresolved citations.",
            file=sys.stderr,
        )

    return digest, analysis_md


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app_digest_aggregator",
        description=(
            "M-app-analysis post-discovery aggregator: reads stage artifacts from "
            "--repo-root and --context-dir, writes app-digest.json + ANALYSIS.md."
        ),
    )
    parser.add_argument(
        "--repo-root",
        required=True,
        type=Path,
        metavar="DIR",
        help="Absolute path to the analysed source repository.",
    )
    parser.add_argument(
        "--context-dir",
        required=True,
        type=Path,
        metavar="DIR",
        help="Output directory (receives app-digest.json + ANALYSIS.md).",
    )
    return parser


def main(argv: list[str] | None = None) -> Literal[0, 1, 2]:
    """CLI entry point.  Returns exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    context_dir: Path = args.context_dir.resolve()

    if not repo_root.is_dir():
        print(f"ERROR: --repo-root {repo_root} is not a directory.", file=sys.stderr)
        return 2

    context_dir.mkdir(parents=True, exist_ok=True)

    try:
        digest, analysis_md = assemble(repo_root=repo_root, context_dir=context_dir)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Write app-digest.json
    digest_path = context_dir / "app-digest.json"
    digest_path.write_text(
        json.dumps(digest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"WROTE {digest_path}", file=sys.stdout)

    # Write ANALYSIS.md
    analysis_path = context_dir / "ANALYSIS.md"
    analysis_path.write_text(analysis_md, encoding="utf-8")
    print(f"WROTE {analysis_path}", file=sys.stdout)

    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "assemble",
    "main",
]
