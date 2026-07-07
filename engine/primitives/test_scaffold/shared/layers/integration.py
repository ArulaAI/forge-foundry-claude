"""Integration-layer renderer — cross-component fixtures + real fakes (#1034).

The inverted snow cone's wide middle: runnable integration tests that exercise a
real component boundary (handler -> repository -> in-memory/seam), with a real
test double (``responses``/``respx`` for Python, ``nock`` for Node, WireMock for
Java) when no container is warranted. Every emitted file is collectable-green —
the sample test imports cleanly and is marked ``skip`` until the engineer wires
the real component, never a generator that errors.

Renderers are pure (return :class:`ScaffoldFile` descriptors); the sink owns I/O,
idempotency, and containment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    from primitives.test_scaffold.shared.registry import ScaffoldContext

# ---------------------------------------------------------------------------
# Python (pytest + responses)
# ---------------------------------------------------------------------------

_PY_CONFTEST = '''"""Integration-test fixtures (cross-component boundary).

Scaffolded by /test-scaffold. Wire the real component (DB session, HTTP client)
into the fixtures below, then drop the ``skip`` marker on the sample test.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def http_client():
    """An HTTP test client for the service under test.

    Replace the placeholder with your app's test client, e.g.::

        from myapp import create_app
        from starlette.testclient import TestClient
        return TestClient(create_app())
    """
    pytest.skip("wire your app's HTTP test client into the http_client fixture")


@pytest.fixture
def fake_upstream():
    """A real fake for an external dependency (responses / respx).

    Records + matches outbound HTTP so the integration test exercises the real
    serialization boundary without the live dependency.
    """
    import responses

    with responses.RequestsMock() as rsps:
        yield rsps
'''

_PY_TEST = '''"""Integration test: a real cross-component boundary.

Collectable-green: it imports cleanly and skips until the fixtures are wired.
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
def test_create_then_read_round_trips(http_client, fake_upstream) -> None:
    # Arrange: register the upstream fake, then drive the handler -> repository path.
    fake_upstream.add("GET", "https://upstream.example/health", json={"ok": True}, status=200)

    # Act: exercise the boundary through the real app (not a mocked unit).
    created = http_client.post("/widgets", json={"name": "w1"})
    fetched = http_client.get(f"/widgets/{created.json()['id']}")

    # Assert: the round-trip preserves the data across the component boundary.
    assert created.status_code == 201
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "w1"
'''

# ---------------------------------------------------------------------------
# Node / TS (vitest + nock)
# ---------------------------------------------------------------------------

_TS_TEST = """import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import nock from 'nock';

// Integration test: a real cross-component boundary, scaffolded by /test-scaffold.
// Wire `request` to your app (e.g. supertest(app)) and drop `it.skip`.

describe('widget integration', () => {
  beforeAll(() => {
    // Fake the upstream dependency at the serialization boundary.
    nock('https://upstream.example').get('/health').reply(200, { ok: true });
  });
  afterAll(() => nock.cleanAll());

  it.skip('creates then reads, round-tripping across the boundary', async () => {
    // const created = await request(app).post('/widgets').send({ name: 'w1' });
    // const fetched = await request(app).get(`/widgets/${created.body.id}`);
    // expect(created.status).toBe(201);
    // expect(fetched.body.name).toBe('w1');
    // Fail-until-wired (#1059): enabling this before wiring the real call fails red,
    // not green-on-a-tautology. Replace with the real assertions above.
    expect.fail('wire the real request/response assertions, then remove this line');
  });
});
"""

# ---------------------------------------------------------------------------
# Java (JUnit 5 + WireMock)
# ---------------------------------------------------------------------------

_JAVA_TEST = """package integration;

import static org.junit.jupiter.api.Assertions.fail;

import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;

/**
 * Integration test: a real cross-component boundary, scaffolded by /test-scaffold.
 * Add a WireMock (or MockServer) stub for the upstream dependency, wire your app's
 * client, and remove @Disabled.
 */
class WidgetIntegrationTest {

    @Test
    @Disabled("wire the app client + WireMock stub, then enable")
    void createThenRead_roundTrips() {
        // stubFor(get("/health").willReturn(okJson("{\\"ok\\":true}")));
        // var created = client.post("/widgets", Map.of("name", "w1"));
        // var fetched = client.get("/widgets/" + created.id());
        // Fail-until-wired (#1059): enabling this before wiring fails red, not
        // green-on-a-tautology. Replace with the real round-trip assertions.
        fail("wire the app client + WireMock stub + real assertions, then remove this");
    }
}
"""

_FILES: dict[str, tuple[tuple[str, str], ...]] = {
    "python": (
        ("tests/integration/conftest.py", _PY_CONFTEST),
        ("tests/integration/test_widget_integration.py", _PY_TEST),
    ),
    "nodejs": (("tests/integration/widget.integration.test.ts", _TS_TEST),),
    "java": (("src/test/java/integration/WidgetIntegrationTest.java", _JAVA_TEST),),
}


def render_integration(context: ScaffoldContext) -> ScaffoldResult:
    """Emit the integration harness + a boundary-exercising sample test."""
    specs = _FILES.get(context.stack, ())
    files = tuple(ScaffoldFile(path=p, content=c) for p, c in specs)
    if context.stack == "python":
        # Register the @pytest.mark.integration mark so the sample collects green
        # under --strict-markers (#1053).
        files = (marks_conftest_file(), *files)
    if context.stack == "nodejs":
        # The Node/TS deps + scripts manifest so the scaffold is npm-runnable (#1062).
        files = (*files, *nodejs_manifest_files())
    files = with_emphasis(files, context)  # cone-emphasis companion (#1060)
    return ScaffoldResult(
        files=files,
        note=(
            f"integration harness + real fake ({context.stack}); "
            "collectable-green, skip until wired"
        ),
    )


for _stack in ("python", "nodejs", "java"):
    register_scaffold(_stack, "integration", render_integration)
