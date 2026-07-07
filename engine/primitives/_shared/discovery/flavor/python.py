"""M5 Phase B flavor pack: Python AST extractor (M5-FLAVOR-PYTHON, #526).

This module is the first non-mainframe flavor pack registered against the
M-EPIC-3 polyglot ``FlavorParser`` Protocol surface (#524 / PR #536). It
parses Python source files into the abstract entity vocabulary from
``shared/models.py`` (#522 / PR #534):

* ``ast.FunctionDef`` / ``ast.AsyncFunctionDef`` / ``ast.ClassDef`` →
  :class:`ExecutionUnit` records. ``kind="function"`` /
  ``kind="class"``. Methods nested inside a class are emitted as
  *additional* nested ``ExecutionUnit`` records (per-function fidelity
  outweighs the duplicate-counting cost — see design note below).
* ``ast.Import`` / ``ast.ImportFrom`` → :class:`SharedComponent` edges
  back-referenced from the importing module. Each unique imported
  module surfaces as one ``SharedComponent`` with ``kind="module"``
  (or ``"package"`` for dotted imports of ≥3 segments).
* FastAPI / Flask / Django / Click decorated handlers →
  :class:`EntryPoint` records. ``kind="api_route"`` for HTTP routes;
  ``kind="cli_command"`` for Click. Detection is decorator-name based
  (``@app.get`` / ``@router.post`` / ``@app.route`` / ``@click.command``)
  — this matches the design spec §"Per-flavor adaptation of the 16
  sections" §10 row.
* SQLAlchemy declarative-base subclasses (``class Foo(Base):``) with a
  ``__tablename__`` attribute → :class:`DataStore` with
  ``kind="db_table"``. Django ``models.Model`` subclasses similar.
  Pydantic ``BaseModel`` subclasses are explicitly **not** DataStores —
  they are DTOs (see Boundary cases below).
* Celery ``@shared_task`` / ``@app.task`` decorated functions →
  :class:`BatchUnit` records with ``kind="celery_beat"``. APScheduler
  decorators (``@scheduler.scheduled_job``) similar.

## Why AST not regex

The mainframe flavor (``cics_cobol``) ships a line-level regex pack
because COBOL has no widely-available pure-Python parser and the dialect
matrix (IBM CICS / Micro Focus / Tmaxsoft) varies enough that AST work
would balloon scope. Python has a real, batteries-included AST parser
(``ast.parse``) — using regex would be a strict downgrade. We use
``ast.parse(mode="exec")`` to handle module-level source verbatim, and
fall back to *recording an audit-shaped empty result* when a file is
syntactically invalid (the playbook synthesis should not crash on a
broken source file — Phase D surfaces the parse failure later).

## Class detection — nested ExecutionUnits

A ``class Foo:`` with methods ``bar`` and ``baz`` emits **three**
``ExecutionUnit`` records: the class itself (``kind="class"``) and the
two methods (``kind="function"``). The redundancy is deliberate:
downstream Phase C renderers (§5 Call Chain Maps, §11 Operational
Patterns) need per-method call-edge fidelity, and the analyzer's
de-dup pass collapses them by ``source_path``/``name`` when the §3
Program Inventory renders. This matches the design spec §"Abstract
entity vocabulary" §ExecutionUnit edge case for decorator-driven
entry-points emitting both a handler-function and the class wrapper.

## Detection heuristic

The :meth:`PythonFlavor.detect` method walks ``code_root`` for the
manifest-sniffing signals documented in the design spec §"Auto-detection
heuristic" table:

* ``high`` — ``pyproject.toml`` *and* ≥1 ``.py`` file under the tree.
* ``medium`` — any of ``requirements.txt`` / ``setup.py`` / ``Pipfile``
  *and* ≥1 ``.py`` file (per spec: "high if pyproject.toml; medium
  otherwise").
* ``low`` — otherwise (empty tree, mainframe sources, JS/Java only).

The walk is bounded to ``code_root`` (not recursive into sub-dirs) for
the manifest-sniffing part — we look for top-level ``pyproject.toml``
etc. The ``.py`` file check *is* recursive (one-level deep) so a
canonical layout (``pyproject.toml`` at the root, ``src/foo/main.py``
nested) registers as ``high``.

## Public entry points

The :class:`PythonFlavor` Protocol instance is what the flavor registry
holds. For the legacy ``FLAVORS`` callable dispatch (#524 backwards
compat), the module also exposes :func:`parse_python_source` —
mirroring the :func:`cics_cobol.parse_cobol_source` shape. The two paths
return the same record-keyed dict.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Final

from primitives._shared.discovery.models import (
    BatchUnit,
    DataStore,
    EntryPoint,
    ExecutionUnit,
    SharedComponent,
)

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import (
        Confidence,
        SharedComponentKind,
    )


# ---------------------------------------------------------------------------
# Constants — detection signal filenames + decorator catalogs
# ---------------------------------------------------------------------------

#: Top-level manifest files whose presence raises confidence to ``high``
#: when paired with ≥1 ``.py`` file. ``pyproject.toml`` is the PEP-621
#: canonical signal — its presence is the strongest indicator the tree is
#: a Python project (not just a tree that happens to contain a script).
_HIGH_CONFIDENCE_MANIFESTS: Final[frozenset[str]] = frozenset({"pyproject.toml"})

#: Top-level manifest files that raise confidence to ``medium`` when
#: paired with ≥1 ``.py`` file. These are the legacy / partial
#: manifests (no PEP-621 metadata block) — Python project signals but
#: not as definitive as a ``pyproject.toml``.
_MEDIUM_CONFIDENCE_MANIFESTS: Final[frozenset[str]] = frozenset(
    {"requirements.txt", "setup.py", "Pipfile"},
)

#: HTTP-method decorator suffix catalog. A decorator of the shape
#: ``@app.get(...)`` / ``@router.post(...)`` / ``@blueprint.delete(...)``
#: matches if the *attribute name* (the suffix after the final dot) is
#: in this set. Case-sensitive — Python style mandates lowercase HTTP
#: methods. The spec also covers ``@app.route(...)`` (Flask) which is
#: handled separately because it carries the method in kwargs.
_HTTP_METHOD_NAMES: Final[frozenset[str]] = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options"},
)

#: Flask-style decorator suffix that takes the HTTP method as a kwarg
#: (``@app.route("/", methods=["GET"])``) rather than as the decorator
#: name itself. Recorded separately so the EntryPoint emitter can pull
#: the method from the call kwargs.
_FLASK_ROUTE_NAMES: Final[frozenset[str]] = frozenset({"route"})

#: Celery-task decorator names. ``@shared_task`` is the cross-app form;
#: ``@app.task`` is the per-app form. Both flag the decorated function
#: as a :class:`BatchUnit` with ``kind="celery_beat"`` (the Celery
#: scheduler is the canonical Python ``celery_beat`` kind value).
_CELERY_TASK_NAMES: Final[frozenset[str]] = frozenset({"shared_task", "task"})

#: APScheduler decorator names. ``@scheduler.scheduled_job`` is the
#: canonical decorator; ``@scheduler.cron`` covers the cron-shorthand
#: form. Treated as ``celery_beat`` for kind-uniformity (the abstract
#: vocabulary closes ``BatchUnitKind`` at four values; APScheduler is
#: not its own kind).
_APSCHEDULER_TASK_NAMES: Final[frozenset[str]] = frozenset(
    {"scheduled_job", "cron"},
)

#: Click decorator names that mark a function as a CLI command.
#: ``@click.command()`` is the canonical single-command form;
#: ``@click.group()`` declares a command group whose members become
#: subcommands. Both surface as ``kind="cli_command"`` EntryPoints.
_CLICK_COMMAND_NAMES: Final[frozenset[str]] = frozenset({"command", "group"})

#: SQLAlchemy / Django base-class names that mark a subclass as a
#: persistent DataStore. The detection is *base-class identifier*
#: matching — we look at the ``bases`` of an ``ast.ClassDef`` for any of
#: these names. SQLAlchemy declarative-base patterns use ``Base`` (the
#: convention) or ``DeclarativeBase`` (the newer typed form); Django
#: uses ``models.Model``.
_ORM_BASE_NAMES: Final[frozenset[str]] = frozenset(
    {"Base", "DeclarativeBase", "Model"},
)

#: Pydantic base-class names that explicitly **disqualify** a class
#: from being a DataStore. A class subclassing ``BaseModel`` /
#: ``RootModel`` is a DTO, not a persistent store — even if it
#: happens to define a ``__tablename__`` attribute (which is
#: vanishingly rare for Pydantic but worth the explicit guard).
_PYDANTIC_BASE_NAMES: Final[frozenset[str]] = frozenset(
    {"BaseModel", "RootModel"},
)

#: Minimum dotted-segment count to classify a ``SharedComponent`` as a
#: ``"package"`` rather than a ``"module"``. ``django.db.models`` (3
#: segments) is a package; ``app.utils`` (2 segments) is a module.
#: Tuned to match the closed-Literal ``SharedComponentKind`` semantics
#: from #522.
_PACKAGE_SEGMENT_THRESHOLD: Final[int] = 3


# ---------------------------------------------------------------------------
# Category keys — the seven record buckets the parse() return dict carries
# ---------------------------------------------------------------------------

#: Category-key catalog the :meth:`PythonFlavor.parse` return dict
#: populates. Keys map 1:1 to the abstract entity vocabulary field names
#: on :class:`AnalysisDigest` (#522 / PR #534), so the Phase B
#: aggregator can ``digest.execution_units.extend(parsed["execution_units"])``
#: without any per-flavor branching.
_CATEGORY_KEYS: Final[tuple[str, ...]] = (
    "execution_units",
    "shared_components",
    "entry_points",
    "data_stores",
    "batch_units",
)


# ---------------------------------------------------------------------------
# Helpers — AST decorator inspection
# ---------------------------------------------------------------------------


def _decorator_attr_name(decorator: ast.expr) -> str | None:
    """Return the decorator's terminal attribute name, if any.

    ``@app.get(...)``        → ``"get"``
    ``@router.post(...)``    → ``"post"``
    ``@click.command(...)``  → ``"command"``
    ``@shared_task``         → ``"shared_task"`` (bare name; treated uniformly)
    ``@shared_task()``       → ``"shared_task"`` (called-with-parens form)
    ``@app.task``            → ``"task"``
    ``@functools.cache``     → ``"cache"``

    Returns ``None`` for shapes we don't recognise (lambda decorators,
    subscripted calls, etc.) — keeping the caller's matching logic
    explicit about "this decorator does not match any known framework".
    """
    # @decorator or @decorator() — strip the Call wrapper if present
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _decorator_call_kwargs(decorator: ast.expr) -> dict[str, ast.expr]:
    """Return the kwargs from a ``@decorator(...)`` invocation.

    Empty dict for bare ``@decorator`` / shapes we cannot introspect.
    Used by the Flask path to pull ``methods=["GET", "POST"]`` from
    ``@app.route("/", methods=["GET", "POST"])``.
    """
    if not isinstance(decorator, ast.Call):
        return {}
    return {kw.arg: kw.value for kw in decorator.keywords if kw.arg is not None}


def _decorator_first_positional_string(decorator: ast.expr) -> str | None:
    """Return the first positional arg if it's a string literal.

    ``@app.get("/items")`` → ``"/items"``
    ``@click.command("foo")`` → ``"foo"``
    ``@scheduler.scheduled_job("cron", hour=12)`` → ``"cron"``

    Returns ``None`` for shapes that can't be resolved at parse time
    (a variable reference, an f-string with embedded expressions, etc.).
    """
    if not isinstance(decorator, ast.Call) or not decorator.args:
        return None
    first = decorator.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _class_has_tablename(class_def: ast.ClassDef) -> bool:
    """Return True iff the class body defines a ``__tablename__`` assignment.

    SQLAlchemy declarative-base subclasses register against a table via
    ``__tablename__ = "users"``. The presence of this assignment is the
    canonical signal we're looking at a persistent-store-backing class
    rather than a plain data class.
    """
    for stmt in class_def.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "__tablename__":
                    return True
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == "__tablename__"
        ):
            return True
    return False


def _class_tablename_value(class_def: ast.ClassDef) -> str | None:
    """Return the string literal ``__tablename__`` value, if statically resolvable.

    ``__tablename__ = "users"`` → ``"users"``. Returns ``None`` if the
    assignment is dynamic (``__tablename__ = compute_name()``), in which
    case the caller falls back to the class name.
    """
    for stmt in class_def.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "__tablename__"
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    return stmt.value.value
    return None


def _class_base_names(class_def: ast.ClassDef) -> list[str]:
    """Return the simple-name list of base classes for an ``ast.ClassDef``.

    ``class Foo(Base):`` → ``["Base"]``
    ``class Foo(models.Model):`` → ``["Model"]`` (terminal attribute)
    ``class Foo(Base, Mixin):`` → ``["Base", "Mixin"]``
    ``class Foo(Generic[T]):`` → ``["Generic"]``

    Used by the DataStore detection to spot SQLAlchemy / Django ORM
    base classes, and by the Pydantic-disqualifier to spot DTOs.
    """
    names: list[str] = []
    for base in class_def.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
        elif isinstance(base, ast.Subscript) and isinstance(base.value, ast.Name):
            names.append(base.value.id)
    return names


# ---------------------------------------------------------------------------
# Extractors — one per category
# ---------------------------------------------------------------------------


def _module_name_from_path(path: Path) -> str:
    """Return a stable module identifier for ``path``.

    The flavor pack receives ``path`` as the file path the analyzer was
    iterating; we don't have the full ``code_root`` here, so we fall
    back to the file's stem. The downstream aggregator (Phase B) is
    responsible for promoting this to a fully-qualified dotted path
    using the recon-known package root.
    """
    return path.stem


def extract_execution_units(
    tree: ast.Module,
    path: Path,
) -> list[ExecutionUnit]:
    """Extract :class:`ExecutionUnit` records from a parsed Python module.

    Module-level functions, async functions, and classes each become one
    ``ExecutionUnit``. Methods nested inside a class become *additional*
    nested ``ExecutionUnit`` records — per-function fidelity is the
    design choice (see module docstring §"Class detection — nested
    ExecutionUnits").

    ``loc`` is computed as the inclusive end-line minus start-line + 1.
    ``shared_component_deps`` is left empty here — the import-pass
    populates it via cross-reference in Phase B's aggregator.
    """
    source_path = str(path)
    units: list[ExecutionUnit] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            units.append(
                ExecutionUnit(
                    name=node.name,
                    source_path=source_path,
                    language="python",
                    kind="function",
                    loc=_loc_span(node),
                )
            )
        elif isinstance(node, ast.ClassDef):
            units.append(
                ExecutionUnit(
                    name=node.name,
                    source_path=source_path,
                    language="python",
                    kind="class",
                    loc=_loc_span(node),
                )
            )
            # Nested methods — surface as per-function ExecutionUnits
            for inner in node.body:
                if isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef):
                    units.append(
                        ExecutionUnit(
                            name=f"{node.name}.{inner.name}",
                            source_path=source_path,
                            language="python",
                            kind="function",
                            loc=_loc_span(inner),
                        )
                    )
    return units


def _loc_span(node: ast.stmt) -> int:
    """Return the inclusive line count for an AST node.

    ``end_lineno`` is set by ``ast.parse`` in Python 3.8+; the fallback
    of ``lineno`` handles the (rare) case of a synthetic node without
    an end position.
    """
    end_lineno = getattr(node, "end_lineno", None) or node.lineno
    return max(end_lineno - node.lineno + 1, 1)


def extract_shared_components(
    tree: ast.Module,
    path: Path,
) -> list[SharedComponent]:
    """Extract :class:`SharedComponent` edges from import statements.

    Each ``ast.Import`` and ``ast.ImportFrom`` statement contributes
    one ``SharedComponent`` per imported module path. The current
    module's identifier (derived from ``path.stem``) is added to
    ``used_by_units`` so the back-edge cross-reference Phase B
    relies on is already partially built.

    Dotted imports of ≥3 segments are classified as
    ``kind="package"`` (e.g. ``import django.db.models``);
    one or two segments are ``kind="module"``. This matches the
    closed ``SharedComponentKind`` Literal — ``library`` and
    ``copybook`` are reserved for non-Python flavors.
    """
    importer = _module_name_from_path(path)
    components: dict[str, SharedComponent] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _record_component(components, alias.name, importer)
        # ImportFrom: ``from package.module import X, Y``
        # The module name is in ``node.module`` (None for relative
        # bare ``from . import X``); we record only when resolvable.
        elif isinstance(node, ast.ImportFrom) and node.module:
            _record_component(components, node.module, importer)
    return list(components.values())


def _record_component(
    sink: dict[str, SharedComponent],
    name: str,
    importer: str,
) -> None:
    """Insert-or-extend a :class:`SharedComponent` record in ``sink``.

    ``kind`` is inferred from the dotted-segment count: ≥3 segments
    indicates a package nest (``django.db.models``); ≤2 indicates a
    flat module (``json`` / ``my_app.utils``).
    """
    segments = name.split(".")
    kind: SharedComponentKind = (
        "package" if len(segments) >= _PACKAGE_SEGMENT_THRESHOLD else "module"
    )
    if name in sink:
        # First-occurrence wins on kind; back-reference accumulates
        existing = sink[name]
        if importer not in existing.used_by_units:
            sink[name] = SharedComponent(
                name=existing.name,
                kind=existing.kind,
                source_path=existing.source_path,
                used_by_units=[*existing.used_by_units, importer],
                field_definitions=list(existing.field_definitions),
            )
    else:
        sink[name] = SharedComponent(
            name=name,
            kind=kind,
            source_path="",
            used_by_units=[importer],
            field_definitions=[],
        )


def extract_entry_points(
    tree: ast.Module,
    path: Path,
) -> list[EntryPoint]:
    """Extract :class:`EntryPoint` records from decorated handlers.

    Walks every ``FunctionDef`` / ``AsyncFunctionDef`` (module-level
    and class-method-level alike) and tests each decorator against the
    framework catalogs:

    * ``@app.get(...)`` / ``@router.post(...)`` → ``kind="api_route"``
    * ``@app.route(...)`` → ``kind="api_route"`` (Flask shape)
    * ``@click.command(...)`` / ``@click.group(...)`` →
      ``kind="cli_command"``

    Django URLconf paths (``urlpatterns = [path("foo", view), ...]``)
    are best-effort: the design spec defers the URLconf-specific scan
    to a follow-up. The current pass surfaces decorator-shaped Django
    views (``@api_view(["GET"])``) under ``api_route``.
    """
    entry_points: list[EntryPoint] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            ep = _decorator_to_entry_point(decorator, node.name, path)
            if ep is not None:
                entry_points.append(ep)
    return entry_points


def _decorator_to_entry_point(
    decorator: ast.expr,
    handler_name: str,
    path: Path,
) -> EntryPoint | None:
    """Classify one decorator. Return ``None`` if it doesn't match a framework.

    The matching is *suffix-based*: we examine the decorator's terminal
    attribute name only, so both ``@app.get`` and ``@router.get`` and
    ``@blueprint.get`` route into ``api_route`` without us having to
    enumerate every framework's app-builder convention. The
    ``target_unit`` field is the handler function's name, prefixed
    with the source path's stem for traceability.
    """
    attr = _decorator_attr_name(decorator)
    if attr is None:
        return None
    module_id = _module_name_from_path(path)
    target_unit = f"{module_id}.{handler_name}"
    if attr in _HTTP_METHOD_NAMES:
        # FastAPI / Starlette / generic ``@app.get("/path")`` shape
        route_path = _decorator_first_positional_string(decorator) or ""
        name = f"{attr.upper()} {route_path}" if route_path else f"{attr.upper()} {handler_name}"
        return EntryPoint(name=name, kind="api_route", target_unit=target_unit)
    if attr in _FLASK_ROUTE_NAMES:
        # Flask ``@app.route("/path", methods=["GET"])`` shape — the
        # method lives in kwargs, not the decorator name.
        route_path = _decorator_first_positional_string(decorator) or ""
        kwargs = _decorator_call_kwargs(decorator)
        methods_node = kwargs.get("methods")
        methods = _coerce_string_list(methods_node) if methods_node is not None else ["GET"]
        # Combine multiple methods into a comma-joined name suffix so
        # the EntryPoint name uniquely identifies the route+method pair.
        method_label = ",".join(sorted(methods)) if methods else "GET"
        name = f"{method_label} {route_path}" if route_path else f"{method_label} {handler_name}"
        return EntryPoint(name=name, kind="api_route", target_unit=target_unit)
    if attr in _CLICK_COMMAND_NAMES:
        cmd_name = _decorator_first_positional_string(decorator) or handler_name
        return EntryPoint(
            name=cmd_name,
            kind="cli_command",
            target_unit=target_unit,
        )
    return None


def _coerce_string_list(node: ast.expr) -> list[str]:
    """Return the list of string-literal members of an ``ast.List`` / ``ast.Tuple``.

    ``["GET", "POST"]`` → ``["GET", "POST"]``. Returns ``[]`` for shapes
    that don't statically resolve (a name reference, a list comp, …).
    """
    if not isinstance(node, ast.List | ast.Tuple):
        return []
    out: list[str] = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            out.append(elt.value)
    return out


def extract_data_stores(
    tree: ast.Module,
    path: Path,
) -> list[DataStore]:
    """Extract :class:`DataStore` records from ORM model class definitions.

    A class is a DataStore iff:

    * Its base-class list contains one of :data:`_ORM_BASE_NAMES`
      (``Base`` / ``DeclarativeBase`` / ``Model``).
    * Its base-class list does **not** contain a Pydantic base
      (``BaseModel`` / ``RootModel``) — Pydantic models are DTOs even
      when they happen to also subclass ``Base`` for some odd reason.
    * For SQLAlchemy (``Base`` / ``DeclarativeBase``) it must declare
      ``__tablename__`` somewhere in its body. Django models
      (``models.Model``) skip the ``__tablename__`` check because
      Django infers the table name from the class name + app label.

    The ``name`` field is the table name (or class name fallback);
    ``kind="db_table"``. ``accessed_by`` is left empty here — the
    Phase B aggregator builds the matrix from cross-referenced
    function bodies in a later pass.
    """
    module_id = _module_name_from_path(path)
    stores: list[DataStore] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = _class_base_names(node)
        if any(b in _PYDANTIC_BASE_NAMES for b in bases):
            continue
        if not any(b in _ORM_BASE_NAMES for b in bases):
            continue
        # SQLAlchemy ``Base`` / ``DeclarativeBase`` require __tablename__;
        # Django ``Model`` does not.
        is_django = "Model" in bases and "Base" not in bases and "DeclarativeBase" not in bases
        if not is_django and not _class_has_tablename(node):
            continue
        table_name = _class_tablename_value(node) or node.name
        stores.append(
            DataStore(
                name=table_name,
                kind="db_table",
                accessed_by={module_id: set()},
            )
        )
    return stores


def extract_batch_units(
    tree: ast.Module,
    path: Path,
) -> list[BatchUnit]:
    """Extract :class:`BatchUnit` records from Celery / APScheduler decorators.

    Walks every top-level ``FunctionDef`` / ``AsyncFunctionDef`` and
    tests each decorator against the scheduler catalogs:

    * ``@shared_task`` / ``@app.task`` → ``kind="celery_beat"``
    * ``@scheduler.scheduled_job(...)`` / ``@scheduler.cron(...)`` →
      ``kind="celery_beat"`` (APScheduler is collapsed into the same
      kind for the closed-Literal vocabulary).

    The ``steps`` field carries one ``(function-name, cron-expr)``
    tuple — the cron expression is extracted from the decorator's
    first positional argument when statically resolvable, else left
    empty.
    """
    module_id = _module_name_from_path(path)
    units: list[BatchUnit] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            attr = _decorator_attr_name(decorator)
            if attr is None:
                continue
            if attr in _CELERY_TASK_NAMES or attr in _APSCHEDULER_TASK_NAMES:
                expr = _decorator_first_positional_string(decorator) or ""
                units.append(
                    BatchUnit(
                        name=f"{module_id}.{node.name}",
                        kind="celery_beat",
                        steps=[(node.name, expr)],
                    )
                )
    return units


# ---------------------------------------------------------------------------
# Public Protocol implementation
# ---------------------------------------------------------------------------


class PythonFlavor:
    """:class:`FlavorParser` Protocol implementation for Python source trees.

    Three public methods satisfy the M-EPIC-3 contract (`docs/superpowers/
    specs/2026-06-03-polyglot-discovery-design.md` §"The flavor-pack
    contract"):

    * :meth:`detect` — manifest-sniffing auto-detect.
    * :meth:`parse` — per-file AST walk emitting category-keyed records.
    * :meth:`recommended_section_branches` — hint list the §12 Build /
      Automation Layer renderer (PR #537) consults.

    The class is stateless; one instance suffices for the whole Phase B
    walk. The registry instantiates it with no constructor arguments.
    """

    name: Final[str] = "python"

    def detect(self, code_root: Path) -> Confidence:
        """Return the auto-detection confidence for ``code_root``.

        See module docstring §"Detection heuristic" for the full
        decision matrix. Recurses one level into ``code_root`` looking
        for ``.py`` files — handles the canonical ``src/`` layout
        without false-negatives.
        """
        if not code_root.exists() or not code_root.is_dir():
            return "low"
        # Walk only top-level + 1-level subdirs for the .py existence check.
        # Bounded depth keeps detect() cheap as the spec requires.
        has_python_file = _has_python_file(code_root)
        if not has_python_file:
            return "low"
        top_level = {p.name for p in code_root.iterdir() if p.is_file()}
        if top_level & _HIGH_CONFIDENCE_MANIFESTS:
            return "high"
        if top_level & _MEDIUM_CONFIDENCE_MANIFESTS:
            return "medium"
        return "low"

    def parse(
        self,
        file_path: Path,
        content: str,
    ) -> dict[str, list[object]]:
        """Parse one Python source file's content into category-keyed lists.

        Delegates to :func:`parse_python_source` so the legacy
        ``FLAVORS`` callable dispatch (a bare function entry) and the
        Protocol instance method share one implementation.
        """
        return parse_python_source(file_path, content)

    def recommended_section_branches(self) -> list[str]:
        """Return ``["python"]`` — the §12 + per-flavor renderer branch token."""
        return ["python"]


# ---------------------------------------------------------------------------
# Public bare-callable entry — legacy FLAVORS dispatch path
# ---------------------------------------------------------------------------


def parse_python_source(
    path: Path,
    content: str,
) -> dict[str, list[object]]:
    """Parse one Python source file's content into category-keyed lists.

    Public bare-callable entry mirroring the
    :func:`cics_cobol.parse_cobol_source` shape. The Phase B aggregator
    (legacy callable dispatch) drives this signature directly via
    ``FLAVORS["python"](source, content)``.

    The five record categories (see :data:`_CATEGORY_KEYS`) are
    extracted in one AST walk:

    * ``execution_units`` — module-level function / class defs + nested
      methods.
    * ``shared_components`` — distinct imports, back-referenced from
      this module.
    * ``entry_points`` — FastAPI / Flask / Click decorated handlers.
    * ``data_stores`` — SQLAlchemy / Django ORM model classes.
    * ``batch_units`` — Celery / APScheduler decorated tasks.

    A syntactically-invalid source file returns the empty category dict
    rather than raising — Phase D's auditor surfaces the parse failure
    as a ``citation_missing`` finding later. Letting the analyzer crash
    here would block the whole playbook synthesis on one broken file.

    Args:
        path: Source file path (used for ``ExecutionUnit.source_path``
            + ``SharedComponent.used_by_units`` cross-reference).
        content: Raw file content (the aggregator reads + decodes the
            file before calling here, so we accept ``str`` not bytes).

    Returns:
        A dict with the five keys from :data:`_CATEGORY_KEYS`. Each
        value is a list of the corresponding abstract entity records.
    """
    try:
        tree = ast.parse(content, filename=str(path), mode="exec")
    except SyntaxError:
        return {key: [] for key in _CATEGORY_KEYS}
    return {
        "execution_units": list(extract_execution_units(tree, path)),
        "shared_components": list(extract_shared_components(tree, path)),
        "entry_points": list(extract_entry_points(tree, path)),
        "data_stores": list(extract_data_stores(tree, path)),
        "batch_units": list(extract_batch_units(tree, path)),
    }


# ---------------------------------------------------------------------------
# Helpers — detect() recursive .py scan
# ---------------------------------------------------------------------------


def _has_python_file(root: Path, max_depth: int = 4) -> bool:
    """Return True iff any ``.py`` file lives under ``root`` within
    ``max_depth`` directory levels.

    Bounded depth keeps :meth:`PythonFlavor.detect` cheap on large
    monorepos. The default depth of 4 covers the canonical layouts:
    ``src/<pkg>/main.py`` (depth 2), ``src/<pkg>/<subpkg>/x.py``
    (depth 3), and one extra level of headroom.

    Iteration skips hidden directories (``.git``, ``.venv``, …) and
    common build / cache directories (``__pycache__``, ``node_modules``,
    ``build``, ``dist``) so the scan completes in O(project) rather
    than O(installed-deps).
    """
    skip_dir_names = {
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        ".tox",
        ".nox",
        ".venv",
        "venv",
        ".eggs",
        "site-packages",
    }
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if entry.is_file() and entry.suffix == ".py":
                return True
            if entry.is_dir() and depth < max_depth:
                if entry.name.startswith(".") or entry.name in skip_dir_names:
                    continue
                stack.append((entry, depth + 1))
    return False


__all__ = [
    "PythonFlavor",
    "extract_batch_units",
    "extract_data_stores",
    "extract_entry_points",
    "extract_execution_units",
    "extract_shared_components",
    "parse_python_source",
]
