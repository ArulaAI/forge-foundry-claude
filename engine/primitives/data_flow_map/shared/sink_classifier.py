"""S60 /data-flow-map — Stage C: Sink classification.

Detects all 7 SinkKind values:
  - logger:        logging.*, log.info/warning/error, logger.info, print(...)
  - http_client:   requests.*, httpx.*, urllib.*, aiohttp.*, fetch(
  - db_write:      session.add, session.commit, Model.save, cursor.execute(INSERT/UPDATE)
  - response_body: FastAPI/Flask return JsonResponse/JSONResponse/Response
  - file_write:    open(...).write, Path.write_text, Path.write_bytes
  - queue:         *.send, *.publish, common queue library calls
  - cache:         redis.*, *.cache.set, cache.put
"""

from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING

from primitives._shared.scan_exclusions import is_excluded as _is_excluded
from primitives.data_flow_map.shared.models import PIISource, Sink, SinkKind

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Regex patterns for each SinkKind (for TS / quick Python line scan)
# ---------------------------------------------------------------------------

_SINK_PATTERNS: list[tuple[SinkKind, re.Pattern[str]]] = [
    # logger
    (
        "logger",
        re.compile(
            r"""(?x)
            (?:logging\.|log\.|logger\.)\s*(?:info|warning|warn|error|debug|critical|exception)\s*\(
            | ^\s*print\s*\(
            """,
            re.VERBOSE | re.MULTILINE,
        ),
    ),
    # http_client
    (
        "http_client",
        re.compile(
            r"""(?x)
            (?:requests|httpx|urllib|aiohttp)\.\w+\s*\(
            | fetch\s*\(
            | \.post\s*\( | \.get\s*\( | \.put\s*\( | \.patch\s*\( | \.delete\s*\(
            """,
            re.VERBOSE,
        ),
    ),
    # db_write
    (
        "db_write",
        re.compile(
            r"""(?x)
            session\.add\s*\(
            | session\.commit\s*\(
            | \.save\s*\(\s*\)
            | cursor\.execute\s*\(\s*(?:f?['"]|[rf]?["'])(?:INSERT|UPDATE)
            | db\.add\s*\( | db\.commit\s*\(
            """,
            re.VERBOSE,
        ),
    ),
    # response_body
    (
        "response_body",
        re.compile(
            r"""(?x)
            JsonResponse\s*\(
            | JSONResponse\s*\(
            | Response\s*\(
            | (?:return\s+(?:jsonify|json\.dumps|json_response)\s*\()
            | (?:return\s+\{)   # bare dict return in view
            """,
            re.VERBOSE,
        ),
    ),
    # file_write
    (
        "file_write",
        re.compile(
            r"""(?x)
            open\s*\([^)]+\)\s*\.write\s*\(
            | \.write_text\s*\(
            | \.write_bytes\s*\(
            | \.write\s*\(     # generic file handle .write
            """,
            re.VERBOSE,
        ),
    ),
    # queue
    (
        "queue",
        re.compile(
            r"""(?x)
            \.send\s*\(       # boto3 SQS, pika, etc.
            | \.publish\s*\(  # pubsub, redis streams
            | \.put\s*\(      # queue.put
            | \.enqueue\s*\(  # rq / celery
            | celery\.send_task\s*\(
            """,
            re.VERBOSE,
        ),
    ),
    # cache
    (
        "cache",
        re.compile(
            r"""(?x)
            redis\.\w+\s*\(
            | \.cache\.set\s*\(
            | cache\.set\s*\(
            | cache\.put\s*\(
            | \.setex\s*\(     # Redis SETEX
            | memcache\.set\s*\(
            """,
            re.VERBOSE,
        ),
    ),
]


# ---------------------------------------------------------------------------
# Python AST-based sink detection (more accurate than regex)
# ---------------------------------------------------------------------------


def _call_to_sink_kind(node: ast.Call) -> SinkKind | None:  # noqa: PLR0911
    """Map a Call node to a SinkKind or None."""
    func = node.func

    # Extract the call string representation
    if isinstance(func, ast.Attribute):
        attr = func.attr
        obj_str = ast.unparse(func.value) if hasattr(ast, "unparse") else ""

        # logger
        _logger_attrs = {"info", "warning", "warn", "error", "debug", "critical", "exception"}
        if attr in _logger_attrs and any(kw in obj_str for kw in ("logging", "log", "logger")):
            return "logger"

        # http_client
        _http_attrs = {"get", "post", "put", "patch", "delete", "request"}
        _http_libs = ("requests", "httpx", "aiohttp", "session", "client")
        if attr in _http_attrs and any(lib in obj_str for lib in _http_libs):
            return "http_client"

        # db_write
        _db_attrs = {"add", "commit", "flush", "save", "bulk_save_objects"}
        if attr in _db_attrs and any(kw in obj_str for kw in ("session", "db", "Session")):
            return "db_write"
        if attr == "execute" and node.args:
            # Check if first arg contains INSERT/UPDATE
            first_arg = node.args[0]
            _ins_upd = r"\b(INSERT|UPDATE)\b"
            if (
                isinstance(first_arg, ast.Constant)
                and isinstance(first_arg.value, str)
                and re.search(_ins_upd, first_arg.value, re.IGNORECASE)
            ):
                return "db_write"
            elif hasattr(ast, "unparse"):
                unparsed = ast.unparse(first_arg)
                if re.search(_ins_upd, unparsed, re.IGNORECASE):
                    return "db_write"

        # queue
        if attr in {"send", "publish", "put", "enqueue"}:
            return "queue"

        # cache
        if attr in {"set", "setex", "put"} and any(
            kw in obj_str for kw in ("redis", "cache", "memcache")
        ):
            return "cache"

        # file_write
        if attr in {"write", "write_text", "write_bytes"}:
            return "file_write"

        # response_body
        if attr in {"json", "jsonify"}:
            return "response_body"

    elif isinstance(func, ast.Name):
        name = func.id
        if name == "print":
            return "logger"
        if name in {"JsonResponse", "JSONResponse", "Response", "jsonify"}:
            return "response_body"
        if name == "fetch":
            return "http_client"
        if name in {"open"}:
            # Need context; default to file_write if in write mode
            return "file_write"

    return None


def _scan_python_file_for_sinks(path: Path, pii_names: set[str]) -> list[Sink]:
    """AST-scan a Python file for sink calls that reference PII names."""
    try:
        source_code = path.read_text(encoding="utf-8")
        tree = ast.parse(source_code)
    except (SyntaxError, UnicodeDecodeError):
        return []

    sinks: list[Sink] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        kind = _call_to_sink_kind(node)
        if kind is None:
            continue

        # Check if any argument references a PII name
        refs: list[str] = []
        for arg in node.args + [kw.value for kw in node.keywords]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Name) and sub.id in pii_names:
                    if sub.id not in refs:
                        refs.append(sub.id)
                # f-string values
                elif isinstance(sub, ast.JoinedStr):
                    for val in sub.values:
                        if (
                            isinstance(val, ast.FormattedValue)
                            and isinstance(val.value, ast.Name)
                            and val.value.id in pii_names
                        ) and val.value.id not in refs:
                            refs.append(val.value.id)

        # Build a name for this sink
        func = node.func
        if isinstance(func, ast.Attribute):
            sink_name = ast.unparse(func) if hasattr(ast, "unparse") else f"<obj>.{func.attr}"
        elif isinstance(func, ast.Name):
            sink_name = func.id
        else:
            sink_name = "<call>"

        sinks.append(
            Sink(
                name=sink_name,
                file=str(path),
                line=node.lineno,
                kind=kind,
                references_pii=refs,
            )
        )

    return sinks


def _scan_file_for_sinks_regex(path: Path, pii_names: set[str], _suffix: str) -> list[Sink]:
    """Regex-based sink detection for TS/Java files."""
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    sinks: list[Sink] = []
    lines_list = source.splitlines()

    for i, line in enumerate(lines_list, 1):
        for kind, pattern in _SINK_PATTERNS:
            if pattern.search(line):
                # Check if any PII name appears in this line or adjacent lines
                window = "\n".join(lines_list[max(0, i - 3) : i + 3])
                refs = [name for name in pii_names if name in window]
                sinks.append(
                    Sink(
                        name=line.strip()[:60],
                        file=str(path),
                        line=i,
                        kind=kind,
                        references_pii=refs,
                    )
                )
                break  # one kind per line

    return sinks


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def classify_sinks(
    code_path: Path,
    sources: list[PIISource],
    language: str = "auto",
    *,
    include_vendored: bool = False,
) -> list[Sink]:
    """Detect all data sinks in *code_path* and classify by SinkKind.

    For Python, uses AST-based detection. For TS/Java, uses regex.

    Args:
        code_path: Directory or single file to scan.
        sources: Detected PIISource objects (for PII reference tracking).
        language: 'python', 'java', 'ts', or 'auto'.
        include_vendored: When True, re-include vendored harness trees.

    Returns:
        List of Sink objects.
    """
    pii_names = {s.name for s in sources}
    sinks: list[Sink] = []

    files: list[Path]
    files = (
        [code_path]
        if code_path.is_file()
        else [
            p
            for p in code_path.rglob("*")
            if not p.is_symlink()
            and not _is_excluded(p, code_path, include_vendored=include_vendored)
        ]
    )

    for file_path in sorted(files):
        if not file_path.is_file():
            continue
        suffix = file_path.suffix.lower()

        if language == "auto":
            if suffix == ".py":
                lang = "python"
            elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
                lang = "ts"
            elif suffix == ".java":
                lang = "java"
            else:
                continue
        elif language == "python" and suffix == ".py":
            lang = "python"
        elif language == "ts" and suffix in {".ts", ".tsx", ".js", ".jsx"}:
            lang = "ts"
        elif language == "java" and suffix == ".java":
            lang = "java"
        else:
            continue

        if lang == "python":
            sinks.extend(_scan_python_file_for_sinks(file_path, pii_names))
        else:
            sinks.extend(_scan_file_for_sinks_regex(file_path, pii_names, suffix))

    return sinks
