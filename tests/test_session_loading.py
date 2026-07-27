import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions import services
from apps.canopy_sessions.models import Message, Session
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _session_with(n: int) -> Session:
    # Workspace.created_by is a required FK (NOT NULL); the brief's helper is
    # otherwise verbatim.
    owner = get_user_model().objects.create_user("owner", "owner@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    s = Session.objects.create(workspace=ws, title="t")
    for i in range(n):
        Message.objects.create(
            session=s, turn_index=i, role=Message.USER, plaintext=f"m{i}",
        )
    return s


def test_tail_default_holds_conversational_density():
    """Sized against the row MIX, not a round number: ~72% of a live session's
    rows are tool calls, so this has to be ~3x a plain-prose tail to open on a
    comparable amount of actual conversation."""
    assert services.SESSION_TAIL_DEFAULT == 60


def test_tail_returns_last_n_chronological_with_cursor():
    s = _session_with(100)
    msgs, has_more, oldest = services.tail_messages(s)
    assert [m.turn_index for m in msgs] == list(range(40, 100))  # last 60, ascending
    assert has_more is True
    assert oldest == 40


def test_tail_short_session_has_no_more():
    s = _session_with(3)
    msgs, has_more, oldest = services.tail_messages(s)
    assert [m.turn_index for m in msgs] == [0, 1, 2]
    assert has_more is False
    assert oldest == 0


def test_tail_empty_session():
    s = _session_with(0)
    msgs, has_more, oldest = services.tail_messages(s)
    assert msgs == []
    assert has_more is False
    assert oldest is None


def test_messages_before_pages_backward():
    s = _session_with(50)
    # scroll-back from the tail's oldest (30), page size 10
    page, has_more = services.messages_before(s, before=30, limit=10)
    assert [m.turn_index for m in page] == list(range(20, 30))  # chronological window
    assert has_more is True
    # walk to the beginning
    page, has_more = services.messages_before(s, before=10, limit=10)
    assert [m.turn_index for m in page] == list(range(0, 10))
    assert has_more is False  # nothing older than index 0


def test_all_messages_returns_everything():
    s = _session_with(50)
    msgs, has_more, oldest = services.all_messages(s)
    assert len(msgs) == 50
    assert has_more is False
    assert oldest == 0


def _api_ctx(n: int):
    # Workspace.created_by is a required FK, and the authed handler gates on
    # WorkspaceMembership (see apps/canopy_sessions/api.py::_visible_slugs) —
    # matching the pattern already used in tests/test_chat_api.py.
    user = get_user_model().objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="apictx", display_name="ApiCtx", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    s = Session.objects.create(workspace=ws, created_by=user, title="t")
    for i in range(n):
        Message.objects.create(session=s, turn_index=i, role=Message.USER, plaintext=f"m{i}")
    c = Client()
    c.force_login(user)
    return c, s


def test_get_session_returns_tail_not_full():
    c, s = _api_ctx(100)
    body = c.get(f"/api/canopy-sessions/{s.id}").json()
    assert len(body["messages"]) == 60
    assert [m["turn_index"] for m in body["messages"]] == list(range(40, 100))
    assert body["has_more_before"] is True
    assert body["oldest_loaded_turn_index"] == 40


def test_get_session_full_returns_everything():
    c, s = _api_ctx(50)
    body = c.get(f"/api/canopy-sessions/{s.id}?full=true").json()
    assert len(body["messages"]) == 50
    assert body["has_more_before"] is False
    assert body["oldest_loaded_turn_index"] == 0


def test_get_empty_session_cursor_is_null():
    c, s = _api_ctx(0)
    body = c.get(f"/api/canopy-sessions/{s.id}").json()
    assert body["messages"] == []
    assert body["has_more_before"] is False
    assert body["oldest_loaded_turn_index"] is None


def test_scrollback_pages_backward_over_rest():
    c, s = _api_ctx(50)
    # First window older than the tail's oldest (30), page of 10
    body = c.get(f"/api/canopy-sessions/{s.id}/messages?before=30&limit=10").json()
    assert [m["turn_index"] for m in body["messages"]] == list(range(20, 30))
    assert body["has_more_before"] is True
    # Final window reaches the start
    body = c.get(f"/api/canopy-sessions/{s.id}/messages?before=10&limit=10").json()
    assert [m["turn_index"] for m in body["messages"]] == list(range(0, 10))
    assert body["has_more_before"] is False


def test_scrollback_before_zero_is_empty():
    c, s = _api_ctx(50)
    body = c.get(f"/api/canopy-sessions/{s.id}/messages?before=0").json()
    assert body["messages"] == []
    assert body["has_more_before"] is False


def test_scrollback_limit_is_clamped():
    c, s = _api_ctx(50)
    # limit=-1 used to hit `queryset[:limit]` -> ValueError -> 500. Now clamped to 1.
    body = c.get(f"/api/canopy-sessions/{s.id}/messages?before=30&limit=-1").json()
    assert len(body["messages"]) == 1
    assert [m["turn_index"] for m in body["messages"]] == [29]
    # limit=0 clamps up to the floor of 1, same as above.
    body = c.get(f"/api/canopy-sessions/{s.id}/messages?before=30&limit=0").json()
    assert len(body["messages"]) == 1
    # A huge limit is capped, not passed through uncapped to the ORM.
    resp = c.get(f"/api/canopy-sessions/{s.id}/messages?before=30&limit=100000")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["messages"]) <= 500
    assert [m["turn_index"] for m in body["messages"]] == list(range(0, 30))


def test_scrollback_tenant_gated():
    c, s = _api_ctx(5)
    other = User.objects.create_user("no", "no@dimagi.com", "pw")
    ws2 = Workspace.objects.create(slug="other", display_name="Other", created_by=other)
    WorkspaceMembership.objects.create(user=other, workspace=ws2, role=WorkspaceMembership.OWNER)
    c2 = Client(); c2.force_login(other)
    assert c2.get(f"/api/canopy-sessions/{s.id}/messages?before=5").status_code == 404
