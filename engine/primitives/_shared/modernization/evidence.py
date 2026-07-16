"""Deterministic migration-evidence scorer (Layer 1) — the "no-docs default".

When a repo arrives with **no documentation**, we derive its modernization
state from the code itself. This module walks a repo tree, collects
framework-generation markers (Java / JS-TS / .NET / Python / Node / generic),
and returns a **banded** verdict (``legacy`` / ``partial`` / ``modern``) with a
**confidence** grade (``high`` / ``medium`` / ``low``) and **file:line-cited**
evidence signals.

Design notes:

* Every :class:`MigrationSignal` cites the actual file + 1-indexed line where
  the marker was found, so citations are **refcheckable** (mirroring
  ``refcheck.py`` — ``line_start``/``line_end`` point at real lines).
* Banding is coexistence-aware (the strangler-fig tell): a legacy-polarity AND
  a modern-polarity signal for the *same* stack → ``partial``.
* **Honesty rule.** Zero signals (or purely ambiguous weak signals) must NOT be
  reported as a false ``modern``. Downstream (MR5) scopes unknown/low-confidence
  repos *in* — it never skips them — and ``modern`` is the only band that would
  drop a repo from scope. So a zero-signal / ambiguous repo is banded ``legacy``
  with ``confidence="low"``: the least-committal band that keeps the repo in
  scope, flagged low-confidence for MR4 harness confirmation. ``legacy`` here is
  a *safe default under uncertainty*, not an asserted claim.

QUAL-2: imports only stdlib / pydantic / ``_shared`` (``discovery.models.entities``
for :data:`Confidence`, ``scan_exclusions`` for the walk). It is a standalone
module — MR4/MR5 wire it into the pipeline/digest; nothing here does.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from primitives._shared.discovery.models.entities import Confidence  # noqa: TC001
from primitives._shared.scan_exclusions import walk_source_files

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

# ---------------------------------------------------------------------------
# Banding / polarity vocabularies
# ---------------------------------------------------------------------------

Band = Literal["legacy", "partial", "modern"]
Polarity = Literal["legacy", "modern"]

#: Confidence ordering, strongest first — used to take the max confidence.
_CONFIDENCE_RANK: dict[Confidence, int] = {"high": 2, "medium": 1, "low": 0}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class MigrationSignal(BaseModel):
    """One cited piece of evidence for a repo's modernization state.

    ``line_start``/``line_end`` are 1-indexed and point at the real line(s) in
    ``path`` where ``marker`` was found — the citation is refcheckable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    stack: str  # "java" | "js" | "dotnet" | "python" | "node" | "generic"
    marker: str  # short label, e.g. "spring-boot-starter-web"
    polarity: Polarity  # does this signal point legacy or modern?
    path: str  # repo-relative
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    claim: str  # human-readable
    confidence: Confidence


class MigrationEvidence(BaseModel):
    """The banded, cited migration-state verdict for one repo/unit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    code_root: str
    band: Band
    confidence: Confidence
    signals: tuple[MigrationSignal, ...] = ()

    @property
    def is_modern(self) -> bool:
        return self.band == "modern"


# ---------------------------------------------------------------------------
# Confirmed migration-state (MR4, #1553) — the harness-confirmed verdict.
#
# The MR3 scorer above produces a *deterministic* band. MR4 defers the
# ``partial`` / low-confidence units to the harness (extract → judge → continue)
# and records the confirmed verdict here. ``source`` traces HOW the band was
# decided so MR5 (which filters the roadmap) can honor the honesty rule: a unit
# the harness never judged stays ``unverified`` and MR5 scopes it IN (⚠️), never
# silently dropping it.
# ---------------------------------------------------------------------------

#: How a :class:`ConfirmedMigrationState.band` was decided.
#:
#: * ``deterministic`` — the MR3 scorer was confident (``modern``/``legacy`` at
#:   ``high``/``medium``), or ``--no-migration-synthesis`` skipped the L3 round.
#: * ``harness`` — the scorer was uncertain (``partial`` / low-confidence) and the
#:   harness judged the migrated-vs-legacy question in-session; the harness band
#:   OVERRIDES the deterministic one.
#: * ``hint`` — the band came from a ``RepoEntry.state`` prior (reserved for MR5;
#:   MR4 does not emit it, but the vocabulary is fixed here).
#: * ``unverified`` — an uncertain unit awaiting (or not receiving) a harness
#:   verdict; MR5 scopes it IN with a ⚠️ marker.
StateSource = Literal["deterministic", "harness", "hint", "unverified"]


class ConfirmedMigrationState(BaseModel):
    """One unit's confirmed migration-state verdict (MR4, #1553)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code_root: str  # the unit / repo anchor (a ``<repo.path>#app`` node id)
    band: Band
    confidence: Confidence
    source: StateSource  # how the band was decided
    rationale: str = ""  # the harness's reason (when ``source == "harness"``)


class MigrationStates(BaseModel):
    """The persisted ``migration-states.json`` payload (MR4, #1553).

    Written in BOTH legs: the extract leg records the deterministic bands +
    ``unverified`` placeholders for the uncertain units; the continue leg merges
    the harness verdicts. MR5 (#1554) reads this file to filter the roadmap.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    states: tuple[ConfirmedMigrationState, ...] = ()


# ---------------------------------------------------------------------------
# Small filesystem helpers
# ---------------------------------------------------------------------------


def _rel(path: Path, repo_root: Path) -> str:
    """Repo-relative POSIX path string for *path* under *repo_root*."""
    return path.relative_to(repo_root).as_posix()


def _read_lines(path: Path) -> list[str]:
    """Read *path* as a list of lines (decode-safe); empty list on failure."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _find_line(lines: list[str], needle: str, *, start: int = 0) -> int | None:
    """1-indexed line number of the first line containing *needle* (or None).

    *start* is a 0-indexed offset into *lines* to begin searching from.
    """
    for i in range(start, len(lines)):
        if needle in lines[i]:
            return i + 1
    return None


def _find_line_re(lines: list[str], pattern: re.Pattern[str]) -> int | None:
    """1-indexed line number of the first line matching *pattern* (or None)."""
    for i, line in enumerate(lines):
        if pattern.search(line):
            return i + 1
    return None


def _root_files(repo_root: Path, names: Iterable[str]) -> list[Path]:
    """Existing files among *names* directly under *repo_root* (shallow)."""
    return [repo_root / n for n in names if (repo_root / n).is_file()]


def _glob_files(repo_root: Path, extensions: set[str]) -> list[Path]:
    """Recursively find files with *extensions*, honoring build-output excludes."""
    return list(walk_source_files(repo_root, extensions))


# ---------------------------------------------------------------------------
# Per-stack detectors — each returns list[MigrationSignal]
# ---------------------------------------------------------------------------

# (marker-substring, polarity, claim) tables for build-manifest scans.
_JAVA_POM_MODERN: tuple[tuple[str, str], ...] = (
    ("spring-boot-starter", "Spring Boot starter (modern web framework)"),
)
_JAVA_POM_LEGACY: tuple[tuple[str, str], ...] = (
    ("struts", "Apache Struts 1.x dependency (legacy MVC)"),
)
# Source-level Java markers (regex, polarity, claim).
_JAVA_SRC_MODERN: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"@SpringBootApplication"), "@SpringBootApplication (Spring Boot entry point)"),
    (re.compile(r"@RestController"), "@RestController (Spring MVC REST controller)"),
    (re.compile(r"@Controller\b"), "@Controller (Spring MVC controller)"),
    (
        re.compile(r"org\.springframework"),
        "org.springframework import (modern Spring framework)",
    ),
)
_JAVA_SRC_LEGACY: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"org\.apache\.struts"), "Struts Action import (legacy MVC)"),
    (re.compile(r"\bextends\s+Action\b"), "Struts Action subclass (legacy MVC)"),
    (re.compile(r"\bActionForm\b"), "Struts ActionForm (legacy MVC form bean)"),
    (re.compile(r"javax\.ejb\.EntityBean"), "EJB 2.x EntityBean (legacy persistence)"),
)


def _detect_java(repo_root: Path) -> list[MigrationSignal]:
    """Java: pom.xml / build.gradle (build manifest, high) + .java / jsp (source)."""
    signals: list[MigrationSignal] = []

    # Build manifests — decisive (high confidence).
    for manifest in _root_files(repo_root, ("pom.xml", "build.gradle", "build.gradle.kts")):
        lines = _read_lines(manifest)
        rel = _rel(manifest, repo_root)
        for needle, claim in _JAVA_POM_MODERN:
            ln = _find_line(lines, needle)
            if ln is not None:
                signals.append(
                    MigrationSignal(
                        stack="java",
                        marker=needle,
                        polarity="modern",
                        path=rel,
                        line_start=ln,
                        line_end=ln,
                        claim=claim,
                        confidence="high",
                    )
                )
        for needle, claim in _JAVA_POM_LEGACY:
            ln = _find_line(lines, needle)
            if ln is not None:
                signals.append(
                    MigrationSignal(
                        stack="java",
                        marker=needle,
                        polarity="legacy",
                        path=rel,
                        line_start=ln,
                        line_end=ln,
                        claim=claim,
                        confidence="high",
                    )
                )

    # struts-config.xml / web.xml — legacy config artifacts (medium, no manifest dep).
    for cfg in _root_files(repo_root, ("struts-config.xml",)) + list(
        walk_source_files(repo_root, {".xml"})
    ):
        if cfg.name != "struts-config.xml":
            continue
        signals.append(
            MigrationSignal(
                stack="java",
                marker="struts-config.xml",
                polarity="legacy",
                path=_rel(cfg, repo_root),
                line_start=1,
                line_end=1,
                claim="struts-config.xml present (legacy Struts MVC configuration)",
                confidence="medium",
            )
        )

    # .java source markers (medium — annotations / imports, no build manifest).
    for src in _glob_files(repo_root, {".java"}):
        lines = _read_lines(src)
        rel = _rel(src, repo_root)
        for pattern, claim in _JAVA_SRC_MODERN:
            ln = _find_line_re(lines, pattern)
            if ln is not None:
                signals.append(
                    MigrationSignal(
                        stack="java",
                        marker=pattern.pattern,
                        polarity="modern",
                        path=rel,
                        line_start=ln,
                        line_end=ln,
                        claim=claim,
                        confidence="medium",
                    )
                )
        for pattern, claim in _JAVA_SRC_LEGACY:
            ln = _find_line_re(lines, pattern)
            if ln is not None:
                signals.append(
                    MigrationSignal(
                        stack="java",
                        marker=pattern.pattern,
                        polarity="legacy",
                        path=rel,
                        line_start=ln,
                        line_end=ln,
                        claim=claim,
                        confidence="medium",
                    )
                )

    # .jsp files — server-rendered legacy views (medium).
    for jsp in _glob_files(repo_root, {".jsp"}):
        signals.append(
            MigrationSignal(
                stack="java",
                marker="*.jsp",
                polarity="legacy",
                path=_rel(jsp, repo_root),
                line_start=1,
                line_end=1,
                claim="JSP server-rendered view (legacy presentation layer)",
                confidence="medium",
            )
        )

    return signals


def _load_json(path: Path) -> dict[str, object] | None:
    """Parse *path* as a JSON object; None on any error / non-object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _pkg_deps(pkg: dict[str, object]) -> dict[str, str]:
    """Merged dependencies + devDependencies from a package.json dict."""
    deps: dict[str, str] = {}
    for key in ("dependencies", "devDependencies"):
        section = pkg.get(key)
        if isinstance(section, dict):
            for name, ver in section.items():
                if isinstance(name, str) and isinstance(ver, str):
                    deps[name] = ver
    return deps


_JS_MODERN_DEPS = ("react", "next", "vue", "vitest", "svelte", "@angular/core")


def _detect_js(repo_root: Path) -> list[MigrationSignal]:
    """JS/TS: package.json deps (high) + tsconfig.json (medium) + jQuery-only (legacy)."""
    signals: list[MigrationSignal] = []
    pkg_path = repo_root / "package.json"
    lines = _read_lines(pkg_path) if pkg_path.is_file() else []
    pkg = _load_json(pkg_path) if pkg_path.is_file() else None
    deps = _pkg_deps(pkg) if pkg is not None else {}

    if pkg is not None:
        rel = _rel(pkg_path, repo_root)
        modern_found = False
        for dep in _JS_MODERN_DEPS:
            if dep in deps:
                # cite the actual line the dependency appears on
                ln = _find_line(lines, f'"{dep}"') or 1
                signals.append(
                    MigrationSignal(
                        stack="js",
                        marker=dep,
                        polarity="modern",
                        path=rel,
                        line_start=ln,
                        line_end=ln,
                        claim=f"{dep} dependency (modern JS framework/tooling)",
                        confidence="high",
                    )
                )
                modern_found = True
        # jQuery with no modern framework → legacy.
        if "jquery" in deps and not modern_found:
            ln = _find_line(lines, '"jquery"') or 1
            signals.append(
                MigrationSignal(
                    stack="js",
                    marker="jquery",
                    polarity="legacy",
                    path=rel,
                    line_start=ln,
                    line_end=ln,
                    claim="jQuery-only front end, no modern framework (legacy)",
                    confidence="medium",
                )
            )

    # tsconfig.json present — TypeScript build (medium modern signal).
    tsconfig = repo_root / "tsconfig.json"
    if tsconfig.is_file():
        signals.append(
            MigrationSignal(
                stack="js",
                marker="tsconfig.json",
                polarity="modern",
                path=_rel(tsconfig, repo_root),
                line_start=1,
                line_end=1,
                claim="tsconfig.json present (TypeScript toolchain)",
                confidence="medium",
            )
        )

    return signals


_CSPROJ_SDK_RE = re.compile(r'<Project\s+Sdk="Microsoft\.NET\.Sdk')
_CSPROJ_MODERN_TFM_RE = re.compile(r"<TargetFramework>\s*net(?:[6-9]|1\d)\.\d", re.IGNORECASE)
_CSPROJ_NULLABLE_RE = re.compile(r"<Nullable>\s*enable", re.IGNORECASE)
_CSPROJ_LEGACY_TFM_RE = re.compile(r"<TargetFrameworkVersion>\s*v4", re.IGNORECASE)


def _detect_dotnet(repo_root: Path) -> list[MigrationSignal]:
    """.NET: SDK-style csproj + net6/8 TFM (modern) vs packages.config / v4 TFM / asmx."""
    signals: list[MigrationSignal] = []

    for csproj in _glob_files(repo_root, {".csproj"}):
        lines = _read_lines(csproj)
        rel = _rel(csproj, repo_root)
        sdk_ln = _find_line_re(lines, _CSPROJ_SDK_RE)
        if sdk_ln is not None:
            signals.append(
                MigrationSignal(
                    stack="dotnet",
                    marker="Microsoft.NET.Sdk",
                    polarity="modern",
                    path=rel,
                    line_start=sdk_ln,
                    line_end=sdk_ln,
                    claim="SDK-style csproj (modern .NET project format)",
                    confidence="high",
                )
            )
        tfm_ln = _find_line_re(lines, _CSPROJ_MODERN_TFM_RE)
        if tfm_ln is not None:
            signals.append(
                MigrationSignal(
                    stack="dotnet",
                    marker="TargetFramework net6+",
                    polarity="modern",
                    path=rel,
                    line_start=tfm_ln,
                    line_end=tfm_ln,
                    claim="TargetFramework net6.0+ (modern .NET runtime)",
                    confidence="high",
                )
            )
        nullable_ln = _find_line_re(lines, _CSPROJ_NULLABLE_RE)
        if nullable_ln is not None:
            signals.append(
                MigrationSignal(
                    stack="dotnet",
                    marker="Nullable enable",
                    polarity="modern",
                    path=rel,
                    line_start=nullable_ln,
                    line_end=nullable_ln,
                    claim="<Nullable>enable</Nullable> (modern C# nullable reference types)",
                    confidence="medium",
                )
            )
        legacy_ln = _find_line_re(lines, _CSPROJ_LEGACY_TFM_RE)
        if legacy_ln is not None:
            signals.append(
                MigrationSignal(
                    stack="dotnet",
                    marker="TargetFrameworkVersion v4",
                    polarity="legacy",
                    path=rel,
                    line_start=legacy_ln,
                    line_end=legacy_ln,
                    claim="TargetFrameworkVersion v4.x (legacy .NET Framework)",
                    confidence="high",
                )
            )

    # packages.config — legacy NuGet restore (high — build-manifest level).
    for pkgcfg in _root_files(repo_root, ("packages.config",)):
        signals.append(
            MigrationSignal(
                stack="dotnet",
                marker="packages.config",
                polarity="legacy",
                path=_rel(pkgcfg, repo_root),
                line_start=1,
                line_end=1,
                claim="packages.config present (legacy NuGet package management)",
                confidence="high",
            )
        )

    # .asmx — legacy ASP.NET web services (medium).
    for asmx in _glob_files(repo_root, {".asmx"}):
        signals.append(
            MigrationSignal(
                stack="dotnet",
                marker="*.asmx",
                polarity="legacy",
                path=_rel(asmx, repo_root),
                line_start=1,
                line_end=1,
                claim="ASMX web service (legacy ASP.NET SOAP endpoint)",
                confidence="medium",
            )
        )

    return signals


_PY_MODERN_DEP_RE = re.compile(r"\b(fastapi|pydantic)\b", re.IGNORECASE)
_PY_LEGACY_DEP_RE = re.compile(r"^\s*(flask\s*==?\s*0\.|django\s*[<=]=?\s*2\.)", re.IGNORECASE)


def _detect_python(repo_root: Path) -> list[MigrationSignal]:
    """Python: pyproject.toml (modern) vs setup.py-only; fastapi/pydantic vs old pins."""
    signals: list[MigrationSignal] = []
    pyproject = repo_root / "pyproject.toml"
    setup_py = repo_root / "setup.py"

    if pyproject.is_file():
        lines = _read_lines(pyproject)
        rel = _rel(pyproject, repo_root)
        signals.append(
            MigrationSignal(
                stack="python",
                marker="pyproject.toml",
                polarity="modern",
                path=rel,
                line_start=1,
                line_end=1,
                claim="pyproject.toml present (modern packaging metadata)",
                confidence="high",
            )
        )
        for i, line in enumerate(lines):
            if _PY_MODERN_DEP_RE.search(line):
                dep = _PY_MODERN_DEP_RE.search(line).group(1).lower()  # type: ignore[union-attr]
                signals.append(
                    MigrationSignal(
                        stack="python",
                        marker=dep,
                        polarity="modern",
                        path=rel,
                        line_start=i + 1,
                        line_end=i + 1,
                        claim=f"{dep} dependency (modern async/typed web stack)",
                        confidence="high",
                    )
                )
    elif setup_py.is_file():
        # setup.py-only (no pyproject.toml) — legacy packaging (medium).
        signals.append(
            MigrationSignal(
                stack="python",
                marker="setup.py-only",
                polarity="legacy",
                path=_rel(setup_py, repo_root),
                line_start=1,
                line_end=1,
                claim="setup.py present without pyproject.toml (legacy packaging)",
                confidence="medium",
            )
        )

    # Old framework pins in requirements.txt (medium legacy).
    reqs = repo_root / "requirements.txt"
    if reqs.is_file():
        lines = _read_lines(reqs)
        rel = _rel(reqs, repo_root)
        for i, line in enumerate(lines):
            m = _PY_LEGACY_DEP_RE.search(line)
            if m is not None:
                signals.append(
                    MigrationSignal(
                        stack="python",
                        marker=m.group(1).strip(),
                        polarity="legacy",
                        path=rel,
                        line_start=i + 1,
                        line_end=i + 1,
                        claim="pinned to an EOL web framework version (legacy)",
                        confidence="medium",
                    )
                )

    return signals


def _detect_node(repo_root: Path) -> list[MigrationSignal]:
    """Node runtime/module-system axis: ESM ``"type":"module"`` (modern) vs no build."""
    signals: list[MigrationSignal] = []
    pkg_path = repo_root / "package.json"
    if not pkg_path.is_file():
        return signals
    lines = _read_lines(pkg_path)
    pkg = _load_json(pkg_path)
    if pkg is None:
        return signals
    rel = _rel(pkg_path, repo_root)

    if pkg.get("type") == "module":
        ln = _find_line(lines, '"type"') or 1
        signals.append(
            MigrationSignal(
                stack="node",
                marker='"type":"module"',
                polarity="modern",
                path=rel,
                line_start=ln,
                line_end=ln,
                claim='ESM package ("type":"module" — modern module system)',
                confidence="medium",
            )
        )
    else:
        # A CommonJS package is only a *legacy* Node signal when there is no
        # modern JS framework/tooling AND no build script — i.e. a genuinely
        # stale project. Modern-framework repos (React/Next/Vue) legitimately
        # ship without a top-level "build" script and must NOT be flagged here
        # (that would spuriously push a modern repo into the `partial` band).
        deps = _pkg_deps(pkg)
        scripts = pkg.get("scripts")
        has_build = isinstance(scripts, dict) and "build" in scripts
        has_modern_dep = any(dep in deps for dep in _JS_MODERN_DEPS)
        if not has_build and not has_modern_dep and deps:
            ln = _find_line(lines, '"name"') or 1
            signals.append(
                MigrationSignal(
                    stack="node",
                    marker="no-build",
                    polarity="legacy",
                    path=rel,
                    line_start=ln,
                    line_end=ln,
                    claim="CommonJS package, no build script or framework (legacy Node project)",
                    confidence="low",
                )
            )
    return signals


_GENERIC_MODERN_FILES: tuple[tuple[str, set[str] | None, str], ...] = (
    ("Dockerfile", None, "Dockerfile present (containerized deployment)"),
)


def _detect_generic(repo_root: Path) -> list[MigrationSignal]:
    """Generic infra: Dockerfile / Helm / Terraform / k8s → WEAK modern (low).

    Their *absence* is not a legacy signal — we never over-claim from missing infra.
    """
    signals: list[MigrationSignal] = []

    for docker in _root_files(repo_root, ("Dockerfile",)):
        signals.append(
            MigrationSignal(
                stack="generic",
                marker="Dockerfile",
                polarity="modern",
                path=_rel(docker, repo_root),
                line_start=1,
                line_end=1,
                claim="Dockerfile present (containerized deployment — weak modern signal)",
                confidence="low",
            )
        )

    # Helm chart, Terraform, k8s manifests (weak modern).
    for tf in _glob_files(repo_root, {".tf"}):
        signals.append(
            MigrationSignal(
                stack="generic",
                marker="*.tf",
                polarity="modern",
                path=_rel(tf, repo_root),
                line_start=1,
                line_end=1,
                claim="Terraform IaC present (weak modern signal)",
                confidence="low",
            )
        )

    for chart in _glob_files(repo_root, {".yaml", ".yml"}):
        if chart.name == "Chart.yaml":
            signals.append(
                MigrationSignal(
                    stack="generic",
                    marker="Chart.yaml",
                    polarity="modern",
                    path=_rel(chart, repo_root),
                    line_start=1,
                    line_end=1,
                    claim="Helm chart present (weak modern signal)",
                    confidence="low",
                )
            )

    return signals


_DETECTORS = (
    _detect_java,
    _detect_js,
    _detect_dotnet,
    _detect_python,
    _detect_node,
    _detect_generic,
)


# ---------------------------------------------------------------------------
# Banding + confidence
# ---------------------------------------------------------------------------


def _band(signals: list[MigrationSignal]) -> Band:
    """Band the collected signals.

    * coexistence (both polarities for the SAME stack) → ``partial``.
    * only modern signals → ``modern``.
    * only legacy signals → ``legacy``.
    * zero signals → ``legacy`` (the honesty rule — safe under uncertainty,
      keeps the repo in scope; ``confidence`` will be ``low``).
    """
    by_stack: dict[str, set[Polarity]] = {}
    for s in signals:
        by_stack.setdefault(s.stack, set()).add(s.polarity)
    if any({"legacy", "modern"} <= pols for pols in by_stack.values()):
        return "partial"

    polarities = {s.polarity for s in signals}
    if not polarities:
        return "legacy"  # honesty rule: no evidence → safe least-committal band
    if polarities == {"modern"}:
        return "modern"
    if polarities == {"legacy"}:
        return "legacy"
    # Mixed polarities across *different* stacks (no same-stack coexistence):
    # treat as partial — the repo carries both generations, just not overlapping.
    return "partial"


def _overall_confidence(signals: list[MigrationSignal]) -> Confidence:
    """Max confidence among signals; ``low`` when there are none."""
    if not signals:
        return "low"
    best = max(signals, key=lambda s: _CONFIDENCE_RANK[s.confidence])
    return best.confidence


def _signal_sort_key(s: MigrationSignal) -> tuple[str, str, int, str]:
    """Deterministic ordering: (stack, path, line_start, marker)."""
    return (s.stack, s.path, s.line_start, s.marker)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def score_migration_evidence(repo_root: Path, *, code_root: str | None = None) -> MigrationEvidence:
    """Walk *repo_root* for framework-generation markers → a banded, cited verdict.

    Args:
        repo_root: the repository tree to score.
        code_root: label recorded on the verdict; defaults to ``str(repo_root)``.

    Returns:
        A :class:`MigrationEvidence` with a :data:`Band` verdict, an overall
        :data:`Confidence`, and the sorted tuple of cited
        :class:`MigrationSignal`\\ s. Every signal's ``path:line_start`` points
        at a real line (refcheckable). See the module docstring for the banding
        rules and the honesty rule for zero-signal repos.
    """
    signals: list[MigrationSignal] = []
    for detector in _DETECTORS:
        signals.extend(detector(repo_root))

    signals.sort(key=_signal_sort_key)

    return MigrationEvidence(
        code_root=code_root if code_root is not None else str(repo_root),
        band=_band(signals),
        confidence=_overall_confidence(signals),
        signals=tuple(signals),
    )


# ---------------------------------------------------------------------------
# MR4 confirmation helpers (#1553) — decide which units are confident vs
# uncertain, and build the extract-leg prompt payload for the uncertain ones.
# ---------------------------------------------------------------------------


def is_uncertain(evidence: MigrationEvidence) -> bool:
    """Does *evidence* warrant a harness migration-state confirmation (L3)?

    A unit is UNCERTAIN — deferred to the harness — when its band is ``partial``
    (the strangler-fig coexistence case, where the migrated-vs-legacy call is a
    judgment) OR its overall confidence is ``low`` (thin / ambiguous evidence,
    including the honesty-rule zero-signal ``legacy``/``low`` default). A confident
    ``modern`` / ``legacy`` verdict at ``high`` / ``medium`` is recorded directly.
    """
    return evidence.band == "partial" or evidence.confidence == "low"


def deterministic_state(evidence: MigrationEvidence) -> ConfirmedMigrationState:
    """A :class:`ConfirmedMigrationState` recording *evidence* as-scored (``deterministic``)."""
    return ConfirmedMigrationState(
        code_root=evidence.code_root,
        band=evidence.band,
        confidence=evidence.confidence,
        source="deterministic",
    )


def unverified_placeholder(evidence: MigrationEvidence) -> ConfirmedMigrationState:
    """An ``unverified`` placeholder for an uncertain unit awaiting a harness verdict.

    Carries the scorer's band/confidence so MR5 has a value to fall back on if the
    harness never judges it — but ``source="unverified"`` flags it for the ⚠️ marker.
    """
    return ConfirmedMigrationState(
        code_root=evidence.code_root,
        band=evidence.band,
        confidence=evidence.confidence,
        source="unverified",
    )


def migration_prompt_item(evidence: MigrationEvidence) -> dict[str, object]:
    """Build one migration-judgment prompt item for an uncertain unit (extract leg).

    Carries everything the SKILL body needs to author a verdict: the unit's
    ``code_root``, the scorer's banded verdict + confidence, every cited
    :class:`MigrationSignal` (band evidence with ``path:line`` citations), and the
    de-duplicated ``ambiguous_files`` — the source files whose stack evidence was
    mixed / thin (derived from the signals' paths) — so the harness can read them
    in-session before deciding migrated-vs-legacy.
    """
    ambiguous_files = sorted({s.path for s in evidence.signals})
    return {
        "code_root": evidence.code_root,
        "band": evidence.band,
        "confidence": evidence.confidence,
        "instruction": (
            "This unit's deterministic migration-state is UNCERTAIN (band "
            f"{evidence.band!r} at {evidence.confidence!r} confidence). Read the "
            "cited evidence + ambiguous_files below and judge whether this unit is "
            "already MODERN (migration complete), still LEGACY (not started), or "
            "PARTIAL (mid-migration / strangler-fig). Author a verdict object: "
            '{"code_root": "<unit>", "band": "legacy|partial|modern", '
            '"confidence": "high|medium|low", "rationale": "<why, cite file:line>"}.'
        ),
        "signals": [s.model_dump(mode="json") for s in evidence.signals],
        "ambiguous_files": ambiguous_files,
    }
