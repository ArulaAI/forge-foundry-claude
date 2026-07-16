"""explore-plan.yaml parse + validate for runtime_explore (#1471).

Secrets are by REFERENCE only (env-var name or gitignored file path) — an inline
password/token/secret anywhere in the plan is rejected (exit 3). The URL-scope is an
allow/deny glob set that gates **every** navigation before the browser is told to go
there — the runtime containment guard (SEC-family discipline: the crawler/step-CLI
cannot wander off the allowed domain).

**URL-scope glob note.** Matching is stdlib ``fnmatch`` over the FULL URL, where
``*`` (and ``**``) match any characters including ``/``. A pattern like
``https://app.example/**`` matches every path under the host but NOT a lookalike host
(``https://app.example.attacker.com/`` is rejected — the literal ``/`` after the host
does not match ``.attacker``). To also allow the bare base URL (no trailing slash),
list both ``https://app.example`` and ``https://app.example/**`` in ``allow`` (or use
``https://app.example**``).

**Case sensitivity.** ``fnmatch`` is case-sensitive on POSIX, so match your globs to
the app's actual casing. In particular a ``deny`` glob only blocks the exact case it
spells (``**/admin/**`` does not block ``/ADMIN/x``) — spell deny paths in the case the
app serves. The case-sensitive *allow* glob still bounds the domain safely regardless
(a case-variant host is off-allowlist and blocked — the guard errs toward blocking).
"""

from __future__ import annotations

import fnmatch
from pathlib import Path  # noqa: TC003
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanError(ValueError):
    """A plan-loading failure carrying its conventional exit code (2 or 3)."""

    def __init__(self, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class UrlScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


#: Playwright navigation wait-strategies. ``domcontentloaded`` (the default) is fast and
#: right for server-rendered pages; ``networkidle`` waits until the network settles and is
#: the correct choice for Angular / SPA targets that fetch their content after the initial
#: HTML (a ``domcontentloaded``-only crawl under-discovers them). ``load`` waits for the
#: ``load`` event (all sub-resources). See ``skill/references/legacy-web-playbook.md`` (#1530).
WaitUntil = Literal["domcontentloaded", "networkidle", "load"]


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_pages: int = 200
    max_depth: int = 6
    page_timeout_ms: int = 15000
    total_timeout_s: int = 900
    #: Navigation wait-strategy applied to every ``page.goto`` (crawl + step-CLI). Default
    #: ``domcontentloaded`` keeps behavior byte-identical; set ``networkidle`` for SPAs.
    wait_until: WaitUntil = "domcontentloaded"


class AuthProfile(BaseModel):
    """One named login realm.

    Multi-realm legacy apps often share ONE login URL but distinguish realms by
    different input selectors (each realm's username/password fields have distinct
    ``name``/``id`` attributes). A profile names one such realm: its selectors, an
    optional per-profile ``login_url`` override, and its own credential *reference*
    (secrets are never inline — see :func:`_reject_inline_secrets`).

    Selectors and profile names are **operator-supplied and fully arbitrary** — there
    are no assumed selector conventions or hard-coded realm names anywhere here.
    """

    model_config = ConfigDict(extra="forbid")
    username_selector: str
    password_selector: str
    #: Optional; when omitted the login step uses a generic submit (e.g. Enter / the
    #: form's implicit submit) rather than a specific button selector.
    submit_selector: str | None = None
    #: Optional per-profile override of the top-level ``login_url`` (the shared default).
    login_url: str | None = None
    creds_from: Literal["env", "file"]
    creds_ref: str


class AuthRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["agent_login"]
    #: The shared default login URL. Always required at the top level; a profile may
    #: override it per-realm via ``AuthProfile.login_url``.
    login_url: str
    # Flat single-realm shape: top-level creds_from + creds_ref (today's shape). These
    # are OPTIONAL so a profiles-only plan need not carry top-level creds — the model
    # validator below enforces EXACTLY ONE of {flat creds, profiles}.
    creds_from: Literal["env", "file"] | None = None
    creds_ref: str | None = None
    #: Named login realms (multi-realm apps). Empty for the flat single-realm shape.
    profiles: dict[str, AuthProfile] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> AuthRef:
        """Enforce EXACTLY ONE of the two auth shapes.

        * **flat single-realm** — ``creds_from`` + ``creds_ref`` set, ``profiles`` empty
          (today's shape; byte-identical behavior).
        * **profiles** — ``profiles`` non-empty, top-level ``creds_from``/``creds_ref``
          NOT set (credentials live per-profile).

        Mixing both, or providing neither, is rejected.
        """
        has_flat = self.creds_from is not None or self.creds_ref is not None
        has_profiles = bool(self.profiles)
        if has_flat and has_profiles:
            raise ValueError(
                "auth provides BOTH top-level creds_from/creds_ref AND profiles — "
                "provide EITHER flat creds_from/creds_ref OR profiles, not both"
            )
        if not has_flat and not has_profiles:
            raise ValueError(
                "auth needs EITHER flat creds_from/creds_ref OR at least one profile "
                "(the auth block carries no credential reference)"
            )
        if has_flat and (self.creds_from is None or self.creds_ref is None):
            raise ValueError("flat auth needs BOTH creds_from and creds_ref (found only one)")
        return self


class RoleRef(BaseModel):
    """One user-role perspective to explore the app from (#1563).

    A role is a named user *point of view* (e.g. an admin vs a regular user). A
    multi-role run does a FULL separate crawl + deep-drive per role into its own
    per-role state slice, then the aggregator merges the per-role graphs and tags each
    screen with the role(s) that reached it — surfacing the role-gated delta.

    ``login_profiles`` is an **ordered** list of :class:`AuthProfile` keys (declared under
    ``auth.profiles``). More than one entry means a **CHAINED login**: the role
    authenticates through each named realm in sequence (realm 1 → realm 2 → …), which
    legacy multi-realm apps require (an outer SSO realm then an inner app realm). Each
    profile supplies the input SELECTORS; the role supplies the actual credential
    *reference* via its own ``creds_from`` + ``creds_ref`` (secrets by reference only).

    An empty ``login_profiles`` is allowed ONLY when ``auth`` is a flat single-realm login
    (no ``auth.profiles``): the role then authenticates via that flat realm's selectors
    (supplied by the SKILL body) with the role's own credential reference.
    """

    model_config = ConfigDict(extra="forbid")
    #: Ordered ``auth.profiles`` keys. Empty ⇒ use the flat single-realm login. More than
    #: one ⇒ chained login (authenticate through each realm in order).
    login_profiles: tuple[str, ...] = ()
    creds_from: Literal["env", "file"]
    creds_ref: str


class ExplorePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    url_scope: UrlScope
    budget: Budget = Field(default_factory=Budget)
    # auth is OPTIONAL: a public / no-auth app (e.g. a local dev instance) needs no
    # login block. Phase A (the crawl) never uses auth; it is only exercised in Phase B
    # when the agent crosses a login frontier via `step_cli creds`. Omit it when the
    # target needs no authentication.
    auth: AuthRef | None = None
    seed_data_ref: str | None = None
    #: Named user-role perspectives (#1563). Empty ⇒ today's single-perspective run
    #: (byte-identical). Non-empty ⇒ a full separate crawl per role, merged + role-tagged.
    roles: dict[str, RoleRef] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_roles(self) -> ExplorePlan:
        """Cross-check the roles block against the auth shape (#1563).

        * ``roles`` non-empty ⇒ ``auth`` MUST be present (roles authenticate via login).
        * When ``auth.profiles`` is used, each role's ``login_profiles`` entries MUST name
          declared profile keys, and ``login_profiles`` MUST be non-empty (the role must
          say which realm(s) to log in through).
        * When ``auth`` is flat single-realm (no profiles), a role's ``login_profiles``
          MUST be empty (there are no profiles to name); the role uses the flat login.
        """
        if not self.roles:
            return self
        if self.auth is None:
            raise ValueError(
                "roles are declared but there is no `auth` block — roles authenticate via "
                "login, so add an auth block (flat single-realm or profiles)"
            )
        profiles = self.auth.profiles
        for role_name, role in self.roles.items():
            if profiles:
                if not role.login_profiles:
                    raise ValueError(
                        f"role '{role_name}' has an empty login_profiles but auth uses "
                        f"named profiles — list the profile(s) to log in through "
                        f"(available: {', '.join(sorted(profiles)) or '(none)'})"
                    )
                for pref in role.login_profiles:
                    if pref not in profiles:
                        raise ValueError(
                            f"role '{role_name}' login_profiles names unknown profile "
                            f"'{pref}' — available profiles: "
                            f"{', '.join(sorted(profiles)) or '(none)'}"
                        )
            elif role.login_profiles:
                raise ValueError(
                    f"role '{role_name}' lists login_profiles {list(role.login_profiles)} "
                    "but auth is a flat single-realm login (no auth.profiles) — leave "
                    "login_profiles empty so the role uses the flat login"
                )
        return self


# Keys that would carry a raw secret inline — forbidden ANYWHERE in the plan. Secrets
# must be by reference (auth.creds_from + creds_ref pointing at an env var / gitignored
# file), never in the committed plan.
_SECRET_KEYS = frozenset(
    {"password", "passwd", "pwd", "token", "secret", "api_key", "apikey", "cookie", "bearer"}
)


def _reject_inline_secrets(raw: object, where: str = "plan") -> None:
    if isinstance(raw, dict):
        for k, v in raw.items():
            if str(k).lower() in _SECRET_KEYS:
                raise PlanError(
                    f"inline secret '{k}' in {where} — provide secrets by reference "
                    "(auth.creds_from: env|file + creds_ref), never inline",
                    exit_code=3,
                )
            _reject_inline_secrets(v, f"{where}.{k}")
    elif isinstance(raw, list):
        for i, v in enumerate(raw):
            _reject_inline_secrets(v, f"{where}[{i}]")


def load_plan(path: Path) -> ExplorePlan:
    """Load + validate an explore-plan.yaml. Raises :class:`PlanError` (exit 2/3)."""
    if not path.is_file():
        raise PlanError(f"explore-plan not found: {path}", exit_code=2)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PlanError(f"explore-plan is not valid YAML: {exc}", exit_code=3) from exc
    if not isinstance(raw, dict):
        raise PlanError("explore-plan must be a YAML mapping", exit_code=3)
    _reject_inline_secrets(raw)
    try:
        return ExplorePlan.model_validate(raw)
    except ValueError as exc:
        raise PlanError(f"explore-plan failed validation: {exc}", exit_code=3) from exc


def url_in_scope(url: str, scope: UrlScope) -> bool:
    """True iff *url* matches an allow glob AND no deny glob. Deny wins.

    This is the containment guard: it is checked BEFORE any navigation, so an
    off-allowlist URL is never visited (see the module glob note).
    """
    if any(fnmatch.fnmatch(url, pat) for pat in scope.deny):
        return False
    return any(fnmatch.fnmatch(url, pat) for pat in scope.allow)


__all__ = [
    "AuthProfile",
    "AuthRef",
    "Budget",
    "ExplorePlan",
    "PlanError",
    "RoleRef",
    "UrlScope",
    "WaitUntil",
    "load_plan",
    "url_in_scope",
]
