"""Phase-A deterministic BFS breadth crawler for runtime_explore (#1473).

Drives a running app with local Playwright (lazily imported so this module imports
without the ``[browser]`` extra) and does a bounded, authenticated breadth-first walk
inside the ``url_scope`` allowlist. Per reachable screen it captures the URL, title, a
screenshot, the forms, and the outbound nav edges. It STOPS at frontiers it cannot
cross alone — a page carrying a form (login, wizard, a search needing input) becomes a
``blocked_frontier`` for the agent to deep-drive in Phase B. It writes
``crawl-graph.json`` + ``explore_prompts.json`` under the state dir and returns
**7** (awaiting agent). Off-allowlist links are recorded as ``external_edges`` and
never navigated (the containment guard).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from primitives.runtime_explore.shared.browser import (
    CAPABILITY_UNAVAILABLE_EXIT,
    playwright_available,
)
from primitives.runtime_explore.shared.models import (
    CrawlGraph,
    ExternalEdge,
    Form,
    FormField,
    NavEdge,
    Screen,
)
from primitives.runtime_explore.shared.plan import (
    ExplorePlan,
    PlanError,
    load_plan,
    url_in_scope,
)
from primitives.runtime_explore.shared.screenshots import screenshot_name

if TYPE_CHECKING:  # pragma: no cover - typing only
    from primitives.runtime_explore.shared.plan import UrlScope

#: Exit code when Phase A finished and wrote the blocked frontiers for the agent.
AWAITING_AGENT_EXIT = 7

#: Input types that count as "needs a real value" — a form with any of these is a
#: blocked frontier the deterministic crawler should not fill blindly.
_INPUT_TYPES_NEEDING_INPUT = frozenset(
    {"text", "password", "email", "search", "tel", "url", "number", "date"}
)


def _screen_id(index: int) -> str:
    return f"SCR-{index:03d}"


def _normalize_url(url: str) -> str:
    """Drop the fragment; keep path + query (query distinguishes real screens)."""
    return url.split("#", 1)[0]


def _extract_forms(page: Any) -> tuple[Form, ...]:
    forms: list[Form] = []
    for form_el in page.query_selector_all("form"):
        name = form_el.get_attribute("name") or form_el.get_attribute("id") or ""
        fields: list[FormField] = []
        for inp in form_el.query_selector_all("input, select, textarea"):
            fname = inp.get_attribute("name") or inp.get_attribute("id") or ""
            ftype = (
                inp.get_attribute("type") or inp.evaluate("e => e.tagName.toLowerCase()")
            ) or ""
            required = inp.get_attribute("required") is not None
            if fname:
                fields.append(FormField(name=fname, type=ftype, required=required))
        forms.append(Form(name=name, fields=tuple(fields)))
    return tuple(forms)


def _form_needs_input(forms: tuple[Form, ...]) -> bool:
    """A form is a blocked frontier when it carries a field needing a real value."""
    return any(f.type.lower() in _INPUT_TYPES_NEEDING_INPUT for form in forms for f in form.fields)


def _outbound_links(
    page: Any, current_id: str, scope: UrlScope
) -> tuple[list[str], list[ExternalEdge]]:
    """Return (in-scope target URLs, external edges) for the anchors on *page*."""
    in_scope: list[str] = []
    external: list[ExternalEdge] = []
    for a in page.query_selector_all("a[href]"):
        try:
            href = a.evaluate("el => el.href")  # resolved absolute URL
        except Exception:  # noqa: BLE001, S112 - a detached/odd node; skip it (no signal to log)
            continue
        if not href or href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        target = _normalize_url(href)
        if url_in_scope(target, scope):
            in_scope.append(target)
        else:
            external.append(ExternalEdge(frm=current_id, to_url=target))
    return in_scope, external


def crawl(
    plan: ExplorePlan,
    *,
    storage_state: Path | None,
    out_dir: Path,
    state_dir: Path,
) -> int:
    """Run the Phase-A breadth crawl. Returns an exit code (see the module docstring).

    * **6** — Playwright / a browser binary is not installed (prints the install hint).
    * **2** — a *storage_state* path was given but does not exist.
    * **7** — success: ``crawl-graph.json`` + ``explore_prompts.json`` written.
    """
    ok, hint = playwright_available()
    if not ok:
        print(hint)
        return CAPABILITY_UNAVAILABLE_EXIT
    if storage_state is not None and not storage_state.is_file():
        print(f"ERROR: storage-state file not found: {storage_state}")
        return 2

    # Fail loud if url_scope excludes the base URL itself. An over-broad or
    # malformed deny glob (e.g. deny: ["**"], or an unterminated ``[`` that fnmatch
    # treats as a literal) can make url_in_scope reject everything — the crawl would
    # then drain its queue and exit 7 with an EMPTY graph and no diagnostic. Catch it
    # here (before launching a browser) so the operator sees the real cause.
    if not url_in_scope(_normalize_url(plan.base_url), plan.url_scope):
        print(
            "ERROR: base_url is excluded by url_scope (allow/deny); nothing to crawl. "
            "Check that an allow glob matches base_url and no deny glob (e.g. '**') "
            "over-matches it."
        )
        return 3

    out_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    shots_dir = out_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    # Lazy import so this module imports without the [browser] extra (air-gapped core).
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    scope = plan.url_scope
    screens: list[Screen] = []
    nav_edges: list[NavEdge] = []
    external_edges: list[ExternalEdge] = []
    blocked: list[dict[str, Any]] = []
    seen: dict[str, str] = {}  # normalized url -> screen id
    truncated = False

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # Guard the whole crawl: any raise between launch and the normal close
        # (page.title/_extract_forms/screenshot on a crashed or detached page)
        # must not orphan the Chromium process. The finally always tears down the
        # browser; the context is closed too when it was successfully created.
        context = None
        try:
            context = (
                browser.new_context(storage_state=str(storage_state))
                if storage_state is not None
                else browser.new_context()
            )
            page = context.new_page()

            # Wall-clock ceiling: max_pages/max_depth bound the crawl by shape, but a
            # large app of slow pages can still run far past the operator's intended
            # budget. total_timeout_s caps the total elapsed time; 0 disables it.
            deadline = (
                time.monotonic() + plan.budget.total_timeout_s
                if plan.budget.total_timeout_s > 0
                else None
            )
            queue: deque[tuple[str, int]] = deque([(_normalize_url(plan.base_url), 0)])
            while queue:
                if len(screens) >= plan.budget.max_pages:
                    truncated = True
                    break
                if deadline is not None and time.monotonic() > deadline:
                    truncated = True
                    break
                url, depth = queue.popleft()
                if url in seen or not url_in_scope(url, scope):
                    continue
                sid = _screen_id(len(screens) + 1)
                seen[url] = sid
                try:
                    page.goto(
                        url,
                        timeout=plan.budget.page_timeout_ms,
                        wait_until=plan.budget.wait_until,
                    )
                except Exception as exc:  # noqa: BLE001 - a dead link / timeout; record + skip
                    blocked.append(
                        {"screen_id": sid, "url": url, "reason": f"navigation failed: {exc}"}
                    )
                    continue

                # Containment after redirects: an in-scope URL can 3xx-redirect the browser
                # to an off-scope host. Re-check the FINAL landed URL — if it left scope,
                # abandon the screen (record the destination as an external edge; do NOT
                # capture/scrape/enqueue an off-scope page). Closes the redirect pivot.
                landed = _normalize_url(page.url)
                if not url_in_scope(landed, scope):
                    del seen[url]
                    external_edges.append(ExternalEdge(frm=sid, to_url=landed))
                    blocked.append(
                        {
                            "screen_id": sid,
                            "url": url,
                            "reason": f"redirected off-scope to {landed}",
                        }
                    )
                    continue

                title = page.title()
                # Name the screenshot by route (+ id suffix for uniqueness) so the
                # screenshots/ folder is browsable by page. Land it in the context
                # out_dir, same as before.
                shot = shots_dir / screenshot_name(sid, url)
                try:
                    page.screenshot(path=str(shot))
                    shot_ref = f"screenshots/{shot.name}"
                except Exception:  # noqa: BLE001 - screenshot best-effort
                    shot_ref = ""
                forms = _extract_forms(page)
                screens.append(
                    Screen(
                        id=sid,
                        url=url,
                        title=title,
                        screenshot=shot_ref,
                        forms=forms,
                        reached_via="crawl",
                    )
                )

                if _form_needs_input(forms):
                    reason = (
                        "login"
                        if any(
                            "pass" in f.type.lower() or "pass" in f.name.lower()
                            for form in forms
                            for f in form.fields
                        )
                        else "form/wizard needs input"
                    )
                    blocked.append(
                        {"screen_id": sid, "url": url, "reason": reason, "screenshot": shot_ref}
                    )

                targets, ext = _outbound_links(page, sid, scope)
                external_edges.extend(ext)
                for target in targets:
                    nav_edges.append(NavEdge(frm=sid, to=target, trigger="link", kind="link"))
                    if target not in seen and depth + 1 <= plan.budget.max_depth:
                        queue.append((target, depth + 1))

        finally:
            if context is not None:
                context.close()
            browser.close()

    # Rewrite nav-edge targets from URLs to screen ids where the target was visited.
    resolved_edges = [
        NavEdge(frm=e.frm, to=seen.get(e.to, e.to), trigger=e.trigger, label=e.label, kind=e.kind)
        for e in nav_edges
    ]
    graph = CrawlGraph(
        screens=tuple(screens),
        nav_edges=tuple(resolved_edges),
        external_edges=tuple(external_edges),
        budget_visited=len(screens),
        truncated=truncated,
    )
    (state_dir / "crawl-graph.json").write_text(
        json.dumps(graph.model_dump(mode="json", by_alias=True), indent=2) + "\n", encoding="utf-8"
    )
    (state_dir / "explore_prompts.json").write_text(
        json.dumps({"blocked_frontiers": blocked}, indent=2) + "\n", encoding="utf-8"
    )
    return AWAITING_AGENT_EXIT


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="runtime-explore-crawl")
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--state-dir", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--auth-storage-state", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m primitives.runtime_explore.shared.crawler …``."""
    args = _build_parser().parse_args(argv)
    try:
        plan = load_plan(args.plan)
    except PlanError as exc:
        print(f"ERROR: {exc}")
        return exc.exit_code
    return crawl(
        plan,
        storage_state=args.auth_storage_state,
        out_dir=args.out,
        state_dir=args.state_dir,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["AWAITING_AGENT_EXIT", "crawl", "main"]
