"""Gemini ``responseSchema`` subset validator.

Google's Gemini CLI accepts ``response_format``-style schemas via the
``responseSchema`` field on the underlying ``generateContent`` API. The
accepted shape is a *subset* of JSON Schema Draft 2020-12 — several
top-level keywords that Pydantic emits freely (``anyOf``, ``$ref``
chains, ``additionalProperties: <schema>``) are rejected by Gemini's
parser or silently ignored.

The HarnessBackend protocol (`docs/superpowers/specs/
2026-06-01-harness-backend-design.md` §6.3 + §7.2) commits foundry to
"intersection enforcement" — primitives whose ``response_format`` is
fed through the harness MUST stay inside the documented subset, so
the same schema works against every supported harness's structured-
output mode (Claude, Copilot, Gemini, Antigravity).

This module provides:

* :func:`is_gemini_compatible` — the build-time gate. Returns
  ``(True, [])`` if a JSON Schema document stays inside the subset,
  ``(False, [violation_strings])`` otherwise. Each violation string
  is a JSON-pointer-prefixed reason so authors can grep to the
  offending node.

* :func:`accepts_extended_schema` — a class decorator that marks a
  Pydantic model as "the author has confirmed Gemini degrades
  gracefully on this schema; do not block". The marker sets
  ``__gemini_extended_schema__ = True`` on the model and is recognised
  by the discovery test in ``tests/test_response_schema_gemini_subset.py``.
  Use it sparingly — every escape-hatch use weakens the guarantee
  that the same primitive runs identically under every harness.

The disallowed-constructs list and their one-line rationales live in
:data:`DISALLOWED_CONSTRUCTS` so both this module and the documentation
draw from the same source.

References:

* SPEC §6.3 — gemini_cli compatibility notes.
* SPEC §7.2 — schema subset for harness portability.
* SPEC §7.3 — open question on harness-side downgrade negotiation
  (answered "no" for v1; this validator enforces the answer).
"""

from __future__ import annotations

from typing import Any, Final

# Maximum permitted depth of a ``$ref`` chain. Gemini's parser will follow at
# most two ``$ref`` hops before refusing — i.e. ``A → $ref B`` is allowed,
# ``A → $ref B → $ref C`` is not. We count the number of ``$ref`` arrows
# walked, not the recursion depth: a single ``$ref`` is depth-1.
MAX_REF_DEPTH: Final[int] = 2

# Forbidden top-level keywords, mapped to a one-line rationale that explains
# *why* Gemini's ``responseSchema`` parser rejects (or silently drops) each
# construct. Rationales reference the spec or Google's API documentation
# where known; "(per SPEC §6.3)" marks reasons sourced from the foundry
# design spec where Google's public docs are ambiguous.
DISALLOWED_CONSTRUCTS: Final[dict[str, str]] = {
    "oneOf": (
        "Gemini's responseSchema parser does not accept polymorphic unions; "
        "use a closed Literal discriminator field instead (per SPEC §6.3)."
    ),
    "anyOf": (
        "Gemini does not accept anyOf in responseSchema; Pydantic emits this "
        "for Optional[X] — switch to ``X | None`` modelled as a nullable "
        "field with ``nullable: true`` (Gemini extension) or fold into a "
        "single Literal with an explicit sentinel (per SPEC §7.2)."
    ),
    "allOf": (
        "Gemini's parser flattens allOf inconsistently; the spec disallows "
        "it so the same schema produces identical results across harnesses "
        "(per SPEC §7.2)."
    ),
    "not": (
        "Negation is not supported by Gemini's responseSchema parser; "
        "encode the inverse constraint positively (per SPEC §7.2)."
    ),
    "if": (
        "Conditional schemas (if/then/else) are not supported by Gemini; "
        "split into separate top-level fields with explicit Literals "
        "(per SPEC §7.2)."
    ),
    "then": (
        "Conditional schemas (if/then/else) are not supported by Gemini; "
        "split into separate top-level fields with explicit Literals "
        "(per SPEC §7.2)."
    ),
    "else": (
        "Conditional schemas (if/then/else) are not supported by Gemini; "
        "split into separate top-level fields with explicit Literals "
        "(per SPEC §7.2)."
    ),
    "patternProperties": (
        "patternProperties is not supported by Gemini; declare each "
        "expected key as an explicit property (per SPEC §7.2)."
    ),
    "dependencies": (
        "dependencies / dependentRequired are not supported by Gemini; "
        "encode the dependency as a Literal-discriminated record "
        "(per SPEC §7.2)."
    ),
    "dependentRequired": (
        "dependencies / dependentRequired are not supported by Gemini; "
        "encode the dependency as a Literal-discriminated record "
        "(per SPEC §7.2)."
    ),
    "dependentSchemas": (
        "dependencies / dependentRequired are not supported by Gemini; "
        "encode the dependency as a Literal-discriminated record "
        "(per SPEC §7.2)."
    ),
}


def _format_pointer(parts: list[str]) -> str:
    """Render a JSON Pointer (RFC 6901) from path parts."""
    if not parts:
        return "#"
    encoded = []
    for part in parts:
        # Per RFC 6901: ~ → ~0, / → ~1, then prefix with '/'.
        encoded.append(part.replace("~", "~0").replace("/", "~1"))
    return "#/" + "/".join(encoded)


def _resolve_pointer(root: dict[str, Any], pointer: str) -> Any:
    """Resolve a JSON Pointer (``#/foo/bar``) against ``root``.

    Returns ``None`` if the pointer cannot be resolved. We don't raise
    here — the walker reports an unresolvable $ref as a violation in its
    own right.
    """
    if not pointer.startswith("#"):
        return None
    if pointer in ("#", "#/"):
        return root
    parts = pointer.removeprefix("#/").split("/")
    node: Any = root
    for raw_part in parts:
        # Inverse of _format_pointer's encoding.
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node


def _walk(
    node: Any,
    path: list[str],
    findings: list[str],
    ref_depth: int,
    visited_refs: frozenset[str],
    root: dict[str, Any],
) -> None:
    """Recursive walker that collects violations into ``findings``.

    ``ref_depth`` tracks how many ``$ref`` arrows we have traversed on the
    current resolution chain. We RESOLVE $refs by walking ``root`` and
    descending into the target so Gemini's depth limit applies to the
    actual chain length (not just structural nesting). ``visited_refs``
    prevents infinite recursion on self-referential schemas; a re-visited
    $ref surfaces as a cycle violation.

    Sibling keys of a ``$ref`` node are NOT walked: per JSON Schema's
    historical $ref semantics (and Gemini's parser), sibling keys are
    ignored when ``$ref`` is present.
    """
    if not isinstance(node, dict):
        if isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, [*path, str(i)], findings, ref_depth, visited_refs, root)
        return

    pointer = _format_pointer(path)

    # Disallowed-keyword surface.
    for keyword, rationale in DISALLOWED_CONSTRUCTS.items():
        if keyword in node:
            findings.append(f"{pointer} {keyword}: {rationale}")

    # ``additionalProperties: <schema>`` is forbidden; only the boolean
    # forms (true, false) are accepted by Gemini. Pydantic emits the
    # schema form for ``dict[str, X]`` typed fields.
    if "additionalProperties" in node:
        ap = node["additionalProperties"]
        if not isinstance(ap, bool):
            findings.append(
                f"{pointer}/additionalProperties additionalProperties: "
                f"only ``true``/``false`` accepted by Gemini; got a schema "
                f"object — replace ``dict[str, X]`` with a closed-Literal "
                f"key set or a list of {{key, value}} records (per SPEC §7.2)."
            )

    # ``$ref`` resolution: follow the pointer and recurse into the target
    # with bumped depth. Sibling keys are ignored (JSON Schema $ref
    # semantics). If the ref cannot be resolved, surface that as a
    # violation.
    if "$ref" in node:
        ref_target = node["$ref"]
        new_depth = ref_depth + 1
        if not isinstance(ref_target, str):
            findings.append(
                f"{pointer}/$ref $ref: must be a JSON Pointer string; "
                f"got {type(ref_target).__name__}"
            )
            return
        if ref_target in visited_refs:
            findings.append(
                f"{pointer}/$ref $ref-cycle: $ref {ref_target!r} re-entered "
                f"on the same chain; Gemini cannot resolve cyclic schemas — "
                f"break the cycle by inlining the recursive shape "
                f"(per SPEC §7.2)."
            )
            return
        if new_depth > MAX_REF_DEPTH:
            findings.append(
                f"{pointer}/$ref $ref-depth: chain depth {new_depth} exceeds "
                f"Gemini's max-depth {MAX_REF_DEPTH} (per SPEC §6.3); "
                f"flatten the referenced model inline."
            )
            return
        target = _resolve_pointer(root, ref_target)
        if target is None:
            findings.append(
                f"{pointer}/$ref $ref-unresolved: pointer {ref_target!r} does "
                f"not resolve against the schema document; Gemini will reject "
                f"an unresolvable $ref."
            )
            return
        _walk(
            target,
            [*path, "$ref→"],
            findings,
            new_depth,
            visited_refs | {ref_target},
            root,
        )
        return  # JSON Schema $ref ignores sibling keys.

    # Normal descent (no $ref). $defs entries are NOT walked at top level
    # unless reached via a $ref — Gemini only parses the schema reachable
    # from the root, so an orphan $defs entry is dead weight, not a
    # violation surface.
    for key, value in node.items():
        if key in {"$defs", "definitions"}:
            continue
        _walk(value, [*path, key], findings, ref_depth, visited_refs, root)


def is_gemini_compatible(schema: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return ``(True, [])`` if ``schema`` stays inside Gemini's accepted subset.

    Otherwise return ``(False, violations)`` where each violation is a string
    of the form ``"<json-pointer> <constraint>: <what failed>"``. Authors can
    grep the violation list to the offending schema node.

    Constructs flagged (see :data:`DISALLOWED_CONSTRUCTS`):

    * ``oneOf``, ``anyOf``, ``allOf`` — polymorphic combinators.
    * ``not``, ``if``/``then``/``else`` — negation + conditional schemas.
    * ``patternProperties``, ``dependencies``, ``dependentRequired``,
      ``dependentSchemas`` — pattern / dependency keywords.
    * ``additionalProperties: <schema>`` beyond ``true``/``false``.
    * ``$ref`` chains exceeding :data:`MAX_REF_DEPTH` or containing cycles.

    The walker resolves ``$ref`` targets by walking ``root`` and descending
    into the referenced node, bumping the ref-chain depth by one per hop.
    Cycles are detected (the same ``$ref`` re-entering the chain) and the
    ``MAX_REF_DEPTH`` cap matches Gemini's documented parse-depth limit.
    Sibling keys of a ``$ref`` node are skipped per JSON Schema's historical
    ``$ref`` semantics.
    """
    if not isinstance(schema, dict):
        return False, [f"# schema-type: top-level must be a dict, got {type(schema).__name__}"]

    findings: list[str] = []
    _walk(schema, [], findings, ref_depth=0, visited_refs=frozenset(), root=schema)
    return (not findings), findings


# ---------------------------------------------------------------------------
# Escape hatch: @accepts_extended_schema
# ---------------------------------------------------------------------------


def accepts_extended_schema[T](cls: type[T]) -> type[T]:
    """Mark a Pydantic model as opted-out of the strict Gemini subset.

    The decorator sets ``__gemini_extended_schema__ = True`` on the class.
    The discovery test in ``tests/test_response_schema_gemini_subset.py``
    reads this attribute and skips the strict subset check for any
    decorated model.

    Use this only when the model's author has confirmed (via fixture, manual
    test, or harness-team coordination) that Gemini degrades gracefully on
    the schema — typically by ignoring the unsupported construct and falling
    back to free-form completion + Pydantic parse on the foundry side
    (SPEC §8.2: ``response_format_unsupported`` → free-form + parse-and-retry).

    Example::

        from primitives._response_schema_validator import accepts_extended_schema

        @accepts_extended_schema
        class SchemaDigest(BaseModel):
            tables: list[Table]
            # ... contains $ref chains > depth 2; author confirmed degradation
    """
    cls.__gemini_extended_schema__ = True  # type: ignore[attr-defined]
    return cls


def is_extended_schema_model(cls: type) -> bool:
    """Return True if ``cls`` was decorated with :func:`accepts_extended_schema`.

    Reads ``__gemini_extended_schema__`` only on the class itself (not inherited)
    so a subclass of an extended-schema base still has to declare its own
    escape-hatch explicitly. Discovery callers use this to filter out models
    that opt out of the subset check.
    """
    return bool(cls.__dict__.get("__gemini_extended_schema__", False))
