"""Pydantic schemas for the /api/system capability catalog (read-only).

The catalog is read from the bundled canopy plugin at ``CANOPY_PLUGIN_PATH``
by :mod:`apps.system.reader` — the plugin file structure is the source of truth.
"""
from __future__ import annotations

from apps.common.schemas import StrictModel


class CapabilityItemOut(StrictModel):
    """One capability — list-level shape (no body)."""

    name: str
    kind: str  # "skill" | "agent" | "command"
    family: str
    display_name: str
    description: str = ""


class CapabilityDetailOut(CapabilityItemOut):
    """Single capability — adds the full markdown body."""

    body: str = ""


class CapabilityCatalogOut(StrictModel):
    """Full catalog returned by GET /api/system/overview."""

    items: list[CapabilityItemOut]
    families: list[str]
    counts: dict[str, int]
    plugin_version: str | None = None
    warning: str | None = None


class PublicStatsOut(StrictModel):
    """Aggregates for the public explainer. Integers only — see apps/system/stats.py."""

    agents: int
    skills: int
    runners_online: int
    turns_executed: int
    demos_published: int
