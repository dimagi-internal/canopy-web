"""`audit_auto_join` management command — makes the "dimagi is the only
auto-join workspace" invariant checkable (and repairable) in production
instead of folklore. Report mode never mutates; `--fix` clears
`auto_join_domains` on every workspace except `dimagi`, leaving it alone."""
from __future__ import annotations

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.services import DEFAULT_WORKSPACE_SLUG

pytestmark = pytest.mark.django_db
User = get_user_model()


def _run(*args):
    out = StringIO()
    call_command("audit_auto_join", *args, stdout=out)
    return out.getvalue()


def _user(email):
    return User.objects.create(username=email, email=email)


def test_reports_nothing_when_only_dimagi_has_auto_join():
    owner = _user("owner@dimagi.com")
    Workspace.objects.create(
        slug=DEFAULT_WORKSPACE_SLUG, display_name="Dimagi", created_by=owner,
        auto_join_domains=["dimagi.com"],
    )
    Workspace.objects.create(
        slug="acme", display_name="Acme", created_by=owner, auto_join_domains=[],
    )
    output = _run()
    assert "acme" not in output
    # dimagi is reported (it's expected to have auto-join), but flagged as OK.
    assert DEFAULT_WORKSPACE_SLUG in output


def test_reports_a_non_dimagi_workspace_with_auto_join_domains():
    owner = _user("owner@dimagi.com")
    ws = Workspace.objects.create(
        slug="acme", display_name="Acme", created_by=owner,
        auto_join_domains=["acme.com"],
    )
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    other = _user("m@acme.com")
    WorkspaceMembership.objects.create(workspace=ws, user=other, role=WorkspaceMembership.EDITOR)

    output = _run()
    assert "acme" in output
    assert "acme.com" in output
    assert "2" in output  # member count


def test_fix_clears_non_dimagi_workspaces_but_leaves_dimagi_alone():
    owner = _user("owner@dimagi.com")
    dimagi = Workspace.objects.create(
        slug=DEFAULT_WORKSPACE_SLUG, display_name="Dimagi", created_by=owner,
        auto_join_domains=["dimagi.com"],
    )
    acme = Workspace.objects.create(
        slug="acme", display_name="Acme", created_by=owner, auto_join_domains=["acme.com"],
    )

    output = _run("--fix")

    dimagi.refresh_from_db()
    acme.refresh_from_db()
    assert dimagi.auto_join_domains == ["dimagi.com"]
    assert acme.auto_join_domains == []
    assert "acme" in output
    assert "cleared" in output.lower()


def test_fix_is_idempotent_second_run_reports_nothing_to_fix():
    owner = _user("owner@dimagi.com")
    Workspace.objects.create(
        slug="acme", display_name="Acme", created_by=owner, auto_join_domains=["acme.com"],
    )
    _run("--fix")
    second = _run("--fix")
    # acme's domains are already empty by the second run — nothing left to
    # report or clear.
    assert "acme" not in second
