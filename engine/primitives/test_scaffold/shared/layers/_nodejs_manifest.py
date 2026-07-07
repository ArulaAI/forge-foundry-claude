"""The Node/TS deps + scripts manifest for /test-scaffold (#1062).

The nodejs layers emit ``.ts`` tests that ``import nock`` / ``testcontainers`` /
``@pact-foundation/pact`` / ``@playwright/test`` / ``vitest`` — but a nodejs consumer
can't ``npm test`` the scaffold without a dependency manifest. Python/Java lean on
the consumer's existing toolchain; nodejs needs the deps + scripts spelled out.

Rather than clobber (or risk a fragile JSON-merge of) an existing ``package.json``,
this emits two **augment-or-skip** files the consumer folds in by hand:

* ``package.json.test-scaffold`` — a ready-to-merge snippet of ``devDependencies`` +
  ``scripts`` (``test:integration`` / ``test:container`` / ``test:contract`` /
  ``test:api-e2e`` / ``e2e``) for the scaffolded layers.
* ``tests/README.test-scaffold.md`` — the install + run instructions.

Every nodejs layer contributes the *same complete* manifest (the dep set is a known
deterministic union); the idempotent sink writes it once and skips the rest, so the
manifest is present whichever nodejs layers a run scaffolds.
"""

from __future__ import annotations

import json

from primitives.test_scaffold.shared.registry import ScaffoldFile

#: The pinned-ish dev dependencies the nodejs layers' emitted tests use. Ranges are
#: conservative majors; the consumer adjusts to their lockfile. ``supertest`` is the
#: one wire-in dep (the integration sample's commented-out ``request(app)`` call uses
#: it); the rest are imported directly by an emitted test.
_DEV_DEPENDENCIES: dict[str, str] = {
    "vitest": "^2.0.0",
    "nock": "^13.5.0",
    "supertest": "^7.0.0",
    "testcontainers": "^10.0.0",
    "@pact-foundation/pact": "^13.0.0",
    "@playwright/test": "^1.45.0",
}

#: The npm scripts for the scaffolded layers (layer key -> (script name, command)).
_SCRIPTS: dict[str, str] = {
    "test:integration": "vitest run tests/integration",
    "test:container": "vitest run tests/integration/container.integration.test.ts",
    "test:contract": "vitest run tests/contract",
    "test:api-e2e": "vitest run tests/api-e2e",
    "e2e": "playwright test",
}


def _snippet() -> str:
    """The ready-to-merge ``package.json`` snippet (devDeps + scripts)."""
    payload = {
        "devDependencies": dict(sorted(_DEV_DEPENDENCIES.items())),
        "scripts": dict(_SCRIPTS),
    }
    return json.dumps(payload, indent=2) + "\n"


_README = """# Node/TS test scaffold — deps + scripts (test-scaffold)

`/test-scaffold` emitted Node/TS tests that need a few dev dependencies and npm
scripts. Your existing `package.json` was **not** modified (no clobber); fold in the
snippet below.

## 1. Merge the manifest

`package.json.test-scaffold` (next to this README, at the project root) contains a
`devDependencies` + `scripts` block. Merge it into your `package.json` — or copy the
entries you need for the layers you scaffolded:

- **integration** → `nock`, `vitest` (+ `supertest` once you wire `request` to your app —
  the sample's commented-out call uses it); script `test:integration`
- **container** → `testcontainers`, `vitest`; script `test:container` (needs Docker)
- **contract** → `@pact-foundation/pact`, `vitest`; script `test:contract`
- **api-e2e** → `vitest`; script `test:api-e2e`
- **ui-e2e** → `@playwright/test`; script `e2e` (then `npx playwright install`)

## 2. Install + run

```bash
npm install            # pulls the merged devDependencies
npx playwright install # only if you scaffolded the ui-e2e layer
npm run test:integration   # etc., per the scripts you merged
```

The emitted tests are collectable-green: they list/skip until you wire the real
component, browser, or live API.
"""


def nodejs_manifest_files() -> tuple[ScaffoldFile, ...]:
    """The ``package.json.test-scaffold`` snippet + the install README (#1062).

    Contributed by every nodejs layer; the idempotent sink writes each once and
    never clobbers a consumer's existing files.
    """
    return (
        ScaffoldFile(path="package.json.test-scaffold", content=_snippet()),
        ScaffoldFile(path="tests/README.test-scaffold.md", content=_README),
    )
