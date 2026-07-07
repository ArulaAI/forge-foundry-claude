"""Language-agnostic "generic" discovery flavor — the universal fallback.

The generic flavor is the last-resort pack the auto-detect registry
falls back to when no per-stack flavor (``java_ee`` / ``cics_cobol`` /
``python`` / …) returns ``"high"`` or ``"medium"`` confidence. Its job
is *not* precision — the per-stack packs are the precision layer — but
*universal coverage*: feed it any source root in any language and it
produces a useful entity inventory via stack-agnostic regex heuristics.

## Scope — six abstract entity categories, universal heuristics

Like every M-EPIC-3 flavor pack the generic flavor emits the abstract
entity vocabulary from ``primitives/_shared/discovery/models.py`` (#522)
keyed on the six abstract type names (``execution_units`` /
``shared_components`` / ``entry_points`` / ``data_stores`` /
``ui_elements`` / ``batch_units``). The dispatch is purely on file
suffix:

* **Source-code files** (a broad cross-language extension set — Python /
  JS / TS / Java / Kotlin / Go / Ruby / PHP / C# / Rust / C / C++ /
  Scala / Swift / Elixir / Clojure / …) yield:
    - one ``ExecutionUnit`` per file (``kind="module"``; ``language``
      narrowed to the closed ``SourceLanguage`` Literal — ``python`` for
      ``.py``, ``sql`` for ``.sql``, ``other`` for everything else);
    - a universal **import catalog** (``SharedComponent``,
      ``kind="package"``) covering the import grammar of ~10 language
      families;
    - a universal **route + CLI catalog** (``EntryPoint``,
      ``kind="api_route"`` / ``"cli_command"``) covering the common web
      frameworks (Flask / FastAPI / Express / Koa / Rails / Sinatra /
      Django / Go net/http / ASP.NET / Laravel) plus four CLI-entrypoint
      idioms;
    - a conservative **data-store catalog** (``DataStore``,
      ``kind="db_table"``) from SQL ``FROM`` / ``JOIN`` / ``INTO`` /
      ``UPDATE`` clauses and a handful of ORM model-declaration idioms.
* **Template / view files** (``.html`` / ``.jsp`` / ``.erb`` / ``.vue``
  / ``.svelte`` / ``.astro`` / … and the dual-natured ``.jsx`` / ``.tsx``)
  yield one ``UIElement`` (``kind="react_component"`` for the
  component-framework extensions, ``"web_template"`` otherwise).
* **Config files** (``.yml`` / ``.yaml`` / ``.toml`` / ``.ini`` /
  ``.cfg`` / ``.properties`` / ``.json`` + crontab-named files) are
  scanned for scheduled-work signals (a 5-field cron expression,
  Kubernetes ``CronJob``, a GitHub-Actions ``cron:`` trigger, Spring
  ``@Scheduled``, ``schedule.every``, Celery ``@periodic_task`` /
  ``@shared_task``) → one ``BatchUnit`` (``kind="cron_task"``).

Every other suffix returns a *fresh* all-empty dict (one new ``[]`` per
key per call — the constant is never aliased, so a caller appending to a
returned list cannot corrupt the next call's result).

## Detection — always "low", always available

:meth:`GenericFlavor.detect` returns ``"low"`` unconditionally. The
generic flavor is the universal last-resort: it must never out-rank a
specific flavor that returns ``"high"`` / ``"medium"`` against a tree it
genuinely understands, but it is *always* available so the auto-detect
path never falls through to "no flavor". The registry wires the
fallback (this module does not touch ``__init__.py`` / ``recon.py``).

## Regex-only — no AST, no heavy parser

Mirroring the ``java_ee`` precedent (and unlike ``python``, which can
lean on the stdlib ``ast`` module): the generic flavor is *necessarily*
polyglot, so a per-language AST is not an option. Every signal is a
compiled regex. Honest precision over recall — Phase D's self-audit
surfaces under-extraction rather than the pack silently fabricating
entities.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from primitives._shared.discovery.models import (
    BatchUnit,
    DataStore,
    EntryPoint,
    ExecutionUnit,
    SharedComponent,
    UIElement,
)

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import (
        Confidence,
        SourceLanguage,
        UIElementKind,
    )


# ---------------------------------------------------------------------------
# Suffix catalogs
# ---------------------------------------------------------------------------

#: Source-code extensions — one ``ExecutionUnit`` per file plus import /
#: route / data-store extraction. Deliberately broad across language
#: families so the generic flavor covers any tree it is pointed at.
_SOURCE_EXTS: Final[frozenset[str]] = frozenset(
    {
        ".py",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".java",
        ".kt",
        ".go",
        ".rb",
        ".php",
        ".cs",
        ".rs",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cc",
        ".scala",
        ".swift",
        ".ex",
        ".exs",
        ".clj",
        ".cljs",
        ".sql",
    }
)

#: Template / view extensions — one ``UIElement`` per file. The
#: component-framework subset (``_COMPONENT_EXTS``) is tagged
#: ``react_component``; everything else here is ``web_template``.
_TEMPLATE_EXTS: Final[frozenset[str]] = frozenset(
    {
        ".html",
        ".htm",
        ".jsp",
        ".jspx",
        ".erb",
        ".ejs",
        ".hbs",
        ".handlebars",
        ".mustache",
        ".vue",
        ".svelte",
        ".razor",
        ".twig",
        ".astro",
        ".jsx",
        ".tsx",
    }
)

#: Component-framework template extensions — ``UIElement.kind`` becomes
#: ``react_component`` rather than ``web_template``. ``.jsx`` / ``.tsx``
#: are dual-natured (both source AND view); they appear here and in
#: :data:`_SOURCE_EXTS`.
_COMPONENT_EXTS: Final[frozenset[str]] = frozenset({".jsx", ".tsx", ".vue", ".svelte", ".astro"})

#: Config extensions scanned for scheduled-work (``BatchUnit``) signals.
_CONFIG_EXTS: Final[frozenset[str]] = frozenset(
    {".yml", ".yaml", ".toml", ".ini", ".cfg", ".properties", ".json"}
)

#: Filenames (stem or name, lowercased) treated as crontab config even
#: without a config suffix.
_CRONTAB_NAMES: Final[frozenset[str]] = frozenset({"crontab"})

#: Source suffixes whose language narrows to a non-``other`` member of
#: the closed :data:`SourceLanguage` Literal. Everything else folds to
#: ``other`` (the Literal is closed — there is no ``java`` / ``go`` / …).
_LANGUAGE_BY_SUFFIX: Final[dict[str, SourceLanguage]] = {
    ".py": "python",
    ".sql": "sql",
}


def _lang(suffix: str) -> SourceLanguage:
    """Narrow a file suffix to the closed ``SourceLanguage`` Literal.

    ``.py`` → ``"python"``, ``.sql`` → ``"sql"``, every other source
    suffix → ``"other"`` (the Literal does not enumerate ``java`` / ``go``
    / ``js`` / …). The ``other`` fallback is correct per the model
    docstring: "the catch-all for non-mainframe files that show up in the
    source tree".
    """
    return _LANGUAGE_BY_SUFFIX.get(suffix, "other")


# ---------------------------------------------------------------------------
# Import catalog — SharedComponent emission (kind="package")
# ---------------------------------------------------------------------------

#: Universal import-grammar catalog. Each pattern captures the imported
#: module / package name in group 1 (the two grouped-import forms below
#: also use group 1). Patterns are applied to *every* source file and the
#: results de-duplicated by name, so applying a Python pattern to a Go
#: file is harmless — it simply does not match.
_IMPORT_RES: Final[tuple[re.Pattern[str], ...]] = (
    # Python — ``import x`` / ``from x import y``
    re.compile(r"^\s*(?:import|from)\s+([\w.]+)", re.MULTILINE),
    # JS / TS — ``import ... from 'x'`` + ``require('x')``
    re.compile(r"""import\s.*?from\s+['"]([^'"]+)['"]"""),
    re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)"""),
    # Java / Kotlin / Scala — ``import a.b.C``
    re.compile(r"^\s*import\s+([\w.]+)", re.MULTILINE),
    # Go — single ``import "x"`` + grouped-block ``"x"`` lines
    re.compile(r'^\s*import\s+"([^"]+)"', re.MULTILINE),
    re.compile(r'^\s*(?:[\w.]+\s+)?"([^"]+)"\s*$', re.MULTILINE),
    # C# — ``using A.B``
    re.compile(r"^\s*using\s+([\w.]+)", re.MULTILINE),
    # Ruby — ``require 'x'`` / ``require_relative 'x'``
    re.compile(r"""require(?:_relative)?\s+['"]([^'"]+)['"]"""),
    # PHP — ``use A\B``
    re.compile(r"use\s+([\w\\]+)"),
    # Rust — ``use a::b``
    re.compile(r"^\s*use\s+([\w:]+)", re.MULTILINE),
    # C / C++ — ``#include <x>`` / ``#include "x"``
    re.compile(r'#include\s*[<"]([^>"]+)[>"]'),
)


def _extract_imports(content: str) -> list[SharedComponent]:
    """Extract imports across ~10 language families as ``SharedComponent``.

    Every pattern in :data:`_IMPORT_RES` is applied and the union of
    captured names de-duplicated. A pattern that does not match the
    file's language simply contributes nothing. Each surviving name
    becomes one ``SharedComponent`` with ``kind="package"``.
    """
    out: list[SharedComponent] = []
    seen: set[str] = set()
    for pattern in _IMPORT_RES:
        for match in pattern.finditer(content):
            name = match.group(1).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(SharedComponent(name=name, kind="package"))
    return out


# ---------------------------------------------------------------------------
# Route catalog — EntryPoint emission (kind="api_route" / "cli_command")
# ---------------------------------------------------------------------------

#: Universal HTTP-route catalog. Each pattern captures the route path in
#: group 1. Covers the common web frameworks across Python / JS / Ruby /
#: Go / C# / PHP. As with imports, every pattern is tried against every
#: source file and the path values de-duplicated.
_ROUTE_RES: Final[tuple[re.Pattern[str], ...]] = (
    # Flask / FastAPI — ``@app.route("/x")`` / ``@router.get("/x")``
    re.compile(r"""@\w+\.(?:route|get|post|put|delete|patch)\(\s*['"]([^'"]+)['"]"""),
    # Express / Koa — ``app.get("/x", ...)`` / ``router.use("/x", ...)``
    re.compile(r"""(?:app|router)\.(?:get|post|put|delete|patch|use|all)\(\s*['"]([^'"]+)['"]"""),
    # Rails / Sinatra — ``get "/x"`` at line start
    re.compile(r"""^\s*(?:get|post|put|patch|delete)\s+['"]([^'"]+)['"]""", re.MULTILINE),
    # Django — ``path(r"^x")`` / ``re_path("^x")`` / ``url("^x")``
    re.compile(r"""(?:path|re_path|url)\(\s*r?['"]([^'"]+)['"]"""),
    # Go — ``mux.HandleFunc("/x", ...)`` / ``http.Handle("/x", ...)``
    re.compile(r"""(?:mux|r|http)\.(?:HandleFunc|Handle|Get|Post)\(\s*"([^"]+)\""""),
    # ASP.NET — ``[HttpGet("x")]`` / ``[Route("x")]``
    re.compile(r"""\[(?:HttpGet|HttpPost|HttpPut|HttpDelete|Route)\(\s*"([^"]+)\""""),
    # Laravel — ``Route::get("/x", ...)``
    re.compile(r"""Route::(?:get|post|put|delete|patch)\(\s*['"]([^'"]+)['"]"""),
)

#: CLI-entrypoint signals. Presence of any of these in a source file →
#: one ``EntryPoint(kind="cli_command")`` named after the file stem.
_PY_MAIN_RE: Final[re.Pattern[str]] = re.compile(r"""if\s+__name__\s*==\s*['"]__main__['"]""")
_GO_MAIN_RE: Final[re.Pattern[str]] = re.compile(r"\bfunc\s+main\s*\(")
_JAVA_MAIN_RE: Final[re.Pattern[str]] = re.compile(r"\bpublic\s+static\s+void\s+main\b")


def _has_cli_entrypoint(content: str) -> bool:
    """Return True iff ``content`` carries a CLI-entrypoint idiom.

    Covers Python ``if __name__ == "__main__"``, Go ``func main(``, Java
    ``public static void main``, or a ``#!`` shebang on line 1.
    """
    if content.startswith("#!"):
        return True
    return bool(
        _PY_MAIN_RE.search(content) or _GO_MAIN_RE.search(content) or _JAVA_MAIN_RE.search(content)
    )


def _extract_entry_points(content: str, stem: str) -> list[EntryPoint]:
    """Extract HTTP routes + CLI entrypoints as ``EntryPoint`` records.

    HTTP routes (``kind="api_route"``) carry the captured route path as
    ``name`` and the file stem as ``target_unit``. A CLI entrypoint
    (``kind="cli_command"``) carries the file stem as ``name``. Route
    paths are de-duplicated; the CLI entry is emitted at most once.
    """
    out: list[EntryPoint] = []
    seen: set[str] = set()
    for pattern in _ROUTE_RES:
        for match in pattern.finditer(content):
            path = match.group(1).strip()
            if not path or path in seen:
                continue
            seen.add(path)
            out.append(EntryPoint(name=path, kind="api_route", target_unit=stem))
    if _has_cli_entrypoint(content):
        out.append(EntryPoint(name=stem, kind="cli_command", target_unit=stem))
    return out


# ---------------------------------------------------------------------------
# Data-store catalog — DataStore emission (kind="db_table")
# ---------------------------------------------------------------------------

#: SQL table references in ``FROM`` / ``JOIN`` / ``INTO`` / ``UPDATE``
#: clauses. Conservative by design — anchored on the SQL verb so prose
#: containing the word "from" does not fabricate a table. Group 1 is the
#: (optionally schema-qualified) table identifier.
_SQL_TABLE_RE: Final[re.Pattern[str]] = re.compile(
    r"""(?:FROM|JOIN|INTO|UPDATE)\s+["'`\[]?([A-Za-z_][\w.]*)""",
    re.IGNORECASE,
)

#: Gate for the ``FROM``/``JOIN`` table scan: only run it when the file actually
#: contains a SQL DML statement. Without this, ``FROM table`` over-matches
#: non-SQL idioms that share the keyword — most importantly Python's
#: ``from <module> import …`` (which produced a bogus ``<module>`` data store).
#: A Python file embedding a real query (``cursor.execute("SELECT … FROM …")``)
#: still passes the gate because the ``SELECT`` is present.
_SQL_DML_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+TABLE)\b",
    re.IGNORECASE | re.DOTALL,
)

#: ORM model-declaration idioms that name a table / model explicitly.
#: Group 1 is the model / table name where the idiom carries one.
_ORM_NAME_RES: Final[tuple[re.Pattern[str], ...]] = (
    # Mongoose — ``mongoose.model("User", ...)``
    re.compile(r"""mongoose\.model\(\s*['"]([^'"]+)['"]"""),
    # SQLAlchemy declarative — ``class Foo(Base):``
    re.compile(r"\bclass\s+(\w+)\([^)]*\bBase\b[^)]*\)"),
    # Sequelize — ``sequelize.define("Foo", ...)``
    re.compile(r"""sequelize\.define\(\s*['"]([^'"]+)['"]"""),
)

#: ORM idioms that flag a *class* as persistent without naming a table
#: inline — the class name (group 1) is the store name.
_ORM_DECORATED_RES: Final[tuple[re.Pattern[str], ...]] = (
    # JPA / TypeORM — ``@Entity`` immediately preceding a class decl
    re.compile(r"@Entity\b[^\n]*\n[^\n]*?\bclass\s+(\w+)"),
    # Django / Flask-SQLAlchemy — ``class Foo(db.Model):`` / ``models.Model``
    re.compile(r"\bclass\s+(\w+)\([^)]*\b(?:db\.Model|models\.Model)\b[^)]*\)"),
)


def _extract_data_stores(content: str) -> list[DataStore]:
    """Extract best-effort ``DataStore`` records (``kind="db_table"``).

    Conservative: SQL table references are anchored on the SQL verb, and
    ORM detection is limited to a handful of explicit model-declaration
    idioms. De-duplicated by name. Precision is preferred over recall —
    the per-stack flavors are the precision layer.
    """
    out: list[DataStore] = []
    seen: set[str] = set()
    # Only scan raw ``FROM``/``JOIN`` table refs when the file actually carries a
    # SQL statement — otherwise ``from <module> import`` and similar keyword
    # collisions produce bogus tables. ORM idioms are unambiguous and run always.
    candidates: list[str] = (
        [m.group(1) for m in _SQL_TABLE_RE.finditer(content)] if _SQL_DML_RE.search(content) else []
    )
    for pattern in (*_ORM_NAME_RES, *_ORM_DECORATED_RES):
        candidates.extend(m.group(1) for m in pattern.finditer(content))
    for raw in candidates:
        name = raw.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(DataStore(name=name, kind="db_table"))
    return out


# ---------------------------------------------------------------------------
# Batch catalog — BatchUnit emission (kind="cron_task")
# ---------------------------------------------------------------------------

#: Scheduled-work signals scanned for in config files. Any match flags
#: the file as carrying a batch unit. The 5-field cron expression is the
#: broadest signal; the rest are framework-specific scheduler idioms.
_CRON_EXPR_RE: Final[re.Pattern[str]] = re.compile(
    r"(?m)^\s*[\d*/,\-]+\s+[\d*/,\-]+\s+[\d*/,\-]+\s+[\d*/,\-]+\s+[\d*/,\-]+\s",
)
_BATCH_SIGNAL_RES: Final[tuple[re.Pattern[str], ...]] = (
    _CRON_EXPR_RE,
    re.compile(r"kind:\s*CronJob"),
    re.compile(r"""cron:\s*['"]?([^'"\n]+)"""),
    re.compile(r"@Scheduled\b"),
    re.compile(r"schedule\.every\b"),
    re.compile(r"@(?:periodic_task|shared_task)\b"),
)


def _extract_batch_units(content: str, stem: str) -> list[BatchUnit]:
    """Extract a ``BatchUnit`` (``kind="cron_task"``) from config content.

    Emits at most one record (named after the file stem) when any
    scheduled-work signal in :data:`_BATCH_SIGNAL_RES` matches — a config
    file is either a scheduler descriptor or it is not.
    """
    for pattern in _BATCH_SIGNAL_RES:
        if pattern.search(content):
            return [BatchUnit(name=stem, kind="cron_task", steps=[])]
    return []


# ---------------------------------------------------------------------------
# Category keys + per-suffix dispatch
# ---------------------------------------------------------------------------

#: The six category keys every parse() return dict carries. Used to build
#: a *fresh* all-empty result (one new list per key) so a caller
#: appending to a returned list can never corrupt the next call.
_CATEGORY_KEYS: Final[tuple[str, ...]] = (
    "execution_units",
    "shared_components",
    "entry_points",
    "data_stores",
    "ui_elements",
    "batch_units",
)


def _empty_result() -> dict[str, list[object]]:
    """Return a fresh all-empty category dict (never an aliased constant)."""
    return {key: [] for key in _CATEGORY_KEYS}


def _parse_source(file_path: Path, content: str, suffix: str) -> dict[str, list[object]]:
    """Parse a source-code file — all source-derived categories."""
    result = _empty_result()
    stem = file_path.stem
    result["execution_units"] = [
        ExecutionUnit(
            name=stem,
            source_path=str(file_path),
            language=_lang(suffix),
            kind="module",
            loc=content.count("\n") + 1,
        )
    ]
    result["shared_components"] = list(_extract_imports(content))
    result["entry_points"] = list(_extract_entry_points(content, stem))
    result["data_stores"] = list(_extract_data_stores(content))
    # Dual-natured component sources (.jsx / .tsx) are BOTH source AND
    # view — emit a react_component UIElement alongside the source records.
    if suffix in _COMPONENT_EXTS:
        result["ui_elements"] = [UIElement(name=stem, kind="react_component")]
    return result


def _parse_template(file_path: Path, suffix: str) -> dict[str, list[object]]:
    """Parse a template / view file — one ``UIElement``."""
    result = _empty_result()
    kind: UIElementKind = "react_component" if suffix in _COMPONENT_EXTS else "web_template"
    result["ui_elements"] = [UIElement(name=file_path.stem, kind=kind)]
    return result


def _parse_config(file_path: Path, content: str) -> dict[str, list[object]]:
    """Parse a config file — scheduled-work ``BatchUnit`` detection."""
    result = _empty_result()
    result["batch_units"] = list(_extract_batch_units(content, file_path.stem))
    return result


def parse_generic_source(file_path: Path, content: str) -> dict[str, list[object]]:
    """Parse one source file into category-keyed lists via universal heuristics.

    Public per-file entry point matching the
    :data:`primitives._shared.discovery.flavor.FlavorParserCallable`
    alias. Dispatches on ``file_path.suffix.lower()``:

    * source-code suffix (:data:`_SOURCE_EXTS`) — ``ExecutionUnit`` +
      import / route / data-store extraction;
    * template suffix (:data:`_TEMPLATE_EXTS`) — one ``UIElement``;
    * config suffix (:data:`_CONFIG_EXTS`) or a crontab-named file —
      scheduled-work ``BatchUnit`` detection;
    * anything else — a fresh all-empty dict.

    Note ``.jsx`` / ``.tsx`` are dual-natured (both source AND view): the
    source branch wins the suffix dispatch (it is checked first) and emits
    the full source record set *plus* a ``react_component`` ``UIElement``,
    so a single call surfaces both the module and the component.

    Args:
        file_path: Source file path (drives suffix dispatch + populates
            ``ExecutionUnit.source_path`` / names).
        content: Raw file content (the aggregator reads + decodes before
            calling here, so we accept ``str`` not bytes).

    Returns:
        A dict with the six keys from :data:`_CATEGORY_KEYS`, each mapping
        to a list of the corresponding abstract entity record. A category
        with no records maps to an empty list.
    """
    suffix = file_path.suffix.lower()
    if suffix in _SOURCE_EXTS:
        return _parse_source(file_path, content, suffix)
    if suffix in _TEMPLATE_EXTS:
        return _parse_template(file_path, suffix)
    if suffix in _CONFIG_EXTS or file_path.name.lower() in _CRONTAB_NAMES:
        return _parse_config(file_path, content)
    # Unknown suffix — fresh empty lists per call (never alias a shared
    # constant, so a caller appending to a returned list can't corrupt it).
    return _empty_result()


# ---------------------------------------------------------------------------
# FlavorParser Protocol implementation
# ---------------------------------------------------------------------------


class GenericFlavor:
    """Language-agnostic universal-fallback flavor pack.

    Implements the
    :class:`primitives._shared.discovery.flavor.FlavorParser` Protocol
    (``detect`` / ``parse`` / ``recommended_section_branches``). The
    registry wires this as the auto-detect fallback — the pack that runs
    when no per-stack flavor returns ``"high"`` / ``"medium"`` against a
    tree it genuinely understands.

    The class is stateless; one instance suffices for a whole Phase B
    walk.
    """

    name: Final[str] = "generic"

    def detect(self, code_root: Path) -> Confidence:
        """Always return ``"low"`` — the universal last-resort.

        The generic flavor must never out-rank a specific flavor that
        returns ``"high"`` / ``"medium"`` against a tree it genuinely
        understands, but it is *always* available so the auto-detect path
        never falls through to "no flavor". The registry uses it as the
        fallback when no specific flavor returns ``high`` / ``medium``.

        Args:
            code_root: Directory under consideration (unused — the
                generic flavor applies to any tree, so detection is
                content-free).

        Returns:
            The constant ``"low"`` ``Confidence`` Literal.
        """
        del code_root  # content-free detection — the generic flavor applies to any tree
        return "low"

    def parse(self, file_path: Path, content: str) -> dict[str, list[object]]:
        """Per-file dispatch — delegate to :func:`parse_generic_source`."""
        return parse_generic_source(file_path, content)

    def recommended_section_branches(self) -> list[str]:
        """Return ``["generic"]`` — the universal section-branch token."""
        return ["generic"]


__all__ = ["GenericFlavor", "parse_generic_source"]
