"""Leaving a workspace, and the people the workspace tree adds to it.

The 2026-10-02 ACL audit, second pass. Two shapes of the same mistake:

- **A grant outliving the membership it hung off.** Removing someone deleted
  their membership row and nothing else. An agent's owner kept `is_admin` (the
  owner leg answered before membership was asked), kept an unconfined profile
  when they messaged the agent, kept lending it their GitHub token and kept
  being pushed about it. Their AgentAdmin / RunnerAdmin / SessionParticipant
  rows and their box on the tenant's agents stayed behind dormant — and `dimagi`
  is self-join, so one click on Join woke every one of them up.
- **A listing that read the workspace's own rows** and so never saw the owners
  of a parent workspace, who own it too: the members page, an agent's roster,
  push recipients, the last-owner guard.
"""
from __future__ import annotations

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin, AgentDelegation
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import SessionParticipant
from apps.harness.models import Runner, RunnerAdmin, RunnerAssignment
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceInvite, WorkspaceMembership
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db

M = WorkspaceMembership
WS = "life-ws"


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _runner(user, name, ws=WS) -> Runner:
    return Runner.objects.create(
        name=name, kind=Runner.EMDASH, paired_by=user, status=Runner.ONLINE,
        workspace_id=ws, last_heartbeat_at=timezone.now(), capabilities={},
    )


@pytest.fixture
def life():
    ws = a_workspace(WS, self_join_domains=["dimagi.com"])
    owner = a_member(ws, email="life-owner@dimagi.com", role=M.OWNER)
    leaver = a_member(ws, email="life-leaver@dimagi.com", role=M.EDITOR)
    agent = Agent.objects.create(slug="lifebot", name="Life", workspace=ws, owner=leaver)
    return {"ws": ws, "owner": owner, "leaver": leaver, "agent": agent}


def _remove(life, who="leaver"):
    wsvc.remove_member(workspace=life["ws"], user_id=life[who].pk, by=life["owner"])


# --- 1. an agent's owner is an admin only while still a member ---------------

def test_an_owner_removed_from_the_workspace_is_no_longer_the_agents_admin(life):
    agent, leaver = life["agent"], life["leaver"]
    assert agent.is_admin(leaver)
    # Remove the row ALONE (no sweep), as the database looked before this fix:
    # the owner field still names them, and that must not be enough.
    WorkspaceMembership.objects.filter(workspace=life["ws"], user=leaver).delete()
    agent.refresh_from_db()
    assert agent.owner_id == leaver.pk
    assert not agent.is_admin(leaver)


def test_an_ex_member_owner_cannot_reach_the_agents_credentials(life):
    WorkspaceMembership.objects.filter(workspace=life["ws"], user=life["leaver"]).delete()
    res = _client(life["leaver"]).get("/api/agents/lifebot/credentials/status")
    assert res.status_code == 404


def test_an_ex_member_owner_messaging_the_agent_is_a_caller(life):
    from apps.harness import caller_context as cc

    agent, leaver = life["agent"], life["leaver"]
    assert cc.relationship_for_user(leaver, agent) == cc.OWNER
    WorkspaceMembership.objects.filter(workspace=life["ws"], user=leaver).delete()
    assert cc.relationship_for_user(leaver, agent) == cc.CALLER


def test_an_ex_member_owners_github_token_is_not_lent(life):
    from apps.agents import delegations

    agent, leaver = life["agent"], life["leaver"]
    AgentDelegation.objects.create(user=leaver, agent=agent, service=AgentDelegation.GITHUB,
                                   secret_enc="x")
    assert delegations.delegation_for(agent) is not None
    WorkspaceMembership.objects.filter(workspace=life["ws"], user=leaver).delete()
    assert delegations.delegation_for(agent) is None


def test_an_ex_member_owner_is_not_pushed_the_workspace_owners_are(life):
    from apps.push import services as push

    agent = life["agent"]
    assert push.agent_audience(agent) == [life["leaver"]]
    WorkspaceMembership.objects.filter(workspace=life["ws"], user=life["leaver"]).delete()
    assert push.agent_audience(agent) == [life["owner"]]


# --- 2. removal takes what the membership carried ----------------------------

def test_removal_sweeps_every_grant_the_membership_carried(life):
    ws, leaver, agent = life["ws"], life["leaver"], life["agent"]
    other = Agent.objects.create(slug="lifebot2", name="Life 2", workspace=ws)
    AgentAdmin.objects.create(agent=other, user=leaver)
    AgentDelegation.objects.create(user=leaver, agent=agent, service=AgentDelegation.GITHUB,
                                   secret_enc="x")
    box = _runner(leaver, "leaver-box")
    RunnerAdmin.objects.create(runner=_runner(life["owner"], "owner-box"), user=leaver)
    RunnerAssignment.objects.create(agent=other, runner=box, rank=0)
    session = chat.create_session(workspace=ws, created_by=life["owner"], agent=other)
    SessionParticipant.objects.create(session=session, user=leaver,
                                      role=SessionParticipant.EDITOR)

    _remove(life)

    assert not AgentAdmin.objects.filter(user=leaver).exists()
    assert not AgentDelegation.objects.filter(user=leaver).exists()
    assert not RunnerAdmin.objects.filter(user=leaver).exists()
    assert not RunnerAssignment.objects.filter(runner=box).exists()
    assert not SessionParticipant.objects.filter(user=leaver).exists()
    agent.refresh_from_db()
    assert agent.owner_id is None


def test_rejoining_does_not_revive_anything(life):
    """The reason for the sweep: `dimagi`-style self-join lets a removed person
    walk straight back in as an editor."""
    ws, leaver = life["ws"], life["leaver"]
    other = Agent.objects.create(slug="lifebot2", name="Life 2", workspace=ws)
    AgentAdmin.objects.create(agent=other, user=leaver)
    _remove(life)

    res = _client(leaver).post(f"/api/workspaces/{WS}/join")
    assert res.status_code == 200, res.content
    assert wsvc.member_role(leaver, WS) == M.EDITOR
    life["agent"].refresh_from_db()
    assert not life["agent"].is_admin(leaver)
    assert not other.is_admin(leaver)


def test_removing_an_agents_owner_is_recorded(life):
    from apps.events.models import Event

    _remove(life)
    ev = Event.objects.get(workspace=life["ws"], kind="agent.owner_cleared")
    assert "lifebot" in ev.summary and "life-leaver@dimagi.com" in ev.summary


def test_removal_only_sweeps_the_workspace_left(life):
    """A grant in a workspace the person is still in stays."""
    elsewhere = a_workspace("life-elsewhere")
    WorkspaceMembership.objects.create(workspace=elsewhere, user=life["leaver"], role=M.EDITOR)
    kept = Agent.objects.create(slug="keptbot", name="Kept", workspace=elsewhere)
    AgentAdmin.objects.create(agent=kept, user=life["leaver"])
    _remove(life)
    assert AgentAdmin.objects.filter(agent=kept, user=life["leaver"]).exists()


def test_removing_an_org_owner_sweeps_the_divisions_they_only_inherited(life):
    org = a_workspace("life-org")
    life["ws"].parent = org
    life["ws"].save()
    boss = a_member(org, email="life-boss@dimagi.com", role=M.OWNER)
    a_member(org, email="life-boss2@dimagi.com", role=M.OWNER)
    division_agent = Agent.objects.create(slug="divbot", name="Div", workspace=life["ws"],
                                          owner=boss)
    wsvc.remove_member(workspace=org, user_id=boss.pk)
    division_agent.refresh_from_db()
    assert division_agent.owner_id is None


def test_demoting_an_org_owner_sweeps_the_divisions_they_lose(life):
    org = a_workspace("life-org")
    life["ws"].parent = org
    life["ws"].save()
    boss = a_member(org, email="life-boss@dimagi.com", role=M.OWNER)
    a_member(org, email="life-boss2@dimagi.com", role=M.OWNER)
    AgentAdmin.objects.create(agent=life["agent"], user=boss)
    wsvc.set_member_role(workspace=org, user_id=boss.pk, role=M.EDITOR)
    assert not AgentAdmin.objects.filter(user=boss).exists()


def test_removal_closes_the_persons_chat_sockets(life, monkeypatch, django_capture_on_commit_callbacks):
    from apps.realtime import groups

    published = []
    monkeypatch.setattr(groups, "publish", lambda g, m: published.append((g, m)))
    with django_capture_on_commit_callbacks(execute=True):
        _remove(life)
    assert (groups.chat_user_group(life["leaver"].pk),
            {"type": "access.recheck", "workspaces": [WS]}) in published


@pytest.mark.django_db(transaction=True)
async def test_an_open_chat_socket_closes_when_its_person_is_removed():
    """End to end through the real consumer and channel layer: the tab a
    removed person still has open stops receiving the conversation now."""
    from channels.db import database_sync_to_async
    from channels.testing import WebsocketCommunicator

    from apps.canopy_sessions.consumers import SessionConsumer

    def _seed():
        ws = a_workspace("life-sock")
        owner = a_member(ws, email="sock-owner@dimagi.com", role=M.OWNER)
        mate = a_member(ws, email="sock-mate@dimagi.com", role=M.EDITOR)
        agent = Agent.objects.create(slug="sockbot", name="Sock", workspace=ws, owner=owner)
        session = chat.create_session(workspace=ws, created_by=owner, agent=agent)
        SessionParticipant.objects.create(session=session, user=mate,
                                          role=SessionParticipant.EDITOR)
        return ws, owner, mate, session

    ws, owner, mate, session = await database_sync_to_async(_seed)()
    comm = WebsocketCommunicator(SessionConsumer.as_asgi(), f"/ws/canopy-sessions/{session.id}/")
    comm.scope["user"] = mate
    comm.scope["url_route"] = {"kwargs": {"session_id": str(session.id)}}
    connected, _ = await comm.connect()
    assert connected

    await database_sync_to_async(wsvc.remove_member)(workspace=ws, user_id=mate.pk, by=owner)

    closed = None
    for _ in range(20):
        out = await comm.receive_output(timeout=2)
        if out["type"] == "websocket.close":
            closed = out
            break
    assert closed is not None and closed.get("code") == 4003


# --- 4. invite tokens are an owner's ------------------------------------------

def test_a_non_owner_sees_invites_without_their_tokens(life):
    viewer = a_member(life["ws"], email="life-viewer@dimagi.com", role=M.VIEWER)
    wsvc.create_invite(workspace=life["ws"], email="new@dimagi.com", role=M.OWNER,
                       invited_by=life["owner"])
    token = WorkspaceInvite.objects.get(email="new@dimagi.com").token

    rows = _client(viewer).get(f"/api/workspaces/{WS}/invites/").json()
    assert [r["email"] for r in rows] == ["new@dimagi.com"]
    assert rows[0]["token"] == ""
    assert token not in str(rows)

    rows = _client(life["owner"]).get(f"/api/workspaces/{WS}/invites/").json()
    assert rows[0]["token"] == token


# --- 5. owners of a parent workspace are listed where they own ---------------

@pytest.fixture
def tree(life):
    org = a_workspace("life-org")
    life["ws"].parent = org
    life["ws"].save()
    boss = a_member(org, email="life-boss@dimagi.com", role=M.OWNER)
    return {**life, "org": org, "boss": boss}


def test_the_members_list_shows_inherited_owners(tree):
    rows = _client(tree["owner"]).get(f"/api/workspaces/{WS}/members/").json()
    by_email = {r["email"]: r for r in rows}
    assert by_email["life-boss@dimagi.com"]["role"] == M.OWNER
    assert by_email["life-boss@dimagi.com"]["inherited"] is True
    assert by_email["life-owner@dimagi.com"]["inherited"] is False


def test_an_inherited_owner_outranks_their_weaker_direct_row(tree):
    WorkspaceMembership.objects.create(workspace=tree["ws"], user=tree["boss"], role=M.VIEWER)
    rows = {m.user_id: m for m in wsvc.effective_memberships(tree["ws"])}
    assert rows[tree["boss"].pk].role == M.OWNER and rows[tree["boss"].pk].inherited
    # Never the stored row mutated: the database still says viewer.
    assert WorkspaceMembership.objects.get(workspace=tree["ws"], user=tree["boss"]).role == M.VIEWER


def test_the_agent_roster_lists_inherited_owners_as_admins(tree):
    from apps.agents import access

    rows = {r["email"]: r for r in access.roster(tree["agent"])}
    assert rows["life-boss@dimagi.com"]["agent_role"] == access.ADMIN
    assert rows["life-boss@dimagi.com"]["basis"] == "Owns a parent workspace"


def test_push_falls_back_to_inherited_owners(tree):
    from apps.push import services as push

    unowned = Agent.objects.create(slug="lonelybot", name="Lonely", workspace=tree["ws"])
    assert {u.email for u in push.agent_audience(unowned)} == {
        "life-owner@dimagi.com", "life-boss@dimagi.com"}


def test_supervisor_fan_out_reaches_inherited_owners(tree):
    assert tree["boss"].pk in wsvc.workspace_member_ids(tree["ws"])


def test_a_direct_owner_may_step_down_while_a_parent_owner_remains(tree):
    res = _client(tree["owner"]).patch(
        f"/api/workspaces/{WS}/members/{tree['owner'].pk}/",
        data={"role": "editor"}, content_type="application/json",
    )
    assert res.status_code == 200, res.content


def test_the_last_owner_with_no_parent_still_cannot_step_down(life):
    res = _client(life["owner"]).patch(
        f"/api/workspaces/{WS}/members/{life['owner'].pk}/",
        data={"role": "editor"}, content_type="application/json",
    )
    assert res.status_code == 400


# --- 6. detaching a workspace from its parent ---------------------------------

def test_an_inherited_owner_cannot_detach_a_workspace(tree):
    """It used to save, then 500 — leaving a root nobody was in to describe."""
    res = _client(tree["boss"]).put(f"/api/workspaces/{WS}/parent",
                                    data={"parent": None}, content_type="application/json")
    assert res.status_code == 409, res.content
    assert Workspace.objects.get(slug=WS).parent_id == "life-org"


def test_a_direct_owner_can_detach_a_workspace(tree):
    res = _client(tree["owner"]).put(f"/api/workspaces/{WS}/parent",
                                     data={"parent": None}, content_type="application/json")
    assert res.status_code == 200, res.content
    assert res.json()["inherited"] is False
    assert Workspace.objects.get(slug=WS).parent_id is None


def test_an_inherited_owner_can_still_move_a_workspace_between_parents_they_own(tree):
    other_org = a_workspace("life-org2")
    WorkspaceMembership.objects.create(workspace=other_org, user=tree["boss"], role=M.OWNER)
    res = _client(tree["boss"]).put(f"/api/workspaces/{WS}/parent",
                                    data={"parent": "life-org2"}, content_type="application/json")
    assert res.status_code == 200, res.content
