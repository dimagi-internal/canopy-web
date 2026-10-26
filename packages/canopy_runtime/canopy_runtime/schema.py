"""The capability manifest an agent ships in its own repo (`capability.yaml`).

A capability is a machine (or a pool of slots) the agent leases from canopy-web.
The agent DECLARES it in its own repo, one `capability.yaml` per capability;
`canopy-runtime publish-capabilities` validates each with `CapabilityManifest` and
sends them to canopy-web, which validates them again with this same model.

`extra="forbid"` everywhere: a typo'd key is a validation error in the agent's PR,
not a silently-ignored field.
"""
from __future__ import annotations

import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- capabilities ----------------------------------------------------------
#
# canopy-web owns the generic lifecycle (docs/architecture/capabilities.md);
# everything specific (the stack, the image, the CLI on it) is the agent's.

CAPABILITY_NAME = r"^[a-z0-9-]{3,40}$"
_PARAM_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_EXEC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_PY_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
}


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


#: The kinds canopy-web brokers. `ec2-ephemeral`: one fresh EC2 instance per
#: lease, from the agent's own stack. `pool`: one NAMED SLOT of a small fixed set
#: per lease — an exclusive checkout (a test account, a licence seat) with no
#: machine, no stack and no AWS call.
CapabilityKind = Literal["ec2-ephemeral", "pool"]
#: The one acquire param a pool accepts; the broker owns it.
POOL_SLOT_PARAM = "slot"
MAX_POOL_SLOTS = 50


class CapabilityManifest(_Strict):
    """`capability.yaml`, version 1 — what canopy-web brokers for the agent.

    `ec2-ephemeral` needs `aws` and `exec` and takes no `slots`. `pool` needs
    `slots` (1-50 unique names) and takes no `aws`, `exec` or `params`; its
    `limits.max_concurrent` defaults to `len(slots)` and may only lower it."""

    version: Literal[1] = 1
    name: str = Field(pattern=CAPABILITY_NAME)
    kind: CapabilityKind
    description: str = ""
    aws: CapabilityAws | None = None
    limits: CapabilityLimits = Field(default_factory=CapabilityLimits)
    params: dict[str, CapabilityParam] = Field(default_factory=dict)
    exec: CapabilityExec | None = None
    #: pool only: the slot names, handed out in this order.
    slots: list[str] | None = None

    @model_validator(mode="after")
    def _shape_of_kind(self) -> "CapabilityManifest":
        if self.kind == "ec2-ephemeral":
            missing = [k for k in ("aws", "exec") if getattr(self, k) is None]
            if missing:
                raise ValueError(f"kind ec2-ephemeral needs {' and '.join(missing)}")
            if self.slots is not None:
                raise ValueError("slots is for kind pool; an ec2-ephemeral capability leases machines")
            return self
        # pool
        stray = [k for k in ("aws", "exec") if getattr(self, k) is not None]
        if stray:
            raise ValueError(f"kind pool has no machine, so no {' or '.join(stray)}")
        if self.params:
            raise ValueError(f"kind pool takes no params: the broker owns its one acquire param, "
                             f"{POOL_SLOT_PARAM!r}")
        if not self.slots:
            raise ValueError("kind pool needs slots: at least one slot name")
        if len(self.slots) > MAX_POOL_SLOTS:
            raise ValueError(f"kind pool takes at most {MAX_POOL_SLOTS} slots")
        bad = [s for s in self.slots if not isinstance(s, str) or not re.match(CAPABILITY_NAME, s)]
        if bad:
            raise ValueError(f"slot names match {CAPABILITY_NAME} (like capability names); got {bad!r}")
        dupes = sorted({s for s in self.slots if self.slots.count(s) > 1})
        if dupes:
            raise ValueError(f"slot names must be unique; repeated: {', '.join(dupes)}")
        if "max_concurrent" not in self.limits.model_fields_set:
            self.limits.max_concurrent = len(self.slots)
        elif self.limits.max_concurrent > len(self.slots):
            raise ValueError(f"limits.max_concurrent {self.limits.max_concurrent} is more than the pool's "
                             f"{len(self.slots)} slots; a pool's concurrency is its slot count, and the "
                             "manifest may only lower it")
        return self

    @property
    def is_pool(self) -> bool:
        return self.kind == "pool"

    @field_validator("params")
    @classmethod
    def _param_names(cls, v: dict[str, CapabilityParam]) -> dict[str, CapabilityParam]:
        for name in v:
            if not _PARAM_NAME.match(name):
                raise ValueError(f"param names are lowercase identifiers; got {name!r}")
        return v

    def resolve_params(self, given: dict[str, Any] | None) -> dict[str, Any]:
        """The params an acquire delivers: `given` checked against the declaration,
        with defaults filled in. Raises ValueError naming every problem. A pool
        accepts only an optional `slot`, one of its declared slots."""
        given = dict(given or {})
        if self.is_pool:
            unknown = sorted(set(given) - {POOL_SLOT_PARAM})
            if unknown:
                raise ValueError(f"a pool acquire takes only {POOL_SLOT_PARAM!r}; unknown param(s) "
                                 + ", ".join(repr(k) for k in unknown))
            if POOL_SLOT_PARAM not in given or given[POOL_SLOT_PARAM] is None:
                return {}
            slot = given[POOL_SLOT_PARAM]
            if not isinstance(slot, str) or slot not in (self.slots or []):
                raise ValueError(f"slot {slot!r} is not one of this pool's slots "
                                 f"({', '.join(self.slots or [])})")
            return {POOL_SLOT_PARAM: slot}
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
