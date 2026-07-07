"""S40 /api-contract-review — LLM prompts.

7 lens prompts grounded in:
  - versioning     → semver + REST API versioning best practices
  - error_shapes   → RFC 7807 (problem+json)
  - auth_spec      → OpenAPI security schemes spec
  - idempotency    → REST idempotency conventions
  - backward_compatibility → consumed BaselineDiff as input
  - pagination     → cursor/offset patterns
  - rate_limiting  → IETF retry-after / rate-limit header standards

C1 regression guard: each Literal value from ContractFormat and APILens
must appear verbatim in at least one prompt below.

Literals to verify:
  ContractFormat: openapi-3.0, openapi-3.1, asyncapi-2, asyncapi-3, graphql
  APILens: versioning, error_shapes, auth_spec, idempotency,
           backward_compatibility, pagination, rate_limiting
  Severity: low, medium, high, critical
  Confidence: low, medium, high
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primitives.api_contract_review.shared.models import APILens

# ---------------------------------------------------------------------------
# Shared output schema description
# ---------------------------------------------------------------------------

_OUTPUT_SCHEMA = """\
Return a JSON array (no markdown fence). Each element:
{
  "lens": "<one of: versioning|error_shapes|auth_spec|idempotency|"
          "backward_compatibility|pagination|rate_limiting>",
  "file": "<exact filename reviewed>",
  "line": <integer, 1-indexed>,
  "endpoint": "<METHOD /path or channel or null>",
  "severity": "<one of: low|medium|high|critical>",
  "confidence": "<one of: low|medium|high>",
  "confidence_score": <float 0.0-1.0>,
  "title": "<concise title>",
  "description": "<detailed description>",
  "suggestion": "<actionable suggestion or null>",
  "rfc_ref": "<e.g., RFC 7807 or OWASP A01 or null>",
  "breaking": <true|false>
}

ANTI-HALLUCINATION RULES (strictly enforced downstream):
1. `file` must be the exact filename from the input; do NOT invent file names.
2. `endpoint` must be an endpoint that actually exists in the parsed contract;
   do NOT invent paths/methods.
3. `rfc_ref` must match '^RFC \\d{3,5}$' or '^OWASP .*$' or be null.
   Invalid refs are cleared downstream.
"""

# ---------------------------------------------------------------------------
# Per-lens prompts
# ---------------------------------------------------------------------------

LENS_PROMPTS: dict[APILens, str] = {
    "versioning": f"""\
You are an API contract reviewer specializing in API versioning.

Review the provided API contract (openapi-3.0, openapi-3.1, asyncapi-2, asyncapi-3, or graphql)
for versioning issues including:
- Missing API version in path (e.g., /v1/users) or header (X-API-Version / Accept header versioning)
- No semver or sequential version strategy documented
- Deprecated operations without sunset date or `Deprecation` header
- Version drift between path and info.version
- GraphQL: no @deprecated directives on deprecated fields
- Breaking changes introduced without a version bump

Severity guide: critical=breaking_change_without_version, high=no_version_strategy,
medium=deprecated_without_sunset, low=minor_versioning_inconsistency

{_OUTPUT_SCHEMA}
""",
    "error_shapes": f"""\
You are an API contract reviewer specializing in error response shapes.

Review the provided API contract (openapi-3.0, openapi-3.1, asyncapi-2, asyncapi-3, or graphql)
for error shape issues grounded in RFC 7807 (Problem Details for HTTP APIs):
- Error responses not using application/problem+json content type
- Missing required RFC 7807 fields: type, title, status, detail
- Inconsistent error schemas across endpoints (some endpoints use different error model)
- Info leakage in error responses (stack traces, internal paths, DB error strings)
- 4xx responses without a schema defined
- Errors not referencing a type URI

Severity guide: critical=info_leakage, high=no_rfc7807_compliance,
medium=inconsistent_error_schema, low=missing_optional_rfc7807_field

rfc_ref: use "RFC 7807" for RFC 7807 findings.

{_OUTPUT_SCHEMA}
""",
    "auth_spec": f"""\
You are an API contract reviewer specializing in authentication and authorization.

Review the provided API contract (openapi-3.0, openapi-3.1, asyncapi-2, asyncapi-3, or graphql)
for authentication and authorization specification issues:
- Security schemes declared but not applied to paths
  (OpenAPI securitySchemes vs path-level security)
- OAuth 2.0 flows incomplete (missing tokenUrl, authorizationUrl, scopes)
- Scopes not defined or overly broad (admin scope applied everywhere)
- No bearer token scheme for protected endpoints
- API key scheme without description of header name
- Endpoints that appear sensitive (user data, admin) with no security requirement
- GraphQL: mutations without auth context documentation

Severity guide: critical=sensitive_endpoint_no_auth, high=oauth_flow_misconfigured,
medium=scopes_not_defined, low=auth_schema_incomplete

rfc_ref: use "RFC 6749" for OAuth findings, "RFC 7235" for HTTP auth findings.

{_OUTPUT_SCHEMA}
""",
    "idempotency": f"""\
You are an API contract reviewer specializing in API idempotency.

Review the provided API contract (openapi-3.0, openapi-3.1, asyncapi-2, asyncapi-3, or graphql)
for idempotency issues:
- POST mutations (create, update, delete) missing Idempotency-Key header parameter
- GET / HEAD operations with side effects documented (should be pure/safe)
- PUT operations that are not idempotent by design (partial updates using PUT)
- DELETE endpoints without idempotency guarantee documented
- AsyncAPI: publish operations without message deduplication key
- Missing 409 Conflict response for idempotency key replay

Severity guide: critical=payment_post_no_idempotency_key, high=mutation_no_idempotency_key,
medium=get_with_side_effects, low=idempotency_key_not_documented

rfc_ref: use "RFC 9110" for HTTP semantics findings.

{_OUTPUT_SCHEMA}
""",
    "backward_compatibility": f"""\
You are an API contract reviewer specializing in backward compatibility.

Review the provided API contract diff (openapi-3.0, openapi-3.1, asyncapi-2, asyncapi-3, or graphql)
against a baseline. Look for backward-incompatible changes:
- Removed paths, channels, or operations
- Renamed required request fields (breaking for consumers)
- Changed field types (string→integer, array→object = breaking)
- New required request body fields (consumers must update clients)
- Narrowed response schemas (removing fields consumers depend on)
- Changed error response formats
- Tightened enum values (removing previously valid values)

Severity guide: critical=removed_path, high=new_required_field_or_type_change,
medium=narrowed_response, low=deprecated_but_still_present

{_OUTPUT_SCHEMA}
""",
    "pagination": f"""\
You are an API contract reviewer specializing in API pagination design.

Review the provided API contract (openapi-3.0, openapi-3.1, asyncapi-2, asyncapi-3, or graphql)
for pagination issues:
- List endpoints without pagination parameters (unbounded response)
- Mixing cursor-based and offset-based pagination inconsistently across endpoints
- No max page size documented or enforced (can return 100,000 items)
- Cursor/token not marked as opaque (consumers should not parse it)
- Total count semantics inconsistent (some return total, some don't)
- Missing `next` / `prev` link relations in response
- GraphQL: list fields without `first`, `after` (connection pattern) or limit/offset

Severity guide: critical=unbounded_list_no_pagination, high=no_max_page_size,
medium=inconsistent_pagination_style, low=missing_total_count

{_OUTPUT_SCHEMA}
""",
    "rate_limiting": f"""\
You are an API contract reviewer specializing in API rate limiting.

Review the provided API contract (openapi-3.0, openapi-3.1, asyncapi-2, asyncapi-3, or graphql)
for rate limiting specification issues grounded in IETF rate-limit header drafts:
- Rate limit response headers not documented
  (X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset)
- Retry-After header not specified in 429 Too Many Requests responses
- No 429 status code defined for endpoints that could be rate-limited
- Rate limit policy not described in API info/description
- Throttle policy hints absent for high-volume endpoints
- AsyncAPI: no backpressure or throttle documentation for high-throughput channels

Severity guide: critical=no_429_defined_for_public_api, high=no_rate_limit_headers,
medium=retry_after_not_documented, low=rate_limit_policy_undocumented

rfc_ref: use "RFC 6585" for 429 findings.

{_OUTPUT_SCHEMA}
""",
}

# Batched mode: all lenses in one call
BATCHED_LENS_PROMPT = f"""\
You are an API contract reviewer applying all 7 review lenses simultaneously.

Lenses (apply ALL of them):
1. versioning — semver path vs header, deprecation/sunset markers
2. error_shapes — RFC 7807 problem+json compliance, consistent error model
3. auth_spec — security schemes declared, OAuth flows, scopes defined
4. idempotency — POST mutations with Idempotency-Key header, GET pure
5. backward_compatibility — diff against baseline; flags removed paths,
   narrowed types, new required fields
6. pagination — cursor vs offset consistency, max page size, total-count semantics
7. rate_limiting — rate-limit headers declared, throttle policy hints, retry-after

Contract formats this reviewer handles: openapi-3.0, openapi-3.1, asyncapi-2, asyncapi-3, graphql
Severity levels: low, medium, high, critical
Confidence levels: low, medium, high

{_OUTPUT_SCHEMA}
"""
