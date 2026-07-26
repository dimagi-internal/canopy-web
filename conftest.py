"""Shared test fixtures — currently just the tenancy baseline.

`Agent.workspace` is NOT NULL as of `agents/0013`, so creating an Agent now
requires a Workspace to home it in, and a Workspace requires a `created_by`
User. Every real deployment satisfies both by construction (you cannot reach
`POST /api/agents/` without being logged in, and the login is what makes the
default workspace exist), but an in-memory test DB starts with neither — which
is why `wsvc.ensure_default_workspace()` returns `None` in a bare test and why
a fixture that passed its result straight to `Agent.objects.create` used to
quietly produce the very unhomed agent the constraint exists to forbid.

Depend on `default_workspace` from any fixture that needs a tenant. Fixtures
that already call `wsvc.ensure_default_workspace()` only need to take it as a
parameter — with a user present, that call starts returning a real workspace.
Plain (non-fixture) helper functions call `apps.workspaces.testing.a_workspace()`
directly instead; both go through the same code.

Deliberately NOT autouse: tests that assert on the empty-database case (the
workspace-backfill migration tests, `ensure_default_workspace()` returning
None) must keep seeing an empty database.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def default_workspace(db):
    """The default (`dimagi`) workspace, created the way the app creates it.

    Returned as the Workspace instance; `wsvc.ensure_default_workspace()` finds
    the same row, so a fixture can keep calling that and simply depend on this.
    """
    from apps.workspaces.testing import a_workspace

    return a_workspace()
