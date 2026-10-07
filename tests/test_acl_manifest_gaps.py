"""Gaps the route-gate manifest surfaced the day it was written (2026-10-02).

Classifying every route by the gate it actually enforces
(`apps/api/route_gates.py`) is what found these; each was a write a viewer
could make, or a NULL-means-allow leg.
"""
from __future__ import annotations

import uuid

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services as hsvc
from apps.harness.models import Runner, Turn
from apps.workspaces.models import WorkspaceMembership as M
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db

WS = "gap-ws"


@pytest.fixture
def gap():
    ws = a_workspace(WS)
    return {
        "ws": ws,
        "agent": Agent.objects.create(slug="gapbot", name="Gap", workspace=ws),
        "owner": a_member(ws, email="gap-owner@dimagi.com", role=M.OWNER),
        "editor": a_member(ws, email="gap-editor@dimagi.com", role=M.EDITOR),
        "viewer": a_member(ws, email="gap-viewer@dimagi.com", role=M.VIEWER),
    }


def _c(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _post(user, path, body):
    return _c(user).post(path, body, content_type="application/json")


def test_a_viewer_cannot_run_a_project_turn(gap):
    r = _post(gap["viewer"], f"/api/w/{WS}/harness/turns/",
              {"project": "canopy-web", "prompt": "rm -rf", "origin": "api", "idempotency_key": "p1"})
    assert r.status_code == 403, r.content


def test_a_viewer_cannot_write_an_agents_runs(gap):
    r = _post(gap["viewer"], "/api/agents/gapbot/runs/", {"label": "x"})
    assert r.status_code == 403, r.content


def test_a_viewer_cannot_mark_a_task_action_applied(gap):
    r = _post(gap["viewer"], "/api/agents/gapbot/actions/1/applied", {"result_note": ""})
    assert r.status_code == 403, r.content


def test_a_viewers_box_serves_no_workspace(gap):
    box = Runner.objects.create(name="viewer-box", kind=Runner.EMDASH, owner=gap["viewer"],
                                workspace=gap["ws"], status=Runner.ONLINE,
                                last_heartbeat_at=timezone.now(), capabilities={"sessions": True})
    assert hsvc.runner_tenant_slugs(box) == set()
    editor_box = Runner.objects.create(name="editor-box", kind=Runner.EMDASH,
                                       owner=gap["editor"], workspace=gap["ws"])
    assert hsvc.runner_tenant_slugs(editor_box) == {WS}


def test_nobody_acts_on_a_runner_nobody_paired(gap):
    orphan = Runner.objects.create(name="orphan", kind=Runner.EMDASH, workspace=gap["ws"])
    assert not hsvc.can_administer_runner(gap["owner"], orphan)
    r = _post(gap["owner"], f"/api/harness/runners/{orphan.id}/heartbeat", {})
    assert r.status_code == 404


def test_only_your_own_send_or_a_writer_cancels_a_chat_turn(gap):
    from apps.canopy_sessions.models import Session, SessionParticipant

    session = Session.objects.create(workspace=gap["ws"], agent=gap["agent"],
                                     created_by=gap["owner"], title="t")
    SessionParticipant.objects.create(session=session, user=gap["viewer"],
                                      role=SessionParticipant.VIEWER)
    turn = Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               prompt="mine", idempotency_key=uuid.uuid4().hex,
                               initiator_user=gap["owner"])
    assert _post(gap["viewer"], f"/api/harness/turns/{turn.id}/cancel", {}).status_code == 403
    assert _post(gap["owner"], f"/api/harness/turns/{turn.id}/cancel", {}).status_code == 200


def test_a_viewer_cannot_rewrite_a_mailbox_watch(gap):
    from apps.inbound.models import InboundMailbox

    InboundMailbox.objects.create(address="gapbot@dimagi-ai.com", agent=gap["agent"])
    body = {"address": "gapbot@dimagi-ai.com", "expires_at": None, "error": ""}
    assert _post(gap["viewer"], "/api/inbound/watch/", body).status_code == 403
    assert _post(gap["editor"], "/api/inbound/watch/", body).status_code == 200


def test_a_viewer_cannot_file_feedback(gap):
    body = {"items": [{"target_ref": "n", "body": "hi"}]}
    assert _post(gap["viewer"], f"/api/w/{WS}/feedback/", body).status_code == 403
    assert _post(gap["editor"], f"/api/w/{WS}/feedback/", body).status_code == 200
