"""A tenant to register a connected site in, for tests that do not care which.

A site belongs to exactly one workspace (`AppCredential.workspace`), so every
site needs one — but most tests of delegated tokens, frames or audit are about
something else entirely, and would only be made noisier by building a tenant.
"""
from __future__ import annotations

from apps.workspaces.models import Workspace


def host_workspace(slug: str = "site-host") -> Workspace:
    from django.contrib.auth.models import User

    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None:
        creator, _ = User.objects.get_or_create(username=f"{slug}-creator",
                                                defaults={"email": f"{slug}@example.com"})
        ws = Workspace.objects.create(slug=slug, display_name=slug, created_by=creator)
    return ws
