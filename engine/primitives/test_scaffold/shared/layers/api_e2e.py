"""API-e2e-layer renderer - behavioural request->assert journeys (#1038).

Behavioural API end-to-end tests: multi-step request->assert journeys against
running endpoints (auth, status codes, payload assertions). Distinct from the
**contract** layer (shape, no running behaviour) and the **UI e2e** layer
(browser). The highest-value e2e layer for backend/service repos and a core
inverted-snow-cone layer. The base URL is a model fill-in
(``fillins["api_base_url"]``, default ``http://localhost:8000``); live-API-absent
at run time -> the test self-skips (collectable-green).
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

_DEFAULT_BASE_URL = "http://localhost:8000"


def _py_test(base_url: str) -> str:
    return f'''"""API e2e journey: a multi-step request->assert flow against the running API.

Scaffolded by /test-scaffold. Point BASE_URL at your running service (or a
fixture that boots it); the test self-skips when the API is unreachable.
Requires ``httpx`` at run time.
"""

from __future__ import annotations

import os

import pytest

BASE_URL = os.environ.get("API_BASE_URL", "{base_url}")


@pytest.fixture
def client():
    httpx = pytest.importorskip("httpx")
    try:
        c = httpx.Client(base_url=BASE_URL, timeout=5.0)
        c.get("/health")
    except Exception:  # noqa: BLE001 - any connect failure means "API not running"
        pytest.skip(f"API at {{BASE_URL}} is not reachable; skipping API e2e")
    return c


@pytest.mark.api_e2e
def test_create_authenticate_and_read_journey(client) -> None:
    # 1. Authenticate (adjust to your auth flow).
    token_resp = client.post("/auth/login", json={{"user": "tester", "password": "secret"}})
    assert token_resp.status_code == 200
    token = token_resp.json()["token"]
    auth = {{"Authorization": f"Bearer {{token}}"}}

    # 2. Create a resource.
    created = client.post("/widgets", json={{"name": "w1"}}, headers=auth)
    assert created.status_code == 201
    widget_id = created.json()["id"]

    # 3. Read it back - the journey asserts behaviour end-to-end.
    fetched = client.get(f"/widgets/{{widget_id}}", headers=auth)
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "w1"
'''


def _ts_test(base_url: str) -> str:
    return f"""import {{ describe, it, expect, beforeAll }} from 'vitest';

// API e2e journey: a multi-step request->assert flow against the running API,
// scaffolded by /test-scaffold. Skips when the API is unreachable.

const BASE_URL = process.env.API_BASE_URL ?? '{base_url}';
let reachable = false;

beforeAll(async () => {{
  try {{
    const res = await fetch(`${{BASE_URL}}/health`);
    reachable = res.ok;
  }} catch {{
    reachable = false;
  }}
}});

describe('widget API journey', () => {{
  it('authenticates, creates, then reads back', async () => {{
    if (!reachable) return; // skip when the API is not running

    const login = await fetch(`${{BASE_URL}}/auth/login`, {{
      method: 'POST',
      headers: {{ 'content-type': 'application/json' }},
      body: JSON.stringify({{ user: 'tester', password: 'secret' }}),
    }});
    expect(login.status).toBe(200);
    const {{ token }} = await login.json();
    const auth = {{ Authorization: `Bearer ${{token}}` }};

    const created = await fetch(`${{BASE_URL}}/widgets`, {{
      method: 'POST',
      headers: {{ 'content-type': 'application/json', ...auth }},
      body: JSON.stringify({{ name: 'w1' }}),
    }});
    expect(created.status).toBe(201);
    const {{ id }} = await created.json();

    const fetched = await fetch(`${{BASE_URL}}/widgets/${{id}}`, {{ headers: auth }});
    expect(fetched.status).toBe(200);
    expect((await fetched.json()).name).toBe('w1');
  }});
}});
"""


def _java_test(base_url: str) -> str:
    return f"""package apie2e;

import static io.restassured.RestAssured.given;
import static org.hamcrest.Matchers.equalTo;

import io.restassured.RestAssured;
import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;

/**
 * API e2e journey (RestAssured): a multi-step request->assert flow against the
 * running API, scaffolded by /test-scaffold. Enable once your service is running.
 */
class WidgetApiJourneyTest {{

    static final String BASE_URL =
        System.getenv().getOrDefault("API_BASE_URL", "{base_url}");

    @Test
    @Disabled("point BASE_URL at a running service, then enable")
    void authenticateCreateAndRead() {{
        RestAssured.baseURI = BASE_URL;
        String token =
            given().body("{{\\"user\\":\\"tester\\",\\"password\\":\\"secret\\"}}")
                .contentType("application/json")
                .post("/auth/login")
                .then().statusCode(200)
                .extract().path("token");

        int id =
            given().header("Authorization", "Bearer " + token)
                .body("{{\\"name\\":\\"w1\\"}}").contentType("application/json")
                .post("/widgets")
                .then().statusCode(201)
                .extract().path("id");

        given().header("Authorization", "Bearer " + token)
            .get("/widgets/" + id)
            .then().statusCode(200).body("name", equalTo("w1"));
    }}
}}
"""


def render_api_e2e(context: ScaffoldContext) -> ScaffoldResult:
    """Emit a behavioural API-e2e journey test for *context*'s stack."""
    base_url = context.fillins.get("api_base_url", _DEFAULT_BASE_URL)
    files: tuple[ScaffoldFile, ...]
    if context.stack == "python":
        files = (
            marks_conftest_file(),
            ScaffoldFile("tests/api_e2e/test_widget_journey.py", _py_test(base_url)),
        )
    elif context.stack == "nodejs":
        files = (ScaffoldFile("tests/api-e2e/widget.journey.test.ts", _ts_test(base_url)),)
    elif context.stack == "java":
        files = (
            ScaffoldFile("src/test/java/apie2e/WidgetApiJourneyTest.java", _java_test(base_url)),
        )
    else:
        files = ()
    if context.stack == "nodejs":
        # The Node/TS deps + scripts manifest so the scaffold is npm-runnable (#1062).
        files = (*files, *nodejs_manifest_files())
    files = with_emphasis(files, context)  # cone-emphasis companion (#1060)
    return ScaffoldResult(
        files=files,
        note=f"API e2e journey ({context.stack}, base {base_url}); skips when API unreachable",
    )


for _stack in ("python", "nodejs", "java"):
    register_scaffold(_stack, "api-e2e", render_api_e2e)
