"""Container-layer renderer — a real Testcontainers/docker harness (#1033).

A container-backed integration harness with a real container lifecycle, gated on
a detected containerizable dependency. The data backend(s) are resolved from the
canonical ``_sdet`` data-backend registry (#1124): an explicit ``--data-backend``
(``fillins["data_backend"]``, comma-separated) wins, else the backends are
auto-detected from the project's dependency manifests, else it falls back to
Postgres. The Testcontainers module/class, exposed port, default image, and
connection-assert idiom all come from each backend's
:class:`~primitives._sdet.stacks.contract.DataBackendMeta`.

Backends span three connection idioms (``DataBackendMeta.kind``): **sql** and
**mongodb** both expose ``get_connection_url()`` (assert the URL scheme);
**dynamodb** has no testcontainers module, so it boots ``amazon/dynamodb-local``
via the generic container and asserts the exposed endpoint port. When more than
one backend resolves, one harness is emitted per backend (filenames suffixed by
backend); a single backend keeps the unsuffixed names. Docker-absent at run time:
the generated test self-skips (collectable-green), never the generator failing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives._sdet.stacks import data_backend_meta
from primitives.test_scaffold.shared.layers._data_select import resolve_backends
from primitives.test_scaffold.shared.layers._emphasis import with_emphasis
from primitives.test_scaffold.shared.layers._marks import marks_conftest_file
from primitives.test_scaffold.shared.layers._nodejs_manifest import (
    nodejs_manifest_files,
)
from primitives.test_scaffold.shared.registry import (
    ScaffoldFile,
    ScaffoldResult,
    register_scaffold,
)

if TYPE_CHECKING:
    from primitives._sdet.stacks.contract import DataBackendMeta
    from primitives.test_scaffold.shared.registry import ScaffoldContext

#: Connection-URL scheme prefix asserted by the generated test, for the backends
#: whose testcontainers module exposes ``get_connection_url()`` (sql + mongodb).
#: A rendering detail, not a registry coordinate.
_URL_PREFIX: dict[str, str] = {
    "postgres": "postgresql",
    "mysql": "mysql",
    "mssql": "mssql",
    "oracle": "oracle",
    "mongodb": "mongodb",
}

#: Testcontainers-Java classes that are NOT self-typed generic, so they take no
#: ``<>`` type witness (``MongoDBContainer extends GenericContainer<MongoDBContainer>``).
_JAVA_NONGENERIC: frozenset[str] = frozenset({"mongodb"})


def _image_for(context: ScaffoldContext, meta: DataBackendMeta, *, multi: bool) -> str:
    """The container image: the ``container_image`` override (single-backend only,
    since one image can't fit every engine) else the backend's default."""
    override = context.fillins.get("container_image")
    if override and not multi:
        return override
    return meta.default_image


# ---------------------------------------------------------------------------
# Python (testcontainers-py)
# ---------------------------------------------------------------------------

_PY_HEADER = '''"""Container fixtures (Testcontainers).

Scaffolded by /test-scaffold. Boots a real container per resolved data backend
for integration tests. Requires Docker + the ``testcontainers`` package at run
time; the sample tests self-skip when Docker is unavailable (collectable-green).
"""

from __future__ import annotations

import pytest


def _docker_available() -> bool:
    try:
        import docker  # noqa: PLC0415

        docker.from_env().ping()
    except Exception:  # noqa: BLE001 - any failure means "no usable Docker"
        return False
    return True
'''


def _py_fixture(name: str, image: str, meta: DataBackendMeta) -> str:
    if meta.kind == "dynamodb":
        imp = "    from testcontainers.core.container import DockerContainer  # noqa: PLC0415"
        ctx = f'    with DockerContainer("{image}").with_exposed_ports({meta.port}) as db:'
    else:
        module_path, _, class_name = meta.py_module.rpartition(".")
        imp = f"    from {module_path} import {class_name}  # noqa: PLC0415"
        ctx = f'    with {class_name}("{image}") as db:'
    return (
        f'@pytest.fixture(scope="session")\n'
        f"def {name}():\n"
        f'    """A real {image} container for the integration suite."""\n'
        f"    if not _docker_available():\n"
        f'        pytest.skip("Docker is not available; skipping container-backed tests")\n'
        f"{imp}\n\n"
        f"{ctx}\n"
        f"        yield db\n"
    )


def _py_test(fixture_name: str, test_name: str, meta: DataBackendMeta) -> str:
    if meta.kind == "dynamodb":
        assert_body = (
            f"    host = {fixture_name}.get_container_host_ip()\n"
            f"    port = {fixture_name}.get_exposed_port({meta.port})\n"
            f"    assert int(port) > 0\n"
            '    # Next: boto3.client("dynamodb", endpoint_url="http://" + host + ":" '
            "+ str(port)).list_tables()\n"
        )
    else:
        prefix = _URL_PREFIX[meta.name]
        assert_body = (
            f"    url = {fixture_name}.get_connection_url()\n"
            f'    assert url.startswith("{prefix}")\n'
            "    # Replace with a real query against your migrated schema (e.g. SELECT 1).\n"
        )
    return (
        '"""Container-backed integration test: boots a real DB and asserts against it."""\n\n'
        "from __future__ import annotations\n\n"
        "import pytest\n\n\n"
        "@pytest.mark.container\n"
        f"def {test_name}({fixture_name}) -> None:\n"
        "    # The container is live here (or the suite skipped). Drive a real connection.\n"
        f"{assert_body}"
    )


def _render_python(
    context: ScaffoldContext, backends: list[str], *, multi: bool
) -> tuple[ScaffoldFile, ...]:
    fixtures: list[str] = []
    tests: list[ScaffoldFile] = []
    for backend in backends:
        meta = data_backend_meta(backend)
        assert meta is not None
        image = _image_for(context, meta, multi=multi)
        fixture_name = f"db_container_{backend}" if multi else "db_container"
        if multi:
            test_path = f"tests/container/test_container_{backend}.py"
            test_name = f"test_{backend}_round_trips_against_a_real_container"
        else:
            test_path = "tests/container/test_container_integration.py"
            test_name = "test_schema_round_trips_against_a_real_container"
        fixtures.append(_py_fixture(fixture_name, image, meta))
        tests.append(ScaffoldFile(test_path, _py_test(fixture_name, test_name, meta)))
    conftest = _PY_HEADER + "\n\n" + "\n\n".join(fixtures)
    # The fixtures live in tests/container/conftest.py so pytest auto-discovers them
    # (#1056); the marks-registering tests/conftest.py keeps @pytest.mark.container
    # collecting green under --strict-markers (#1053).
    return (
        marks_conftest_file(),
        ScaffoldFile("tests/container/conftest.py", conftest),
        *tests,
    )


# ---------------------------------------------------------------------------
# Node / TS (testcontainers node — generic container for every engine)
# ---------------------------------------------------------------------------


def _ts_test(image: str, port: int) -> str:
    return f"""import {{ describe, it, expect, beforeAll, afterAll }} from 'vitest';
import {{ GenericContainer, type StartedTestContainer }} from 'testcontainers';

// Container-backed integration test, scaffolded by /test-scaffold.
// Requires Docker at run time; skips cleanly when unavailable.

let container: StartedTestContainer | undefined;

describe('container integration', () => {{
  beforeAll(async () => {{
    try {{
      container = await new GenericContainer('{image}')
        .withExposedPorts({port})
        .start();
    }} catch {{
      container = undefined; // Docker unavailable - the test below skips.
    }}
  }}, 120_000);

  afterAll(async () => {{
    if (container) await container.stop();
  }});

  it('connects to the real container', async () => {{
    if (!container) return; // skip when Docker is unavailable
    expect(container.getMappedPort({port})).toBeGreaterThan(0);
    // Replace with a real connection + query against your schema.
  }});
}});
"""


def _render_nodejs(
    context: ScaffoldContext, backends: list[str], *, multi: bool
) -> tuple[ScaffoldFile, ...]:
    files: list[ScaffoldFile] = []
    for backend in backends:
        meta = data_backend_meta(backend)
        assert meta is not None
        image = _image_for(context, meta, multi=multi)
        path = (
            f"tests/integration/container.{backend}.integration.test.ts"
            if multi
            else "tests/integration/container.integration.test.ts"
        )
        files.append(ScaffoldFile(path, _ts_test(image, meta.port)))
    # The Node/TS deps + scripts manifest so the scaffold is npm-runnable (#1062).
    return (*files, *nodejs_manifest_files())


# ---------------------------------------------------------------------------
# Java (JUnit5 + Testcontainers)
# ---------------------------------------------------------------------------


def _java_test(class_name: str, image: str, meta: DataBackendMeta) -> str:
    java_class = meta.java_class
    if meta.name in _JAVA_NONGENERIC:
        decl = f'static final {java_class} DB = new {java_class}("{image}");'
    elif meta.kind == "dynamodb":
        decl = (
            f"static final {java_class}<?> DB = "
            f'new {java_class}<>("{image}").withExposedPorts({meta.port});'
        )
    else:
        decl = f'static final {java_class}<?> DB = new {java_class}<>("{image}");'
    return f"""package container;

import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;
import org.testcontainers.containers.{java_class};
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

/**
 * Container-backed integration test, scaffolded by /test-scaffold.
 * Requires Docker; Testcontainers skips the class when Docker is unavailable.
 */
@Testcontainers
class {class_name} {{

    @Container
    {decl}

    @Test
    void connectsToTheRealContainer() {{
        assertTrue(DB.isRunning());
        // Replace with a real connection + query against your backend
        // (e.g. DB.getJdbcUrl() for SQL, DB.getConnectionString() for Mongo).
    }}
}}
"""


def _render_java(
    context: ScaffoldContext, backends: list[str], *, multi: bool
) -> tuple[ScaffoldFile, ...]:
    files: list[ScaffoldFile] = []
    for backend in backends:
        meta = data_backend_meta(backend)
        assert meta is not None
        image = _image_for(context, meta, multi=multi)
        if multi:
            class_name = f"Container{backend.capitalize()}IT"
            path = f"src/test/java/container/{class_name}.java"
        else:
            class_name = "ContainerIntegrationIT"
            path = "src/test/java/container/ContainerIntegrationIT.java"
        files.append(ScaffoldFile(path, _java_test(class_name, image, meta)))
    return tuple(files)


def render_container(context: ScaffoldContext) -> ScaffoldResult:
    """Emit a Testcontainers harness + a container-backed sample test per backend."""
    backends = resolve_backends(context)
    multi = len(backends) > 1
    if context.stack == "python":
        files = _render_python(context, backends, multi=multi)
    elif context.stack == "nodejs":
        files = _render_nodejs(context, backends, multi=multi)
    elif context.stack == "java":
        files = _render_java(context, backends, multi=multi)
    else:
        files = ()
    files = with_emphasis(files, context)  # cone-emphasis companion (#1060)
    shown = "/".join(backends)
    note = f"Testcontainers harness ({context.stack}/{shown}); skips without Docker"
    return ScaffoldResult(files=files, note=note)


for _stack in ("python", "nodejs", "java"):
    register_scaffold(_stack, "container", render_container)
