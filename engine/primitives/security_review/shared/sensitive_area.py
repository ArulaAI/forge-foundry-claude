"""S11 /security-review — Sensitive area detector (Stage A.1).

Detects security-sensitive files using heuristic patterns and optional
.foundry/security.yaml overrides.

Heuristic patterns:
  - auth*.py, login*.py, crypto*.py, secrets*.py
  - */auth/*, */security/*, */crypto/*
  - Any path declared in security_config.sensitive_paths / auth_modules /
    crypto_modules / secret_locations
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from primitives._shared.scan_exclusions import is_excluded as _is_excluded
from primitives.security_review.shared.models import SensitiveArea

if TYPE_CHECKING:
    from primitives.security_review.shared.security_yaml_loader import SecurityConfig

# ---------------------------------------------------------------------------
# Heuristic patterns
# ---------------------------------------------------------------------------

# (glob_pattern, kind, reason)
_HEURISTIC_PATTERNS: list[tuple[str, str, str]] = [
    ("auth*.py", "auth", "Matches auth* filename pattern (authentication module)"),
    ("login*.py", "auth", "Matches login* filename pattern (login flow)"),
    ("session*.py", "auth", "Matches session* filename pattern (session management)"),
    ("jwt*.py", "auth", "Matches jwt* filename pattern (JWT handling)"),
    ("crypto*.py", "crypto", "Matches crypto* filename pattern (cryptography module)"),
    ("encrypt*.py", "crypto", "Matches encrypt* filename pattern (encryption module)"),
    ("cipher*.py", "crypto", "Matches cipher* filename pattern (cipher implementation)"),
    ("hash*.py", "crypto", "Matches hash* filename pattern (hash implementation)"),
    ("secrets*.py", "secret", "Matches secrets* filename pattern (secrets management)"),
    ("password*.py", "secret", "Matches password* filename pattern (password handling)"),
    ("credential*.py", "secret", "Matches credential* filename pattern (credential management)"),
    ("key*.py", "secret", "Matches key* filename pattern (key management)"),
    ("token*.py", "secret", "Matches token* filename pattern (token management)"),
]

_HEURISTIC_DIR_PATTERNS: list[tuple[str, str, str]] = [
    ("*/auth/*", "auth", "File is under */auth/ directory"),
    ("*/auth/", "auth", "File is under auth/ directory"),
    ("*/security/*", "general", "File is under */security/ directory"),
    ("*/crypto/*", "crypto", "File is under */crypto/ directory"),
    ("*/secrets/*", "secret", "File is under */secrets/ directory"),
    ("*/credentials/*", "secret", "File is under */credentials/ directory"),
    ("*/login/*", "auth", "File is under */login/ directory"),
]


def _matches_dir_pattern(path: Path, root: Path, pattern: str) -> bool:
    """Check if a path matches a directory-based glob pattern."""
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)
    # Normalize to forward slashes
    rel = rel.replace("\\", "/")
    return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch("/" + rel, pattern)


def _classify_yaml_path(yaml_path: str) -> tuple[str, str]:
    """Classify a YAML-declared path into (kind, reason)."""
    lower = yaml_path.lower()
    if any(kw in lower for kw in ("auth", "login", "session", "jwt", "rbac")):
        return "auth", f"Declared in security.yaml auth_modules/sensitive_paths: {yaml_path}"
    if any(kw in lower for kw in ("crypto", "encrypt", "cipher", "hash", "kdf")):
        return "crypto", f"Declared in security.yaml crypto_modules: {yaml_path}"
    if any(kw in lower for kw in ("secret", "key", "credential", "password", "token")):
        return "secret", f"Declared in security.yaml secret_locations: {yaml_path}"
    return "general", f"Declared in security.yaml sensitive_paths: {yaml_path}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_sensitive_areas(
    code_path: Path,
    security_config: SecurityConfig | None,
    *,
    include_vendored: bool = False,
) -> list[SensitiveArea]:
    """Detect security-sensitive files under code_path.

    Args:
        code_path: Root of the code to scan.
        security_config: Optional config from .foundry/security.yaml.
        include_vendored: When True, re-include vendored harness trees in the walk.

    Returns:
        List of SensitiveArea objects (deduplicated by path).
    """
    if not code_path.exists():
        return []

    # Collect all source files
    if code_path.is_file():
        all_files = [code_path]
        root = code_path.parent
    else:
        root = code_path
        all_files = [
            p
            for p in code_path.rglob("*")
            if p.is_file()
            and not p.is_symlink()
            and not _is_excluded(p, code_path, include_vendored=include_vendored)
        ]

    # Track already-added paths to avoid duplicates
    seen: set[str] = set()
    areas: list[SensitiveArea] = []

    def _add(path: Path, kind: str, reason: str) -> None:
        path_str = str(path)
        if path_str not in seen:
            seen.add(path_str)
            areas.append(
                SensitiveArea(
                    path=path_str,
                    kind=kind,  # type: ignore[arg-type]
                    reason=reason,
                )
            )

    # --- Heuristic: filename patterns ---
    for file in all_files:
        for pattern, kind, reason in _HEURISTIC_PATTERNS:
            if fnmatch.fnmatch(file.name.lower(), pattern):
                _add(file, kind, reason)
                break  # First match wins

    # --- Heuristic: directory patterns ---
    for file in all_files:
        for dir_pattern, kind, reason in _HEURISTIC_DIR_PATTERNS:
            if _matches_dir_pattern(file, root, dir_pattern):
                _add(file, kind, reason)
                break

    # --- YAML-driven: sensitive_paths ---
    if security_config is not None:
        all_yaml_paths = (
            [(p, "sensitive") for p in security_config.sensitive_paths]
            + [(p, "auth") for p in security_config.auth_modules]
            + [(p, "crypto") for p in security_config.crypto_modules]
            + [(p, "secret") for p in security_config.secret_locations]
        )
        for yaml_path, _src in all_yaml_paths:
            # Resolve relative to code_path
            candidate = Path(yaml_path) if Path(yaml_path).is_absolute() else code_path / yaml_path
            if candidate.exists():
                kind, reason = _classify_yaml_path(yaml_path)
                _add(candidate, kind, reason)
            else:
                # Try glob match against all files
                for file in all_files:
                    try:
                        rel = str(file.relative_to(root))
                    except ValueError:
                        rel = str(file)
                    if fnmatch.fnmatch(rel, yaml_path) or fnmatch.fnmatch(file.name, yaml_path):
                        kind, reason = _classify_yaml_path(yaml_path)
                        _add(file, kind, reason)

    return areas
