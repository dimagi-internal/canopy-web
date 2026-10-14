"""Django-free schema for an agent's declarative runtime spec (`runtime.yaml`).

The agent ships this file in its own repo; the reconciler reads + validates it to
provision a box for the agent's turn. canopy-web only points a runner at the repo
(it never parses the spec), so this library carries the shape both sides agree on.
"""
from __future__ import annotations

from canopy_runtime.reconcile import (
    Environment,
    Gap,
    LocalEnvironment,
    ReconcileResult,
    reconcile,
)
from canopy_runtime.schema import (
    CapabilityAws,
    CapabilityExec,
    CapabilityLimits,
    CapabilityManifest,
    CapabilityParam,
    CapabilityRef,
    Engine,
    McpRef,
    PluginRef,
    PreflightCheck,
    RuntimeSpec,
    SecretRef,
    ToolRef,
    load_capability_manifest,
    load_runtime_yaml,
)
from canopy_runtime.stores import (
    CREDENTIAL_FIELD,
    EnvVarStore,
    OnePasswordStore,
    SecretNotFoundError,
    SecretStore,
    SecretStoreError,
)

__all__ = [
    # schema
    "CapabilityAws",
    "CapabilityExec",
    "CapabilityLimits",
    "CapabilityManifest",
    "CapabilityParam",
    "CapabilityRef",
    "Engine",
    "McpRef",
    "PluginRef",
    "PreflightCheck",
    "RuntimeSpec",
    "SecretRef",
    "ToolRef",
    "load_capability_manifest",
    "load_runtime_yaml",
    # stores
    "CREDENTIAL_FIELD",
    "EnvVarStore",
    "OnePasswordStore",
    "SecretNotFoundError",
    "SecretStore",
    "SecretStoreError",
    # reconcile
    "Environment",
    "Gap",
    "LocalEnvironment",
    "ReconcileResult",
    "reconcile",
]
