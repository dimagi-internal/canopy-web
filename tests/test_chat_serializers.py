"""Canonical DTO serializers — shape parity with the ace-web wire contract."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.canopy_sessions import serializers
from apps.canopy_sessions.models import Draft, Message, Session, SessionParticipant
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _ws_user():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=u)
    return ws, u


def test_message_dto_shape():
    ws, u = _ws_user()
    s = Session.objects.create(workspace=ws, created_by=u, title="t")
    m = Message.objects.create(session=s, turn_index=0, role="user",
                               content={"text": "hi"}, plaintext="hi")
    dto = serializers.message_dto(m)
    assert dto["id"] == str(m.pk)
    assert dto["turn_index"] == 0
    assert dto["role"] == "user"
    assert dto["plaintext"] == "hi"
    assert dto["status"] == "complete"
    assert dto["error_detail"] is None
    assert set(dto) == {"id", "turn_index", "role", "content", "plaintext",
                        "status", "error_detail", "started_at", "completed_at", "created_at"}


def test_draft_dto_shape_and_none():
    ws, u = _ws_user()
    assert serializers.draft_dto(None) is None
    s = Session.objects.create(workspace=ws, created_by=u, title="t")
    d = Draft.objects.create(session=s, slot="next", body="wip", version=3, last_editor=u)
    dto = serializers.draft_dto(d)
    assert dto["id"] == str(d.pk)
    assert dto["slot"] == "next"
    assert dto["status"] == "open"
    assert dto["body"] == "wip"
    assert dto["version"] == 3
    assert dto["last_editor"] == u.pk
    assert dto["last_edit_at"] is not None


def test_session_state_dto_keys():
    ws, u = _ws_user()
    s = Session.objects.create(workspace=ws, created_by=u, title="t")
    sp = SessionParticipant.objects.create(session=s, user=u, role="owner")
    state = serializers.session_state_dto(
        session=s, current_user_id=u.pk, participants=[sp],
        present_ids=[u.pk], draft=None, messages=[])
    # `menu` joined the snapshot deliberately (2026-07-31): the dialog an agent
    # is blocked on used to exist only as a view-only live frame, so it reached
    # a client only if it was already connected when the agent asked — and you
    # go and look precisely BECAUSE the session stopped. Always present, null
    # when nothing is pending, so a client needs no second code path.
    assert set(state) == {"messages", "active_draft", "participants",
                          "presence_user_ids", "current_user_id", "menu"}
    assert state["menu"] is None
    assert state["current_user_id"] == u.pk
    assert state["presence_user_ids"] == [u.pk]
    assert state["participants"][0]["email"] == u.email
    assert state["participants"][0]["role"] == "owner"


def test_turnout_surfaces_project_session_target_and_workspace():
    from django.contrib.auth.models import User
    from apps.canopy_sessions.models import Session
    from apps.harness import services
    from apps.harness.models import Turn
    from apps.harness.schemas import TurnOut
    from apps.workspaces.models import Workspace

    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    s = Session.objects.create(workspace=ws, created_by=user, project="canopy-web")
    turn, _ = services.enqueue_turn(
        session=s, origin=Turn.ORIGIN_API, idempotency_key="pk", prompt="hi"
    )
    out = TurnOut.model_validate(turn)
    assert out.agent_slug is None
    assert out.project == "canopy-web"
    assert out.workspace_slug == "canopy"
    assert out.target == "canopy-web"
