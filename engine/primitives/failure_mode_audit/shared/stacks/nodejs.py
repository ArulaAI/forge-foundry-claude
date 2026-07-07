"""FM1 (hallucinated dependencies) detector for Node.js / TypeScript.

Foundry-native (no prowess vendor source — prowess U1's YAML had Node.js
rules defined but no Python handler implemented them). Reads package.json
deps + Node.js built-ins. Handles scoped packages (@org/pkg) and subpath
imports ('pkg/sub' resolves to 'pkg').
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from primitives._shared.scan_exclusions import walk_source_files
from primitives.failure_mode_audit.shared.findings import Finding

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.failure_mode_audit.shared.config import AuditConfig

_NODE_BUILTINS: frozenset[str] = frozenset(
    [
        "assert",
        "async_hooks",
        "buffer",
        "child_process",
        "cluster",
        "console",
        "crypto",
        "dgram",
        "diagnostics_channel",
        "dns",
        "domain",
        "events",
        "fs",
        "fs/promises",
        "http",
        "http2",
        "https",
        "inspector",
        "module",
        "net",
        "os",
        "path",
        "path/posix",
        "path/win32",
        "perf_hooks",
        "process",
        "punycode",
        "querystring",
        "readline",
        "repl",
        "stream",
        "string_decoder",
        "sys",
        "timers",
        "tls",
        "trace_events",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "wasi",
        "worker_threads",
        "zlib",
        "node:fs",
        "node:path",
        "node:crypto",
        "node:http",
        "node:url",
        "node:os",
    ]
)

_IMPORT_FROM = re.compile(r"""(?:import|export)\s+(?:[^'"]*?\sfrom\s+)?['"]([^'"]+)['"]""")
_REQUIRE = re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]""")


_SCOPED_MIN_PARTS = 2  # @scope/pkg requires at least 2 slash-delimited parts


def _normalize_pkg(raw: str) -> str:
    """Resolve a raw import path to a package name."""
    if raw.startswith("."):
        return ""
    if raw.startswith("@"):
        parts = raw.split("/", 2)
        return "/".join(parts[:2]) if len(parts) >= _SCOPED_MIN_PARTS else raw
    return raw.split("/", 1)[0]


def extract_imports_ts(line: str) -> set[str]:
    """Extract package names from a single TS/JS import or require line."""
    packages: set[str] = set()
    for match in _IMPORT_FROM.finditer(line):
        pkg = _normalize_pkg(match.group(1))
        if pkg:
            packages.add(pkg)
    for match in _REQUIRE.finditer(line):
        pkg = _normalize_pkg(match.group(1))
        if pkg:
            packages.add(pkg)
    return packages


def read_declared_deps_node(project_root: Path) -> set[str]:
    """Read declared deps from package.json + Node.js built-ins."""
    deps: set[str] = set(_NODE_BUILTINS)

    pkg_json = project_root / "package.json"
    if pkg_json.is_file():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return deps
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            section = data.get(key, {})
            if isinstance(section, dict):
                deps.update(section.keys())

    return deps


def audit_nodejs_fm1(
    path: Path,
    *,
    project_root: Path,
    config: AuditConfig,
) -> list[Finding]:
    """Audit *path* for FM1, JS/TS files only."""
    findings: list[Finding] = []
    declared = read_declared_deps_node(project_root)
    declared.update(t.lower() for t in config.project.ok_toplevels)

    extensions = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
    files = list(walk_source_files(path, extensions, include_vendored=config.include_vendored))

    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        for line_num, line in enumerate(content.splitlines(), 1):
            for pkg in extract_imports_ts(line):
                if pkg not in declared:
                    findings.append(
                        Finding(
                            mode="fm1",
                            severity="medium",
                            file=str(f),
                            line=line_num,
                            message=(
                                f"Import `{pkg}` not in any declared manifest "
                                "(possible hallucinated dependency)"
                            ),
                            snippet=line.strip(),
                            rule_id="fm1.nodejs.import-not-in-deps",
                        )
                    )
    return findings
