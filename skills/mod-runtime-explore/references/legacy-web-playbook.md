# Driving dirty legacy web apps with Playwright — a field playbook

Guidance for the Phase-B **agent deep-drive** of `/mod-runtime-explore` when the target is a
messy legacy web app — Angular / SPA hash-routing, autocomplete fields, NgRx state, lazy-loaded
routes, server-rendered JSP/Thymeleaf. The bundled `step_cli` verbs are the primitives; this
playbook is *how* to wield them (and the browser generally) so a crawl of a real legacy app is
robust rather than flaky.

> **The golden rules:** (1) **never hard-sleep** — always wait for a specific UI/network
> condition; (2) prefer **stable selectors** (`data-testid` > ARIA role+name > label > text >
> placeholder > CSS); (3) on SPAs, wait for the **client route** and the **network to settle**,
> not just `domcontentloaded`.

---

## 1. Navigation

- **Navigate + wait for the app to be ready.** For Angular/SPA targets, navigate and wait until
  **network activity settles** (`wait_until="networkidle"`) — `domcontentloaded` fires before the
  SPA has fetched its data. For a plain server-rendered page, `domcontentloaded` is enough and
  faster.
- **Relative paths** work when a `base_url` is configured (the plan supplies it).
- **Client-side route change:** on a hash-routed Angular app, a "navigation" may be a URL *hash*
  change with no full load — wait for the **URL to match the expected pattern**, then for the
  network to settle.
- **Refresh:** reload and wait for the network to settle.

## 2. Selector strategy — most-stable first

1. **`data-testid`** — the gold standard; explicit, survives restyling.
2. **ARIA role + accessible name** — `role=button name="Save"`; best for form controls / links.
3. **Label text** — a form input by its associated `<label>`.
4. **Visible text** — buttons/links/headings by their text (fragile if copy changes).
5. **Placeholder text** — text inputs by placeholder.
6. **CSS selector** — **last resort**; breaks on DOM/style churn.

When several match, take the **nth** (`.nth(1)` = second) rather than a brittle CSS path.

## 3. Waiting — no hard sleeps, ever

- Wait for an element to **appear or disappear** (e.g. a loading spinner to vanish) before acting.
- Wait for **navigation after a click**: arm the wait, click, then await it (avoids the race).
- Wait for a specific **API response** (optionally assert `200`) when the UI reacts to a fetch.
- Wait for the **URL to change** to a pattern (Angular routing).
- Wait for the **network to settle** after an action that triggers background requests.

## 4. Forms

- **Fill:** clear first, then type. For **autocomplete**, type **character-by-character** so the
  field's keystroke handlers fire, then wait for the suggestion list and pick from it.
- **Dropdown/select:** choose by value, label, or index.
- **Checkbox / radio:** toggle / select by role+label.
- **Keys:** `Tab` / `Enter` / `Escape` for wizards, modals, keyboard-only flows.
- **File upload:** attach by local path.

## 5. Screenshots (checkpoints)

Capture full-page shots at meaningful checkpoints — **after load, after filling a form, after
submit** — plus element-only or clipped shots when a region matters. `step_cli snapshot` handles
naming/numbering/saving (route-named PNGs); take one on **every distinct screen** you reach so the
visual inventory is complete.

## 6. Data extraction

Read text / attributes (`href`) / input values; check visibility and disabled state; bulk-extract
repeated rows (tables/lists) in one pass; run in-page JavaScript to **detect the framework**
(Angular/React/Vue globals) or inspect page state when the DOM alone is ambiguous.

## 7. Assertions

Verify an element is visible, contains expected text, the URL is right, the element **count**
matches, and an input holds the value you filled — fail loud with detail, never silently continue.

## 8. Network interception (when needed)

Stub an API response (exercise UI without a live backend); block images/fonts to speed a crawl;
log all calls to understand the app; modify a request (e.g. inject an auth header). Use sparingly —
runtime discovery wants to observe the **real** app, so prefer logging over stubbing unless a
frontier is otherwise unreachable.

## 9. Keyboard / accessibility

`Tab` through and log focus order; `Escape` to close modals/dropdowns; arrow keys for
dropdown/date-picker/list navigation.

## 10. Dialogs

Handle `alert`/`confirm`/`prompt`: log the message and accept, or dismiss, or provide prompt input
— **but never accept a destructive confirm** (the plan's `deny` list still governs; treat a
"Delete?" confirm as out of bounds).

## 11. Multiple tabs / popups

Open a fresh context/tab for an isolated flow; for a click that spawns a popup, wait for the new
window, switch to it, interact, then return.

## 12. Saving output

Structured findings → JSON in the output dir; a human summary → a text/markdown report. (The
aggregator already renders `runtime-journeys.json` + `RUNTIME-DISCOVERY.md`; extra artifacts are
supplementary.)

## 13. Angular / SPA specifics

- After navigating, wait for the **hash to update** *and* the **network to settle** before reading
  the DOM — Angular renders after its data resolves.
- For **NgRx**-driven fields, **don't poll a timer** — wait for the loading spinner to disappear or
  the content to appear, then read the **rendered DOM value** (don't try to reach into the store).
- **Lazy-loaded routes** fetch code on navigation — always let the network fully settle afterward.

---

## Quick selector reference

| What you want                | How to find it                              |
| ---------------------------- | ------------------------------------------- |
| Element with a test id       | by `data-testid`                            |
| A button by its label        | by role + visible text                      |
| A text input by its label    | by the associated label text                |
| A link by its text           | by role + visible text                      |
| A dropdown / combobox        | by role + label                             |
| A checkbox                   | by role + label                             |
| A heading                    | by role + visible text                      |
| An alert / status message    | by role `alert`                             |
| Any text on the page         | by exact text content                       |
| The second match of a thing  | `.nth(1)`                                    |

---

## Safety (unchanged, always)

Every navigation stays inside the plan's `url_scope`; the `deny` list (logout, delete,
purchase-confirm) is never crossed — including via a confirm dialog or a keyboard shortcut. Secrets
stay by reference (`creds`/`--from-seed`); they never enter a screenshot, a report, or the JSON.
