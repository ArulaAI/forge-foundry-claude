"""Adapter: S40 api-contract-review output → :class:`ModernizationDigest`.

This adapter normalizes the JSON artifact produced by the S40 ``api-contract-review``
primitive into a :class:`~primitives._shared.modernization.models.ModernizationDigest`
so that :func:`~primitives._shared.modernization.correlate.build_system_map` can join
API-exposed endpoints against the UI-consumed interfaces emitted by the functional-map
adapter (SP1.1 / #1312).

Agreed input dict shape (coordination point — the SP4 ``mod-modernization`` orchestrator
MUST feed this shape; field names chosen to match what ``api-contract-review`` naturally
serialises):

.. code-block:: python

    {
        # Required for useful output; safe-defaulted to [] when absent.
        "operations": [
            {
                "method": "GET",          # HTTP verb (str, case-insensitive)
                "path": "/owners/{id}",   # path template (OpenAPI / Express / etc.)
                "kind": "exposed",        # "exposed" (server-side) | "consumed" (outbound call)
            },
            ...
        ],
        # Schema / DTO names; mapped to key_entities.
        "schemas": ["Owner", "Pet", "Vet"],
        # Normalised flavor label (e.g. "openapi", "rest", "graphql", "grpc").
        "flavor": "openapi",
        # Debt / breaking-change / versioning notes (one-liner strings).
        "debt": [
            "No versioning strategy detected",
            "Breaking change: removed GET /owners/{id}/visits",
        ],
    }

All fields are optional via ``.get()`` with empty-collection defaults so a partial or
future-extended dict never raises ``KeyError``.

Cross-adapter join guarantee
----------------------------
Both this adapter and the functional-map adapter (``adapters/functional_map.py``) MUST
call :func:`~primitives._shared.modernization.iface.http_iface` for every HTTP interface
string.  :func:`http_iface` collapses all path-parameter spellings
(``{id}``, ``{ownerId}``, ``:id``, ``<int:pk>``, ``*``) to ``{}``, so two routes that
differ only in parameter *names* canonicalise to the same join key and
:func:`~primitives._shared.modernization.correlate.build_system_map` connects them:

    ``http_iface("GET", "/owners/{ownerId}")``  →  ``"GET /owners/{}"``
    ``http_iface("get", "/owners/{id}")``        →  ``"GET /owners/{}"``
"""

from __future__ import annotations

from typing import Any

from primitives._shared.modernization.iface import http_iface
from primitives._shared.modernization.models import ModernizationDigest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default ``flavor`` when the input dict carries no ``"flavor"`` key.
_DEFAULT_FLAVOR = "rest"

#: Maximum number of key entities / debt lines retained in the digest.
#: Keeps the digest within the ≤1.5k-token target.
_MAX_ENTITIES = 20
_MAX_DEBT = 10
_MAX_INTERFACES = 50


def to_digest(api_json: dict[str, Any], *, code_root: str) -> ModernizationDigest:
    """Normalise an S40 api-contract-review output dict into a :class:`ModernizationDigest`.

    Parameters
    ----------
    api_json:
        The dict produced by the S40 pipeline (or serialised from
        :class:`~primitives.api_contract_review.shared.models.APIContractReviewReport`).
        All keys are optional — missing keys are safe-defaulted to empty collections.
        See the module docstring for the agreed shape.
    code_root:
        Absolute path to the repository / service root being analysed.  Passed straight
        through to :attr:`~ModernizationDigest.code_root`.

    Returns
    -------
    ModernizationDigest
        A token-budgeted digest ready for
        :func:`~primitives._shared.modernization.correlate.build_system_map`.

    Notes
    -----
    *   Exposed operations (``"kind": "exposed"`` or omitted) → ``exposed_interfaces``
        via :func:`~primitives._shared.modernization.iface.http_iface`.
    *   Consumed operations (``"kind": "consumed"``) → ``consumed_interfaces``.
    *   ``"schemas"`` → ``key_entities`` (top-:data:`_MAX_ENTITIES`).
    *   ``"flavor"`` → ``flavor``; ``"debt"`` → ``debt_summary`` (top-:data:`_MAX_DEBT`).
    *   ``unit_count`` is set to the number of exposed operations (each endpoint is
        conceptually one service unit from the API surface perspective).
    """
    operations: list[dict[str, Any]] = list(api_json.get("operations") or [])
    schemas: list[str] = api_json.get("schemas") or []
    flavor: str = api_json.get("flavor") or _DEFAULT_FLAVOR
    debt: list[str] = api_json.get("debt") or []

    exposed: list[str] = []
    consumed: list[str] = []

    for op in operations:
        method: str = op.get("method") or ""
        path: str = op.get("path") or ""
        if not path:
            continue
        iface_str = http_iface(method, path)
        kind: str = op.get("kind") or "exposed"
        if kind == "consumed":
            consumed.append(iface_str)
        else:
            # "exposed" or unspecified — server-side declared endpoint
            exposed.append(iface_str)

    # Deduplicate while preserving order (dict trick, Python 3.7+)
    exposed = list(dict.fromkeys(exposed))[:_MAX_INTERFACES]
    consumed = list(dict.fromkeys(consumed))[:_MAX_INTERFACES]

    key_entities = list(dict.fromkeys(s for s in schemas if s))[:_MAX_ENTITIES]
    debt_summary = list(dict.fromkeys(d for d in debt if d))[:_MAX_DEBT]

    return ModernizationDigest(
        code_root=code_root,
        flavor=flavor,
        exposed_interfaces=exposed,
        consumed_interfaces=consumed,
        key_entities=key_entities,
        debt_summary=debt_summary,
        unit_count=len(exposed),
    )


# ---------------------------------------------------------------------------
# SP-F (#1386) — REST-API endpoint threading for the Current API Surface spec §8.
#
# ``to_endpoints`` maps an S40 ``ApiExtraction`` (serialised to a plain dict via
# ``model_dump(mode="json")``) into the endpoint-dict shape the Layer-1 spec
# renderer (:func:`primitives._shared.modernization.scope.render_spec`) reads for
# its §8 Current API Surface table: ``verb / path / request / response /
# status_codes / validation / security`` (kind ``current``/unset). SP3 (#1411)
# renamed §8 and merges these S40 endpoints with the functional-map controller
# inventory.
#
# QUAL-2 (cross-primitive ``shared`` guard): this module lives in Layer-1
# (``primitives/_shared/``) and MUST NOT import the Layer-2
# ``primitives.api_contract_review.shared`` models. The extraction is threaded as
# PLAIN DATA — a dict in, a list-of-dicts out — never by importing
# ``ApiExtraction`` here. See the module docstring and the design spec §SP-F.
# ---------------------------------------------------------------------------


def _validation_labels(fields: list[dict[str, Any]]) -> list[str]:
    """Compact per-field validation labels from a list of serialised FieldConstraints.

    Reads only the constraints the contract declared (all optional). A field with
    no declared bounds and not required contributes no label (nothing to render).
    """
    labels: list[str] = []
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        name = f.get("field") or ""
        if not name:
            continue
        parts: list[str] = []
        if f.get("required"):
            parts.append("required")
        if f.get("format"):
            parts.append(f"format={f['format']}")
        if f.get("minimum") is not None:
            parts.append(f"min={f['minimum']:g}")
        if f.get("maximum") is not None:
            parts.append(f"max={f['maximum']:g}")
        if f.get("min_length") is not None:
            parts.append(f"minLength={f['min_length']}")
        if f.get("max_length") is not None:
            parts.append(f"maxLength={f['max_length']}")
        if f.get("pattern"):
            parts.append(f"pattern={f['pattern']}")
        if f.get("enum"):
            parts.append("enum=" + "|".join(str(v) for v in f["enum"]))
        if parts:
            labels.append(f"{name} ({', '.join(parts)})")
    return labels


def _request_label(op: dict[str, Any]) -> str:
    """A human-readable request-DTO label for the §8 table (name or field list)."""
    name = op.get("request_schema_name") or ""
    if name:
        return str(name)
    fields = op.get("request_fields") or []
    names = [str(f.get("field")) for f in fields if isinstance(f, dict) and f.get("field")]
    return "{" + ", ".join(names) + "}" if names else ""


def _response_label(responses: list[dict[str, Any]]) -> str:
    """A human-readable response-DTO label (the named 2xx/other DTOs, in order)."""
    named: list[str] = []
    for r in responses or []:
        if not isinstance(r, dict):
            continue
        schema_name = r.get("schema_name") or ""
        if schema_name and str(schema_name) not in named:
            named.append(str(schema_name))
    return ", ".join(named)


def to_endpoints(api_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Map an S40 ``ApiExtraction`` dict → Target-REST-API endpoint dicts (§8).

    Parameters
    ----------
    api_json:
        A plain dict — typically ``ApiExtraction.model_dump(mode="json")`` from the
        S40 ``extract_operations`` pass. Every key is optional; a partial or absent
        ``operations`` list yields ``[]`` (the renderer then shows the honest SP-F
        stub). This function never imports the Layer-2 extraction model — it reads
        the dict positionally so the QUAL-2 cross-primitive guard stays green.

    Returns
    -------
    list[dict[str, Any]]
        One dict per operation, in the shape
        :func:`primitives._shared.modernization.scope.render_spec` reads for §8:
        ``{verb, path, request, response, status_codes, validation, security}``.
        ``validation`` is a list of compact per-field constraint labels (the
        request DTO's declared constraints); ``security`` is the operation's
        security-requirement scheme names.
    """
    operations = api_json.get("operations") or []
    endpoints: list[dict[str, Any]] = []
    for op in operations:
        if not isinstance(op, dict):
            continue
        verb = str(op.get("method") or "").upper()
        path = str(op.get("path") or "")
        if not verb or not path:
            continue
        responses = op.get("responses") or []
        status_codes = [
            str(r.get("status_code"))
            for r in responses
            if isinstance(r, dict) and r.get("status_code")
        ]
        endpoints.append(
            {
                "verb": verb,
                "path": path,
                "request": _request_label(op),
                "response": _response_label(responses),
                "status_codes": status_codes,
                "validation": _validation_labels(op.get("request_fields") or []),
                "security": [str(s) for s in (op.get("security") or [])],
            }
        )
    return endpoints
