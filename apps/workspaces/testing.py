"""Test-support helpers for tenancy. Imported by tests only; nothing in the
running app imports this module.

`Agent.workspace` is NOT NULL as of `agents/0013`, so a test that creates an
Agent must first have a Workspace to home it in — and a `Workspace` needs a
`created_by` User, which an in-memory test DB does not start with. Before the
constraint, dozens of test helpers wrote `Agent.objects.create(slug=..., name=...)`
with no tenant at all, and a handful more passed the `None` that
`ensure_default_workspace()` returns on an empty DB. Both minted exactly the
workspace-less agent that six separate tenancy predicates then had to
special-case (see the constraint's migration docstring).

These live here, in the app that owns the tenancy concept, rather than being
copy-pasted per test module — the copies are how the fixtures drifted in the
first place. They are get-or-create shaped, so several fixtures in one test may
ask for the same baseline without fighting over who made it.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model

from . import services
from .models import Workspace, WorkspaceMembership


def a_user(email: str = "fixture-owner@dimagi.com"):
    """Someone for a Workspace to belong to. Idempotent by email."""
    User = get_user_model()
    existing = User.objects.filter(email=email).first()
    if existing is not None:
        return existing
    return User.objects.create_user(username=email, email=email, password="x")


def a_workspace(slug: str | None = None, **defaults) -> Workspace:
    """A real tenant to home test agents in. Idempotent by slug.

    With no slug this returns the DEFAULT workspace via the app's own
    `ensure_default_workspace()` — including its `auto_join_domains`, which the
    auto-join paths under test depend on — after making sure there is a user for
    it to be owned by. A named slug gets a plain workspace with no auto-join,
    which is what a test wants when it is checking cross-tenant isolation.
    """
    owner = a_user()
    if slug is None or slug == services.DEFAULT_WORKSPACE_SLUG:
        return services.ensure_default_workspace()
    existing = Workspace.objects.filter(slug=slug).first()
    if existing is not None:
        return existing
    defaults.setdefault("display_name", slug.title())
    defaults.setdefault("created_by", owner)
    return Workspace.objects.create(slug=slug, **defaults)


def a_member(workspace: Workspace | None = None, *, email: str = "fixture-owner@dimagi.com",
             role: str = WorkspaceMembership.OWNER):
    """A user who is a MEMBER of `workspace` (default: the default workspace).

    This is what a runner's `paired_by` needs to be: claim routing and schedule
    sync both derive a runner's tenant from the workspaces of the human who
    paired it (`services.runner_tenant_slugs`), so a runner paired by a
    non-member — or by nobody — can claim nothing. Tests used to sidestep that
    by leaving `paired_by` NULL and the agent unhomed, which only worked because
    the tenancy predicate had a NULL-means-allow leg on both sides.
    """
    ws = workspace if workspace is not None else a_workspace()
    user = a_user(email)
    services.ensure_member(ws, user, role)
    return user
