"""UI-e2e-layer renderer - a runnable Playwright project (#1036).

The sparing tip of the inverted snow cone: a runnable Playwright UI-e2e project
(config + page objects + fixtures + a smoke spec) plus a dormant
(``workflow_dispatch``-only) CI overlay the consumer opts into. JS/TS is the
native case; Python uses pytest-playwright, Java uses Playwright-for-Java. The
base URL is a model fill-in (``fillins["ui_base_url"]``, default
``http://localhost:3000``). Page-object pattern, not flat selectors. The smoke
spec is collectable-green (lists clean; runs only when a browser + the app are
available).
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

_DEFAULT_BASE_URL = "http://localhost:3000"


# ---------------------------------------------------------------------------
# Shared: a dormant CI overlay (workflow_dispatch only; consumer opts into PRs).
# ---------------------------------------------------------------------------


def _ci_overlay(run_cmd: str) -> str:
    return f"""# UI e2e (Playwright) - scaffolded by /test-scaffold (DORMANT).
# Emitted workflow_dispatch-only; opt into a PR trigger yourself to go live.
name: ui-e2e
on:
  workflow_dispatch:
permissions:
  contents: read
jobs:
  ui-e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install Playwright browsers
        run: npx playwright install --with-deps
      - name: Run UI e2e
        run: {run_cmd}
"""


# ---------------------------------------------------------------------------
# JS / TS (native @playwright/test)
# ---------------------------------------------------------------------------


def _ts_config(base_url: str) -> str:
    return f"""import {{ defineConfig, devices }} from '@playwright/test';

// Playwright config scaffolded by /test-scaffold.
export default defineConfig({{
  testDir: './tests/e2e',
  retries: process.env.CI ? 2 : 0,
  reporter: 'html',
  use: {{
    baseURL: process.env.UI_BASE_URL ?? '{base_url}',
    trace: 'on-first-retry',
  }},
  projects: [
    {{ name: 'chromium', use: {{ ...devices['Desktop Chrome'] }} }},
    {{ name: 'webkit', use: {{ ...devices['Desktop Safari'] }} }},
  ],
}});
"""


_TS_PAGE_OBJECT = """import { type Page, type Locator } from '@playwright/test';

// Page object scaffolded by /test-scaffold - keep selectors here, not in tests.
export class HomePage {
  readonly page: Page;
  readonly heading: Locator;

  constructor(page: Page) {
    this.page = page;
    this.heading = page.getByRole('heading', { level: 1 });
  }

  async goto(): Promise<void> {
    await this.page.goto('/');
  }
}
"""


_TS_SMOKE = """import { test, expect } from '@playwright/test';
import { HomePage } from './pages/HomePage';

// Smoke UI e2e (the sparing tip of the cone) - scaffolded by /test-scaffold.
test('home page loads and shows a heading', async ({ page }) => {
  const home = new HomePage(page);
  await home.goto();
  await expect(home.heading).toBeVisible();
});
"""


# ---------------------------------------------------------------------------
# Python (pytest-playwright)
# ---------------------------------------------------------------------------

_PY_PAGE_OBJECT = '''"""Page object (pytest-playwright) - keep selectors here, not in tests."""

from __future__ import annotations


class HomePage:
    def __init__(self, page) -> None:
        self.page = page
        self.heading = page.get_by_role("heading", level=1)

    def goto(self) -> None:
        self.page.goto("/")
'''


def _py_smoke(base_url: str) -> str:
    return f'''"""Smoke UI e2e (pytest-playwright) - scaffolded by /test-scaffold.

Run with ``pytest --base-url {base_url}`` against your running app; requires the
``pytest-playwright`` plugin + installed browsers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Import the page object whether or not tests/e2e is a package (collection-safe).
sys.path.insert(0, str(Path(__file__).parent / "pages"))
from home_page import HomePage  # noqa: E402


@pytest.mark.ui_e2e
def test_home_page_loads(page) -> None:
    home = HomePage(page)
    home.goto()
    from playwright.sync_api import expect

    expect(home.heading).to_be_visible()
'''


# ---------------------------------------------------------------------------
# Java (Playwright for Java)
# ---------------------------------------------------------------------------


def _java_smoke(base_url: str) -> str:
    return f"""package uie2e;

import com.microsoft.playwright.Browser;
import com.microsoft.playwright.Page;
import com.microsoft.playwright.Playwright;
import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;

/**
 * Smoke UI e2e (Playwright for Java), scaffolded by /test-scaffold.
 * Enable once the app + browsers are available.
 */
class HomePageE2ETest {{

    static final String BASE_URL =
        System.getenv().getOrDefault("UI_BASE_URL", "{base_url}");

    @Test
    @Disabled("install browsers + run the app, then enable")
    void homePageLoads() {{
        try (Playwright playwright = Playwright.create()) {{
            Browser browser = playwright.chromium().launch();
            Page page = browser.newPage();
            page.navigate(BASE_URL);
            assert page.getByRole(
                com.microsoft.playwright.options.AriaRole.HEADING).isVisible();
            browser.close();
        }}
    }}
}}
"""


def render_ui_e2e(context: ScaffoldContext) -> ScaffoldResult:
    """Emit a runnable Playwright UI-e2e project + a dormant CI overlay."""
    base_url = context.fillins.get("ui_base_url", _DEFAULT_BASE_URL)
    files: tuple[ScaffoldFile, ...]
    if context.stack == "nodejs":
        files = (
            ScaffoldFile("playwright.config.ts", _ts_config(base_url)),
            ScaffoldFile("tests/e2e/pages/HomePage.ts", _TS_PAGE_OBJECT),
            ScaffoldFile("tests/e2e/home.spec.ts", _TS_SMOKE),
            ScaffoldFile(".github/workflows/ui-e2e.yml", _ci_overlay("npx playwright test")),
        )
    elif context.stack == "python":
        files = (
            marks_conftest_file(),
            ScaffoldFile("tests/e2e/pages/home_page.py", _PY_PAGE_OBJECT),
            ScaffoldFile("tests/e2e/test_home_smoke.py", _py_smoke(base_url)),
            ScaffoldFile("tests/e2e/pages/__init__.py", ""),
            ScaffoldFile(
                ".github/workflows/ui-e2e.yml",
                _ci_overlay("pytest tests/e2e --base-url " + base_url),
            ),
        )
    elif context.stack == "java":
        java_ci = _ci_overlay("mvn -Dtest=HomePageE2ETest test")
        files = (
            ScaffoldFile("src/test/java/uie2e/HomePageE2ETest.java", _java_smoke(base_url)),
            ScaffoldFile(".github/workflows/ui-e2e.yml", java_ci),
        )
    else:
        files = ()
    if context.stack == "nodejs":
        # The Node/TS deps + scripts manifest so the scaffold is npm-runnable (#1062).
        files = (*files, *nodejs_manifest_files())
    files = with_emphasis(files, context)  # cone-emphasis companion (#1060)
    return ScaffoldResult(
        files=files,
        note=f"Playwright UI-e2e project ({context.stack}, base {base_url}); dormant CI overlay",
    )


for _stack in ("python", "nodejs", "java"):
    register_scaffold(_stack, "ui-e2e", render_ui_e2e)
