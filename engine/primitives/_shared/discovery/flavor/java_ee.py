"""M5 Phase B flavor pack: Java EE / Jakarta EE / Spring (M5-FLAVOR-JAVA-EE, #527).

Second non-mainframe flavor pack for the M-EPIC-3 polyglot reframing
of /mod-playbook-discovery. Companion to the v1 ``cics_cobol`` pack and
the first non-mainframe pack ``python`` (#526) — together the three
prove the polyglot framing end-to-end before the long tail of
PowerBuilder / RPG / Delphi / VB6 packs lands.

## Scope

The Java EE flavor parses ``.java`` source files for the canonical
five-record set the M-EPIC-3 abstract entity vocabulary defines:

* **ExecutionUnit** — every ``class`` / ``interface`` / ``enum``
  declaration. ``kind`` is ``"class"`` (the abstract Literal does not
  yet enumerate ``interface`` / ``enum`` separately; both fold to
  ``"class"`` for v1, with the original keyword captured verbatim in
  the ``language`` field via a ``"java"`` fallback into ``"other"``).
  The class's package-relative name becomes ``ExecutionUnit.name``;
  the source file path becomes ``source_path``.
* **SharedComponent** — every ``import package.Class;`` (or
  ``import static package.Class.member;``) declaration produces one
  ``SharedComponent`` record with ``kind="package"``. The imported
  fully-qualified name becomes ``SharedComponent.name``. The wildcard
  form ``import package.*;`` is preserved verbatim — Phase B's
  aggregator decides whether to expand it against the resolved file
  set or to leave it as a coarse-grained edge.
* **EntryPoint** — every ``@WebServlet`` (Servlet 3.0+), JAX-RS
  ``@Path``, and Spring MVC ``@Controller`` / ``@RestController`` /
  ``@GetMapping`` / ``@PostMapping`` / ``@PutMapping`` /
  ``@DeleteMapping`` / ``@PatchMapping`` / ``@RequestMapping``
  declaration produces an ``EntryPoint`` with ``kind="api_route"``.
  Where the annotation specifies a path value (``@Path("/api")``,
  ``@WebServlet(urlPatterns = "/foo")``), that path is captured as
  the ``EntryPoint.name``; absent a path value, the annotated symbol's
  name (class or method) is captured instead.
* **DataStore** — every ``@Entity`` declaration on a class produces a
  ``DataStore`` with ``kind="db_table"``. When the class also carries
  a ``@Table(name="X")`` annotation, ``X`` becomes the
  ``DataStore.name``; otherwise the class's simple name is used (JPA
  default-table convention).
* **BatchUnit** — three Java batch idioms produce ``BatchUnit``
  records:
    - ``@Scheduled`` (Spring) on a method → ``kind="scheduled_workflow"``.
    - Quartz pattern: a class annotated ``@Component`` that
      ``implements Job`` (or ``extends QuartzJobBean``) →
      ``kind="scheduled_workflow"``.
    - Spring Batch: ``@Bean public Step xStep(...)`` or
      ``@Bean public Job xJob(...)`` declarations →
      ``kind="scheduled_workflow"``.

The five categories above are emitted as a dict keyed on the abstract
type names from #522 (``execution_units`` / ``shared_components`` /
``entry_points`` / ``data_stores`` / ``batch_units``) — the design
spec §"The flavor-pack contract" sanctions both vocabularies; we
choose the abstract one because Java EE was never part of the v1
mainframe vocabulary and there is no concrete-vocab projection to
preserve.

## UI element extraction — directory-level, not per-file

The ``UIElement`` abstraction (``@kind="web_template"``) covers JSP
and Thymeleaf templates, which are *not* Java source files. Phase B's
per-file dispatch (``FLAVORS[flavor](path, content)``) is keyed on
``.java`` files in the v1 flavor walk, so a per-file ``parse()`` call
cannot return UI elements.

Two clean options exist:

1. Expose a separate :func:`_detect_ui_elements` callable the Phase B
   aggregator invokes once per code-root walk.
2. Treat UI element extraction as out-of-scope for the per-file
   ``parse()`` contract and document the carve-out explicitly.

We pick **(1)**: ship :func:`_detect_ui_elements` as a module-level
helper the analyzer can call separately when the v1.1 polyglot
aggregator wires JSP / Thymeleaf walks. The Phase B aggregator does
*not* call it in v1 (the per-file dispatch contract is preserved as
shipped) — when the polyglot aggregator graduates from the mainframe
walk, it will call :func:`_detect_ui_elements` once over the resolved
code-root before iterating ``.java`` files. The function is part of
the public surface so external integrators can drive it ahead of that
graduation.

## Regex-only — no heavyweight Java parser

This pack mirrors the C8 precedent
(``primitives/architecture_review/shared/language_parsers/java_parser.py``):
regex-based extraction. Real Java grammars are AST-grade (JavaParser,
Eclipse JDT, tree-sitter-java) and pulling one into foundry would
balloon scope. The regex pack ships honest precision (every positive
case anchored on a Servlet 3.0 / Spring 5+ / JPA 2.2 idiom present in
the public reference apps Sakai, Spring PetClinic, JBoss SeamSpace)
at the cost of imperfect recall on macro-heavy or generated-source
trees — which Phase D's self-audit (M5-AUDIT-1) catches and surfaces
in §Validation Report rather than silently dropping.

Conventions the regex pack respects:

* **Line and block comments.** ``//`` and ``/* ... */`` are stripped
  before pattern matching to avoid annotating commented-out
  declarations. The stripper is deliberately lightweight and does
  not handle nested ``/*`` blocks (the Java grammar forbids them).
* **Annotation positioning.** Annotations precede the
  class/method/field they target, optionally on the same line. The
  scanner walks forward from an annotation match looking for the
  next ``class``, ``interface``, ``enum``, or method signature to
  attribute the annotation to.
* **Annotation argument forms.** Java annotations accept
  ``@Foo``, ``@Foo("value")``, ``@Foo(value = "x")``,
  ``@Foo(value = "x", other = 42)``. The extractors handle all
  three for the annotations we care about; the regex is
  case-preserving on the value (paths / table names / bean names
  round-trip into the playbook narrative verbatim).
* **Package and class naming.** Java packages use dot-delimited
  fully-qualified names; classes use Pascal-case. Imports are
  ``import a.b.C;`` or ``import static a.b.C.member;`` — the static
  form is preserved verbatim because the static-import target is a
  real (resolvable) symbol the graph cares about.

## Detection heuristic

:meth:`JavaEEFlavor.detect` returns:

* ``"high"`` when ``pom.xml`` or ``build.gradle`` (or
  ``build.gradle.kts``) exists at ``code_root`` AND at least one
  ``.java`` file exists anywhere under ``code_root``. The manifest +
  source-presence pair is a strong signal: a Maven / Gradle project
  with Java sources is a Java project.
* ``"medium"`` when ``.java`` files exist but no build manifest. The
  source-only case applies (the user might be pointing at a subdir of
  a polyglot monorepo, or at a vendored Java tree without its build
  descriptor) but the lack of a manifest weakens the signal.
* ``"low"`` otherwise. Per the design spec §"Conflict resolution"
  bullet 3, the auto-detect path treats ``"low"`` as last-resort —
  the user can still force this flavor via ``--platform-flavor
  java_ee``, but auto-detect will prefer any flavor returning
  ``"high"`` or ``"medium"``.

## Sub-call into other flavors

The Java EE flavor is *single-purpose*: it does not chain into
``cics_cobol`` even when the source tree carries an embedded mainframe
component. The polyglot auto-detector (#525) handles flavor selection
at the recon level; flavor packs themselves stay focused on their own
source-tree vocabulary.

## Reference points

* C8 precedent: ``primitives/architecture_review/shared/language_parsers/java_parser.py``
  for the regex-based import-extraction shape we mirror.
* Sister pack: ``primitives/_shared/discovery/flavor/cics_cobol.py``
  for the per-file ``parse()`` callable shape + record-emission discipline.
* Polyglot design spec: ``docs/superpowers/specs/2026-06-03-polyglot-discovery-design.md``
  §"The flavor-pack contract" + §"Per-flavor adaptation of the 16 sections"
  (cell "java_ee" column).
* Abstract entity vocabulary: ``primitives/_shared/discovery/models.py``
  §"Abstract entity vocabulary" — ``ExecutionUnit`` / ``SharedComponent`` /
  ``EntryPoint`` / ``DataStore`` / ``UIElement`` / ``BatchUnit`` shapes.
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

    from primitives._shared.discovery.models import Confidence


# ---------------------------------------------------------------------------
# Comment stripping
# ---------------------------------------------------------------------------

# ``//`` line comments through end-of-line, plus ``/* ... */`` block
# comments (greedy across newlines but DOTALL-aware). Java forbids
# nested block comments so a single non-greedy match suffices.
_BLOCK_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"//[^\n]*")


def _strip_comments(source: str) -> str:
    """Remove ``//`` line and ``/* ... */`` block comments from Java source.

    Used to avoid attributing annotations that live inside commented-out
    declarations. Deliberately lightweight — handles the common Java
    grammar but does not preserve string-literal context (a ``//``
    inside a string literal would erroneously trigger line stripping).
    Acceptable: false negatives (under-extraction) are caught by Phase
    D's self-audit; the alternative (a full lexer) violates the
    foundry no-heavy-parser invariant.
    """
    no_blocks = _BLOCK_COMMENT_RE.sub("", source)
    return _LINE_COMMENT_RE.sub("", no_blocks)


# ---------------------------------------------------------------------------
# Imports — SharedComponent emission
# ---------------------------------------------------------------------------

# ``import a.b.C;`` or ``import static a.b.C.member;`` — the captured
# group is the dot-delimited fully-qualified name, with the optional
# ``static`` modifier discarded (the import target is a symbol either
# way; the static-vs-non-static distinction is irrelevant for the
# shared-component edge).
_IMPORT_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;",
    re.MULTILINE,
)


def _extract_imports(source: str) -> list[SharedComponent]:
    """Extract ``import`` declarations as ``SharedComponent`` records."""
    out: list[SharedComponent] = []
    seen: set[str] = set()
    for match in _IMPORT_RE.finditer(source):
        name = match.group(1)
        if name in seen:
            # Deduplicate — a file legitimately importing the same FQN
            # twice produces a single graph edge, not two.
            continue
        seen.add(name)
        out.append(SharedComponent(name=name, kind="package"))
    return out


# ---------------------------------------------------------------------------
# Class / interface / enum declarations — ExecutionUnit emission
# ---------------------------------------------------------------------------

# Java type-declaration verbs (``class`` / ``interface`` / ``enum``)
# preceded by optional modifiers (``public`` / ``abstract`` / ``final``
# / ``static`` / ``sealed`` / annotation-class ``@interface``). The
# captured group is the declared identifier; the optional
# ``extends`` / ``implements`` clauses are ignored (they round-trip
# into the graph via the import edges + the ``@Entity`` /
# ``implements Job`` detectors below).
_TYPE_DECL_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    (?:^|\n)
    \s*
    (?:(?:public|protected|private|abstract|final|static|sealed|non-sealed|@)\s+)*  # modifiers
    (class|interface|enum)\s+
    (\w+)
    """,
    re.VERBOSE,
)


def _extract_execution_units(
    source: str,
    file_path: Path,
) -> list[ExecutionUnit]:
    """Extract ``class`` / ``interface`` / ``enum`` declarations.

    Each top-level (and inner — the regex does not distinguish nesting)
    type declaration produces one ``ExecutionUnit``. The unit's
    ``kind`` is uniformly ``"class"`` because the abstract Literal does
    not yet enumerate ``interface`` / ``enum`` separately. The original
    Java keyword would round-trip via a future ``ExecutionUnit.subkind``
    field; for v1 we conflate.

    ``language`` is the closed ``SourceLanguage`` Literal value
    ``"other"`` — Java is not yet a member of that closed Literal
    (which v1 enumerates ``cobol`` / ``copybook`` / ``pli`` / ``jcl``
    / ``bms`` / ``asm`` / ``sql`` / ``python`` / ``other``). The
    ``other`` fallback is correct per the model docstring: "the
    catch-all for non-mainframe files that show up in the source
    tree". Future polyglot expansion of the Literal will add ``java``.
    """
    out: list[ExecutionUnit] = []
    for match in _TYPE_DECL_RE.finditer(source):
        name = match.group(2)
        out.append(
            ExecutionUnit(
                name=name,
                source_path=str(file_path),
                language="other",
                kind="class",
                loc=source.count("\n") + 1,
            )
        )
    return out


# ---------------------------------------------------------------------------
# EntryPoint annotations — @WebServlet / JAX-RS @Path / Spring MVC
# ---------------------------------------------------------------------------

# The route-annotation list. Each name maps to the abstract
# ``EntryPointKind.api_route``. ``@RequestMapping`` is included even
# though it can also annotate a class (acting as a path prefix);
# the regex picks up both occurrences and the analyzer can fold class-
# level prefixes when it cross-references method-level mappings.
_ROUTE_ANNOTATIONS: Final[tuple[str, ...]] = (
    "WebServlet",
    "Path",
    "GetMapping",
    "PostMapping",
    "PutMapping",
    "DeleteMapping",
    "PatchMapping",
    "RequestMapping",
    "Controller",
    "RestController",
)

# ``@AnnotationName`` optionally followed by parens containing the
# annotation arguments. The captured groups are the annotation name
# (group 1) and the argument body (group 2, possibly empty). The
# scanner downstream walks forward from each match to find the
# annotated symbol (class or method name).
_ROUTE_ANNOTATION_RE: Final[re.Pattern[str]] = re.compile(
    r"@(" + "|".join(_ROUTE_ANNOTATIONS) + r")\b(?:\s*\(([^)]*)\))?",
)

# Extract a string-literal value from an annotation argument body.
# Handles ``@Foo("value")`` and ``@Foo(value = "x")`` and
# ``@Foo(urlPatterns = "/foo")`` — all of which surface a string we
# want as the EntryPoint.name.
_ANNOTATION_STRING_RE: Final[re.Pattern[str]] = re.compile(
    r"""(?:(?:value|path|urlPatterns|name)\s*=\s*)?"([^"]+)\"""",
)

# Forward-scan target after an annotation match: ``class Foo`` /
# ``interface Foo`` / ``enum Foo`` (class-level annotation) or a method
# signature ``returnType methodName(...)`` (method-level annotation).
# We capture the trailing identifier — for a class match it's the type
# name; for a method match it's the method name.
_FOLLOWING_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    (?:(?:public|protected|private|abstract|final|static|default|synchronized)\s+)*  # modifiers
    (?:
        (?:class|interface|enum)\s+(\w+)                              # type decl
        |
        (?:[\w<>\[\],?\s.]+?)\s+(\w+)\s*\(                            # method sig
    )
    """,
    re.VERBOSE,
)


def _extract_route_target(
    source: str,
    end_offset: int,
) -> str | None:
    """Walk forward from ``end_offset`` to find the annotated symbol.

    Returns the next class / interface / enum identifier or method
    name. ``None`` means the annotation was orphaned (no following
    symbol in the file) — caller decides whether to drop the record
    or fall back to a synthesized placeholder.
    """
    tail = source[end_offset:]
    follow = _FOLLOWING_SYMBOL_RE.search(tail)
    if follow is None:
        return None
    return follow.group(1) or follow.group(2)


def _extract_entry_points(source: str) -> list[EntryPoint]:
    """Extract route / controller / servlet annotations as ``EntryPoint`` records.

    Each match produces one record. The record's ``name`` is:
    - the annotation's path / urlPatterns value if present
      (``@Path("/api")`` → ``"/api"``);
    - otherwise the annotated symbol's name (class or method) found by
      walking forward in the source.

    Records with neither a path value nor a following symbol are
    dropped — they would otherwise pollute the graph with anonymous
    entries.
    """
    out: list[EntryPoint] = []
    seen: set[tuple[str, str]] = set()
    for match in _ROUTE_ANNOTATION_RE.finditer(source):
        ann_name = match.group(1)
        ann_args = match.group(2) or ""

        # First, look for an explicit string value in the args.
        path_value: str | None = None
        if ann_args:
            string_match = _ANNOTATION_STRING_RE.search(ann_args)
            if string_match is not None:
                path_value = string_match.group(1)

        # Second, walk forward for the annotated symbol.
        symbol = _extract_route_target(source, match.end())

        # Pick the most specific name available.
        if path_value is not None:
            name = path_value
        elif symbol is not None:
            name = symbol
        else:
            # Orphaned annotation — skip.
            continue

        key = (ann_name, name)
        if key in seen:
            continue
        seen.add(key)

        out.append(EntryPoint(name=name, kind="api_route"))
    return out


# ---------------------------------------------------------------------------
# JPA entities — DataStore emission
# ---------------------------------------------------------------------------

# ``@Entity`` annotation. May be accompanied by a ``@Table(name="X")``
# annotation on the same class (immediately following or preceding the
# ``@Entity`` line). We capture both shapes.
_ENTITY_ANNOTATION_RE: Final[re.Pattern[str]] = re.compile(
    r"@Entity\b(?:\s*\([^)]*\))?",
)

# ``@Table(name = "users")`` — only the ``name`` parameter is captured
# (other parameters like ``schema``, ``catalog`` round-trip into the
# graph via a future ``DataStore.schema`` field).
_TABLE_ANNOTATION_RE: Final[re.Pattern[str]] = re.compile(
    r"""@Table\s*\(\s*name\s*=\s*"([^"]+)\"""",
)


def _extract_data_stores(source: str) -> list[DataStore]:
    """Extract ``@Entity`` (+ optional ``@Table``) declarations.

    Each ``@Entity``-annotated class produces one ``DataStore`` with
    ``kind="db_table"``. When the class also carries a ``@Table(name=X)``
    annotation, ``X`` is used as the ``DataStore.name``. Otherwise the
    next class identifier following the ``@Entity`` annotation is used
    (the JPA default-table convention — the table name equals the
    class's simple name when no ``@Table`` annotation is present).
    """
    out: list[DataStore] = []
    seen: set[str] = set()
    for match in _ENTITY_ANNOTATION_RE.finditer(source):
        # Look for a @Table annotation within a small window before or
        # after the @Entity match — both orderings are common in real
        # Java EE / Spring source.
        window_start = max(0, match.start() - 200)
        window_end = min(len(source), match.end() + 200)
        window = source[window_start:window_end]

        table_match = _TABLE_ANNOTATION_RE.search(window)
        if table_match is not None:
            name = table_match.group(1)
        else:
            # Fall back to the next class identifier.
            symbol = _extract_route_target(source, match.end())
            if symbol is None:
                continue
            name = symbol

        if name in seen:
            continue
        seen.add(name)

        out.append(DataStore(name=name, kind="db_table"))
    return out


# ---------------------------------------------------------------------------
# Batch units — @Scheduled / Quartz / Spring Batch
# ---------------------------------------------------------------------------

# Spring ``@Scheduled`` annotation on a method.
_SCHEDULED_RE: Final[re.Pattern[str]] = re.compile(
    r"@Scheduled\b(?:\s*\(([^)]*)\))?",
)

# Quartz ``implements Job`` (or ``extends QuartzJobBean``) on a class.
# We anchor on the class declaration line; the modifiers + the
# ``implements`` clause are captured in a single match to avoid
# false-positives on commented-out or partial declarations.
_QUARTZ_JOB_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    (?:public|protected|private|abstract|final|static|\s)+
    class\s+(\w+)
    (?:\s+extends\s+\w+)?
    \s+(?:implements\s+(?:[\w,\s]*?\bJob\b|[\w,\s]*?\bQuartzJobBean\b)
        |extends\s+QuartzJobBean)
    """,
    re.VERBOSE,
)

# Spring Batch ``@Bean public Step xStep(...)`` or
# ``@Bean public Job xJob(...)``. The annotation may appear on its own
# line preceding the method signature.
_BATCH_BEAN_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    @Bean\b(?:\s*\([^)]*\))?
    \s+
    (?:(?:public|protected|private|static|final)\s+)*
    (Step|Job)\s+(\w+)\s*\(
    """,
    re.VERBOSE,
)


def _extract_batch_units(source: str) -> list[BatchUnit]:
    """Extract @Scheduled / Quartz / Spring Batch declarations as BatchUnit records.

    Each match produces one ``BatchUnit`` with
    ``kind="scheduled_workflow"`` — the abstract Literal does not
    distinguish "scheduled trigger" from "job pipeline" at v1, so all
    three Java idioms fold to the same kind. Future ``BatchUnitKind``
    expansion (``quartz_job`` / ``spring_batch_step`` / ``cron_method``)
    would refine this without changing the per-flavor extractor.
    """
    out: list[BatchUnit] = []
    seen: set[str] = set()

    for match in _SCHEDULED_RE.finditer(source):
        symbol = _extract_route_target(source, match.end())
        if symbol is None:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        out.append(BatchUnit(name=symbol, kind="scheduled_workflow"))

    for match in _QUARTZ_JOB_RE.finditer(source):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        out.append(BatchUnit(name=name, kind="scheduled_workflow"))

    for match in _BATCH_BEAN_RE.finditer(source):
        # group(1) is "Step" or "Job"; group(2) is the bean method name.
        name = match.group(2)
        if name in seen:
            continue
        seen.add(name)
        out.append(BatchUnit(name=name, kind="scheduled_workflow"))

    return out


# ---------------------------------------------------------------------------
# UI elements — directory-level JSP / Thymeleaf discovery
# ---------------------------------------------------------------------------


def _detect_ui_elements(code_root: Path) -> list[UIElement]:
    """Walk ``code_root`` for JSP / Thymeleaf templates.

    Returns one ``UIElement`` per ``.jsp`` or ``.html`` file with
    ``kind="web_template"``. The ``.html`` discriminator is best-effort:
    a Thymeleaf template is conventionally ``.html`` (sometimes
    ``.thtml``) and lives under ``src/main/resources/templates/``;
    plain static ``.html`` files (typically under
    ``src/main/resources/static/``) are not UI templates in the Java
    EE sense. The walk distinguishes via path-prefix heuristic:
    ``.html`` files under a ``templates`` directory are included;
    those under ``static`` are excluded.

    This function is the carve-out described in the module docstring
    §"UI element extraction — directory-level, not per-file": the
    per-file ``parse()`` contract receives only ``.java`` content, so
    JSP / Thymeleaf templates must be discovered at the directory
    level. Phase B's polyglot aggregator (v1.1+) calls this once per
    code-root walk.

    Args:
        code_root: Directory to walk for templates.

    Returns:
        List of ``UIElement`` records, one per template file. Empty
        list when no templates exist or ``code_root`` does not exist.
    """
    out: list[UIElement] = []
    if not code_root.exists() or not code_root.is_dir():
        return out

    for candidate in sorted(code_root.rglob("*.jsp")):
        out.append(
            UIElement(
                name=candidate.stem,
                kind="web_template",
            )
        )

    for candidate in sorted(code_root.rglob("*.html")):
        # Heuristic: Thymeleaf templates live under .../templates/...;
        # plain static .html under .../static/... are not templates.
        parts = candidate.parts
        if "templates" in parts and "static" not in parts:
            out.append(
                UIElement(
                    name=candidate.stem,
                    kind="web_template",
                )
            )

    return out


# ---------------------------------------------------------------------------
# Struts Action / ActionForm detection in .java files
# ---------------------------------------------------------------------------

# A class that extends Action or ActionForm (or org.apache.struts.* equivalents).
# We use a regex that captures the class name to emit ExecutionUnit records and,
# for Action subclasses, an EntryPoint as well (the class is a request handler).
_STRUTS_ACTION_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    (?:public|protected|private|abstract|final|\s)*
    class\s+(\w+)
    [^{]*?
    \bextends\s+
    (?:org\.apache\.struts\.\w+\.)?
    (Action|ActionForm)\b
    """,
    re.VERBOSE | re.DOTALL,
)


def _extract_struts_java_units(
    source: str,
    file_path: Path,
) -> tuple[list[ExecutionUnit], list[EntryPoint]]:
    """Extract Struts ``Action`` / ``ActionForm`` subclasses from ``.java`` source.

    Each match produces an ``ExecutionUnit(kind="class")``.  For ``Action``
    subclasses (but *not* ``ActionForm``) an ``EntryPoint(kind="api_route")``
    is also emitted because an ``Action`` is a request handler; the class name
    is used as ``target_unit`` and as the ``name`` (no path is derivable
    from the Java source alone — the path lives in ``struts-config.xml``).

    ``ActionForm`` subclasses produce only an ``ExecutionUnit`` — they are
    data-binders, not handlers.
    """
    units: list[ExecutionUnit] = []
    entry_points: list[EntryPoint] = []
    seen_units: set[str] = set()
    seen_eps: set[str] = set()

    for match in _STRUTS_ACTION_RE.finditer(source):
        class_name = match.group(1)
        base_class = match.group(2)

        if class_name not in seen_units:
            seen_units.add(class_name)
            units.append(
                ExecutionUnit(
                    name=class_name,
                    source_path=str(file_path),
                    language="other",
                    kind="class",
                    loc=source.count("\n") + 1,
                )
            )

        if base_class == "Action" and class_name not in seen_eps:
            seen_eps.add(class_name)
            entry_points.append(
                EntryPoint(
                    name=class_name,
                    kind="api_route",
                    target_unit=class_name,
                )
            )

    return units, entry_points


# ---------------------------------------------------------------------------
# XML extractors — Struts config, Spring XML handler mappings, web.xml security
# ---------------------------------------------------------------------------

# struts-config.xml: ``<action path="/foo" type="com.x.FooAction">``
# We use a regex over raw XML rather than lxml (no-heavy-parser invariant).
_STRUTS_ACTION_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"""<action\b[^>]*\bpath\s*=\s*["']([^"']+)["'][^>]*\btype\s*=\s*["']([^"']+)["'][^>]*/?>""",
    re.IGNORECASE | re.DOTALL,
)
_STRUTS_ACTION_PATH_ALT_RE: Final[re.Pattern[str]] = re.compile(
    r"""<action\b[^>]*\btype\s*=\s*["']([^"']+)["'][^>]*\bpath\s*=\s*["']([^"']+)["'][^>]*/?>""",
    re.IGNORECASE | re.DOTALL,
)


def _extract_struts_config_entry_points(content: str) -> list[EntryPoint]:
    """Parse ``struts-config.xml`` action elements into ``EntryPoint`` records.

    Each ``<action path="/foo" type="com.x.FooAction">`` element produces one
    ``EntryPoint(kind="api_route", name=path, target_unit=type_fqn)``.  Both
    attribute orderings (``path`` before ``type`` and vice-versa) are handled.
    """
    if "struts-config" not in content:
        return []

    out: list[EntryPoint] = []
    seen: set[str] = set()

    # path-first ordering
    for match in _STRUTS_ACTION_PATH_RE.finditer(content):
        path, action_type = match.group(1), match.group(2)
        if path not in seen:
            seen.add(path)
            out.append(EntryPoint(name=path, kind="api_route", target_unit=action_type))

    # type-first ordering (distinct from path-first — group order flips)
    for match in _STRUTS_ACTION_PATH_ALT_RE.finditer(content):
        action_type, path = match.group(1), match.group(2)
        if path not in seen:
            seen.add(path)
            out.append(EntryPoint(name=path, kind="api_route", target_unit=action_type))

    return out


# Spring XML SimpleUrlHandlerMapping: ``<prop key="/url">beanName</prop>``
_SPRING_XML_PROP_RE: Final[re.Pattern[str]] = re.compile(
    r"""<prop\s+key\s*=\s*["']([^"']+)["']\s*>([^<]*)</prop>""",
    re.IGNORECASE,
)


def _extract_spring_xml_entry_points(content: str) -> list[EntryPoint]:
    """Parse Spring XML ``SimpleUrlHandlerMapping`` ``<prop>`` entries.

    Each ``<prop key="/url">beanName</prop>`` inside a file that mentions
    ``SimpleUrlHandlerMapping`` produces one
    ``EntryPoint(kind="api_route", name=url, target_unit=beanName)``.
    """
    if "SimpleUrlHandlerMapping" not in content:
        return []

    out: list[EntryPoint] = []
    seen: set[str] = set()
    for match in _SPRING_XML_PROP_RE.finditer(content):
        url = match.group(1).strip()
        bean = match.group(2).strip()
        if url not in seen and url:
            seen.add(url)
            out.append(EntryPoint(name=url, kind="api_route", target_unit=bean))
    return out


# web.xml security-constraint: blocks containing <url-pattern> and <role-name>.
# We extract the url-pattern + all role-names per constraint block, emitting
# one EntryPoint per url-pattern with security_requirements = list[role].
_SECURITY_CONSTRAINT_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"<security-constraint\b.*?</security-constraint>",
    re.IGNORECASE | re.DOTALL,
)
_URL_PATTERN_RE: Final[re.Pattern[str]] = re.compile(
    r"<url-pattern\b[^>]*>([^<]+)</url-pattern>",
    re.IGNORECASE,
)
_ROLE_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"<role-name\b[^>]*>([^<]+)</role-name>",
    re.IGNORECASE,
)


def _extract_webxml_security_entry_points(content: str) -> list[EntryPoint]:
    """Parse ``web.xml`` ``<security-constraint>`` blocks into ``EntryPoint`` records.

    For each ``<security-constraint>`` block: extracts all ``<url-pattern>``
    values and all ``<role-name>`` values. Emits one
    ``EntryPoint(kind="web_route", name=url_pattern,
    security_requirements=[role, ...])`` per url-pattern.  If a block has no
    ``<url-pattern>`` it is skipped.  Multiple url-patterns in a single block
    each produce their own record with the same role list (the mfx v0.1 behavior
    captured only the last url-pattern; we faithfully split per spec here).
    """
    if "security-constraint" not in content:
        return []

    out: list[EntryPoint] = []
    seen: set[str] = set()

    for block_match in _SECURITY_CONSTRAINT_BLOCK_RE.finditer(content):
        block = block_match.group(0)
        roles = [m.group(1).strip() for m in _ROLE_NAME_RE.finditer(block)]
        for url_match in _URL_PATTERN_RE.finditer(block):
            url = url_match.group(1).strip()
            if url and url not in seen:
                seen.add(url)
                out.append(
                    EntryPoint(
                        name=url,
                        kind="web_route",
                        security_requirements=roles,
                    )
                )
    return out


# ---------------------------------------------------------------------------
# Frontend extractors — JSP views + AngularJS client (port of mfx jsp.py /
# frontend_js.py)
# ---------------------------------------------------------------------------
#
# These mirror the backend XML/Java extractors above: regex-only (no lxml /
# no JS parser), faithful to the mfx ``extract/jsp.py`` + ``extract/
# frontend_js.py`` reference. They emit into the abstract entity vocabulary
# rather than mfx's ``ViewPage`` / consumed-REST dicts:
#
# * A JSP file → one ``UIElement(kind="web_template")`` for the view itself,
#   plus one ``EntryPoint(kind="api_route")`` per jQuery / raw-AJAX endpoint
#   the page calls (the URL is the ``name``). Form ``action`` targets become
#   ``EntryPoint(kind="web_route")``.
# * An AngularJS ``.js`` file (guarded on ``angular.module`` so plain JS is
#   not over-parsed) → one ``EntryPoint(kind="api_route")`` per ``$http`` /
#   ``$resource`` / ``$.ajax`` REST URL and per ``$routeProvider.when(...)``
#   route, plus one ``UIElement(kind="web_template")`` per ``templateUrl``
#   referenced template (the template stem is the ``name``).
#
# mfx cross-cutting extractors NOT ported here (validation rules, config
# files, navigation aggregation, the test harness shape) are the
# model-fallback / aggregator domain and out of scope for the per-file
# flavor parser.

# jQuery AJAX with an inline ``url:`` option:
# ``$.ajax({url: "/api/x"})`` / ``$.get({url: "/api/x"})`` / etc.
_JSP_AJAX_RE: Final[re.Pattern[str]] = re.compile(
    r'\$\.(?:ajax|get|post|getJSON)\s*\(\s*\{?[^)]*?url\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)
# jQuery short form with the URL as the first positional arg:
# ``$.get("/api/x")`` / ``$.post("/api/x")`` / ``$.getJSON("/api/x")``.
_JSP_AJAX_SHORT_RE: Final[re.Pattern[str]] = re.compile(
    r'\$\.(?:get|post|getJSON)\s*\(\s*"([^"]+)"',
    re.IGNORECASE,
)
# ``<form action="...">`` / ``<html:form action="...">`` — the action target.
_JSP_FORM_ACTION_RE: Final[re.Pattern[str]] = re.compile(
    r'<(?:html:)?form\b[^>]*\baction\s*=\s*"([^"]*)"',
    re.IGNORECASE,
)


def _parse_jsp(file_path: Path, content: str) -> dict[str, list[object]]:
    """Parse a ``.jsp`` / ``.jspx`` file — view UIElement + AJAX/form EntryPoints.

    Emits:

    * one ``UIElement(name=<file stem>, kind="web_template")`` for the JSP
      view itself;
    * one ``EntryPoint(name=<url>, kind="api_route")`` per jQuery / raw-AJAX
      endpoint (``$.ajax({url: ...})``, ``$.get("...")``, ``$.post(...)``,
      ``$.getJSON(...)``);
    * one ``EntryPoint(name=<action>, kind="web_route")`` per ``<form>`` /
      ``<html:form>`` ``action`` target (non-empty actions only).

    Includes / taglibs / scriptlet counts (which mfx's ``ViewPage`` carries)
    are not modelled in the abstract vocabulary, so they are not emitted —
    the migration-state detector already keys on the Struts taglib signal.
    """
    ui_elements: list[object] = [UIElement(name=file_path.stem, kind="web_template")]

    entry_points: list[object] = []
    seen: set[str] = set()

    for match in _JSP_AJAX_RE.finditer(content):
        url = match.group(1)
        if url and url not in seen:
            seen.add(url)
            entry_points.append(EntryPoint(name=url, kind="api_route"))
    for match in _JSP_AJAX_SHORT_RE.finditer(content):
        url = match.group(1)
        if url and url not in seen:
            seen.add(url)
            entry_points.append(EntryPoint(name=url, kind="api_route"))

    for match in _JSP_FORM_ACTION_RE.finditer(content):
        action = match.group(1).strip()
        if action and action not in seen:
            seen.add(action)
            entry_points.append(EntryPoint(name=action, kind="web_route"))

    return {
        "execution_units": [],
        "shared_components": [],
        "entry_points": entry_points,
        "data_stores": [],
        "batch_units": [],
        "ui_elements": ui_elements,
    }


# AngularJS module guard — a ``.js`` file is only parsed for Angular idioms
# when it declares ``angular.module(...)``; this keeps plain client JS from
# being over-parsed (an arbitrary ``$http`` in a non-Angular file is rare
# but the guard mirrors mfx's MODULE-presence intent).
_ANGULAR_MODULE_RE: Final[re.Pattern[str]] = re.compile(
    r"angular\.module\s*\(",
    re.IGNORECASE,
)
# ``$routeProvider.when("/path", { ... })`` route declarations. We capture
# the route path; the body (controller / templateUrl) is mined separately
# by the templateUrl regex below.
_ANGULAR_ROUTE_RE: Final[re.Pattern[str]] = re.compile(
    r"""\.when\(\s*['"]([^'"]+)['"]\s*,""",
    re.IGNORECASE,
)
# ``templateUrl: "views/x.html"`` — the referenced template.
_ANGULAR_TEMPLATE_RE: Final[re.Pattern[str]] = re.compile(
    r"""templateUrl\s*:\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
# ``$http.get('/api/y')`` / ``$http.post(...)`` / ``$http.put`` / ``.delete``.
_ANGULAR_HTTP_RE: Final[re.Pattern[str]] = re.compile(
    r"""\$http\.(?:get|post|put|delete)\(\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
# ``$resource('/api/z')`` REST factory.
_ANGULAR_RESOURCE_RE: Final[re.Pattern[str]] = re.compile(
    r"""\$resource\(\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
# ``$.ajax({url: '/api/w'})`` jQuery interop inside an Angular app.
_ANGULAR_JQ_AJAX_RE: Final[re.Pattern[str]] = re.compile(
    r"""\$\.ajax\s*\(\s*\{[^}]*?url\s*:\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)


def _stem(template_url: str) -> str:
    """Return the bare template name for a ``templateUrl`` reference.

    ``"views/home.html"`` → ``"home"``; ``"partial.html"`` → ``"partial"``.
    Mirrors the JSP ``file_path.stem`` convention so a UIElement name is the
    template's basename without directory or extension.
    """
    tail = template_url.rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[0] if "." in tail else tail


def _parse_angular(content: str, file_path: Path) -> dict[str, list[object]]:
    """Parse an AngularJS ``.js`` file — route/REST EntryPoints + template UIElements.

    Guarded on ``angular.module(...)`` presence: a ``.js`` file without it
    returns all-empty (plain client JS is not over-parsed). When the guard
    passes, emits:

    * one ``EntryPoint(name=<url>, kind="api_route")`` per ``$http`` /
      ``$resource`` / ``$.ajax`` REST URL the client consumes;
    * one ``EntryPoint(name=<path>, kind="api_route")`` per
      ``$routeProvider.when("/path", ...)`` route;
    * one ``UIElement(name=<template stem>, kind="web_template")`` per
      ``templateUrl: "..."`` referenced template.

    AngularJS templates are server-side HTML partials — ``"web_template"`` is
    the correct ``UIElementKind`` (``"react_component"`` would be wrong; this
    is not React).
    """
    if not _ANGULAR_MODULE_RE.search(content):
        return {key: [] for key in _EMPTY_PARSE_RESULT_WITH_UI}

    entry_points: list[object] = []
    seen_eps: set[str] = set()

    def _add_ep(url: str) -> None:
        if url and url not in seen_eps:
            seen_eps.add(url)
            entry_points.append(EntryPoint(name=url, kind="api_route"))

    for match in _ANGULAR_ROUTE_RE.finditer(content):
        _add_ep(match.group(1))
    for match in _ANGULAR_HTTP_RE.finditer(content):
        _add_ep(match.group(1))
    for match in _ANGULAR_RESOURCE_RE.finditer(content):
        _add_ep(match.group(1))
    for match in _ANGULAR_JQ_AJAX_RE.finditer(content):
        _add_ep(match.group(1))

    ui_elements: list[object] = []
    seen_ui: set[str] = set()
    for match in _ANGULAR_TEMPLATE_RE.finditer(content):
        name = _stem(match.group(1))
        if name and name not in seen_ui:
            seen_ui.add(name)
            ui_elements.append(UIElement(name=name, kind="web_template"))

    # ``file_path`` is accepted for parity with the JSP signature and future
    # source-path attribution; the per-file dispatch passes it uniformly.
    del file_path
    return {
        "execution_units": [],
        "shared_components": [],
        "entry_points": entry_points,
        "data_stores": [],
        "batch_units": [],
        "ui_elements": ui_elements,
    }


# ---------------------------------------------------------------------------
# Migration-state detection — whole-repo signals (port of mfx detect.py)
# ---------------------------------------------------------------------------


def detect_migration_state(code_root: Path) -> list[dict[str, str]]:
    """Detect migration-state signals across the repo rooted at ``code_root``.

    Faithful port of ``mfx.extract.detect.detect_migration_state``.  Walks
    ``code_root.rglob(...)`` for the relevant suffixes and returns a list of
    signal dicts, each with three string keys:

    * ``"signal"`` — machine-readable signal name (one of three values below)
    * ``"detail"`` — human-readable description
    * ``"file"`` — repo-relative path of the first file that triggered it

    The three signals:

    * ``"struts-config-with-spring"`` — a ``*.xml`` file contains
      ``struts-config`` AND a ``*.java`` file contains ``@Controller``
      (Struts config coexists with Spring — partial migration in progress).
    * ``"actionform-subclass"`` — a ``*.java`` file contains
      ``extends ActionForm`` (legacy Struts data-binder still present).
    * ``"struts-taglib-in-jsp"`` — a ``*.jsp`` / ``.jspx`` file contains
      ``struts.apache.org/tags`` (Struts tag library still in use in views).

    Results are returned in that deterministic order (same order the three
    checks appear in ``mfx.extract.detect``). Signals are omitted when not
    triggered. Empty list ⇒ no legacy Struts signals detected.

    Args:
        code_root: Directory to walk. Non-existent or non-directory paths
            produce an empty list (no crash).

    Returns:
        ``list[dict[str, str]]`` — zero to three signal dicts.
    """
    if not code_root.exists() or not code_root.is_dir():
        return []

    def _find_first(suffixes: tuple[str, ...], needle: str) -> Path | None:
        for suffix in suffixes:
            for p in sorted(code_root.rglob(f"*{suffix}")):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if needle in text:
                    return p
        return None

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(code_root))
        except ValueError:
            return str(p)

    signals: list[dict[str, str]] = []

    struts_cfg = _find_first((".xml",), "struts-config")
    has_spring = _find_first((".java",), "@Controller")
    if struts_cfg and has_spring:
        signals.append(
            {
                "signal": "struts-config-with-spring",
                "detail": "Struts config coexists with Spring @Controller",
                "file": _rel(struts_cfg),
            }
        )

    action_form_file = _find_first((".java",), "extends ActionForm")
    if action_form_file:
        signals.append(
            {
                "signal": "actionform-subclass",
                "detail": "Legacy Struts ActionForm subclass",
                "file": _rel(action_form_file),
            }
        )

    taglib_file = _find_first((".jsp", ".jspx"), "struts.apache.org/tags")
    if taglib_file:
        signals.append(
            {
                "signal": "struts-taglib-in-jsp",
                "detail": "Struts taglib used in JSP",
                "file": _rel(taglib_file),
            }
        )

    return signals


# ---------------------------------------------------------------------------
# Top-level per-file parse + detect
# ---------------------------------------------------------------------------

#: Return value for file suffixes the flavor does not handle. The five
#: backend keys are the historical (M5-FLAVOR-JAVA-EE / #527) shape — kept
#: verbatim so existing ``.java`` / ``.xml`` / unknown-suffix callers
#: round-trip unchanged.
_EMPTY_PARSE_RESULT: Final[dict[str, list[object]]] = {
    "execution_units": [],
    "shared_components": [],
    "entry_points": [],
    "data_stores": [],
    "batch_units": [],
}

#: Key set for the frontend parsers (#636 remainder). Adds the sixth
#: ``ui_elements`` key the backend parsers omit — JSP / AngularJS are the
#: only suffixes that emit UIElements per-file (``.java`` / ``.xml`` do not,
#: and the directory-level :func:`_detect_ui_elements` covers JSP/Thymeleaf
#: walks for callers that prefer the aggregate carve-out).
_EMPTY_PARSE_RESULT_WITH_UI: Final[dict[str, list[object]]] = {
    **_EMPTY_PARSE_RESULT,
    "ui_elements": [],
}


def _parse_java(file_path: Path, content: str) -> dict[str, list[object]]:
    """Parse a ``.java`` file — all five record types."""
    clean = _strip_comments(content)
    eu_list: list[ExecutionUnit] = list(_extract_execution_units(clean, file_path))
    ep_list: list[EntryPoint] = list(_extract_entry_points(clean))

    # Struts Action / ActionForm subclasses: the existing class extractor
    # already captures every ``class`` declaration (including Action / ActionForm
    # subclasses) as ExecutionUnit records, so we must NOT double-emit.  We
    # only ADD new ExecutionUnit records for classes that were missed (the regex
    # is anchored on ``extends Action|ActionForm`` which the generic
    # ``_TYPE_DECL_RE`` captures as well).  Therefore: extract Struts units,
    # filter out names already present in ``eu_list``, and append new ones.
    existing_unit_names = {u.name for u in eu_list}
    struts_units, struts_eps = _extract_struts_java_units(clean, file_path)
    for su in struts_units:
        if su.name not in existing_unit_names:
            eu_list.append(su)
            existing_unit_names.add(su.name)
    # Struts Action entry-points: deduplicate against existing EP names.
    existing_ep_names = {ep.name for ep in ep_list}
    for sep in struts_eps:
        if sep.name not in existing_ep_names:
            ep_list.append(sep)
            existing_ep_names.add(sep.name)

    return {
        "execution_units": list(eu_list),
        "shared_components": list(_extract_imports(clean)),
        "entry_points": list(ep_list),
        "data_stores": list(_extract_data_stores(clean)),
        "batch_units": list(_extract_batch_units(clean)),
    }


def _parse_xml(content: str) -> dict[str, list[object]]:
    """Parse an ``.xml`` file — entry-points only (no Java-source records)."""
    entry_points: list[object] = []
    entry_points.extend(_extract_struts_config_entry_points(content))
    entry_points.extend(_extract_spring_xml_entry_points(content))
    entry_points.extend(_extract_webxml_security_entry_points(content))
    return {
        "execution_units": [],
        "shared_components": [],
        "entry_points": entry_points,
        "data_stores": [],
        "batch_units": [],
    }


def parse_java_ee_source(
    file_path: Path,
    content: str,
) -> dict[str, list[object]]:
    """Parse one source file's content into category-keyed lists.

    Public per-file entry point matching the
    :data:`primitives._shared.discovery.flavor.FlavorParserCallable`
    alias. Dispatches on ``file_path.suffix.lower()``:

    * ``.java`` — full extraction (ExecutionUnit / SharedComponent /
      EntryPoint / DataStore / BatchUnit) plus Struts Action/ActionForm
      subclasses and EJB/stereotype classes (the existing class extractor
      already captures those; see ``_parse_java`` for the dedup discipline).
    * ``.xml`` — entry-point extraction only: Struts action paths,
      Spring XML ``SimpleUrlHandlerMapping`` handler mappings, and
      web.xml ``<security-constraint>`` routes.
    * ``.jsp`` / ``.jspx`` — frontend view extraction: one
      ``UIElement(kind="web_template")`` for the page plus
      ``EntryPoint`` records for jQuery/AJAX endpoints (``api_route``)
      and ``<form action>`` targets (``web_route``). Returns the six-key
      dict (adds ``ui_elements``).
    * ``.js`` — AngularJS client extraction *when* the file declares
      ``angular.module(...)``: ``EntryPoint(api_route)`` per
      ``$http`` / ``$resource`` / ``$.ajax`` REST URL and per
      ``$routeProvider.when(...)`` route, plus
      ``UIElement(web_template)`` per ``templateUrl``. Plain ``.js``
      (no ``angular.module``) returns the all-empty six-key dict.
    * other suffixes — returns the all-empty (five-key) dict.

    The ``.java`` / ``.xml`` / unknown-suffix paths produce the historical
    five-key dict (no ``ui_elements``); the JSP / AngularJS paths produce a
    six-key dict that additionally carries ``ui_elements``. Callers iterate
    the keys present rather than assuming a fixed set.

    Directory-level JSP / Thymeleaf discovery is also available via
    :func:`_detect_ui_elements` (the aggregate carve-out documented in the
    module docstring) for callers that prefer a single code-root walk.

    Args:
        file_path: Source file path (used to populate
            ``ExecutionUnit.source_path`` and for suffix dispatch).
        content: Raw file content. The aggregator reads + decodes the
            file before calling here, so we accept ``str`` not bytes.

    Returns:
        A dict with keys ``execution_units`` / ``shared_components`` /
        ``entry_points`` / ``data_stores`` / ``batch_units``, each
        mapping to a list of the corresponding Pydantic record. A
        category that produced no records returns an empty list.
    """
    suffix = file_path.suffix.lower()
    if suffix == ".java":
        return _parse_java(file_path, content)
    if suffix == ".xml":
        return _parse_xml(content)
    if suffix in (".jsp", ".jspx"):
        return _parse_jsp(file_path, content)
    if suffix == ".js":
        return _parse_angular(content, file_path)
    # Unknown suffix — fresh empty lists per call (never alias the shared
    # constant, so a caller appending to a returned list can't corrupt it).
    return {key: [] for key in _EMPTY_PARSE_RESULT}


# ---------------------------------------------------------------------------
# FlavorParser Protocol implementation
# ---------------------------------------------------------------------------


class JavaEEFlavor:
    """Java EE / Jakarta EE / Spring flavor pack.

    Implements the
    :class:`primitives._shared.discovery.flavor.FlavorParser`
    Protocol (three methods: :meth:`detect`, :meth:`parse`,
    :meth:`recommended_section_branches`). Registered into both
    :data:`FLAVORS` and :data:`FLAVOR_PACKS` via the package
    ``__init__.py`` so it appears alongside the built-in three
    (``cics_cobol`` / ``jcl_batch`` / ``ims_db``) without going through
    the entry-point loader.

    The Protocol contract: see module docstring + design spec
    §"The flavor-pack contract".
    """

    name: Final[str] = "java_ee"

    def detect(self, code_root: Path) -> Confidence:
        """Auto-detection heuristic — manifest sniff + ``.java`` presence.

        See module docstring §"Detection heuristic" for the rules.

        Args:
            code_root: Directory to inspect. The walk is cheap (manifest
                check + a single ``rglob("*.java")`` peek).

        Returns:
            Confidence Literal value. ``"high"`` when a build manifest
            *and* ``.java`` files exist; ``"medium"`` when only ``.java``
            files exist; ``"low"`` otherwise (including when
            ``code_root`` does not exist).
        """
        if not code_root.exists() or not code_root.is_dir():
            return "low"

        has_manifest = (
            (code_root / "pom.xml").is_file()
            or (code_root / "build.gradle").is_file()
            or (code_root / "build.gradle.kts").is_file()
        )

        # Cheap presence check — short-circuit on the first .java file
        # found. ``next(..., None)`` avoids materialising the full
        # iterator for repos with thousands of sources.
        has_java = next(code_root.rglob("*.java"), None) is not None

        if has_manifest and has_java:
            return "high"
        if has_java:
            return "medium"
        return "low"

    def parse(
        self,
        file_path: Path,
        content: str,
    ) -> dict[str, list[object]]:
        """Per-file dispatch — delegate to :func:`parse_java_ee_source`."""
        return parse_java_ee_source(file_path, content)

    def recommended_section_branches(self) -> list[str]:
        """Single token: ``"java_ee"``.

        The Phase C section renderers consult this list when picking
        per-flavor branches for §4 (Three-Layer Architecture), §7
        (Screen Navigation), §9 (Batch Processing), §10 (Security),
        §11 (Deployment), §12 (Build / Automation Layer), §15
        (Modernization Analysis) — see polyglot design spec table
        §"Per-flavor adaptation of the 16 sections".
        """
        return ["java_ee"]

    def detect_migration_state(self, code_root: Path) -> list[dict[str, str]]:
        """Thin wrapper around the module-level :func:`detect_migration_state`.

        Exposes migration-state detection as a method so callers holding a
        ``JavaEEFlavor`` instance can invoke it without importing the module
        function directly.

        Args:
            code_root: Directory to walk for migration signals.

        Returns:
            See :func:`detect_migration_state`.
        """
        return detect_migration_state(code_root)


__all__ = [
    "JavaEEFlavor",
    "detect_migration_state",
    "parse_java_ee_source",
]
