from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Content pre-check signals
_CONTROLLER_SIGNALS = ("@Controller", "@RestController")

# Class-level annotations
_CLASS_REQUEST_MAPPING_RE = re.compile(
    r'@RequestMapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']',
    re.DOTALL,
)
_SESSION_ATTRS_RE = re.compile(
    r"@SessionAttributes\s*\(\s*\{([^}]+)\}",
    re.DOTALL,
)

# Method-level mapping annotations
_METHOD_MAPPING_RE = re.compile(
    r"@(Get|Post|Put|Patch|Delete|Request)Mapping\b",
    re.IGNORECASE,
)

# Handler method detection
_METHOD_DECL_RE = re.compile(r"(?:public|protected|private)\s+\S+\s+(\w+)\s*\(")

# View name extraction
_RETURN_STRING_RE = re.compile(r'return\s+"([^"]+)"')
_MODEL_AND_VIEW_RE = re.compile(r'new\s+ModelAndView\s*\(\s*"([^"]+)"')
_REDIRECT_RE = re.compile(r'"redirect:([^"]+)"')

# Exception handler
_EXC_HANDLER_RE = re.compile(r"@ExceptionHandler\s*\(\s*(\w+)\.class\s*\)")

# @Scheduled
_SCHEDULED_RE = re.compile(r"@Scheduled\b")

# Async patterns
_ASYNC_SIGNALS = ("DeferredResult", "CompletableFuture", "@Async")

# InitBinder custom editor
_CUSTOM_EDITOR_RE = re.compile(r'registerCustomEditor\s*\([^,]+,\s*"([^"]+)"')

# Notification hints
_NOTIFY_RE = re.compile(
    r"(\w+Service|emailService|smsService|notificationService)\."
    r"(send\w*|notify\w*|publish\w*|email\w*|sms\w*)"
)

# Session calls
_SESSION_SET_RE = re.compile(r'session\.setAttribute\s*\(\s*"([^"]+)"')
_SESSION_GET_RE = re.compile(r'session\.getAttribute\s*\(\s*"([^"]+)"')

# @ModelAttribute in method params
_MODEL_ATTR_RE = re.compile(r'@ModelAttribute\s*\(\s*"([^"]+)"\s*\)')

# @RequestParam with defaultValue
_REQUEST_PARAM_DEFAULT_RE = re.compile(
    r'@RequestParam\s*\([^)]*defaultValue\s*=\s*"([^"]+)"[^)]*\)\s+\w+\s+(\w+)'
)

# Pagination signals
_PAGE_PARAM_RE = re.compile(r"@RequestParam[^)]*\)\s+(?:int|Integer|Pageable)\s+(page|size|sort)\b")

# RestController marker
_REST_CTRL_RE = re.compile(r"@RestController\b")


def humanize_view_name(view_name: str) -> str:
    """Convert a Spring MVC view/template name to a human-readable display label.

    Rules (applied in priority order):
    1. Empty string → ``""`` (never guess; the extractor passes the view name only
       when it has one; callers must pass ``""`` when no view is available).
    2. Strip a leading directory segment (e.g. ``"owners/ownerDetails"`` → ``"ownerDetails"``).
    3. Replace ``-`` / ``_`` with a space.
    4. Split camelCase tokens on casing boundaries.
    5. Title-case each word and join with a space.

    The function is conservative: it only transforms recognizable slug/camelCase strings.
    The result is a human label (e.g. ``"owners"`` → ``"Owners"``; ``"find-owners"`` →
    ``"Find Owners"``; ``"createOrUpdateOwnerForm"`` → ``"Create Or Update Owner Form"``).
    """
    if not view_name:
        return ""
    # Strip a leading directory segment: "owners/ownerDetails" → "ownerDetails"
    slug = view_name.rsplit("/", 1)[-1]
    # Replace hyphens and underscores with spaces
    slug = slug.replace("-", " ").replace("_", " ")
    # Split camelCase: insert space before each uppercase letter that follows a lowercase
    slug = re.sub(r"([a-z])([A-Z])", r"\1 \2", slug)
    # Collapse multiple spaces and title-case each word
    words = slug.split()
    if not words:
        return ""
    return " ".join(w.capitalize() for w in words)


def _collect_annotation_args(content: str, start: int) -> str:
    """Paren-depth collector starting at index of opening '('."""
    depth = 0
    i = start
    buf: list[str] = []
    while i < len(content):
        ch = content[i]
        buf.append(ch)
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth <= 0:
                break
        i += 1
    return "".join(buf)


def _extract_mapping_path(args_str: str) -> str:
    m = re.search(r'value\s*=\s*["\']([^"\']+)["\']', args_str)
    if m:
        return m.group(1)
    m = re.search(r'["\']([^"\']+)["\']', args_str)
    if m:
        return m.group(1)
    return ""


#: The ``method = RequestMethod.XXX`` attribute of ``@RequestMapping``. Matches a
#: single verb or the first verb of a ``{RequestMethod.GET, RequestMethod.POST}``
#: array (the array form is rare; we take the first as the primary verb).
_REQUEST_METHOD_RE = re.compile(
    r"method\s*=\s*\{?\s*RequestMethod\.([A-Z]+)",
)

_VERB_VOCAB = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}


def _extract_request_method(args_str: str) -> str | None:
    """Return the HTTP verb from a ``@RequestMapping(method = RequestMethod.XXX)``
    attribute, or ``None`` when no ``method`` attribute is present (Spring then
    defaults to matching all verbs — callers treat that as GET for inventory)."""
    m = _REQUEST_METHOD_RE.search(args_str)
    if m and m.group(1).upper() in _VERB_VOCAB:
        return m.group(1).upper()
    return None


def _is_comment_line(line: str) -> bool:
    """True if ``line`` is a Java comment line (``//`` / ``/*`` / ``*`` / ``*/``)."""
    stripped = line.lstrip()
    return stripped.startswith(("//", "/*", "*"))


_CLASS_DECL_RE = re.compile(r"\bclass\s+\w")


def _class_declaration_offset(content: str) -> int:
    """Character offset of the class declaration, ignoring `class` words in comments.

    Scans line by line for the first non-comment line containing ``class <Name>`` and
    returns its absolute offset; ``-1`` if none is found. Skipping comment lines keeps a
    ``class`` mention in a preceding Javadoc/line comment from shifting the offset early
    (which would wrongly reject a real class-level @RequestMapping).
    """
    offset = 0
    for line in content.split("\n"):
        if not _is_comment_line(line):
            m = _CLASS_DECL_RE.search(line)
            if m:
                return offset + m.start()
        offset += len(line) + 1
    return -1


def parse(  # noqa: PLR0912, PLR0915 — legitimately branchy extractor
    file_path: Path, content: str
) -> dict[str, list[Any]]:
    """Returns {} if file is not a .java file with @Controller/@RestController."""
    if Path(file_path).suffix.lower() != ".java":
        return {}
    if not any(sig in content for sig in _CONTROLLER_SIGNALS):
        return {}

    endpoints: list[dict[str, Any]] = []
    business_contexts: list[dict[str, Any]] = []
    form_fields: list[dict[str, Any]] = []
    nav_edges: list[dict[str, Any]] = []

    # A class-level @RequestMapping must appear BEFORE the class declaration. A plain
    # `content`-wide search would otherwise grab the first *method*-level
    # @RequestMapping(value="/full/path", ...) — the Spring PetClinic OwnerController
    # shape (no class prefix, absolute method paths) — and prepend it to every route,
    # doubling paths like "/owners/new/owners/new" (#functional-map Spring fix).
    # Locate the class declaration on the first NON-comment line matching `class <Name>`
    # so a "class" word in a preceding comment can't shift the offset early.
    class_decl_offset = _class_declaration_offset(content)
    class_prefix = ""
    m = _CLASS_REQUEST_MAPPING_RE.search(content)
    if m and (class_decl_offset == -1 or m.start() < class_decl_offset):
        class_prefix = m.group(1).rstrip("/")

    session_attrs: list[str] = []
    m = _SESSION_ATTRS_RE.search(content)
    if m:
        session_attrs = [s.strip().strip("\"'") for s in m.group(1).split(",")]

    is_rest = bool(_REST_CTRL_RE.search(content))
    endpoint_kind = "rest" if is_rest else "mvc"

    # Collect exception handler view names
    exc_views: list[tuple[str, str]] = []
    for m in _EXC_HANDLER_RE.finditer(content):
        exc_class = m.group(1)
        pos = m.end()
        body = content[pos : pos + 300]
        vm = _MODEL_AND_VIEW_RE.search(body)
        if vm and "redirect:" not in vm.group(1):
            exc_views.append((exc_class, vm.group(1)))
            continue
        # return "viewName"; — String-returning handler
        rv = _RETURN_STRING_RE.search(body)
        if rv and "redirect:" not in rv.group(1):
            exc_views.append((exc_class, rv.group(1)))

    lines = content.split("\n")
    class_decl_index = next(
        (
            idx
            for idx, ln in enumerate(lines)
            if not _is_comment_line(ln) and _CLASS_DECL_RE.search(ln)
        ),
        len(lines),
    )

    line_offsets: list[int] = []
    _off = 0
    for _ln in lines:
        line_offsets.append(_off)
        _off += len(_ln) + 1

    i = 0
    while i < len(lines):
        line = lines[i]

        # @Scheduled method → SYSTEM endpoint
        if _SCHEDULED_RE.search(line):
            for j in range(i + 1, min(i + 5, len(lines))):
                dm = _METHOD_DECL_RE.search(lines[j])
                if dm:
                    method_name = dm.group(1)
                    ep = {
                        "path": f"/system/{method_name}",
                        "method": "SYSTEM",
                        "handler": method_name,
                        "file": str(file_path),
                        "line": j + 1,
                    }
                    endpoints.append(ep)
                    business_contexts.append(
                        {
                            "path": ep["path"],
                            "method": "SYSTEM",
                            "handler": method_name,
                            "endpoint_kind": "system",
                            "session_writes": [],
                            "session_reads": [],
                            "error_flows": [],
                            "model_attributes": [],
                            "notification_hints": [],
                            "http_security_roles": [],
                            "form_fields": [],
                            "validation_rules": [],
                            "async_endpoint": False,
                            "pagination": False,
                            "view_name": "",
                        }
                    )
                    break

        # @InitBinder
        if "@InitBinder" in line:
            for j in range(i + 1, min(i + 15, len(lines))):
                ce_m = _CUSTOM_EDITOR_RE.search(lines[j])
                if ce_m:
                    field_name = ce_m.group(1)
                    form_fields.append(
                        {
                            "endpoint_id": "",
                            "name": field_name,
                            "field_type": "date",
                            "required": False,
                            "constraints": ("DateFormat",),
                            "source": "annotation",
                            "default_value": "",
                        }
                    )
                if lines[j].strip() == "}":
                    break

        # Mapping annotations
        mm = _METHOD_MAPPING_RE.search(line)
        if mm and _is_comment_line(line):
            # A Javadoc/line comment mentioning "@RequestMapping" (e.g. "Called before
            # each @RequestMapping annotated method") is not an endpoint — skip it so a
            # following @ModelAttribute/@InitBinder method is not emitted as a phantom
            # route (#functional-map Spring fix).
            mm = None
        if mm:
            if mm.group(1).upper() == "REQUEST" and i < class_decl_index:
                i += 1
                continue
            verb_prefix = mm.group(1).upper()
            # Shorthand mappings (@GetMapping/@PostMapping/…) carry the verb in the
            # annotation name. @RequestMapping does NOT — its verb lives in the
            # `method = RequestMethod.XXX` attribute (parsed from args below); with
            # no such attribute Spring matches all verbs and we treat it as GET for
            # inventory purposes.
            shorthand_method = {
                "GET": "GET",
                "POST": "POST",
                "PUT": "PUT",
                "PATCH": "PATCH",
                "DELETE": "DELETE",
            }.get(verb_prefix)

            # bare @GetMapping (no parens) → endpoint at the class prefix
            annotation_paren = line.find("(", mm.end())
            if annotation_paren == -1:
                path_suffix = ""
                args_str = ""
            else:
                line_start = line_offsets[i]
                paren_idx = content.find("(", line_start + mm.end())
                args_str = _collect_annotation_args(content, paren_idx)
                path_suffix = _extract_mapping_path(args_str)
            full_path = (class_prefix + path_suffix) if path_suffix else class_prefix

            # Resolve the HTTP verb: shorthand name wins; otherwise read the
            # @RequestMapping `method =` attribute (#1349); default GET.
            http_method = shorthand_method or _extract_request_method(args_str) or "GET"

            handler_name = "handler"
            view_name = ""
            session_writes: list[str] = list(session_attrs)
            session_reads: list[str] = []
            model_attributes: list[str] = []
            notification_hints: list[str] = []
            is_async = False
            is_pagination = False
            default_params: list[tuple[str, str]] = []
            handler_line = i + 1

            for j in range(i + 1, min(i + 60, len(lines))):
                jline = lines[j]
                dm = _METHOD_DECL_RE.search(jline)
                if dm and handler_name == "handler":
                    handler_name = dm.group(1)
                    handler_line = j + 1

                for ma in _MODEL_ATTR_RE.finditer(jline):
                    model_attributes.append(ma.group(1))

                for rp in _REQUEST_PARAM_DEFAULT_RE.finditer(jline):
                    default_params.append((rp.group(2), rp.group(1)))

                if _PAGE_PARAM_RE.search(jline):
                    is_pagination = True

                if any(sig in jline for sig in _ASYNC_SIGNALS):
                    is_async = True

                for sw in _SESSION_SET_RE.finditer(jline):
                    if sw.group(1) not in session_writes:
                        session_writes.append(sw.group(1))
                for sr in _SESSION_GET_RE.finditer(jline):
                    if sr.group(1) not in session_reads:
                        session_reads.append(sr.group(1))

                for nh in _NOTIFY_RE.finditer(jline):
                    hint = f"{nh.group(1)}.{nh.group(2)}"
                    if hint not in notification_hints:
                        notification_hints.append(hint)

                if not view_name:
                    rv = _RETURN_STRING_RE.search(jline)
                    if rv and "redirect:" not in rv.group(1):
                        view_name = rv.group(1)
                    mv = _MODEL_AND_VIEW_RE.search(jline)
                    if mv and "redirect:" not in mv.group(1):
                        view_name = mv.group(1)
                    rd = _REDIRECT_RE.search(jline)
                    if rd:
                        nav_edges.append(
                            {
                                "from_screen": full_path,
                                "to_screen": rd.group(1),
                                "kind": "redirect",
                                "file": str(file_path),
                                "line": j + 1,
                            }
                        )

                # Stop at next method-level annotation
                if (
                    j > i + 5
                    and jline.strip().startswith("@")
                    and any(
                        sig in jline
                        for sig in (
                            "@GetMapping",
                            "@PostMapping",
                            "@PutMapping",
                            "@PatchMapping",
                            "@DeleteMapping",
                            "@RequestMapping",
                            "@ExceptionHandler",
                            "@Scheduled",
                            "@InitBinder",
                        )
                    )
                ):
                    break

            error_flows = [exc_class for exc_class, _ in exc_views]

            ep = {
                "path": full_path,
                "method": http_method,
                "handler": handler_name,
                "file": str(file_path),
                "line": handler_line,
            }
            endpoints.append(ep)

            business_contexts.append(
                {
                    "path": full_path,
                    "method": http_method,
                    "handler": handler_name,
                    "endpoint_kind": endpoint_kind,
                    "view_name": view_name,
                    "session_writes": session_writes,
                    "session_reads": session_reads,
                    "error_flows": error_flows,
                    "model_attributes": model_attributes,
                    "notification_hints": notification_hints,
                    "http_security_roles": [],
                    "form_fields": [],
                    "validation_rules": [],
                    "async_endpoint": is_async,
                    "pagination": is_pagination,
                }
            )

            for param_name, default_val in default_params:
                form_fields.append(
                    {
                        "endpoint_id": "",
                        "name": param_name,
                        "field_type": "text",
                        "required": False,
                        "constraints": (),
                        "source": "annotation",
                        "default_value": default_val,
                    }
                )

        i += 1

    result: dict[str, list[Any]] = {}
    if endpoints:
        result["endpoints"] = endpoints
    if business_contexts:
        result["business_contexts"] = business_contexts
    if form_fields:
        result["form_fields"] = form_fields
    if nav_edges:
        result["nav_edges"] = nav_edges
    return result
