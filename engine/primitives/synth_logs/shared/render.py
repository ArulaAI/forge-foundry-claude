"""M6 /mod-synth-logs — Jinja2 template rendering helper.

Phase C's ``shared/generate.py`` (lands in #473) orchestrates two
distinct steps: (1) *render* the ``generate_logs.py`` script source from
the Jinja2 templates in ``primitives/synth_logs/shared/templates/``,
and (2) *write + execute* that script per ``--run-script
{subprocess,import,skip}``. This module owns step (1) only: a pure
function that takes the Phase B outputs (``ScenarioPlan`` +
``EntitiesDigest``) plus the deterministic seed, returns the rendered
Python source as a ``str``, and never touches the filesystem.

The rendered source is the same script shape the BankDemo
``synth-logs-generate.md`` slash command produces by hand — see
``primitives/synth_logs/SPEC.md`` §"Pipeline" and the canonical
reference at ``BankDemo/.claude/commands/synth-logs-generate.md``.

The ``_render_<format>()`` helpers inside the rendered script all have
implemented bodies as of M6-TEMPLATE-4 / #472: every format in
``_ALL_LOG_FORMATS`` ships a matching ``log_<format>.j2`` body template
that gets rendered to Python source and interpolated into the function
body. No stubs remain — the dispatcher emits a real function body for
every ``LogFormat`` literal.

Per-format bodies and the issues that landed them:

- ``cics_transaction`` / ``cics_access`` / ``network`` / ``database``
  — M6-TEMPLATE-2 / #470.
- ``enterprise_server`` / ``downstream_calls`` / ``upstream_requests``
  / ``cics_queues`` — M6-TEMPLATE-3 / #471.
- ``metrics`` / ``security_audit`` — M6-TEMPLATE-4 / #472 (this PR
  flips the last two formats out of the stub bucket).

``FORMAT_TO_ISSUE`` is the cross-walk between the ``LogFormat`` literal
and the issue label baked into the implemented bodies' module-level
docstring. The outer ``generate_logs.py.j2`` template no longer falls
back to a ``NotImplementedError`` stub for any format — the
``rendered_bodies`` dict covers the full ``LogFormat`` surface.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib import resources
from typing import TYPE_CHECKING, Final

from jinja2 import Environment, StrictUndefined, select_autoescape

from primitives.synth_logs.shared.models import (
    _ALL_LOG_FORMATS,
    EntitiesDigest,
    LogFormat,
    ScenarioPlan,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Format → follow-up issue mapping.
#
# Each follow-up issue flips one or more ``_render_<format>()`` stubs on.
# The mapping is baked into the rendered script's ``NotImplementedError``
# messages so a consumer reading the stub traceback gets a direct pointer
# to the issue that will implement it.
#
# ``_IMPLEMENTED_FORMATS`` is the per-PR axis: the formats whose body
# template ships with this revision. The dispatcher emits real
# function bodies for these and ``NotImplementedError`` stubs for the
# rest. As each M6-TEMPLATE-* issue lands, one or more formats move from
# the stub bucket to the implemented bucket here.
# ---------------------------------------------------------------------------

FORMAT_TO_ISSUE: Final[dict[str, str]] = {
    "cics_transaction": "#470 (M6-TEMPLATE-2)",
    "cics_access": "#470 (M6-TEMPLATE-2)",
    "network": "#470 (M6-TEMPLATE-2)",
    "database": "#470 (M6-TEMPLATE-2)",
    "enterprise_server": "#471 (M6-TEMPLATE-3)",
    "downstream_calls": "#471 (M6-TEMPLATE-3)",
    "upstream_requests": "#471 (M6-TEMPLATE-3)",
    "cics_queues": "#471 (M6-TEMPLATE-3)",
    "metrics": "#472 (M6-TEMPLATE-4)",
    "security_audit": "#472 (M6-TEMPLATE-4)",
}
"""Maps every ``LogFormat`` literal to the GH issue that implements the
matching per-format Jinja2 template. Exhaustiveness is guarded by
``_assert_format_coverage()`` below; the static guard fires at import
time so a drift between ``LogFormat`` and this map is a hard failure,
not a silent fall-through."""


_IMPLEMENTED_FORMATS: Final[tuple[LogFormat, ...]] = (
    "cics_transaction",
    "cics_access",
    "network",
    "database",
    "enterprise_server",
    "downstream_calls",
    "upstream_requests",
    "cics_queues",
    "metrics",
    "security_audit",
)
"""Formats whose per-format body template ships in this revision.
M6-TEMPLATE-4 / #472 flipped the final two (``metrics`` and
``security_audit``) into the implemented set — all 10 of 10 formats
are now wired. The drift guard ``_assert_implemented_coverage`` fires
at import time if this tuple ever diverges from ``_BODY_RENDERERS``."""


_TEMPLATE_PACKAGE: Final[str] = "primitives.synth_logs.shared.templates"
_TEMPLATE_NAME: Final[str] = "generate_logs.py.j2"
_FUNCTION_BODY_INDENT: Final[str] = "    "


def _assert_format_coverage() -> None:
    """Guarantee ``FORMAT_TO_ISSUE`` covers every ``LogFormat`` literal.

    Run at import time. A mismatch raises ``RuntimeError`` so a future
    addition to ``LogFormat`` (or a stray removal here) is caught at
    process start, not at render time when the missing key would
    silently surface as a Jinja2 ``StrictUndefined`` error.
    """
    missing = set(_ALL_LOG_FORMATS) - FORMAT_TO_ISSUE.keys()
    extra = FORMAT_TO_ISSUE.keys() - set(_ALL_LOG_FORMATS)
    if missing or extra:
        raise RuntimeError(
            "FORMAT_TO_ISSUE drift vs LogFormat literal: "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )


_assert_format_coverage()


def _read_template_source(name: str = _TEMPLATE_NAME) -> str:
    """Read a Jinja2 template source from the templates package.

    Uses ``importlib.resources`` so the lookup works whether the
    foundry library is installed as a wheel, run from a source
    checkout, or zip-deployed. The template files are shipped under
    ``primitives/synth_logs/shared/templates/`` and bundled into the
    wheel by the ``[tool.hatch.build.targets.wheel] packages =
    ["..."]`` line in ``pyproject.toml`` (``primitives`` is one of the
    declared packages, so its subpackages are bundled too).
    """
    pkg = resources.files(_TEMPLATE_PACKAGE)
    return (pkg / name).read_text(encoding="utf-8")


def _build_environment() -> Environment:
    """Build the Jinja2 ``Environment`` used to render the skeleton.

    ``StrictUndefined`` is the anti-hallucination guard (design spec
    §"Anti-hallucination"): a missing context variable raises rather
    than rendering an empty string. ``autoescape`` is intentionally
    off — we're emitting Python source, not HTML — but the
    ``select_autoescape`` call is wired with the conservative defaults
    so adding HTML-suffix templates later is a no-op.
    """
    return Environment(
        autoescape=select_autoescape(default=False, default_for_string=False),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def _indent_body(source: str, indent: str = _FUNCTION_BODY_INDENT) -> str:
    """Indent every non-blank line of ``source`` by ``indent``.

    The per-format body templates are written as unindented Python
    source so they read like a normal function body; the outer
    ``generate_logs.py.j2`` template interpolates them inside the
    ``_render_<format>()`` function so they need a four-space indent
    to be syntactically valid. Blank lines stay blank so we don't
    introduce stray whitespace that ``ruff format`` would later flag.
    """
    out_lines: list[str] = []
    for line in source.splitlines():
        if line.strip():
            out_lines.append(indent + line)
        else:
            out_lines.append("")
    return "\n".join(out_lines)


def _render_cics_transaction() -> str:
    """Load + indent the ``cics_transaction`` body template.

    Returns the Python source that becomes ``_render_cics_transaction``'s
    function body in the emitted ``generate_logs.py``. Format shape:
    ``<timestamp> <trans_id> <program_name> <user_id> <terminal_id> <duration_ms> <abend_code|OK>``.
    """
    env = _build_environment()
    template = env.from_string(_read_template_source("log_cics_transaction.j2"))
    return _indent_body(template.render())


def _render_cics_access() -> str:
    """Load + indent the ``cics_access`` body template.

    Format shape::

        <timestamp> <program_name> <vsam_file>
        <mode READ|WRITE|REWRITE|DELETE> <record_count> <duration_us>
    """
    env = _build_environment()
    template = env.from_string(_read_template_source("log_cics_access.j2"))
    return _indent_body(template.render())


def _render_network() -> str:
    """Load + indent the ``network`` body template.

    Format shape:
    ``<timestamp> <src_ip>:<src_port> -> <dst_ip>:<dst_port> <bytes_sent> <bytes_recv> <protocol>``.
    """
    env = _build_environment()
    template = env.from_string(_read_template_source("log_network.j2"))
    return _indent_body(template.render())


def _render_database() -> str:
    """Load + indent the ``database`` body template.

    Format shape::

        <timestamp> <program> <sql_kind SELECT|INSERT|UPDATE|DELETE>
        <table> <rows_affected> <duration_ms> <sqlcode>
    """
    env = _build_environment()
    template = env.from_string(_read_template_source("log_database.j2"))
    return _indent_body(template.render())


def _render_enterprise_server() -> str:
    """Load + indent the ``enterprise_server`` body template.

    Format shape::

        <timestamp> ES LEVEL=<INFO|WARN|ERROR>
        SUBJECT=<program|file> MSG=<event>

    Rocket Enterprise Server / CICS region log: system-level startup
    messages, region warnings, system-task lifecycle. Subject is drawn
    from ``programs`` (60%) or ``files`` (40%).
    """
    env = _build_environment()
    template = env.from_string(_read_template_source("log_enterprise_server.j2"))
    return _indent_body(template.render())


def _render_downstream_calls() -> str:
    """Load + indent the ``downstream_calls`` body template.

    Format shape: JSON Lines, one record per line. Fields:
    ``call_type`` (LINK / XCTL / VSAM_READ / EXEC_SQL / HTTP),
    ``direction`` (always ``outbound``), ``duration_ms``, ``response``,
    ``source_program``, ``target``, ``timestamp``. ``source_program``
    drawn from ``programs``; ``target`` drawn from ``programs`` / ``files``
    / ``networks_and_ports`` per call type.
    """
    env = _build_environment()
    template = env.from_string(_read_template_source("log_downstream_calls.j2"))
    return _indent_body(template.render())


def _render_upstream_requests() -> str:
    """Load + indent the ``upstream_requests`` body template.

    Format shape: JSON Lines, one record per line. Fields: ``direction``
    (always ``inbound``), ``duration_ms``, ``source_ip``, ``source_type``
    (terminal / batch / escwa_api), ``terminal_id`` (only for terminal),
    ``timestamp``, ``transaction``. Models inbound TN3270E sessions,
    batch initiators, and ESCWA admin polls.
    """
    env = _build_environment()
    template = env.from_string(_read_template_source("log_upstream_requests.j2"))
    return _indent_body(template.render())


def _render_cics_queues() -> str:
    """Load + indent the ``cics_queues`` body template.

    Format shape::

        <timestamp> QUEUE TYPE=<TS|TD> QUEUE=<name>
        OP=<READQ|WRITEQ> ITEM=<n> LENGTH=<n>
        RESP=<NORMAL|NOSPACE>

    CICS Temporary Storage (TS) and Transient Data (TD) queue activity.
    TS queue names are synthesised from a ``transactions`` entry; TD
    queue names come from a closed CICS system-constant pool
    (``CSMT``, ``CSML``, ``CSSL``, ``CDBC``).
    """
    env = _build_environment()
    template = env.from_string(_read_template_source("log_cics_queues.j2"))
    return _indent_body(template.render())


def _render_metrics() -> str:
    """Load + indent the ``metrics`` body template.

    Format shape::

        <timestamp> <metric_name> <metric_kind gauge|counter|histogram>
        <value> <unit> <labels>

    Enterprise Server / CICS region performance-metric snapshots
    covering CPU, memory, TPS, transaction latency, abend counters,
    CICS storage headroom, and VSAM I/O throughput (BankDemo
    reference §"LOG FILE 9"). Aggregates per-minute (or per-5-minute
    at higher scale) — the renderer spaces ``entries_count`` snapshots
    linearly across the spec window. ``labels`` is a JSON object with
    ``region`` always present plus a metric-specific second key
    (``file`` for ``vsam_io_throughput``, drawn from
    ``entities_digest["files"]``).
    """
    env = _build_environment()
    template = env.from_string(_read_template_source("log_metrics.j2"))
    return _indent_body(template.render())


def _render_security_audit() -> str:
    """Load + indent the ``security_audit`` body template.

    Format shape: JSON Lines, one record per line. Fields:
    ``event_kind`` (signon_success / signon_failure / password_change /
    privileged_op / user_lockout), ``user_id``, ``terminal_id``,
    ``source_ip``, ``resource``, ``result`` (OK / DENIED), ``timestamp``.
    Models the CICS security audit trail (BankDemo reference
    §"LOG FILE 10"). The Day-3 cascading-incident post-mortem trace
    pulls ``result=DENIED`` lines and walks back to the originating
    session via ``terminal_id`` + ``source_ip``.
    """
    env = _build_environment()
    template = env.from_string(_read_template_source("log_security_audit.j2"))
    return _indent_body(template.render())


_BODY_RENDERERS: Final[dict[LogFormat, Callable[[], str]]] = {
    "cics_transaction": _render_cics_transaction,
    "cics_access": _render_cics_access,
    "network": _render_network,
    "database": _render_database,
    "enterprise_server": _render_enterprise_server,
    "downstream_calls": _render_downstream_calls,
    "upstream_requests": _render_upstream_requests,
    "cics_queues": _render_cics_queues,
    "metrics": _render_metrics,
    "security_audit": _render_security_audit,
}
"""Per-format body renderers. Keys must be a subset of
``_IMPLEMENTED_FORMATS``; the consistency check below fires at import
time so a stale ``_IMPLEMENTED_FORMATS`` tuple is a hard failure."""


def _assert_implemented_coverage() -> None:
    """Guarantee ``_BODY_RENDERERS`` matches ``_IMPLEMENTED_FORMATS``.

    Run at import time so a developer adding a new implemented format
    has to update both axes in lock-step. A mismatch raises
    ``RuntimeError`` with the missing / extra format names.
    """
    implemented = set(_IMPLEMENTED_FORMATS)
    renderers = set(_BODY_RENDERERS.keys())
    if implemented != renderers:
        raise RuntimeError(
            "_IMPLEMENTED_FORMATS vs _BODY_RENDERERS drift: "
            f"only_in_tuple={sorted(implemented - renderers)} "
            f"only_in_dict={sorted(renderers - implemented)}"
        )


_assert_implemented_coverage()


def _render_all_bodies() -> dict[str, str]:
    """Render every implemented per-format body to indented Python.

    Returned mapping is passed into ``generate_logs.py.j2`` as the
    ``rendered_bodies`` context variable: the outer template emits a
    real function body for any format present in the dict, and falls
    back to the ``NotImplementedError`` stub for the rest.
    """
    return {fmt: renderer() for fmt, renderer in _BODY_RENDERERS.items()}


def _render_template(
    scenario_plan: ScenarioPlan,
    entities_digest: EntitiesDigest,
    seed: int,
) -> str:
    """Render ``generate_logs.py.j2`` into a Python source string.

    The returned string is syntactically valid Python (the
    ``tests/test_synth_logs_template_skeleton.py`` test parses it with
    ``ast.parse``) and imports stdlib only at top level (the same test
    walks the AST to assert this — no third-party imports leak into the
    consumer's repo from this skeleton). Per-format function bodies are
    free to import additional stdlib modules at function scope (e.g.
    ``datetime``); the top-level import-purity check ignores those.

    This function is *pure*: it does not write to disk. The Phase C
    orchestrator (``shared/generate.py``, lands in #473) takes the
    returned string and writes it to ``--script-path`` plus optionally
    executes it per ``--run-script``.

    Parameters
    ----------
    scenario_plan:
        Phase B output. Its ``log_specs`` drive the dispatch loop in
        the rendered script — one ``_render_<format>()`` call per spec.
    entities_digest:
        Phase A output. Baked into the rendered script as
        ``ENTITIES_DIGEST`` so every renderer can ground every
        synthesised entity against the closed catalog (design spec
        §"Anti-hallucination" layer 1).
    seed:
        Deterministic seed. Baked as the default for the rendered
        script's ``--seed`` flag; passing ``--seed`` on the command
        line overrides it.

    Returns
    -------
    str
        The rendered Python source.
    """
    env = _build_environment()
    template = env.from_string(_read_template_source())

    log_specs_dicts = [spec.model_dump(mode="json") for spec in scenario_plan.log_specs]

    return template.render(
        scenario_plan_json=scenario_plan.model_dump_json(),
        entities_digest_json=entities_digest.model_dump_json(),
        log_specs_json=json.dumps(log_specs_dicts, sort_keys=True, default=str),
        log_formats=list(_ALL_LOG_FORMATS),
        format_to_issue=FORMAT_TO_ISSUE,
        rendered_bodies=_render_all_bodies(),
        seed=int(seed),
        generated_at=datetime.now(UTC).isoformat(),
    )


__all__ = [
    "FORMAT_TO_ISSUE",
    "_render_template",
]
