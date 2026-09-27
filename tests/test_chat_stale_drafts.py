"""A peer's draft is shown only while it is live (final review I4).

A draft outlives the tab that typed it: an abandoned box, or a line sent over
HTTP (which never cleared the server copy), used to come back on every later
connect as "Alice is typing: …" — the live `presence.left` cleared it, and the
next snapshot brought it straight back."""
from __future__ import annotations

import datetime as dt
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

# Imported BEFORE any test patches `groups.publish`: these modules bind
# `publish` at import, and a first import under the patch would keep the mock
# for the rest of the run.
from apps.canopy_sessions import drafts, presence, queued_feed, status_feed  # noqa: F401
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import Draft, SessionParticipant
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_presence():
    cache.clear()
    yield
    cache.clear()


def _ctx():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    alice = User.objects.create_user("alice", "alice@dimagi.com", "pw", first_name="Alice")
    WorkspaceMembership.objects.create(user=alice, workspace=ws, role=WorkspaceMembership.EDITOR)
    session = chat.create_session(workspace=ws, created_by=owner)
    SessionParticipant.objects.create(session=session, user=alice, role=SessionParticipant.EDITOR)
    return owner, alice, session


def _bodies(ds):
    return [d.body for d in ds]


def test_an_absent_authors_draft_is_not_a_peer_draft():
    owner, alice, session = _ctx()
    drafts.update_draft(session, user=alice, expected_version=0, body="half a thought")
    assert _bodies(drafts.peer_drafts(session, owner)) == []
    presence.touch(session.id, alice.id)
    assert _bodies(drafts.peer_drafts(session, owner)) == ["half a thought"]


def test_a_present_authors_draft_untouched_for_ten_minutes_is_not_shown():
    owner, alice, session = _ctx()
    presence.touch(session.id, alice.id)
    drafts.update_draft(session, user=alice, expected_version=0, body="left open")
    Draft.objects.filter(author=alice).update(updated_at=timezone.now() - dt.timedelta(minutes=11))
    assert drafts.peer_drafts(session, owner) == []


def test_a_contact_sees_only_live_drafts_too():
    _owner, alice, session = _ctx()
    drafts.update_draft(session, user=alice, expected_version=0, body="wip")
    assert drafts.peer_drafts(session, None) == []
    presence.touch(session.id, alice.id)
    assert _bodies(drafts.peer_drafts(session, None)) == ["wip"]


def test_a_rest_send_clears_the_senders_draft_and_tells_the_room(django_capture_on_commit_callbacks):
    _owner, alice, session = _ctx()
    drafts.update_draft(session, user=alice, expected_version=0, body="ship it")
    client = Client()
    client.force_login(alice)
    with mock.patch("apps.realtime.groups.publish") as publish:
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(f"/api/canopy-sessions/{session.id}/send",
                               {"text": "ship it", "client_id": "c1"},
                               content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert Draft.objects.get(session=session, author=alice).body == ""
    frames = [c.args[1] for c in publish.call_args_list if c.args[1].get("type") == "draft.updated"]
    assert frames, "peers were never told the draft cleared"
    assert frames[-1]["author_id"] == alice.id
    assert frames[-1]["peer"]["body"] == "" and frames[-1]["draft"]["body"] == ""


def test_a_rest_send_leaves_a_draft_that_is_not_what_was_sent():
    """The HTTP path is a fallback for a dead socket; by the time it lands the
    person may already be typing the NEXT line in another tab."""
    _owner, alice, session = _ctx()
    drafts.update_draft(session, user=alice, expected_version=0, body="the next thing")
    client = Client()
    client.force_login(alice)
    resp = client.post(f"/api/canopy-sessions/{session.id}/send",
                       {"text": "ship it", "client_id": "c1"}, content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert Draft.objects.get(session=session, author=alice).body == "the next thing"
