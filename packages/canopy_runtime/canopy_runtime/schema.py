"""The declarative runtime spec an agent ships in its own repo (`runtime.yaml`).

This is the *desired* runtime for one agent: which plugins it needs (canopy + ace +
its own), which MCP servers and tools, which execution engine it prefers, which
secrets it needs (**by reference name only — never a value**), and the preflight
checks that define "ready". The reconciler (RS2) reads this from the agent's repo,
diffs it against the current box, applies only the gaps, and runs the preflight.

canopy-web does NOT parse this — it only points a runner at the repo (RS1). This
module *defines and validates* the shape so the agent's PRs can be linted and the
reconciler can load it with confidence.

Two hard rules encoded here:
  1. `extra="forbid"` everywhere — a typo'd key is a validation error in the
     agent's PR, not a silently-ignored field that never provisions.
  2. `secrets` is a list of reference *names*. There is no field anywhere that
     holds a secret value; the value is resolved from the env's store (1Password
     on a laptop, Secrets Manager on a cloud box) by the reconciler.
"""
from __future__ import annotations

import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The engine axis (RS4). Mirrors agents.Agent.runtime_engine on canopy-web.
Engine = Literal["emdash", "cloud_p", "any"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PluginRef(_Strict):
    """A `.claude-plugin` the agent depends on (canopy, ace, its own …)."""

    name: str = Field(min_length=1)
    # Where to get it: a git URL, a marketplace ref, or a local path. Empty means
    # "resolved by name from the default marketplace" — non-secret either way.
    source: str = ""


class McpRef(_Strict):
    """An MCP server the agent's runtime must have wired up."""

    name: str = Field(min_length=1)
    # Optional endpoint/command hint; empty = the plugin that provides it wires it.
    url: str = ""


class ToolRef(_Strict):
    """A CLI tool / binary the runtime requires on PATH (e.g. `gh`, `claude`)."""

    name: str = Field(min_length=1)


class PreflightCheck(_Strict):
    """One check that must pass for the box to be 'ready' for this agent.

    `run` is a NON-SECRET shell probe descriptor (e.g. `claude whoami`); `expect`
    is an optional substring the reconciler looks for in its output. A failing
    check the box can't self-heal (interactive OAuth) becomes 'needs bootstrap'.
    """

    name: str = Field(min_length=1)
    run: str = ""
    expect: str = ""


class SecretRef(_Strict):
    """A secret the agent needs, declared by REFERENCE NAME plus where its value
    must land. The name resolves against the env's store (1Password); `env` and/or
    `path` say how the runtime consumes it — the repo declares this because only
    the repo knows its own tools (claude wants `CLAUDE_CODE_OAUTH_TOKEN`, gog wants
    a credentials file). Still **never a value** — only a reference + destination.
    """

    name: str = Field(min_length=1)
    # Inject the resolved value into this environment variable (the common case).
    env: str = ""
    # Or write it to this file path (e.g. a gog credentials.json). `~` is expanded.
    path: str = ""
    # If true, absence is fine (skipped) rather than a "needs bootstrap" gap.
    optional: bool = False

    @field_validator("name")
    @classmethod
    def _name_is_a_ref_not_a_value(cls, v: str) -> str:
        if not v or len(v) > 120 or any(c in v for c in "= \t\n"):
            raise ValueError(
                f"secret name is a reference NAME, not a value; got {v!r}. "
                "Put the value in the env's secret store and reference its name."
            )
        return v


# --- capabilities ----------------------------------------------------------
#
# A capability is a machine the agent leases from canopy-web for as long as it
# needs it (docs/architecture/capabilities.md in canopy-web). The agent DECLARES
# it here, in its own repo: `runtime.yaml` points at one `capability.yaml` per
# capability, and `canopy-runtime publish-capabilities` sends them to canopy-web,
# which validates them with this same model. canopy-web owns the generic
# lifecycle; everything specific (the stack, the image, the CLI on it) is the
# agent's.

CAPABILITY_NAME = r"^[a-z0-9-]{3,40}$"
_PARAM_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_EXEC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_PY_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
}


class CapabilityRef(_Strict):
    """`runtime.yaml` → one capability: a path to its manifest, relative to runtime.yaml."""

    manifest: str = Field(min_length=1)

    @field_validator("manifest")
    @classmethod
    def _relative(cls, v: str) -> str:
        if v.startswith("/") or v.startswith("~"):
            raise ValueError(f"manifest is a path in the agent's repo, relative to runtime.yaml; got {v!r}")
        return v


class CapabilityAws(_Strict):
    region: str = Field(pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")
    #: The CloudFormation stack (an instance of canopy's shared
    #: ondemand-capability template, Mode=ephemeral) whose Outputs canopy-web reads.
    stack: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9-]{0,127}$")


class CapabilityLimits(_Strict):
    """Ceilings. A workspace admin's grant may only LOWER them."""

    max_concurrent: int = Field(default=1, ge=1, le=50)
    max_lease_s: int = Field(default=3600, ge=60, le=172800)
    idle_release_s: int = Field(default=900, ge=60, le=172800)
    ready_timeout_s: int = Field(default=900, ge=60, le=7200)

    @model_validator(mode="after")
    def _consistent(self) -> "CapabilityLimits":
        if self.idle_release_s > self.max_lease_s:
            raise ValueError("idle_release_s cannot exceed max_lease_s")
        return self


class CapabilityParam(_Strict):
    """One parameter an acquire may pass; delivered to the machine as user-data."""

    type: Literal["string", "integer", "number", "boolean"] = "string"
    default: Any = None
    enum: list[Any] | None = None
    description: str = ""

    def accepts(self, value: Any) -> bool:
        if isinstance(value, bool) and self.type != "boolean":
            return False  # bool is an int subclass; True is not a number here
        if not isinstance(value, _PY_TYPES[self.type]):
            return False
        return self.enum is None or value in self.enum

    @model_validator(mode="after")
    def _default_is_valid(self) -> "CapabilityParam":
        if self.enum is not None:
            if not self.enum:
                raise ValueError("enum, when given, needs at least one value")
            bad = [v for v in self.enum if not CapabilityParam(type=self.type).accepts(v)]
            if bad:
                raise ValueError(f"enum values {bad!r} are not of type {self.type}")
        if self.default is not None and not self.accepts(self.default):
            raise ValueError(f"default {self.default!r} is not a valid {self.type}"
                             + (f" in {self.enum!r}" if self.enum else ""))
        return self


class CapabilityExec(_Strict):
    #: argv[0] allowlist: bare command names on the machine's PATH. canopy-web
    #: refuses any exec whose argv[0] is not exactly one of these.
    allow: list[str] = Field(min_length=1)

    @field_validator("allow")
    @classmethod
    def _bare_names(cls, v: list[str]) -> list[str]:
        for name in v:
            if not _EXEC_NAME.match(name):
                raise ValueError(f"exec.allow entries are bare command names (no path, no spaces); got {name!r}")
        return v


class CapabilityManifest(_Strict):
    """`capability.yaml`, version 1 — what canopy-web brokers for the agent."""

    version: Literal[1] = 1
    name: str = Field(pattern=CAPABILITY_NAME)
    #: The only kind v1 brokers: one fresh EC2 instance per lease.
    kind: Literal["ec2-ephemeral"]
    description: str = ""
    aws: CapabilityAws
    limits: CapabilityLimits = Field(default_factory=CapabilityLimits)
    params: dict[str, CapabilityParam] = Field(default_factory=dict)
    exec: CapabilityExec

    @field_validator("params")
    @classmethod
    def _param_names(cls, v: dict[str, CapabilityParam]) -> dict[str, CapabilityParam]:
        for name in v:
            if not _PARAM_NAME.match(name):
                raise ValueError(f"param names are lowercase identifiers; got {name!r}")
        return v

    def resolve_params(self, given: dict[str, Any] | None) -> dict[str, Any]:
        """The params an acquire delivers: `given` checked against the declaration,
        with defaults filled in. Raises ValueError naming every problem."""
        given = dict(given or {})
        problems = [f"unknown param {k!r}" for k in sorted(set(given) - set(self.params))]
        out: dict[str, Any] = {}
        for name, decl in self.params.items():
            if name in given:
                value = given[name]
                if not decl.accepts(value):
                    problems.append(f"param {name!r}={value!r} is not a valid {decl.type}"
                                    + (f" (one of {decl.enum!r})" if decl.enum else ""))
                    continue
                out[name] = value
            elif decl.default is not None:
                out[name] = decl.default
            else:
                problems.append(f"param {name!r} is required")
        if problems:
            raise ValueError("; ".join(problems))
        return out


def load_capability_manifest(text: str) -> CapabilityManifest:
    """Parse + validate one capability.yaml."""
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("capability.yaml must be a mapping at the top level")
    return CapabilityManifest.model_validate(data)


class RuntimeSpec(_Strict):
    """The whole `runtime.yaml` — one agent's declarative runtime."""

    version: int = 1
    engine: Engine = "any"
    plugins: list[PluginRef] = Field(default_factory=list)
    mcp: list[McpRef] = Field(default_factory=list)
    tools: list[ToolRef] = Field(default_factory=list)
    # Secret REFERENCES (name + destination), never values — see SecretRef.
    secrets: list[SecretRef] = Field(default_factory=list)
    # Non-secret literal env the runtime needs (Drive folder IDs, a gog client
    # name, doc IDs — echo's .env is full of these). NOT secrets: values live here
    # in the clear because they aren't sensitive. The reconciler injects them
    # alongside the resolved secrets.
    env: dict[str, str] = Field(default_factory=dict)
    preflight: list[PreflightCheck] = Field(default_factory=list)
    # Capabilities this agent declares (published to canopy-web by
    # `canopy-runtime publish-capabilities`), each a path to its capability.yaml.
    capabilities: list[CapabilityRef] = Field(default_factory=list)


def load_runtime_yaml(text: str) -> RuntimeSpec:
    """Parse + validate a runtime.yaml document. Raises on malformed YAML or a
    spec that violates the schema (unknown key, secret value, missing name)."""
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("runtime.yaml must be a mapping at the top level")
    return RuntimeSpec.model_validate(data)
