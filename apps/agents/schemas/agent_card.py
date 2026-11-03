from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel

from apps.common.schemas import StrictModel


class _A2AModel(StrictModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True,
                              extra="forbid", from_attributes=True)


class A2AAgentProvider(_A2AModel):
    organization: str
    url: str


class A2AAgentCapabilities(_A2AModel):
    streaming: bool = False
    push_notifications: bool = False
    extended_agent_card: bool = False


class A2AAgentInterface(_A2AModel):
    url: str
    protocol_binding: str
    protocol_version: str


class A2AStringList(_A2AModel):
    # The proto field is `list`; named otherwise here so it cannot shadow the
    # builtin inside the class body.
    values: list[str] = Field(default_factory=list, alias="list")


class A2ASecurityRequirement(_A2AModel):
    # A scheme NAME (a key of `securitySchemes`) → the scopes it needs.
    schemes: dict[str, A2AStringList]


class A2AHTTPAuthSecurityScheme(_A2AModel):
    description: str = ""
    scheme: str
    bearer_format: str = ""


class A2AAPIKeySecurityScheme(_A2AModel):
    description: str = ""
    location: Literal["query", "header", "cookie"]
    name: str


class A2ASecurityScheme(_A2AModel):
    # A proto `oneof`: exactly one of these is set on any one scheme.
    http_auth_security_scheme: A2AHTTPAuthSecurityScheme | None = None
    api_key_security_scheme: A2AAPIKeySecurityScheme | None = None


class A2AAgentSkill(_A2AModel):
    id: str
    name: str
    description: str
    tags: list[str]
    input_modes: list[str] | None = None
    output_modes: list[str] | None = None
    security_requirements: list[A2ASecurityRequirement] | None = None


class A2AAgentCardOut(_A2AModel):
    """An A2A v1.0 Agent Card, generated from the agent's declared interface."""

    name: str
    description: str
    supported_interfaces: list[A2AAgentInterface]
    provider: A2AAgentProvider
    version: str
    documentation_url: str | None = None
    capabilities: A2AAgentCapabilities
    security_schemes: dict[str, A2ASecurityScheme]
    security_requirements: list[A2ASecurityRequirement]
    default_input_modes: list[str]
    default_output_modes: list[str]
    skills: list[A2AAgentSkill]
    icon_url: str | None = None


# ---- ideas + contributor pages (hal T69) ----
