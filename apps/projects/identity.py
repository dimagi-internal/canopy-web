"""The product side of the project-identity seam registered with
`apps.agents.delegations` in `ProjectsConfig.ready()` — see that module's
`register_project_identity_resolver` for why this is a registration and not an
import.
"""
from __future__ import annotations

from .models import Project


def resolve_default_identity_agent_slug(project_slug: str) -> str:
    """The agent slug a bare project turn should borrow its GitHub identity
    from, or "" when the project has none configured (or doesn't exist)."""
    return (
        Project.objects.filter(slug=project_slug)
        .exclude(default_identity_agent=None)
        .values_list("default_identity_agent__slug", flat=True)
        .first()
        or ""
    )
