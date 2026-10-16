"""Django-free schema for an agent's declarative runtime spec (`runtime.yaml`).

The agent ships this file in its own repo; canopy-web validates the capability
manifests it declares (`canopy-runtime publish-capabilities`) and reads its declared
secrets for the credentials screen. The schema is Django-free so an agent's deploy can
validate it without installing canopy-web.
"""
from __future__ import annotations

from canopy_runtime.schema import (
    POOL_SLOT_PARAM,
    CapabilityAws,
    CapabilityExec,
    CapabilityKind,
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

__all__ = [
    # schema
    "POOL_SLOT_PARAM",
    "CapabilityAws",
    "CapabilityExec",
    "CapabilityKind",
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
]
