"""Tiny builders for runs-aggregation tests."""
from __future__ import annotations

from django.contrib.auth import get_user_model

from apps.reviews.models import ReviewRequest
from apps.walkthroughs.models import Walkthrough
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()

DEFAULT_TEST_WS = "test-ws"


def make_user(email="owner@dimagi.com"):
    return User.objects.get_or_create(username=email, defaults={"email": email})[0]


def make_workspace(slug, *, creator=None, self_join_domains=None):
    """A tenant for scoping tests. ``self_join_domains`` defaults to empty so a
    workspace only gains the members a test adds explicitly."""
    creator = creator or make_user(f"creator-{slug}@dimagi.com")
    ws, _ = Workspace.objects.get_or_create(
        slug=slug,
        defaults={
            "display_name": slug,
            "created_by": creator,
            "self_join_domains": self_join_domains or [],
        },
    )
    return ws


def add_member(workspace, user, role=WorkspaceMembership.EDITOR):
    WorkspaceMembership.objects.get_or_create(
        workspace=workspace, user=user, defaults={"role": role}
    )


def _resolve_workspace(owner, workspace):
    """Every walkthrough/review is workspace-owned in production (the API always
    assigns one). Mirror that in tests: default to a shared ``test-ws`` and make
    the content owner a member so a logged-in owner can see their own rows once
    scoping is on. An explicit ``workspace`` (scoping tests) is used as-is, with
    the owner enrolled on it."""
    ws = workspace or make_workspace(DEFAULT_TEST_WS)
    add_member(ws, owner)
    return ws


def make_walkthrough(owner, *, kind, run_id=None, narrative_slug=None, role=None, **kw):
    workspace = _resolve_workspace(owner, kw.pop("workspace", None))
    defaults = dict(
        title=kw.pop("title", f"{run_id or 'oneoff'}-{kind}"),
        kind=kind,
        owner=owner,
        workspace=workspace,
        run_id=run_id,
        narrative_slug=narrative_slug,
        role=role,
        project_slug=kw.pop("project_slug", None),
        links=kw.pop("links", []),
        content_type="video/mp4" if kind == "video" else "text/html",
        size_bytes=kw.pop("size_bytes", 123),
        duration_sec=kw.pop("duration_sec", None),
        drive_file_id="f1",
        drive_folder_id="d1",
        visibility=kw.pop("visibility", "private"),
    )
    defaults.update(kw)
    return Walkthrough.objects.create(**defaults)


def make_review(owner, *, run_id, gate="narrative-agreement", request_json=None, **kw):
    rj = request_json if request_json is not None else {"run_id": run_id, "gate": gate}
    workspace = _resolve_workspace(owner, kw.pop("workspace", None))
    return ReviewRequest.objects.create(
        owner=owner,
        workspace=workspace,
        run_id=run_id,
        gate=gate,
        request_json=rj,
        **kw,
    )
