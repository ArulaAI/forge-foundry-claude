"""`forge-validate` CLI — lazy-loaded subcommands (thin entry-point).

Each subcommand lives in its own `forge_validate.commands.<name>` module.
The LazyGroup below imports a subcommand only when it is actually invoked,
so `forge-validate --version` and `forge-validate --help` do not pay the
import cost of every primitive (closes #101 / TOOL-2).

Vendored (trimmed) from forge-prowess-primitives@v0.4.0 — see CHANGELOG-VENDOR.md
for divergence. Curriculum-specific checks (lab structure, sim.yaml, exercises
subtree) are not part of foundry; only slug naming + manifest validation remain.
"""

from __future__ import annotations

import importlib
from typing import Any, Final

import click

# ---------------------------------------------------------------------------
# Shared CLI helpers (re-exported for backward compat + used by command modules)
# ---------------------------------------------------------------------------
from forge_validate._cli_common import _RECOMMENDATION_ORDER, _SEVERITY_ORDER

# Explicitly exported so command modules can access names via `forge_validate.cli`.
__all__ = [
    "_RECOMMENDATION_ORDER",
    "_SEVERITY_ORDER",
    "main",
]


# ---------------------------------------------------------------------------
# Lazy-loading Click group
# ---------------------------------------------------------------------------

# v2.0.0 breaking change: 14 skill-first primitive commands were removed from this
# registry.  Running `forge-validate <removed-name>` now yields Click's
# "No such command" error (hard break — no stubs).  The command modules under
# forge_validate/commands/ are archived in PR-B (phase3-archive-python).
#
# Removed (skill-first, archived in PR-B):
#   plan, code-gen, sdet-architect, threat-model,
#   cloud-arch-review, frontend-design, slo-author, spec-write
# (mod-playbook-discovery, mod-sql-strategist, mod-sp-converter re-registered
#  as Python-load-bearing in v3; work-orchestrator re-registered in #971;
#  test-from-spec re-registered as Python-load-bearing in #1114/#1115 (B2/B3);
#  mod-sql-modernization (M0) remains skill-first/orchestrator per charter §4.1 —
#  it is an orchestrator with no cli.py command key.)
# (M0/M3 slash commands renamed to the mod-sql-* namespace in #1326/#1327 (SP5 of
#  epic #1311): /mod-legacy-modernization → /mod-sql-modernization and
#  /mod-modernization-strategist → /mod-sql-strategist. Only the CLI command-map
#  key below changes; the Python module (mod_modernization_strategist) is
#  unchanged to avoid import/test churn.)
# (mod-playbook-discovery re-registered in v3 as Python-load-bearing, #641;
#  mod-sql-strategist (M3) re-registered in v3 as Python-load-bearing, #644;
#  mod-sp-converter re-registered in v3 as Python-load-bearing, #645/#646.)
_COMMAND_MODULES: Final[dict[str, str]] = {
    "validate": "validate",
    "code-gen": "code_gen",
    "failure-mode-audit": "failure_mode_audit",
    "test-from-spec": "test_from_spec",
    "test-maintainer": "test_maintainer",
    "test-scaffold": "test_scaffold",
    "repo-digest": "repo_digest",
    "production-failure-audit": "production_failure_audit",
    "compliance-map": "compliance_map",
    "architecture-review": "architecture_review",
    "data-flow-map": "data_flow_map",
    "code-review": "code_review",
    "security-review": "security_review",
    "api-contract-review": "api_contract_review",
    "spec-validate": "spec_validate",
    "confidence-gate": "confidence_gate",
    "work-orchestrator": "work_orchestrator",
    "rules-gen": "rules_gen",
    "mod-db-schema-analyze": "mod_db_schema_analyze",
    "mod-stored-proc-analyze": "mod_stored_proc_analyze",
    "mod-sql-strategist": "mod_modernization_strategist",
    "mod-sp-converter": "mod_sp_converter",
    "mod-playbook-discovery": "mod_playbook_discovery",
    "mod-synth-logs": "mod_synth_logs",
    "mod-modernization-scope": "mod_modernization_scope",
    "modernization-rollup": "modernization_rollup",
    "red-team-review": "red_team_review",
    "functional-map": "functional_map",
}


class _LazyCommandsDict(dict):  # type: ignore[type-arg]
    """Dict subclass that lazily loads a command module on key access.

    Used as `LazyCLI.commands` so that test code like
    `main.commands["plan"]` works without pre-importing every module.
    """

    def __missing__(self, name: str) -> click.Command:
        module_name = _COMMAND_MODULES.get(name)
        if module_name is None:
            raise KeyError(name)
        module = importlib.import_module(f"forge_validate.commands.{module_name}")
        cmd = module.cli
        self[name] = cmd  # cache for subsequent access
        return cmd  # type: ignore[no-any-return]


class LazyCLI(click.Group):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Replace the empty dict with a lazy-loading one so direct
        # dict access (e.g. main.commands["plan"]) works in tests.
        self.commands: dict[str, click.Command] = _LazyCommandsDict()

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(_COMMAND_MODULES)

    def get_command(self, ctx: click.Context, name: str) -> click.Command | None:
        module_name = _COMMAND_MODULES.get(name)
        if module_name is None:
            return None
        module = importlib.import_module(f"forge_validate.commands.{module_name}")
        cmd = getattr(module, "cli", None)
        if cmd is None:
            return None
        return cmd  # type: ignore[no-any-return]


@click.group(cls=LazyCLI)
@click.version_option(package_name="forge-foundry-primitives")
def main() -> None:
    """Forge Foundry CLI — run primitives, validate layout, and sync tooling."""


if __name__ == "__main__":
    main()
