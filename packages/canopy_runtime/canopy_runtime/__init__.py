"""Django-free schema for a capability manifest (`capability.yaml`).

An agent ships one `capability.yaml` per capability in its own repo;
`canopy-runtime publish-capabilities` validates them and sends them to canopy-web.
The schema is Django-free so an agent's deploy can validate a manifest without
installing canopy-web.
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
    load_capability_manifest,
)

__all__ = [
    "POOL_SLOT_PARAM",
    "CapabilityAws",
    "CapabilityExec",
    "CapabilityKind",
    "CapabilityLimits",
    "CapabilityManifest",
    "CapabilityParam",
    "load_capability_manifest",
]
