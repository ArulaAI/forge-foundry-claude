"""Output data models for runtime_explore (#1472).

The Phase-A crawler emits a :class:`CrawlGraph` (intermediate); Phase C renders the
final :class:`RuntimeJourneys` artifact. ``NavEdge``/``ExternalEdge`` use the JSON key
``from`` (a Python keyword) via a Pydantic alias on ``frm``. All models are frozen
(immutable value objects). Pure Pydantic — no Playwright import here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)

_Reached = Literal["crawl", "agent"]


class FormField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    type: str
    required: bool = False


class Form(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    fields: tuple[FormField, ...] = ()


class Screen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    url: str
    title: str = ""
    screenshot: str = ""
    forms: tuple[Form, ...] = ()
    reached_via: _Reached = "crawl"
    auth_required: bool = False
    #: The role(s) whose separate crawl reached this screen (#1563). A sorted tuple in a
    #: multi-role run; EMPTY for a single-perspective/untagged run — and omitted from the
    #: serialized output when empty, so single-role JSON is byte-identical (no `roles: []`).
    roles: tuple[str, ...] = ()

    @model_serializer(mode="wrap")
    def _ser(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        # Omit `roles` entirely when empty so a single-perspective dump matches the
        # pre-#1563 shape byte-for-byte (the runtime-journeys schema is
        # additionalProperties:false and existing goldens/assertions expect no `roles`).
        if not self.roles:
            data.pop("roles", None)
        return data


class NavEdge(BaseModel):
    """A screen→screen navigation edge. Serializes the source as JSON key ``from``.

    ``frm`` maps to/from the JSON key ``from`` (a Python keyword): construct with
    ``frm=`` (mypy-friendly — no pydantic mypy plugin needed), dump with
    ``by_alias=True`` to get ``from``, and validate from either key.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    frm: str = Field(validation_alias=AliasChoices("frm", "from"), serialization_alias="from")
    to: str
    trigger: str
    label: str = ""
    kind: str = ""


class ExternalEdge(BaseModel):
    """An outbound edge to an off-allowlist URL — recorded but never navigated."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    frm: str = Field(validation_alias=AliasChoices("frm", "from"), serialization_alias="from")
    to_url: str
    not_followed: bool = True


class Journey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    persona: str
    goal: str
    path: tuple[str, ...]
    narrative: str = ""
    reached_via: _Reached = "agent"


class CrawlGraph(BaseModel):
    """The Phase-A intermediate: the deterministic breadth crawl result."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    screens: tuple[Screen, ...] = ()
    nav_edges: tuple[NavEdge, ...] = ()
    external_edges: tuple[ExternalEdge, ...] = ()
    budget_visited: int = 0
    truncated: bool = False


class RuntimeJourneys(BaseModel):
    """The final machine artifact (``runtime-journeys.json``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    primitive: Literal["runtime-explore"] = "runtime-explore"
    base_url: str
    generated_at: str
    screens: tuple[Screen, ...] = ()
    nav_edges: tuple[NavEdge, ...] = ()
    journeys: tuple[Journey, ...] = ()
    external_edges: tuple[ExternalEdge, ...] = ()
    budget: dict[str, object] = Field(default_factory=dict)
    #: Per-role reachable-screen count (#1563): role name → number of screens that role's
    #: crawl reached. EMPTY for a single-perspective run — and omitted from the serialized
    #: output when empty, so single-role JSON is byte-identical.
    role_coverage: dict[str, int] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def _ser(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if not self.role_coverage:
            data.pop("role_coverage", None)
        return data


__all__ = [
    "CrawlGraph",
    "ExternalEdge",
    "Form",
    "FormField",
    "Journey",
    "NavEdge",
    "RuntimeJourneys",
    "Screen",
]
