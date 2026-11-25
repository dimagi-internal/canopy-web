"""Two small cross-workspace leaks: a narrative's version number and an MCP
call's workspace attribution must each answer about the caller's own workspace."""
import pytest
from django.contrib.auth import get_user_model

from apps.mcp.audit import _attributed_workspace
from apps.reviews.models import ReviewRequest
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ws(slug, user):
    ws = Workspace.objects.create(slug=slug, display_name=slug, created_by=user)
    wsvc.ensure_member(ws, user, WorkspaceMembership.OWNER)
    return ws


def test_narrative_versions_are_numbered_per_workspace():
    a = get_user_model().objects.create_user("a@dimagi.com", "a@dimagi.com")
    b = get_user_model().objects.create_user("b@dimagi.com", "b@dimagi.com")
    ws_a, ws_b = _ws("ws-a", a), _ws("ws-b", b)
    for v in (1, 2, 3):
        ReviewRequest.objects.create(run_id=f"r{v}", narrative_slug="demo", version=v,
                                     gate="concept_change", workspace=ws_a, request_json={})
    assert ReviewRequest.next_version("demo", workspace=ws_b.slug) == 1
    assert ReviewRequest.next_version("demo", workspace=ws_a.slug) == 4


def test_an_mcp_call_is_filed_only_under_a_workspace_the_caller_is_in():
    a = get_user_model().objects.create_user("a@dimagi.com", "a@dimagi.com")
    b = get_user_model().objects.create_user("b@dimagi.com", "b@dimagi.com")
    _ws("ws-a", a)
    _ws("ws-b", b)
    assert _attributed_workspace(a.pk, "ws-a") == "ws-a"
    assert _attributed_workspace(a.pk, "ws-b") == ""
