"""Data-structural layer renderer — schema/structural-integrity probes per backend kind.

Emits structural test skeletons for every resolved data backend, dispatching by kind:

**SQL** (postgres/mysql/mssql/oracle):

1. ``tests/container/test_structural_migration[_<backend>].py`` — an up→down→up
   migration reversibility probe using the detected migration tool (Alembic for
   Python when ``alembic`` appears in deps or an ``alembic.ini`` exists; generic
   otherwise).
2. ``tests/container/test_structural_integrity[_<backend>].py`` — FK / uniqueness /
   not-null / check + orphan-row referential-integrity probes.

**MongoDB** (kind == ``"mongodb"``):

3. ``tests/container/test_structural_mongodb[_<backend>].py`` — collection JSON-schema
   validator existence + index presence. Relational migration reversibility and FK
   referential integrity are **Not applicable** (document-store).

**DynamoDB** (kind == ``"dynamodb"``):

4. ``tests/container/test_structural_dynamodb[_<backend>].py`` — table key-schema
   (partition/sort key) + GSI existence. Uses ``get_container_host_ip()`` +
   ``get_exposed_port(<port>)`` (no ``get_connection_url``). Relational migration
   reversibility and FK referential integrity are **Not applicable** (NoSQL key-value).

All files share the container layer's ``db_container[_<backend>]`` fixture so
``--data-tests`` (which implies ``container``) is the typical entry point. The ``multi``
flag is computed on the **full** resolved backend set so fixture names match the
container layer exactly.

All emitted tests are collectable-green: module-level imports are ``pytest``-only; the
real DB interaction is shown as ``# Replace with ...`` comments; a cheap-and-true
assertion makes the test pass when Docker is present and skip (via the fixture) otherwise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives._sdet.stacks import data_backend_meta
from primitives.test_scaffold.shared.layers._data_select import resolve_backends
from primitives.test_scaffold.shared.layers._emphasis import with_emphasis
from primitives.test_scaffold.shared.registry import (
    ScaffoldFile,
    ScaffoldResult,
    register_scaffold,
)

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.test_scaffold.shared.registry import ScaffoldContext


def _detect_migration_tool(project_root: Path) -> str:
    """Return 'alembic' when Alembic is detectable in the project, else 'migration-tool'."""
    for pattern in ("pyproject.toml", "requirements*.txt", "setup.cfg", "setup.py"):
        for f in sorted(project_root.glob(pattern)):
            if f.is_symlink() or not f.is_file():
                continue
            try:
                if "alembic" in f.read_text(encoding="utf-8", errors="ignore").lower():
                    return "alembic"
            except OSError:
                continue
    if (project_root / "alembic.ini").is_file():
        return "alembic"
    return "migration-tool"


# ---------------------------------------------------------------------------
# Python skeletons — SQL
# ---------------------------------------------------------------------------


def _py_migration(fixture_name: str, fn_name: str, migration_tool: str) -> str:
    if migration_tool == "alembic":
        tool_note = "Alembic (detected in project dependencies)"
        tool_cmds = (
            "    # Replace with real Alembic reversibility check:\n"
            "    # import subprocess  # noqa: PLC0415\n"
            f"    # url = {fixture_name}.get_connection_url()\n"
            "    # subprocess.run(['alembic', '-x', f'db_url={url}', 'upgrade', 'head'],"
            " check=True)\n"
            "    # subprocess.run(['alembic', '-x', f'db_url={url}', 'downgrade', 'base'],"
            " check=True)\n"
            "    # subprocess.run(['alembic', '-x', f'db_url={url}', 'upgrade', 'head'],"
            " check=True)\n"
        )
    else:
        tool_note = "your migration tool"
        tool_cmds = (
            "    # Replace with your migration tool's up→down→up reversibility check:\n"
            f"    # url = {fixture_name}.get_connection_url()\n"
            "    # run_migrations(url, direction='up')\n"
            "    # run_migrations(url, direction='down')\n"
            "    # run_migrations(url, direction='up')\n"
        )

    return (
        f'"""Migration reversibility test (structural layer, /test-scaffold).\n\n'
        f"Verifies that {tool_note} migrations are fully reversible (up→down→up).\n"
        f"Requires Docker + the container layer's db_container fixture; self-skips\n"
        f"without Docker (collectable-green).\n"
        f'"""\n\n'
        f"from __future__ import annotations\n\n"
        f"import pytest\n\n\n"
        f"@pytest.mark.container\n"
        f"def {fn_name}({fixture_name}) -> None:\n"
        f"    url = {fixture_name}.get_connection_url()\n"
        f"    assert url  # container is live (or the fixture skipped)\n"
        f"{tool_cmds}"
    )


def _py_integrity(fixture_name: str, fn_name: str) -> str:
    return (
        '"""Referential-integrity test (structural layer, /test-scaffold).\n\n'
        "Probes FK constraints, uniqueness, NOT NULL, CHECK + orphan-row invariants\n"
        "against the live containerised schema. Requires Docker + the container\n"
        "layer's db_container fixture; self-skips without Docker (collectable-green).\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import pytest\n\n\n"
        "@pytest.mark.container\n"
        f"def {fn_name}({fixture_name}) -> None:\n"
        f"    url = {fixture_name}.get_connection_url()\n"
        "    assert url  # container is live (or the fixture skipped)\n"
        "    # Replace with referential-integrity probes, e.g.:\n"
        "    # engine = sqlalchemy.create_engine(url)\n"
        "    # with engine.connect() as conn:\n"
        "    #     # FK constraints present\n"
        "    #     fks = conn.execute(sqlalchemy.text(\n"
        '    #         "SELECT COUNT(*) FROM information_schema.table_constraints"\n'
        "    #         \" WHERE constraint_type = 'FOREIGN KEY'\"\n"
        "    #     )).scalar()\n"
        "    #     assert fks > 0, 'no FK constraints found — schema may be missing migrations'\n"
        "    #     # Orphan-row probe (example):\n"
        "    #     orphans = conn.execute(sqlalchemy.text(\n"
        "    #         'SELECT COUNT(*) FROM child_table c'\n"
        "    #         ' LEFT JOIN parent_table p ON c.parent_id = p.id'\n"
        "    #         ' WHERE p.id IS NULL'\n"
        "    #     )).scalar()\n"
        "    #     assert orphans == 0, f'{orphans} orphan rows found'\n"
    )


# ---------------------------------------------------------------------------
# Python skeletons — MongoDB
# ---------------------------------------------------------------------------


def _py_mongo(fixture_name: str, fn_name: str) -> str:
    """MongoDB collection-structure skeleton (validator + index existence)."""
    return (
        '"""Collection-structure test (data-structural layer, /test-scaffold).\n\n'
        "Probes MongoDB collection JSON-schema validator existence and index presence\n"
        "against a live MongoDB container. Requires Docker + the container layer's\n"
        "db_container fixture; self-skips without Docker (collectable-green).\n\n"
        "# Not applicable for mongodb: relational migration reversibility\n"
        "#   (document-store has no schema migrations).\n"
        "# Not applicable for mongodb: FK referential integrity\n"
        "#   (document-store has no FK constraints).\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import pytest\n\n\n"
        "@pytest.mark.container\n"
        f"def {fn_name}({fixture_name}) -> None:\n"
        f"    url = {fixture_name}.get_connection_url()\n"
        '    assert url.startswith("mongodb")  # container is live (or the fixture skipped)\n'
        "    # Replace with real pymongo / MongoClient probes, e.g.:\n"
        "    # from pymongo import MongoClient  # noqa: PLC0415\n"
        "    # client = MongoClient(url)\n"
        "    # db = client.get_default_database()\n"
        "    # # Collection JSON-schema validator existence:\n"
        "    # cols_with_validators = [\n"
        "    #     name for name in db.list_collection_names()\n"
        "    #     if db.get_collection(name).options().get('validator')\n"
        "    # ]\n"
        "    # assert cols_with_validators, 'no collection validators found'\n"
        "    # # Index existence:\n"
        "    # indexes = list(db['your_collection'].list_indexes())\n"
        "    # assert len(indexes) > 1, 'only _id index found — expected application indexes'\n"
    )


# ---------------------------------------------------------------------------
# Python skeletons — DynamoDB
# ---------------------------------------------------------------------------


def _py_dynamo(fixture_name: str, fn_name: str, port: int) -> str:
    """DynamoDB table-structure skeleton (key-schema + GSI existence)."""
    return (
        '"""Table-structure test (data-structural layer, /test-scaffold).\n\n'
        "Probes DynamoDB table key-schema (partition/sort key) and GSI presence against\n"
        "a live DynamoDB Local container. Requires Docker + the container layer's\n"
        "db_container fixture; self-skips without Docker (collectable-green).\n\n"
        "# Not applicable for dynamodb: relational migration reversibility\n"
        "#   (NoSQL key-value store; no schema migrations).\n"
        "# Not applicable for dynamodb: FK referential integrity\n"
        "#   (no FK constraints).\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import pytest\n\n\n"
        "@pytest.mark.container\n"
        f"def {fn_name}({fixture_name}) -> None:\n"
        f"    host = {fixture_name}.get_container_host_ip()\n"
        f"    port = {fixture_name}.get_exposed_port({port})\n"
        "    assert int(port) > 0  # container is live (or the fixture skipped)\n"
        "    # Replace with real boto3 probes, e.g.:\n"
        "    # import boto3  # noqa: PLC0415\n"
        "    # client = boto3.client(\n"
        '    #     "dynamodb",\n'
        f'    #     endpoint_url=f"http://{{host}}:{{port}}",\n'
        '    #     region_name="us-east-1",\n'
        '    #     aws_access_key_id="test",\n'
        '    #     aws_secret_access_key="test",\n'
        "    # )\n"
        '    # tables = client.list_tables()["TableNames"]\n'
        "    # assert tables, 'no tables found'\n"
        "    # # Key-schema probe (partition + sort key):\n"
        "    # desc = client.describe_table(TableName=tables[0])\n"
        '    # key_schema = desc["Table"]["KeySchema"]\n'
        "    # key_types = {k['KeyType'] for k in key_schema}\n"
        "    # assert 'HASH' in key_types, 'no partition key found'\n"
        "    # # GSI probe:\n"
        '    # gsis = desc["Table"].get("GlobalSecondaryIndexes", [])\n'
        "    # assert gsis, 'no GSIs found — expected at least one global secondary index'\n"
    )


# ---------------------------------------------------------------------------
# Python renderer — dispatches all backends by kind
# ---------------------------------------------------------------------------


def _render_python(
    context: ScaffoldContext, backends: list[str], *, multi: bool
) -> tuple[ScaffoldFile, ...]:
    migration_tool = _detect_migration_tool(context.project_root)
    files: list[ScaffoldFile] = []
    for backend in backends:
        meta = data_backend_meta(backend)
        assert meta is not None
        fixture_name = f"db_container_{backend}" if multi else "db_container"
        if meta.kind == "sql":
            if multi:
                mig_path = f"tests/container/test_structural_migration_{backend}.py"
                int_path = f"tests/container/test_structural_integrity_{backend}.py"
            else:
                mig_path = "tests/container/test_structural_migration.py"
                int_path = "tests/container/test_structural_integrity.py"
            files.append(
                ScaffoldFile(
                    mig_path,
                    _py_migration(fixture_name, "test_migrations_are_reversible", migration_tool),
                )
            )
            files.append(
                ScaffoldFile(
                    int_path,
                    _py_integrity(fixture_name, "test_referential_integrity_holds"),
                )
            )
        elif meta.kind == "mongodb":
            path = (
                f"tests/container/test_structural_mongodb_{backend}.py"
                if multi
                else "tests/container/test_structural_mongodb.py"
            )
            files.append(
                ScaffoldFile(path, _py_mongo(fixture_name, "test_collection_schema_and_indexes"))
            )
        elif meta.kind == "dynamodb":
            path = (
                f"tests/container/test_structural_dynamodb_{backend}.py"
                if multi
                else "tests/container/test_structural_dynamodb.py"
            )
            files.append(
                ScaffoldFile(
                    path, _py_dynamo(fixture_name, "test_table_key_schema_and_gsi", meta.port)
                )
            )
    return tuple(files)


# ---------------------------------------------------------------------------
# Java skeletons
# ---------------------------------------------------------------------------


def _java_migration(class_name: str, java_class: str, image: str, backend: str) -> str:
    if backend in ("postgres", "mysql", "mssql", "oracle"):
        jdbc_note = (
            "    // Replace with real Flyway/Liquibase reversibility check:\n"
            "    // String url = DB.getJdbcUrl();\n"
            "    // Flyway.configure().dataSource(url, DB.getUsername(), DB.getPassword())\n"
            "    //     .load().migrate();\n"
            "    // // downgrade then re-upgrade to verify reversibility\n"
            '    // // (Liquibase: liquibase.update("") / liquibase.rollback(1, ""))\n'
        )
    else:
        jdbc_note = "    // Replace with your migration tool's up→down→up reversibility check.\n"
    return f"""package container;

import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;
import org.testcontainers.containers.{java_class};
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

/**
 * Migration reversibility test (structural layer, /test-scaffold).
 * Verifies that Flyway/Liquibase migrations are fully reversible (up→down→up).
 * Requires Docker; Testcontainers skips the class when Docker is unavailable.
 */
@Testcontainers
class {class_name} {{

    @Container
    static final {java_class}<?> DB = new {java_class}<>("{image}");

    @Test
    void migrationsAreReversible() {{
        assertTrue(DB.isRunning());
{jdbc_note}    }}
}}
"""


def _java_integrity(class_name: str, java_class: str, image: str) -> str:
    body = f"""package container;

import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;
import org.testcontainers.containers.{java_class};
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

/**
 * Referential-integrity test (structural layer, /test-scaffold).
 * Probes FK / uniqueness / NOT NULL / CHECK constraints + orphan-row invariants.
 * Requires Docker; Testcontainers skips the class when Docker is unavailable.
 */
@Testcontainers
class {class_name} {{

    @Container
    static final {java_class}<?> DB = new {java_class}<>("{image}");

    @Test
    void referentialIntegrityHolds() {{
        assertTrue(DB.isRunning());
        // Replace with referential-integrity probes via JDBC, e.g.:
        // String url = DB.getJdbcUrl();
        // try (Connection conn = DriverManager.getConnection(url, ...)) {{
        //     // FK count probe
        //     ResultSet rs = conn.createStatement().executeQuery(
        //         "SELECT COUNT(*) FROM information_schema.table_constraints"
        //         + " WHERE constraint_type = 'FOREIGN KEY'");
        //     rs.next();
        //     assertTrue(rs.getInt(1) > 0, "no FK constraints found");
        //     // Orphan-row probe:
        //     // ResultSet orphans = conn.createStatement().executeQuery(
        //     //   "SELECT COUNT(*) FROM child c LEFT JOIN parent p ON c.parent_id = p.id"
        //     //   + " WHERE p.id IS NULL");
        //     // orphans.next();
        //     // assertEquals(0, orphans.getInt(1), orphans.getInt(1) + " orphan rows found");
        // }}
    }}
}}
"""
    return body


def _render_java(
    _context: ScaffoldContext, sql_backends: list[str], *, multi: bool
) -> tuple[ScaffoldFile, ...]:
    files: list[ScaffoldFile] = []
    for backend in sql_backends:
        meta = data_backend_meta(backend)
        assert meta is not None
        image = meta.default_image
        java_class = meta.java_class
        if multi:
            mig_cls = f"StructuralMigration{backend.capitalize()}IT"
            int_cls = f"StructuralIntegrity{backend.capitalize()}IT"
        else:
            mig_cls = "StructuralMigrationIT"
            int_cls = "StructuralIntegrityIT"
        mig_path = f"src/test/java/container/{mig_cls}.java"
        int_path = f"src/test/java/container/{int_cls}.java"
        files.append(ScaffoldFile(mig_path, _java_migration(mig_cls, java_class, image, backend)))
        files.append(ScaffoldFile(int_path, _java_integrity(int_cls, java_class, image)))
    return tuple(files)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render_data_structural(context: ScaffoldContext) -> ScaffoldResult:
    """Emit structural tests for every resolved data backend, dispatching by kind.

    - ``kind == "sql"`` (postgres/mysql/mssql/oracle) → migration reversibility +
      referential-integrity skeletons (SQL only; unchanged from the DT-3a behaviour).
    - ``kind == "mongodb"`` → collection JSON-schema validator + index existence skeleton.
    - ``kind == "dynamodb"`` → table key-schema (partition/sort key) + GSI skeleton.

    Python renders all three kinds. Java renders SQL backends only (NoSQL Java skeletons
    are deferred — see #1169). All tests share the container layer's
    ``db_container[_<backend>]`` fixture, so ``--data-tests`` (which implies ``container``)
    is the typical entry point. The ``multi`` flag is computed on the **full** resolved
    backend set so fixture names match the container layer exactly.
    """
    backends = resolve_backends(context)

    if not backends:
        files: tuple[ScaffoldFile, ...] = ()
        note = "data-structural: no backends resolved — skipped"
        return ScaffoldResult(files=files, note=note)

    # `multi` is computed on the FULL resolved backend set (not any kind-filtered subset)
    # so the db_container[_<backend>] fixture names match the container layer exactly —
    # e.g. a [mongodb, postgres] resolve is multi for both layers, so structural references
    # db_container_postgres (not the unsuffixed db_container).
    multi = len(backends) > 1

    if context.stack == "python":
        files = _render_python(context, backends, multi=multi)
    elif context.stack == "java":
        # Java: SQL only — NoSQL Java skeletons are deferred (#1169).
        sql_backends = [
            b for b in backends if (meta := data_backend_meta(b)) and meta.kind == "sql"
        ]
        files = _render_java(context, sql_backends, multi=multi) if sql_backends else ()
    else:
        files = ()

    files = with_emphasis(files, context)
    shown = "/".join(backends)
    note = f"data-structural tests ({shown})"
    return ScaffoldResult(files=files, note=note)


register_scaffold("python", "data-structural", render_data_structural)
register_scaffold("java", "data-structural", render_data_structural)
