"""Contract-layer renderer — Pact consumer tests, composes S40 (#1037).

Consumer-driven contract tests at every service boundary (the inverted snow
cone is contract-heavy). Emits a runnable Pact consumer test per stack (which
produces a pact file when run), and — when an ``openapi.*``/``*.proto`` is
present — a pointer to **S40 ``/api-contract-review``** for the schema/shape side
rather than reimplementing schema validation. The provider name is a model
fill-in (``fillins["contract_provider"]``, default ``provider-service``).

Pact consumer tests are collectable-green: they import cleanly and skip until the
provider interaction is wired.
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

_DEFAULT_PROVIDER = "provider-service"

#: An openapi/proto spec anywhere here means S40 owns the schema/shape side.
_API_SPECS = ("openapi.yaml", "openapi.yml", "openapi.json", "swagger.yaml", "swagger.json")


def _py_test(provider: str) -> str:
    return f'''"""Consumer-driven contract test (pact-python).

Scaffolded by /test-scaffold. Running this produces a pact file for the
"{provider}" provider. Wire the real consumer call, then drop the skip marker.
Requires the ``pact-python`` package at run time.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def pact():
    """A Pact between this consumer and the provider."""
    pytest.importorskip("pact")
    from pact import Consumer, Provider  # noqa: PLC0415

    return Consumer("this-service").has_pact_with(Provider("{provider}"))


@pytest.mark.contract
def test_get_widget_contract(pact) -> None:
    # Define the expected interaction (the contract), then drive the real consumer.
    (
        pact.given("a widget exists")
        .upon_receiving("a request for the widget")
        .with_request("get", "/widgets/1")
        .will_respond_with(200, body={{"id": 1, "name": "w1"}})
    )
    with pact:
        # response = my_consumer_client.get_widget(1)
        # assert response["name"] == "w1"
        pytest.skip("wire the real consumer call into the pact interaction")
'''


def _ts_test(provider: str) -> str:
    return f"""import {{ describe, it, expect }} from 'vitest';
import {{ PactV3, MatchersV3 }} from '@pact-foundation/pact';

// Consumer-driven contract test (@pact-foundation/pact), scaffolded by /test-scaffold.
// Running this produces a pact file for the "{provider}" provider.

describe('widget contract', () => {{
  const provider = new PactV3({{ consumer: 'this-service', provider: '{provider}' }});

  it.skip('GET /widgets/1 honours the contract', async () => {{
    provider
      .given('a widget exists')
      .uponReceiving('a request for the widget')
      .withRequest({{ method: 'GET', path: '/widgets/1' }})
      .willRespondWith({{ status: 200, body: {{ id: MatchersV3.integer(1), name: 'w1' }} }});

    await provider.executeTest(async (mockServer) => {{
      // const res = await myClient.getWidget(mockServer.url, 1);
      // expect(res.name).toBe('w1');
      // Fail-until-wired (#1059): enabling this before wiring the real consumer
      // call fails red, not green-on-a-tautology. Replace with the assertions above.
      expect.fail('wire the real consumer call + assertions, then remove this line');
    }});
  }});
}});
"""


def _java_test(provider: str) -> str:
    return f"""package contract;

import static org.junit.jupiter.api.Assertions.fail;

import au.com.dius.pact.consumer.dsl.PactDslWithProvider;
import au.com.dius.pact.consumer.junit5.PactConsumerTestExt;
import au.com.dius.pact.consumer.junit5.PactTestFor;
import au.com.dius.pact.core.model.RequestResponsePact;
import au.com.dius.pact.core.model.annotations.Pact;
import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;

/**
 * Consumer-driven contract test (pact-jvm), scaffolded by /test-scaffold.
 * Produces a pact file for the "{provider}" provider.
 */
@ExtendWith(PactConsumerTestExt.class)
@PactTestFor(providerName = "{provider}")
class WidgetContractTest {{

    @Pact(consumer = "this-service")
    RequestResponsePact widget(PactDslWithProvider builder) {{
        return builder
            .given("a widget exists")
            .uponReceiving("a request for the widget")
            .path("/widgets/1")
            .method("GET")
            .willRespondWith()
            .status(200)
            .body("{{\\"id\\":1,\\"name\\":\\"w1\\"}}")
            .toPact();
    }}

    @Test
    @Disabled("wire the real consumer call against the mock provider, then enable")
    void honoursTheContract() {{
        // var res = myClient.getWidget(mockServer.getUrl(), 1);
        // Fail-until-wired (#1059): enabling this before wiring fails red, not
        // green-on-a-tautology. Replace with the real contract assertion.
        fail("wire the real consumer call + assertion, then remove this line");
    }}
}}
"""


def _s40_pointer(provider: str) -> str:
    return (
        "# Contract layer — schema/shape side (S40 `/api-contract-review`)\n\n"
        "An API spec (`openapi.*` / `*.proto`) was detected. `/test-scaffold` scaffolds the\n"
        "**behavioural** contract (Pact consumer tests); the **schema/shape** side is owned by\n"
        "**S40 `/api-contract-review`** — run it against your spec rather than duplicating schema\n"
        f"validation here. Together they cover the `{provider}` boundary: Pact proves behaviour,\n"
        "S40 proves the contract shape matches the agreed spec.\n"
    )


#: Per-stack provider-verification entry points (the other end of consumer-driven
#: contract testing). Named so the README can point the engineer at the right tool.
_PROVIDER_VERIFY_TOOL = {
    "python": "pact-python's `Verifier` (`pact-verifier` / `Verifier().verify_pacts(...)`)",
    "nodejs": "`@pact-foundation/pact`'s `Verifier`",
    "java": (
        "pact-jvm's `PactVerificationInvocationContextProvider` "
        "(`@Provider` + `@PactBroker`/`@PactFolder`)"
    ),
}


def _provider_verify_readme(provider: str, stack: str) -> str:
    """The provider-verification side of the contract (#1065).

    The Pact consumer test produces a ``.pact`` file; it is only useful once the
    *provider* verifies it. This README explains the verify step + broker wiring so
    the consumer-driven contract loop is complete, not a dangling pact file.
    """
    tool = _PROVIDER_VERIFY_TOOL.get(stack, "your Pact provider verifier")
    return (
        "# Contract layer — provider-verification side\n\n"
        "`/test-scaffold` scaffolded the **consumer** test, which produces a `.pact` "
        f"file for the\n`{provider}` provider. Consumer-driven contract testing is only "
        "useful once the **provider**\nverifies that pact — otherwise you have a pact "
        "file nothing consumes.\n\n"
        "## Wire the provider-verify step\n\n"
        f"On the **`{provider}` provider** side (a different repo/service), add a "
        f"verification test using\n{tool}:\n\n"
        "1. Point the verifier at the pact source: a local `pacts/` folder for a quick "
        "start, or a\n   **Pact Broker** (recommended) so the consumer publishes and the "
        "provider pulls by version/tag.\n"
        "2. Stand up the provider (or a slice of it) and run the verifier against the "
        "recorded\n   interactions; a mismatch fails the provider build — that's the "
        "contract doing its job.\n"
        "3. In CI, gate the provider on `can-i-deploy` (broker) so an unverified "
        "contract blocks release.\n\n"
        "## Broker wiring (recommended)\n\n"
        "- Consumer CI: publish the generated pact (`pact-broker publish` / the SDK's "
        "publish call) on\n  every green build, tagged with the branch/version.\n"
        "- Provider CI: verify against the broker and publish the verification result "
        "back.\n\n"
        "This README is documentation (collectable-green by construction); there is no "
        "runnable\n"
        "provider test here because the provider lives in another codebase.\n"
    )


def render_contract(context: ScaffoldContext) -> ScaffoldResult:
    """Emit a Pact consumer test + a provider-verify README + (when an API spec
    exists) an S40 pointer."""
    provider = context.fillins.get("contract_provider", _DEFAULT_PROVIDER)
    files: tuple[ScaffoldFile, ...]
    if context.stack == "python":
        files = (
            marks_conftest_file(),
            ScaffoldFile("tests/contract/test_widget_contract.py", _py_test(provider)),
        )
    elif context.stack == "nodejs":
        files = (ScaffoldFile("tests/contract/widget.contract.test.ts", _ts_test(provider)),)
    elif context.stack == "java":
        files = (
            ScaffoldFile("src/test/java/contract/WidgetContractTest.java", _java_test(provider)),
        )
    else:
        files = ()

    # The provider-verification side (#1065): the consumer test alone leaves a
    # dangling .pact file. Emit a README explaining the provider-verify step +
    # broker wiring so the consumer-driven contract loop is complete. (Only for the
    # stacks that emit a consumer test.)
    if context.stack in ("python", "nodejs", "java"):
        files = (
            *files,
            ScaffoldFile(
                "tests/contract/README.contract.md",
                _provider_verify_readme(provider, context.stack),
            ),
        )

    # Compose S40 for the schema/shape side when an API spec is present.
    has_spec = any((context.project_root / spec).is_file() for spec in _API_SPECS)
    if has_spec:
        files = (
            *files,
            ScaffoldFile("tests/contract/README.api-contract-review.md", _s40_pointer(provider)),
        )

    if context.stack == "nodejs":
        # The Node/TS deps + scripts manifest so the scaffold is npm-runnable (#1062).
        files = (*files, *nodejs_manifest_files())
    files = with_emphasis(files, context)  # cone-emphasis companion (#1060)
    return ScaffoldResult(
        files=files,
        note=(
            f"Pact consumer + provider-verify README ({context.stack}, provider {provider})"
            + ("; + S40 schema pointer" if has_spec else "")
        ),
    )


for _stack in ("python", "nodejs", "java"):
    register_scaffold(_stack, "contract", render_contract)
