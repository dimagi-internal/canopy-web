"""Feedback rounds on a storyboard (spec C)."""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentProject, ProjectParticipant
from apps.feedback.models import Feedback, FeedbackRound
from apps.storyboards import rounds as board_rounds
from apps.storyboards.models import Act, Entry, Storyboard
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")
    ace_user = User.objects.create_user("ace", "ace@dimagi-ai.com", "pw")
    viewer = User.objects.create_user("lily", "lily@dimagi.com", "pw", first_name="Lily")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=ace_user, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=jj, user=ace_user)
    project = AgentProject.objects.create(agent=agent, ext_id="P9", name="Connect Supply demos")
    board = Storyboard.objects.create(slug="supply", title="Connect Supply", workspace=ws,
                                      capability=Storyboard.CAP_SUGGEST, agent_project=project)
    act = Act.objects.create(storyboard=board, title="RUTF", position=0)
    Entry.objects.create(act=act, narrative_slug="supply-rutf", position=0)
    token = board.ensure_share_token()
    clients = {}
    for name, u in (("owner", jj), ("ace", ace_user), ("viewer", viewer)):
        c = Client()
        c.force_login(u)
        clients[name] = c
    return {"ws": ws, "board": board, "token": token, "project": project, "agent": agent,
            "users": {"owner": jj, "ace": ace_user, "viewer": viewer}, **clients}


def _open(world):
    resp = world["owner"].post("/api/storyboards/supply/rounds")
    assert resp.status_code == 200, resp.content
    return resp.json()


def _note(world, client=None, **body):
    c = client or Client()
    payload = {"narrative_slug": "supply-rutf", "anchor_id": "cut:procurements", "body": "Too fast",
               **body}
    return c.post(f"/api/storyboards/supply/feedback?t={world['token']}", payload,
                  content_type="application/json")


def test_open_requires_a_linked_project(world):
    world["board"].agent_project = None
    world["board"].save()
    resp = world["owner"].post("/api/storyboards/supply/rounds")
    assert resp.status_code == 422
    assert "project" in resp.json()["detail"]


def test_only_editors_open(world):
    assert world["viewer"].post("/api/storyboards/supply/rounds").status_code == 403


def test_open_pins_versions_and_adopts_loose_notes(world, monkeypatch):
    monkeypatch.setattr(board_rounds, "pinned_versions", lambda b: {"supply-rutf": 3})
    _note(world, author_name="Early bird")
    out = _open(world)
    assert out["number"] == 1 and out["pinned_versions"] == {"supply-rutf": 3}
    assert Feedback.objects.get(author_name="Early bird").round.number == 1


def test_guest_note_in_round_needs_a_name(world):
    _open(world)
    assert _note(world, author_name="").status_code == 422
    assert _note(world, author_name="Sophie").status_code == 200
    assert Feedback.objects.get(author_name="Sophie").round.number == 1


def test_member_note_uses_account_name_and_joins_the_project(world, monkeypatch):
    from apps.contacts import people

    sentinel = object()
    monkeypatch.setattr(people, "by_email", lambda e: sentinel if e == "lily@dimagi.com" else None)
    linked = []
    monkeypatch.setattr("apps.agents.participants.ensure",
                        lambda project, person, *, source, role="": linked.append((project, person, source)))
    _open(world)
    assert _note(world, client=world["viewer"], author_name="Not me").status_code == 200
    assert Feedback.objects.get(body="Too fast").author_name == "Lily"
    assert linked == [(world["project"], sentinel, ProjectParticipant.REVIEW)]


def test_note_anchor_must_be_known(world):
    _open(world)
    assert _note(world, author_name="S", anchor_id="decision:nope").status_code == 404
    assert _note(world, author_name="S", anchor_id="weird:thing").status_code == 422
    assert _note(world, author_name="S", anchor_id="product:procurements").status_code == 200
    assert _note(world, author_name="S", anchor_id="overview").status_code == 200  # a bare scene id


def test_note_after_close_waits_for_next_round(world):
    _open(world)
    rnd = FeedbackRound.objects.get()
    rnd.state = FeedbackRound.CLOSED
    rnd.save()
    assert _note(world, author_name="Late").status_code == 200
    assert Feedback.objects.get(author_name="Late").round is None
