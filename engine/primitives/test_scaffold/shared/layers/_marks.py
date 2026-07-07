"""Shared: the generated `tests/conftest.py` that registers /test-scaffold's marks (#1053).

Every Python layer's sample test carries a custom mark (`@pytest.mark.integration`
/ `.container` / `.contract` / `.api_e2e` / `.ui_e2e`). Under ``--strict-markers``
(or ``filterwarnings = error``) — a common SDET-shop default — an *unregistered*
mark is a hard collection ERROR, which would break the "collectable-green"
guarantee. So any Python-emitting layer also contributes a top-level
``tests/conftest.py`` that registers the five marks via ``pytest_configure``.

The write sink is idempotent, so multiple layers contributing the same conftest
is fine (first wins; the rest skip). A consumer's existing ``tests/conftest.py``
is never clobbered (augment-or-skip) — in that case the consumer registers the
marks themselves (documented in the generated file's docstring).
"""

from __future__ import annotations

from primitives.test_scaffold.shared.registry import ScaffoldFile

#: The five custom marks /test-scaffold's Python templates apply.
SCAFFOLD_MARKS: tuple[str, ...] = (
    "integration",
    "container",
    "contract",
    "api_e2e",
    "ui_e2e",
)

_CONFTEST = '''"""pytest config scaffolded by /test-scaffold - registers the scaffold's marks.

Registers the custom marks the scaffolded tests use so they collect green even
under ``--strict-markers`` / ``-W error``. If you already have a ``tests/conftest.py``,
this file was NOT written (no clobber); add these marks to your own config:

    integration, container, contract, api_e2e, ui_e2e
"""

from __future__ import annotations

_SCAFFOLD_MARKS = (
    "integration: a /test-scaffold integration test (cross-component boundary)",
    "container: a /test-scaffold container-backed test (Testcontainers)",
    "contract: a /test-scaffold consumer-driven contract test (Pact)",
    "api_e2e: a /test-scaffold behavioural API end-to-end journey",
    "ui_e2e: a /test-scaffold Playwright UI end-to-end test",
)


def pytest_configure(config) -> None:
    for line in _SCAFFOLD_MARKS:
        config.addinivalue_line("markers", line)
'''


def marks_conftest_file() -> ScaffoldFile:
    """The ``tests/conftest.py`` that registers the scaffold's pytest marks (#1053).

    Contributed by every Python-emitting layer; the idempotent sink writes it once
    and never clobbers a consumer's existing ``tests/conftest.py``.
    """
    return ScaffoldFile(path="tests/conftest.py", content=_CONFTEST)
