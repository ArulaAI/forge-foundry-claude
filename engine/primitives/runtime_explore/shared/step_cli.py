"""Agent-driven step-CLI for runtime_explore Phase B (#1474).

The deterministic crawler (Phase A) stops at blocked frontiers — login, wizards, forms
needing realistic values. The SKILL body crosses them by driving the browser one action
at a time through THIS CLI (via the ``Bash`` tool), reasoning over each observation:

    python -m primitives.runtime_explore.shared.step_cli session start --plan … --state-dir …
    python -m primitives.runtime_explore.shared.step_cli goto <url> --plan … --state-dir …
    python -m primitives.runtime_explore.shared.step_cli fill <selector> <value> …
    python -m primitives.runtime_explore.shared.step_cli click <selector> …
    python -m primitives.runtime_explore.shared.step_cli submit <selector> …
    python -m primitives.runtime_explore.shared.step_cli snapshot [--label <slug>] …
    python -m primitives.runtime_explore.shared.step_cli read <selector> [--by …] …
    python -m primitives.runtime_explore.shared.step_cli controls detect …
    python -m primitives.runtime_explore.shared.step_cli controls auto-exercise …
    python -m primitives.runtime_explore.shared.step_cli session end …

Each invocation is its own process, so the browser session is **persisted across
invocations** via a Playwright ``storageState`` + a ``current_url`` marker under the
state dir. A ``snapshot`` records the current screen into ``crawl-graph.json`` with
``reached_via="agent"`` so Phase C sees the agent-reached screens. Every ``goto`` is
scope-checked (out-of-scope → exit 3). Missing Playwright → exit 6.

New in #1599:
* ``snapshot --label <slug>`` — a LABELED screenshot uses the filename
  ``<route>__<label>__<id>.png`` and prints the ref on stdout.  Without ``--label``
  the behavior is byte-identical to the pre-#1599 form.
* ``read <selector> [--by …]`` — prints the visible text content of the matched
  region; the agent diffs before/after to populate ``data_state_change``.
* ``controls detect`` — runs ``detect_controls`` on the current page and prints the
  inventory as a JSON array of Control dicts.
* ``controls auto-exercise`` — mechanically exercises TRIVIAL-kind controls
  (``sort`` / ``dropdown``) from ``detect_controls``: captures a labeled snapshot +
  reads the affected region text before and after, then emits a JSON array of Control
  records with ``exercised_by="auto"`` and the populated ``data_state_change``.  Only
  sort/dropdown are auto-exercised; filter/edit/modal are left for the harness.  Any
  navigation-changing exercise re-checks URL-scope containment via the same
  ``url_in_scope`` guard the goto/crawler use.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from primitives.runtime_explore.shared.browser import (
    CAPABILITY_UNAVAILABLE_EXIT,
    playwright_available,
)
from primitives.runtime_explore.shared.models import (
    Control,
    CrawlGraph,
    Form,
    FormField,
    Screen,
)
from primitives.runtime_explore.shared.plan import PlanError, load_plan, url_in_scope
from primitives.runtime_explore.shared.screenshots import screenshot_name

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from primitives.runtime_explore.shared.plan import ExplorePlan

_STORAGE = "step_storage_state.json"
_MARKER = "step_session.json"  # {"current_url": "..."}


def _state_files(state_dir: Path) -> tuple[Path, Path]:
    return state_dir / _STORAGE, state_dir / _MARKER


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* atomically: a full write to a sibling temp file then
    ``os.replace`` (an atomic rename on the same filesystem). A crash mid-write leaves
    the temp file, never a half-written *path* — so a reader always sees either the old
    complete file or the new complete file, never a truncated one."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)  # atomic rename on the same filesystem


def _load_marker(state_dir: Path) -> dict[str, Any]:
    _, marker = _state_files(state_dir)
    if not marker.is_file():
        return {}
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt/half-written marker (e.g. a crash mid-write on an older run) must
        # not crash the step — degrade to an empty marker (restore falls back to base_url).
        return {}
    return data if isinstance(data, dict) else {}


def _save_marker(state_dir: Path, data: dict[str, Any]) -> None:
    _, marker = _state_files(state_dir)
    _atomic_write(marker, json.dumps(data))


def _resolve_ref(creds_from: str, creds_ref: str) -> dict[str, str]:
    """Resolve one ``creds_from`` + ``creds_ref`` reference into a ``{field: value}`` map.

    Shared by both the flat single-realm path and the per-profile path so credential
    resolution behaves identically regardless of shape:

    * ``creds_from: env``  — ``creds_ref`` is an env var whose value is either a JSON
      object (``{"user": "...", "pass": "..."}``) or a bare string (bound to ``value``).
    * ``creds_from: file`` — ``creds_ref`` is a path to a **gitignored** YAML/JSON file
      holding a ``{field: value}`` map.

    Raises :class:`PlanError` (exit 2 missing / 3 unreadable).
    """
    if creds_from == "env":
        raw = os.environ.get(creds_ref)
        if raw is None:
            raise PlanError(f"auth.creds_ref env var '{creds_ref}' is not set", exit_code=2)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"value": raw}
        if not isinstance(parsed, dict):
            return {"value": raw}
        return {str(k): str(v) for k, v in parsed.items()}
    # creds_from == "file"
    path = Path(creds_ref)
    if not path.is_file():
        raise PlanError(f"auth.creds_ref file '{creds_ref}' not found", exit_code=2)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PlanError(
            f"auth.creds_ref file '{creds_ref}' is not valid YAML/JSON: {exc}", exit_code=3
        ) from exc
    if not isinstance(data, dict):
        raise PlanError(
            f"auth.creds_ref file '{creds_ref}' must hold a field→value map", exit_code=3
        )
    return {str(k): str(v) for k, v in data.items()}


def _select_profile(plan: ExplorePlan, profile: str | None) -> str:
    """Resolve which named profile to use, raising :class:`PlanError` on ambiguity.

    * ``profile`` names one explicitly; unknown → exit 3 (listing available).
    * ``profile is None`` + exactly one profile → that one.
    * ``profile is None`` + more than one → exit 3 (``--profile`` required).
    """
    assert plan.auth is not None  # caller guarantees
    profiles = plan.auth.profiles
    if profile is not None:
        if profile not in profiles:
            avail = ", ".join(sorted(profiles)) or "(none)"
            raise PlanError(f"unknown login profile '{profile}' — available: {avail}", exit_code=3)
        return profile
    if len(profiles) == 1:
        return next(iter(profiles))
    names = ", ".join(sorted(profiles))
    raise PlanError(f"multiple login profiles ({names}) — pass --profile <name>", exit_code=3)


def _resolve_creds(
    plan: ExplorePlan, *, profile: str | None = None, role: str | None = None
) -> dict[str, str]:
    """Resolve the auth credentials into a ``{field: value}`` map the login step consumes.

    The plan carries only a *reference*, never a secret (see the inline-secret guard).

    * **role** (``--role``, #1563) — a declared role owns its OWN credential reference
      (``roles.<name>.creds_from``/``creds_ref``). When ``role`` is named it takes
      PRECEDENCE: the role's creds are resolved (the role supplies the credentials; a
      profile only supplies the input selectors). ``profile`` is ignored in this case.
      An unknown role → exit 3 (listing the declared roles).
    * **flat single-realm** plan — resolves the top-level ``creds_from``/``creds_ref``
      (byte-identical to the pre-profiles behavior). Passing ``--profile`` to a flat plan
      is an error (exit 3): it has no profiles.
    * **profiles** plan — ``profile`` names which realm's credentials to resolve; when
      ``None`` and there is exactly one profile it defaults to that one, and when ``None``
      with more than one it errors (exit 3, listing them).

    Raises :class:`PlanError` (exit 2 missing / 3 unreadable/bad-selection) so a
    mis-referenced credential fails loudly instead of silently logging in as nobody.
    """
    if role is not None:
        if role not in plan.roles:
            avail = ", ".join(sorted(plan.roles)) or "(none)"
            raise PlanError(f"unknown role '{role}' — declared roles: {avail}", exit_code=3)
        r = plan.roles[role]
        return _resolve_ref(r.creds_from, r.creds_ref)
    if plan.auth is None:
        raise PlanError(
            "no `auth` block in the plan — `creds` needs auth.creds_from + creds_ref. "
            "Add an auth block, or (for a no-auth target) don't call `creds`.",
            exit_code=3,
        )
    if plan.auth.profiles:
        name = _select_profile(plan, profile)
        prof = plan.auth.profiles[name]
        return _resolve_ref(prof.creds_from, prof.creds_ref)
    # Flat single-realm shape — no profiles.
    if profile is not None:
        raise PlanError(
            f"this plan has no login profiles, but --profile '{profile}' was passed",
            exit_code=3,
        )
    # The plan validator guarantees flat creds are both set when there are no profiles.
    assert plan.auth.creds_from is not None and plan.auth.creds_ref is not None
    return _resolve_ref(plan.auth.creds_from, plan.auth.creds_ref)


def _resolve_login_selectors(
    plan: ExplorePlan, profile: str | None
) -> tuple[str, str, str | None, str | None]:
    """Return ``(username_selector, password_selector, submit_selector, login_url)``.

    For a **profiles** plan, resolves the named profile's selectors (with ``--profile``
    selection / defaulting via :func:`_select_profile`), inheriting the top-level
    ``login_url`` when the profile does not override it. A **flat** plan has no declared
    selectors — the SKILL body supplies them directly — so this errors (exit 3), pointing
    the caller at the flat login path.

    Read-only + secret-free: selectors are not secrets.
    """
    if plan.auth is None:
        raise PlanError(
            "no `auth` block in the plan — `login-selectors` needs auth.profiles.",
            exit_code=3,
        )
    if not plan.auth.profiles:
        raise PlanError(
            "this plan has no login profiles — `login-selectors` applies to "
            "profiles plans; a flat plan's selectors are supplied by the SKILL body.",
            exit_code=3,
        )
    name = _select_profile(plan, profile)
    prof = plan.auth.profiles[name]
    login_url = prof.login_url if prof.login_url is not None else plan.auth.login_url
    return (prof.username_selector, prof.password_selector, prof.submit_selector, login_url)


def _load_seed(plan: ExplorePlan) -> dict[str, str]:
    """Load the ``seed_data_ref`` field→value map (realistic form values), or ``{}``.

    The referenced file is a **gitignored** YAML/JSON map of field name → realistic
    value, used by ``fill --from-seed <field>`` so the agent drives wizards/forms with
    representative data instead of typing raw values by hand. Absent ref → ``{}``.
    """
    if plan.seed_data_ref is None:
        return {}
    path = Path(plan.seed_data_ref)
    if not path.is_file():
        raise PlanError(f"seed_data_ref file '{plan.seed_data_ref}' not found", exit_code=2)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PlanError(
            f"seed_data_ref '{plan.seed_data_ref}' is not valid YAML/JSON: {exc}",
            exit_code=3,
        ) from exc
    if not isinstance(data, dict):
        raise PlanError(
            f"seed_data_ref '{plan.seed_data_ref}' must hold a field→value map", exit_code=3
        )
    return {str(k): str(v) for k, v in data.items()}


def _extract_forms(page: Any) -> tuple[Form, ...]:
    forms: list[Form] = []
    for form_el in page.query_selector_all("form"):
        name = form_el.get_attribute("name") or form_el.get_attribute("id") or ""
        fields: list[FormField] = []
        for inp in form_el.query_selector_all("input, select, textarea"):
            fname = inp.get_attribute("name") or inp.get_attribute("id") or ""
            ftype = inp.get_attribute("type") or ""
            if fname:
                fields.append(
                    FormField(
                        name=fname, type=ftype, required=inp.get_attribute("required") is not None
                    )
                )
        forms.append(Form(name=name, fields=tuple(fields)))
    return tuple(forms)


def _capture_labeled_screenshot(
    out_dir: Path, page: Any, screen_id: str, url: str, label: str
) -> str:
    """Capture a labeled screenshot and return its ``screenshots/<name>`` ref (or ``""``).

    The filename is ``<route>__<label>__<screen_id>.png`` — distinct from the plain
    route-named shot so a screen can accumulate MULTIPLE labeled shots without collision.
    Capture is best-effort: a failure returns ``""`` (never aborts).
    """
    shots_dir = out_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    shot = shots_dir / screenshot_name(screen_id, url, label=label)
    try:
        page.screenshot(path=str(shot))
        return f"screenshots/{shot.name}"
    except Exception:  # noqa: BLE001 - best-effort, matches the crawler
        return ""


def _persist_controls_to_graph(state_dir: Path, url: str, controls: Sequence[Control]) -> None:
    """Merge detected/exercised *controls* onto the matching ``Screen.controls[]`` in
    crawl-graph.json, keyed by *url* (#1623).

    ``controls detect`` / ``controls auto-exercise`` print their inventory to stdout,
    but the Phase-C ``journey_aggregator`` reads controls from the crawl graph — so
    without this write-back the aggregator sees **0 controls** and the #1596 control
    sweep silently produces nothing. This persists them so the documented path works
    without a manual hand-merge.

    * Merges by control ``selector`` (a re-run of ``detect`` on the same screen
      updates in place rather than duplicating; an ``auto-exercise`` record — which
      carries the before/after ``data_state_change`` + screenshot — supersedes the
      bare ``detected_only`` record for the same selector).
    * No matching ``Screen`` for *url* (e.g. the crawler never recorded it) → a
      minimal ``agent``-reached screen is created so the controls are not lost.
    * No graph file yet → no-op (nothing to attach to; the SKILL always crawls first).
    * Empty *controls* → no-op (never rewrites the graph for nothing).
    """
    if not controls:
        return
    graph_path = state_dir / "crawl-graph.json"
    if not graph_path.is_file():
        return
    graph = CrawlGraph.model_validate(json.loads(graph_path.read_text(encoding="utf-8")))
    screens = list(graph.screens)
    idx = next((i for i, s in enumerate(screens) if s.url == url), None)
    if idx is None:
        sid = f"AGT-{len(screens) + 1:03d}"
        merged = _merge_controls((), controls)
        screens.append(Screen(id=sid, url=url, reached_via="agent", controls=merged))
    else:
        target = screens[idx]
        merged = _merge_controls(target.controls, controls)
        screens[idx] = target.model_copy(update={"controls": merged})
    updated = graph.model_copy(update={"screens": tuple(screens)})
    _atomic_write(
        graph_path,
        json.dumps(updated.model_dump(mode="json", by_alias=True), indent=2) + "\n",
    )


def _merge_controls(
    existing: Sequence[Control], incoming: Sequence[Control]
) -> tuple[Control, ...]:
    """Merge *incoming* controls into *existing*, keyed by ``selector`` (last wins).

    An incoming record for a selector already present replaces it (so an
    auto-exercise result with a real ``data_state_change`` supersedes the earlier
    ``detected_only`` detection); a new selector is appended in first-seen order.
    """
    by_selector: dict[str, Control] = {c.selector: c for c in existing}
    order: list[str] = [c.selector for c in existing]
    for c in incoming:
        if c.selector not in by_selector:
            order.append(c.selector)
        by_selector[c.selector] = c
    return tuple(by_selector[sel] for sel in order)


def _append_agent_screen(
    state_dir: Path,
    out_dir: Path,
    page: Any,
    url: str,
    title: str,
    forms: tuple[Form, ...],
    label: str | None = None,
) -> str:
    """Add an agent-reached screen to crawl-graph.json (idempotent by url) and capture a
    route-named screenshot into ``out_dir/screenshots/`` (parity with Phase A).

    When *label* is given the screenshot filename becomes
    ``<route>__<label>__<id>.png`` and the ref is also printed on stdout so the
    harness can stash it.  Returns the screenshot ref (``"screenshots/…"`` or ``""``).
    """
    graph_path = state_dir / "crawl-graph.json"
    if graph_path.is_file():
        graph = CrawlGraph.model_validate(json.loads(graph_path.read_text(encoding="utf-8")))
    else:
        graph = CrawlGraph()
    existing = {s.url for s in graph.screens}
    if url in existing:
        # The screen is already recorded — still capture a NEW labeled shot if requested.
        if label:
            existing_screen = next(s for s in graph.screens if s.url == url)
            ref = _capture_labeled_screenshot(out_dir, page, existing_screen.id, url, label)
            if ref:
                print(ref)
            return ref
        return ""
    # Compute the id once so the screenshot filename's id-suffix matches the graph.
    sid = f"AGT-{len(graph.screens) + 1:03d}"
    shots_dir = out_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    shot = shots_dir / screenshot_name(sid, url, label=label)
    try:
        page.screenshot(path=str(shot))
        shot_ref = f"screenshots/{shot.name}"
    except Exception:  # noqa: BLE001 - screenshot best-effort, matches the crawler
        shot_ref = ""
    screen = Screen(
        id=sid, url=url, title=title, forms=forms, screenshot=shot_ref, reached_via="agent"
    )
    updated = graph.model_copy(update={"screens": (*graph.screens, screen)})
    _atomic_write(
        graph_path,
        json.dumps(updated.model_dump(mode="json", by_alias=True), indent=2) + "\n",
    )
    if label and shot_ref:
        print(shot_ref)
    return shot_ref


def _observe(page: Any) -> str:
    """A compact observation line the agent reasons over."""
    forms = page.query_selector_all("form")
    links = page.query_selector_all("a[href]")
    return f"url={page.url} title={page.title()!r} forms={len(forms)} links={len(links)}"


def _handle_offline_verb(
    verb: str, args: argparse.Namespace, plan: ExplorePlan, state_dir: Path, storage: Path
) -> int | None:
    """Handle the verbs that need no browser. Returns an exit code, or ``None`` to
    fall through to the browser-driving path."""
    if verb == "session" and args.action == "start":
        _save_marker(state_dir, {"current_url": ""})
        if not storage.is_file():
            _atomic_write(storage, json.dumps({"cookies": [], "origins": []}))
        print("session started")
        return 0
    if verb == "session" and args.action == "end":
        print("session ended")
        return 0
    # `creds <field>` resolves auth.creds_ref (env/file) and prints the value, so the
    # SKILL body pipes plan-declared credentials into a fill instead of hardcoding an
    # env-var name. No browser needed. Resolution raises PlanError on a bad reference.
    if verb == "creds":
        creds = _resolve_creds(
            plan,
            profile=getattr(args, "profile", None),
            role=getattr(args, "role", None),
        )
        if args.field not in creds:
            avail = ", ".join(sorted(creds)) or "(none)"
            print(f"ERROR: field '{args.field}' not in resolved credentials; have: {avail}")
            return 2
        print(creds[args.field], end="")
        return 0
    # `login-selectors [--profile <name>]` prints a profile's selectors + login_url as
    # JSON so the SKILL body fills the right realm's fields. Selectors are NOT secrets —
    # printed freely; no browser needed.
    if verb == "login-selectors":
        username, password, submit, login_url = _resolve_login_selectors(
            plan, getattr(args, "profile", None)
        )
        print(
            json.dumps(
                {
                    "username_selector": username,
                    "password_selector": password,
                    "submit_selector": submit,
                    "login_url": login_url,
                }
            )
        )
        return 0
    return None


def _resolve_fill_value(
    args: argparse.Namespace, plan: ExplorePlan
) -> tuple[str | None, int | None]:
    """Resolve a ``fill`` verb's value from ``--from-seed``/literal, pre-launch (#1501).

    Returns ``(value, None)`` on success, or ``(None, exit_code)`` on a validation
    failure that must surface WITHOUT launching a browser:

    * exit 3 — neither a literal ``<value>`` nor ``--from-seed <field>`` was given.
    * exit 2 — ``--from-seed <field>`` names a field absent from the resolved seed data.

    **Precedence:** ``--from-seed`` wins when BOTH a literal ``<value>`` and
    ``--from-seed`` are supplied (the seed-driven value is authoritative for wizards).
    """
    if args.from_seed is not None:
        seed = _load_seed(plan)
        if args.from_seed not in seed:
            avail = ", ".join(sorted(seed)) or "(none)"
            print(f"ERROR: seed field '{args.from_seed}' not found; have: {avail}")
            return None, 2
        return seed[args.from_seed], None
    if args.value is not None:
        return args.value, None
    print("ERROR: fill needs a <value> or --from-seed <field>")
    return None, 3


def _resolve_select_option(args: argparse.Namespace) -> tuple[dict[str, object] | None, int | None]:
    """Resolve a ``select`` verb's option to a Playwright ``select_option`` kwarg (#1531).

    Exactly ONE of ``--value`` / ``--label`` / ``--index`` must be given (mirrors
    ``fill``'s pre-launch validation, exit 3 on none/multiple), so a mis-specified
    ``select`` never launches Chromium only to error out. Returns ``(kwargs, None)`` where
    ``kwargs`` is the ``page.select_option`` keyword (``value=`` / ``label=`` / ``index=``).
    """
    given = [n for n in ("value", "label", "index") if getattr(args, n) is not None]
    if len(given) != 1:
        which = ", ".join(f"--{n}" for n in ("value", "label", "index"))
        print(f"ERROR: select needs exactly one of {which} (got {len(given)})")
        return None, 3
    key = given[0]
    return {key: getattr(args, key)}, None


#: Selector-resolution strategies, most-stable first (#1532). ``css`` (the default) is
#: the current raw-selector behavior; the others resolve via Playwright ``get_by_*``
#: locators so the agent can target stable handles (a ``data-testid``, an ARIA role+name,
#: a label) instead of a brittle CSS path. See ``references/legacy-web-playbook.md``.
SelectorBy = str  # one of: testid | role | label | text | placeholder | css
_SELECTOR_STRATEGIES = ("testid", "role", "label", "text", "placeholder", "css")


def _resolve_locator(page: Any, selector: str, by: str, nth: int | None) -> Any:
    """Resolve ``selector`` to a Playwright ``Locator`` per the ``--by`` strategy (#1532).

    * ``css`` (default) → ``page.locator(selector)`` — byte-identical to the pre-#1532
      raw behavior.
    * ``testid`` → ``page.get_by_test_id(selector)``.
    * ``role`` → ``page.get_by_role(<role>, name=<name>)`` where ``selector`` is
      ``"<role>=<name>"`` (e.g. ``"button=Save"``); a bare ``"<role>"`` matches by role
      only.
    * ``label`` / ``text`` / ``placeholder`` → the matching ``get_by_*`` locator.

    ``nth`` (from ``--nth``) selects the nth match (the playbook's ``.nth(1)``)."""
    if by == "testid":
        loc = page.get_by_test_id(selector)
    elif by == "role":
        role, _, name = selector.partition("=")
        loc = (
            page.get_by_role(role.strip(), name=name.strip())
            if name
            else page.get_by_role(role.strip())
        )
    elif by == "label":
        loc = page.get_by_label(selector)
    elif by == "text":
        loc = page.get_by_text(selector)
    elif by == "placeholder":
        loc = page.get_by_placeholder(selector)
    else:  # css (default) — the raw-selector behavior
        loc = page.locator(selector)
    return loc.nth(nth) if nth is not None else loc


@dataclass(frozen=True)
class _PreLaunch:
    """Result of pre-launch validation: resolved values + a fail-fast exit code.

    ``exit_code`` is non-None when a verb's arguments are invalid and the failure must be
    surfaced WITHOUT launching a browser (goto/fill/select/wait-for). Otherwise the
    resolved ``fill_value`` / ``select_kwargs`` are threaded into the browser block."""

    exit_code: int | None = None
    fill_value: str | None = None
    select_kwargs: dict[str, object] | None = None


def _validate_pre_launch(verb: str, args: argparse.Namespace, plan: ExplorePlan) -> _PreLaunch:
    """Validate a verb's args BEFORE launching Chromium (the #1501/#1531 discipline).

    * ``goto`` — the target URL is scope-checked (exit 3 off-scope).
    * ``fill`` — value resolved from ``--from-seed``/literal (exit 3/2).
    * ``select`` — exactly one of ``--value/--label/--index`` (exit 3).
    * ``wait-for`` — exactly one target (selector/``--url``/``--networkidle``, exit 3),
      and a ``--url`` glob must be in ``url_scope`` (exit 3 — the wait must not steer the
      browser off-scope).
    """
    if verb == "goto" and not url_in_scope(args.url, plan.url_scope):
        print(f"ERROR: {args.url} is out of url_scope — refusing to navigate (exit 3)")
        return _PreLaunch(exit_code=3)
    if verb == "fill":
        fill_value, err = _resolve_fill_value(args, plan)
        return _PreLaunch(exit_code=err, fill_value=fill_value)
    if verb == "select":
        select_kwargs, err = _resolve_select_option(args)
        return _PreLaunch(exit_code=err, select_kwargs=select_kwargs)
    if verb == "wait-for":
        targets = [
            getattr(args, "selector", None) is not None,
            getattr(args, "url", None) is not None,
            getattr(args, "networkidle", False),
        ]
        if sum(targets) != 1:
            print("ERROR: wait-for needs exactly one of <selector>, --url, or --networkidle")
            return _PreLaunch(exit_code=3)
        if getattr(args, "url", None) is not None and not url_in_scope(args.url, plan.url_scope):
            print(f"ERROR: wait-for --url {args.url} is out of url_scope (exit 3)")
            return _PreLaunch(exit_code=3)
    return _PreLaunch()


def _read_region_text(page: Any, selector: str) -> str:
    """Return the inner-text of the first element matching *selector*, or ``""``."""
    try:
        loc = page.locator(selector)
        return loc.inner_text() or ""
    except Exception:  # noqa: BLE001 - best-effort
        return ""


def _auto_exercise_controls(state_dir: Path, out_dir: Path, page: Any, plan: ExplorePlan) -> None:
    """Mechanically exercise TRIVIAL-kind controls (sort + dropdown) on the current page.

    For each exercisable control:
    1. Capture the *before* text of the nearest data-table / list region.
    2. Exercise the control (click a sort header / select the first non-selected option).
    3. Capture the *after* text.
    4. Emit a Control record (``exercised_by="auto"``) with the before→after delta.

    **Pagination controls are navigation-changing** — every post-click URL is scope-checked
    via ``url_in_scope``; an off-scope URL is refused (the control is emitted with
    ``data_state_change="SCOPE_REFUSED"`` and the page is navigated back).

    The full list of exercised Control records is printed as a JSON array on stdout.
    Filter / edit / modal / checkbox / radio / button / other kinds are NOT auto-exercised
    (left for the agent-driven Phase B).
    """
    from primitives.runtime_explore.shared.controls import detect_controls  # noqa: PLC0415

    url = page.url.split("#", 1)[0]
    # Derive the screen-id from the crawl-graph so the labeled shots share the same id.
    graph_path = state_dir / "crawl-graph.json"
    if graph_path.is_file():
        graph = CrawlGraph.model_validate(json.loads(graph_path.read_text(encoding="utf-8")))
        match = next((s for s in graph.screens if s.url == url), None)
        sid = match.id if match else "AGT-AUTO"
    else:
        sid = "AGT-AUTO"

    controls_found = detect_controls(page)
    exercised: list[Any] = []

    for ctl in controls_found:
        if ctl.kind not in ("sort", "dropdown"):
            continue

        # --- before text: try the closest table/list parent as the affected region ---
        try:
            el = page.locator(ctl.selector).first
            # Walk up to find the nearest <table>, <ul>, <ol>, or <tbody>
            _js = (
                "e => {"
                "  let n = e;"
                "  const TAGS = ['TABLE','TBODY','UL','OL'];"
                "  while (n && !TAGS.includes(n.tagName)) n = n.parentElement;"
                "  if (!n) return null;"
                "  const id = n.id ? '#' + n.id : null;"
                "  const cls = n.classList[0]"
                "    ? n.tagName.toLowerCase() + '.' + n.classList[0] : null;"
                "  return id || cls || n.tagName.toLowerCase();"
                "}"
            )
            region_sel = el.evaluate(_js) or ""
        except Exception:  # noqa: BLE001
            region_sel = ""

        before_text = _read_region_text(page, region_sel) if region_sel else ""

        # --- exercise ---
        pre_url = page.url.split("#", 1)[0]
        data_state_change = ""
        label_slug = ctl.label.lower().replace(" ", "-")[:20] if ctl.label else ctl.kind
        shot_ref = ""
        try:
            if ctl.kind == "sort":
                page.locator(ctl.selector).first.click()
            elif ctl.kind == "dropdown":
                # Select the first option that is NOT currently selected.
                _opts_js = (
                    "el => Array.from(el.options || [])"
                    ".map((o, i) => ({v: o.value, sel: o.selected, i}))"
                )
                opts = page.locator(ctl.selector).first.evaluate(_opts_js)
                unselected = [o for o in (opts or []) if not o.get("sel")]
                if unselected:
                    page.locator(ctl.selector).first.select_option(value=str(unselected[0]["v"]))
                else:
                    # All options selected or single-option dropdown — skip.
                    continue

            # Scope-check the post-exercise URL (navigation-changing controls).
            post_url = page.url.split("#", 1)[0]
            if post_url != pre_url and not url_in_scope(post_url, plan.url_scope):
                # Navigate back and record the refusal.
                page.go_back()
                data_state_change = "SCOPE_REFUSED"
            else:
                after_text = _read_region_text(page, region_sel) if region_sel else ""
                data_state_change = (
                    f"before={before_text[:200]!r} after={after_text[:200]!r}"
                    if before_text != after_text
                    else "(no visible data change detected)"
                )
                # Labeled screenshot AFTER exercising.
                shot_ref = _capture_labeled_screenshot(out_dir, page, sid, url, label_slug)

        except Exception as exc:  # noqa: BLE001 - exercise is best-effort
            data_state_change = f"ERROR: {exc}"

        exercised.append(
            ctl.model_copy(
                update={
                    "exercised_by": "auto",
                    "data_state_change": data_state_change,
                    "screenshot": shot_ref,
                }
            )
        )

    # #1623: persist onto the graph's Screen.controls[] so the Phase-C aggregator
    # reads them. Record the WHOLE detected set — the exercised (sort/dropdown) records
    # with their before/after delta, plus every other detected control as
    # ``detected_only`` — so the #1596 "every detected control is recorded" invariant
    # holds without a manual merge (exercised records supersede by selector).
    _persist_controls_to_graph(state_dir, url, [*controls_found, *exercised])

    print(json.dumps([c.model_dump(mode="json") for c in exercised], indent=2))


def _form_scoped_submit(page: Any, selector: str) -> None:
    """Click *selector*, preferring a match inside a ``<form>`` that has a password field.

    A generic ``button[type=submit]`` selector matches EVERY submit button on the page —
    on a login page that also renders a header search form, the page-wide first match is
    often the search button, so "submit login" silently submits a search (#1624a). This
    scopes the click: if any ``<form>`` containing a ``input[type=password]`` also contains
    a match for *selector*, click THAT match; otherwise fall back to the plain first match
    (byte-identical to the pre-fix behavior for non-login forms).
    """
    scoped = page.locator(
        f"form:has(input[type=password]) :is({selector}), form:has(input[type=password]){selector}"
    )
    try:
        if scoped.count() > 0:
            scoped.first.click()
            return
    except Exception:  # noqa: BLE001, S110 - a malformed :is()/selector falls back to the raw click
        pass
    page.click(selector)


def _do_login(page: Any, args: argparse.Namespace, plan: ExplorePlan) -> int:
    """Fill the login fields AND submit within ONE browser-context/page lifetime (#1622).

    The documented ``fill username`` → ``fill password`` → ``submit`` step sequence CANNOT
    complete a CSRF-protected multi-field form: each step_cli invocation launches a fresh
    context and re-``goto``s the page before its verb, wiping the fields a prior invocation
    filled and rotating the per-render CSRF token. This single verb navigates to the login
    URL, fills both fields, and submits — all in one page lifetime — so the hidden CSRF
    token and both field values survive to submission.

    Selectors: explicit ``--username-selector``/``--password-selector``/``--submit-selector``
    win; otherwise they resolve from the named ``--profile`` (a profiles plan). The
    login URL is ``--login-url`` if given, else the profile's / plan's ``login_url``, else
    the current page. The submit is **form-scoped** (:func:`_form_scoped_submit`) so it
    can't hit a header search form (#1624a). A post-login redirect off the URL scope is a
    clean exit 3.
    """
    username_selector = args.username_selector
    password_selector = args.password_selector
    submit_selector = args.submit_selector
    login_url = args.login_url

    # Fill any unset selectors from the named profile (profiles plans).
    if (username_selector is None or password_selector is None) and (
        plan.auth is not None and plan.auth.profiles
    ):
        u, p, sub, prof_login_url = _resolve_login_selectors(plan, getattr(args, "profile", None))
        username_selector = username_selector or u
        password_selector = password_selector or p
        submit_selector = submit_selector or sub
        login_url = login_url or prof_login_url

    if not username_selector or not password_selector:
        print(
            "ERROR: login needs --username-selector and --password-selector "
            "(or a --profile that declares them)"
        )
        return 3

    if login_url:
        if not url_in_scope(login_url.split("#", 1)[0], plan.url_scope):
            print(f"ERROR: login_url {login_url} is off-scope (exit 3)")
            return 3
        page.goto(login_url, wait_until=plan.budget.wait_until, timeout=plan.budget.page_timeout_ms)

    # One page lifetime: fill both fields, then submit — no intermediate goto/reload, so
    # the CSRF token and filled values survive.
    page.locator(username_selector).first.fill(args.username)
    page.locator(password_selector).first.fill(args.password)
    if submit_selector:
        _form_scoped_submit(page, submit_selector)
    else:
        # No submit selector — submit the form that holds the password field.
        page.locator(password_selector).first.press("Enter")
    page.wait_for_load_state(plan.budget.wait_until)

    landed = page.url.split("#", 1)[0]
    if not url_in_scope(landed, plan.url_scope):
        print(f"ERROR: post-login redirect off-scope to {landed} (exit 3)")
        return 3
    return 0


def _run_step(verb: str, args: argparse.Namespace, plan: ExplorePlan) -> int:  # noqa: PLR0912
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    storage, _ = _state_files(state_dir)
    # Screenshots land in the CONTEXT folder (same screenshots/ dir as Phase A), not the
    # .state dir. Default to state_dir.parent (the SKILL passes --state-dir
    # .foundry/context/runtime/.state, so the parent is the crawler's --out); --out
    # overrides for non-standard layouts.
    out_dir = Path(args.out) if getattr(args, "out", None) else state_dir.parent

    offline = _handle_offline_verb(verb, args, plan, state_dir, storage)
    if offline is not None:
        return offline

    # All validation that can fail BEFORE launching Chromium happens here (goto/fill/
    # select/wait-for scope + arg checks) — a mis-specified verb must never start a
    # browser only to error out (the #1501 discipline, extended to the #1531 verbs).
    prelaunch = _validate_pre_launch(verb, args, plan)
    if prelaunch.exit_code is not None:
        return prelaunch.exit_code
    fill_value = prelaunch.fill_value
    select_kwargs = prelaunch.select_kwargs

    # Lazy import so the module imports without the [browser] extra (air-gapped core).
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    marker = _load_marker(state_dir)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # Any raise while acting on the page (goto/fill/click on a missing selector
        # or a crashed page, storage_state failing) must not orphan Chromium. The
        # finally always tears down the browser; the context too once it exists.
        context = None
        try:
            context = (
                browser.new_context(storage_state=str(storage))
                if storage.is_file()
                else browser.new_context()
            )
            page = context.new_page()
            # Restore the session's current URL — but scope-check it first (defense in
            # depth: a stale/tampered marker must not pivot the browser off-scope). The
            # ``login`` verb manages its own navigation (goto login_url then fill+submit in
            # one page lifetime, #1622), so skip the restore-goto for it — an extra reload
            # would rotate the CSRF token before the login even starts.
            current = marker.get("current_url") or plan.base_url
            if current and not url_in_scope(current.split("#", 1)[0], plan.url_scope):
                current = plan.base_url
            if current and verb != "login":
                page.goto(
                    current,
                    wait_until=plan.budget.wait_until,
                    timeout=plan.budget.page_timeout_ms,
                )

            if verb == "goto":
                page.goto(
                    args.url,
                    wait_until=plan.budget.wait_until,
                    timeout=plan.budget.page_timeout_ms,
                )
                landed = page.url.split("#", 1)[0]
                if not url_in_scope(landed, plan.url_scope):
                    print(f"ERROR: redirected off-scope to {landed} (exit 3)")
                    return 3
            elif verb == "fill":
                # Value was resolved + validated before launch (#1501); fill_value is
                # guaranteed non-None here (the None/error paths returned pre-launch).
                assert fill_value is not None
                _resolve_locator(page, args.selector, args.by, args.nth).fill(fill_value)
            elif verb == "click":
                _resolve_locator(page, args.selector, args.by, args.nth).click()
            elif verb == "submit":
                _form_scoped_submit(page, args.selector)
                page.wait_for_load_state(plan.budget.wait_until)
            elif verb == "login":
                rc = _do_login(page, args, plan)
                if rc != 0:
                    return rc
            elif verb == "select":
                # Option validated pre-launch; select_kwargs is guaranteed non-None here.
                assert select_kwargs is not None
                page.select_option(args.selector, **select_kwargs)  # type: ignore[arg-type]
            elif verb == "wait-for":
                timeout = (
                    args.timeout_ms
                    if getattr(args, "timeout_ms", None) is not None
                    else plan.budget.page_timeout_ms
                )
                try:
                    if getattr(args, "networkidle", False):
                        page.wait_for_load_state("networkidle", timeout=timeout)
                    elif getattr(args, "url", None) is not None:
                        page.wait_for_url(args.url, timeout=timeout)
                    else:
                        _resolve_locator(page, args.selector, args.by, args.nth).wait_for(
                            state=args.state, timeout=timeout
                        )
                except Exception as exc:  # noqa: BLE001 - a wait timeout is a clean exit-1, not a traceback
                    print(f"ERROR: wait-for timed out: {exc}")
                    return 1

            if verb == "snapshot":
                label = getattr(args, "label", None)
                _append_agent_screen(
                    state_dir,
                    out_dir,
                    page,
                    page.url.split("#", 1)[0],
                    page.title(),
                    _extract_forms(page),
                    label=label,
                )

            elif verb == "read":
                loc = _resolve_locator(page, args.selector, args.by, args.nth)
                try:
                    text = loc.inner_text()
                    print(text or "")
                except Exception:  # noqa: BLE001 - best-effort; missing element → empty
                    print("")

            elif verb == "controls" and args.action == "detect":
                from primitives.runtime_explore.shared.controls import (  # noqa: PLC0415
                    detect_controls,
                )

                controls_found = detect_controls(page)
                # #1623: persist onto the graph's Screen.controls[] (keyed by url) so
                # the Phase-C aggregator picks them up — not just stdout.
                _persist_controls_to_graph(state_dir, page.url.split("#", 1)[0], controls_found)
                print(json.dumps([c.model_dump(mode="json") for c in controls_found], indent=2))

            elif verb == "controls" and args.action == "auto-exercise":
                _auto_exercise_controls(state_dir, out_dir, page, plan)

            if verb not in ("read", "controls"):
                print(_observe(page))
            context.storage_state(path=str(storage))
            _save_marker(state_dir, {"current_url": page.url})
        finally:
            if context is not None:
                context.close()
            browser.close()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    # --plan / --state-dir are on a shared parent so they can appear AFTER the verb
    # (the SKILL body calls e.g. `step_cli goto <url> --plan … --state-dir …`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--plan", required=True, type=Path)
    common.add_argument("--state-dir", required=True)

    # Selector-strategy flags shared by the element-targeting verbs (fill/click/wait-for),
    # so the agent can resolve stable selectors (data-testid/role/label/…) before CSS (#1532).
    targeting = argparse.ArgumentParser(add_help=False)
    targeting.add_argument(
        "--by",
        choices=list(_SELECTOR_STRATEGIES),
        default="css",
        help="selector strategy (default css): testid|role|label|text|placeholder|css",
    )
    targeting.add_argument(
        "--nth", default=None, type=int, help="pick the nth (0-based) match of the selector"
    )

    p = argparse.ArgumentParser(prog="runtime-explore-step")
    sub = p.add_subparsers(dest="verb", required=True)
    g = sub.add_parser("goto", parents=[common])
    g.add_argument("url")
    s = sub.add_parser("fill", parents=[common, targeting])
    s.add_argument("selector")
    s.add_argument("value", nargs="?", default=None)
    s.add_argument(
        "--from-seed",
        dest="from_seed",
        default=None,
        help="look the value up in seed_data_ref by field name instead of a literal value",
    )
    # click takes the selector-strategy flags; submit stays a raw form-submit click.
    cl = sub.add_parser("click", parents=[common, targeting])
    cl.add_argument("selector")
    sm = sub.add_parser("submit", parents=[common])
    sm.add_argument("selector")
    # login — fill BOTH fields + submit in ONE browser/page lifetime so a CSRF-protected
    # multi-field form survives to submission (#1622). Composes with `creds`:
    #   step_cli login "$(step_cli creds user …)" "$(step_cli creds pass …)" \
    #     --profile realm_a --plan … --state-dir …
    lg = sub.add_parser("login", parents=[common])
    lg.add_argument("username", help="the username value (e.g. from `step_cli creds user …`)")
    lg.add_argument("password", help="the password value (e.g. from `step_cli creds pass …`)")
    lg.add_argument(
        "--username-selector",
        dest="username_selector",
        default=None,
        help="username field selector (defaults to the --profile's declared selector)",
    )
    lg.add_argument(
        "--password-selector",
        dest="password_selector",
        default=None,
        help="password field selector (defaults to the --profile's declared selector)",
    )
    lg.add_argument(
        "--submit-selector",
        dest="submit_selector",
        default=None,
        help="submit selector (form-scoped, #1624a); omitted → press Enter in the password field",
    )
    lg.add_argument(
        "--login-url",
        dest="login_url",
        default=None,
        help="URL to navigate to before filling (defaults to the --profile's / plan's login_url)",
    )
    lg.add_argument(
        "--profile",
        default=None,
        help="named login profile whose selectors + login_url to use (multi-realm plans)",
    )
    # select — pick a <select> option by exactly one of value/label/index (#1531).
    sel = sub.add_parser("select", parents=[common])
    sel.add_argument("selector")
    sel.add_argument("--value", default=None, help="the option's value attribute")
    sel.add_argument("--label", default=None, help="the option's visible label text")
    sel.add_argument("--index", default=None, type=int, help="the option's 0-based position")
    # wait-for — no-hard-sleep wait for a selector / a URL glob / the network to settle (#1531).
    wf = sub.add_parser("wait-for", parents=[common, targeting])
    wf.add_argument(
        "selector",
        nargs="?",
        default=None,
        help="CSS selector to wait for (default target unless --url/--networkidle)",
    )
    wf.add_argument(
        "--state",
        choices=["visible", "hidden", "attached", "detached"],
        default="visible",
        help="selector state to wait for (default: visible)",
    )
    wf.add_argument("--url", default=None, help="wait until the browser URL matches this glob")
    wf.add_argument(
        "--networkidle",
        action="store_true",
        help="wait until the network settles (SPA data loads)",
    )
    wf.add_argument(
        "--timeout-ms",
        dest="timeout_ms",
        default=None,
        type=int,
        help="override budget.page_timeout_ms for this wait",
    )
    snap = sub.add_parser("snapshot", parents=[common])
    snap.add_argument(
        "--out",
        default=None,
        help="context dir for screenshots (default: the --state-dir's parent)",
    )
    snap.add_argument(
        "--label",
        default=None,
        help=(
            "optional label slug for a NAMED screenshot (#1599); "
            "filename becomes <route>__<label>__<id>.png.  "
            "Without --label behavior is byte-identical to the pre-#1599 form."
        ),
    )
    # read — print the visible text content of a page region so before/after can be diffed.
    rd = sub.add_parser("read", parents=[common, targeting])
    rd.add_argument("selector", help="CSS selector (or --by strategy target) of the region to read")
    # controls detect / controls auto-exercise — per-control evidence (#1599).
    ctrl = sub.add_parser("controls", parents=[common])
    ctrl.add_argument(
        "action",
        choices=["detect", "auto-exercise"],
        help="detect: print the control inventory as JSON; auto-exercise: exercise trivials",
    )
    ctrl.add_argument(
        "--out",
        default=None,
        help="context dir for labeled screenshots (default: the --state-dir's parent)",
    )
    se = sub.add_parser("session", parents=[common])
    se.add_argument("action", choices=["start", "end"])
    c = sub.add_parser("creds", parents=[common])
    c.add_argument("field", help="the credential field to resolve from auth.creds_ref")
    c.add_argument(
        "--profile",
        default=None,
        help="named login profile to resolve creds for (multi-realm plans)",
    )
    c.add_argument(
        "--role",
        default=None,
        help="named role whose OWN creds_ref to resolve (#1563; takes precedence over --profile)",
    )
    # login-selectors — print a named profile's selectors + login_url as JSON (read-only,
    # secret-free) so the SKILL body fills the right realm. Offline (no browser).
    ls = sub.add_parser("login-selectors", parents=[common])
    ls.add_argument(
        "--profile",
        default=None,
        help="named login profile whose selectors to print (defaults to the sole profile)",
    )
    return p


#: Verbs that never launch a browser — resolvable without the [browser] extra.
_OFFLINE_VERBS = frozenset({"session", "creds", "login-selectors"})


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # Load + validate the plan first, so a bad plan reports its own exit code (2/3)
    # regardless of whether the browser extra is installed.
    try:
        plan = load_plan(args.plan)
    except PlanError as exc:
        print(f"ERROR: {exc}")
        return exc.exit_code
    # Offline verbs (session/creds) need no browser; only gate the browser-driving
    # verbs on the [browser] extra so credential resolution works air-gapped.
    if args.verb not in _OFFLINE_VERBS:
        ok, hint = playwright_available()
        if not ok:
            print(hint)
            return CAPABILITY_UNAVAILABLE_EXIT
    try:
        return _run_step(args.verb, args, plan)
    except PlanError as exc:
        # A credential/seed reference that fails to resolve (e.g. `creds`/`fill
        # --from-seed`) surfaces its exit code instead of a traceback.
        print(f"ERROR: {exc}")
        return exc.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["main"]
