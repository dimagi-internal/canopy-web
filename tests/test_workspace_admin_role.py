"""The workspace ADMIN role — between editor and owner (2026-10-02).

An admin RUNS the workspace: reads every log, manages members and invites
strictly below themselves, and runs the integrations (mailboxes, connected-site
health, Slack sync). An admin does not hold the KEYS: the shared vault,
deleting or moving the workspace, the Slack app itself, registering a site that
vouches for visitors, and any agent's credentials stay the owner's (or the
agent's own admins').

Below admin, a turn's content is readable by whoever started it and the
agent's admins — not by every member, which is what it was.
"""
from __future__ import annotations

import uuid

import pytest
from django.test import Client

from apps.agents.models import Agent
from apps.events.models import Event
from apps.harness.models import Turn
from apps.workspaces import permissions as perms
from apps.workspaces.models import WorkspaceInvite, WorkspaceMembership as M
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db

WS = "adm-ws"


@pytest.fixture
def adm():
    ws = a_workspace(WS)
    people = {
        role: a_member(ws, email=f"adm-{role}@dimagi.com", role=role)
        for role in (M.OWNER, M.ADMIN, M.EDITOR, M.VIEWER)
    }
    agent = Agent.objects.create(slug="admbot", name="Adm", workspace=ws)
    return {"ws": ws, "agent": agent, **people}


def _c(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


# --- the table --------------------------------------------------------------------

def test_the_ladder_is_viewer_editor_admin_owner():
    assert [r for r, _ in sorted(M.ROLE_RANK.items(), key=lambda kv: kv[1])] == [
        M.VIEWER, M.EDITOR, M.ADMIN, M.OWNER]


@pytest.mark.parametrize("capability,lowest", [
    (perms.READ, M.VIEWER), (perms.CONTENT_WRITE, M.EDITOR), (perms.AGENT_WORK, M.EDITOR),
    (perms.LOGS_READ, M.ADMIN), (perms.MEMBERS_MANAGE, M.ADMIN), (perms.INTEGRATIONS, M.ADMIN),
    (perms.OWN, M.OWNER),
])
def test_each_capability_starts_at_its_role(capability, lowest):
    for role, rank in M.ROLE_RANK.items():
        assert perms.role_allows(role, capability) == (rank >= M.ROLE_RANK[lowest]), (role, capability)


def test_an_admin_manages_only_below_themselves():
    may = perms.may_manage_member
    assert may(M.ADMIN, M.EDITOR, M.VIEWER)
    assert may(M.ADMIN, M.VIEWER, M.EDITOR)
    assert may(M.ADMIN, M.EDITOR)                 # remove an editor
    assert not may(M.ADMIN, M.EDITOR, M.ADMIN)    # cannot mint a peer
    assert not may(M.ADMIN, M.ADMIN)              # cannot touch a peer
    assert not may(M.ADMIN, M.OWNER)
    assert not may(M.ADMIN, None, M.OWNER)        # cannot invite an owner
    assert may(M.OWNER, M.OWNER, M.VIEWER)
    assert not may(M.EDITOR, M.VIEWER)


# --- members and invites ----------------------------------------------------------

def test_an_admin_invites_an_editor_but_not_an_admin(adm):
    c = _c(adm[M.ADMIN])
    ok = c.post(f"/api/workspaces/{WS}/invites/", {"email": "new@dimagi.com", "role": "editor"},
                content_type="application/json")
    assert ok.status_code == 201, ok.content
    assert ok.json()["token"]
    no = c.post(f"/api/workspaces/{WS}/invites/", {"email": "n2@dimagi.com", "role": "admin"},
                content_type="application/json")
    assert no.status_code == 403
    assert not WorkspaceInvite.objects.filter(email="n2@dimagi.com").exists()


def test_an_admin_changes_and_removes_editors_but_never_owners(adm):
    c = _c(adm[M.ADMIN])
    r = c.patch(f"/api/workspaces/{WS}/members/{adm[M.EDITOR].pk}/", {"role": "viewer"},
                content_type="application/json")
    assert r.status_code == 200, r.content
    assert c.patch(f"/api/workspaces/{WS}/members/{adm[M.OWNER].pk}/", {"role": "viewer"},
                   content_type="application/json").status_code == 403
    assert c.delete(f"/api/workspaces/{WS}/members/{adm[M.OWNER].pk}/").status_code == 403
    assert c.delete(f"/api/workspaces/{WS}/members/{adm[M.VIEWER].pk}/").status_code == 204


def test_an_editor_manages_no_one(adm):
    c = _c(adm[M.EDITOR])
    assert c.post(f"/api/workspaces/{WS}/invites/", {"email": "x@dimagi.com", "role": "viewer"},
                  content_type="application/json").status_code == 403
    assert c.delete(f"/api/workspaces/{WS}/members/{adm[M.VIEWER].pk}/").status_code == 403


def test_an_admin_sees_invite_links_only_for_roles_below_them(adm):
    from apps.workspaces import services

    services.create_invite(workspace=adm["ws"], email="o@dimagi.com", role=M.OWNER,
                           invited_by=adm[M.OWNER])
    services.create_invite(workspace=adm["ws"], email="e@dimagi.com", role=M.EDITOR,
                           invited_by=adm[M.OWNER])
    rows = {i["email"]: i["token"] for i in _c(adm[M.ADMIN]).get(f"/api/workspaces/{WS}/invites/").json()}
    assert rows["e@dimagi.com"] and not rows["o@dimagi.com"]
    assert not any(i["token"] for i in _c(adm[M.EDITOR]).get(f"/api/workspaces/{WS}/invites/").json())


# --- the keys stay the owner's ----------------------------------------------------

def test_an_admin_cannot_delete_the_workspace_or_set_its_shared_vault(adm):
    c = _c(adm[M.ADMIN])
    assert c.delete(f"/api/workspaces/{WS}/").status_code == 403
    assert c.put(f"/api/workspaces/{WS}/shared-vault", {"vault": "op://x"},
                 content_type="application/json").status_code == 403


def test_an_admin_does_not_hold_agents_keys(adm):
    assert not adm["agent"].is_admin(adm[M.ADMIN])
    assert adm["agent"].is_admin(adm[M.OWNER])
    r = _c(adm[M.ADMIN]).put("/api/agents/admbot/credentials", {"values": {"k": "v"}},
                             content_type="application/json")
    assert r.status_code == 403


def test_an_admin_does_editor_work(adm):
    r = _c(adm[M.ADMIN]).patch("/api/agents/admbot/turn-mode", {"turn_mode": "auto"},
                               content_type="application/json")
    assert r.status_code == 200, r.content


# --- integrations -----------------------------------------------------------------

def test_an_admin_registers_a_mailbox_and_an_editor_cannot(adm):
    body = {"address": "admbot@dimagi-ai.com", "agent_slug": "admbot"}
    assert _c(adm[M.EDITOR]).post(f"/api/inbound/mailboxes/{WS}", body,
                                  content_type="application/json").status_code == 403
    r = _c(adm[M.ADMIN]).post(f"/api/inbound/mailboxes/{WS}", body, content_type="application/json")
    assert r.status_code in (200, 201), r.content


# --- logs -------------------------------------------------------------------------

def _turn(adm, by):
    return Turn.objects.create(agent=adm["agent"], origin=Turn.ORIGIN_API, prompt="the secret plan",
                               idempotency_key=uuid.uuid4().hex, initiator_user=by)


def test_the_event_log_is_read_by_admins(adm):
    Event.objects.create(workspace=adm["ws"], source="runner", key="k", summary="box down")
    for role, rows in ((M.VIEWER, 0), (M.EDITOR, 0), (M.ADMIN, 1), (M.OWNER, 1)):
        assert len(_c(adm[role]).get("/api/events/").json()["items"]) == rows, role


@pytest.mark.parametrize("path", ["", "/events", "/transcript", "/caller-context"])
def test_a_turns_content_is_read_by_its_starter_and_admins(adm, path):
    turn = _turn(adm, adm[M.VIEWER])
    url = f"/api/harness/turns/{turn.id}{path}"
    readable = {M.VIEWER: True, M.EDITOR: False, M.ADMIN: True, M.OWNER: True}
    for role, can in readable.items():
        res = _c(adm[role]).get(url)
        if path == "":
            assert res.status_code == 200
            assert (res.json()["prompt"] == "the secret plan") == can, role
            assert res.json()["content_hidden"] == (not can), role
        else:
            assert (res.status_code == 200) == can, (role, res.status_code)


def test_turn_lists_show_that_a_turn_ran_but_not_its_prompt(adm):
    _turn(adm, adm[M.OWNER])
    rows = _c(adm[M.EDITOR]).get("/api/harness/turns/").json()
    assert rows and rows[0]["prompt"] == "" and rows[0]["content_hidden"]
    page = _c(adm[M.EDITOR]).get("/api/agents/admbot/turns/").json()["items"]
    assert page and page[0]["prompt"] == "" and page[0]["content_hidden"]
    assert _c(adm[M.ADMIN]).get("/api/harness/turns/").json()[0]["prompt"] == "the secret plan"
