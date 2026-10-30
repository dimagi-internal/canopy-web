"""The workspace SUPERUSER role, and the workspace controls it holds.

Asked for on 2026-10-30: superuser exists at two levels — the system (`User.is_superuser`,
the whole fleet's switches) and a workspace — and both are first class. A workspace
superuser holds everything an owner does, in the workspace and every one below it, as
OVERSIGHT: the owner is told (blocking alerts, access requests), the superuser is not.
Owners and superusers both hold `control`: hold the workspace's work (a toggle) and stop
what is running there (an action).
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness import claim
from apps.harness.models import Runner, Turn
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

M = WorkspaceMembership


@pytest.fixture()
def tree():
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    coo = User.objects.create_user("coo", "coo@dimagi.com", "pw")
    editor = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    root = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    kid = Workspace.objects.create(slug="connect", display_name="Connect", parent=root, created_by=jj)
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=jj)
    M.objects.create(workspace=root, user=jj, role=M.OWNER)
    M.objects.create(workspace=root, user=coo, role=M.SUPERUSER)
    M.objects.create(workspace=kid, user=editor, role=M.EDITOR)
    M.objects.create(workspace=other, user=jj, role=M.OWNER)
    return jj, coo, editor, root, kid, other


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def test_a_superuser_holds_every_owner_capability_down_the_tree(tree):
    _jj, coo, _ed, root, kid, other = tree
    for ws in (root, kid):
        assert all(perms.can(coo, ws, cap) for cap in perms.MINIMUM_ROLE)
    assert wsvc.member_role(coo, "connect") == M.SUPERUSER
    assert not wsvc.is_member(coo, "other")


def test_owners_and_superusers_manage_each_other(tree):
    assert perms.may_manage_member(M.SUPERUSER, M.OWNER, M.EDITOR)
    assert perms.may_manage_member(M.OWNER, M.SUPERUSER, None)
    assert perms.may_manage_member(M.OWNER, None, M.SUPERUSER)
    assert not perms.may_manage_member(M.ADMIN, None, M.SUPERUSER)


def test_an_owner_grants_superuser_from_the_members_api(tree):
    jj, _coo, _ed, _root, _kid, _other = tree
    ceo2 = User.objects.create_user("cto", "cto@dimagi.com", "pw")
    M.objects.create(workspace_id="connect", user=ceo2, role=M.VIEWER)  # in the tree, so addable
    resp = _client(jj).post("/api/workspaces/dimagi/members/", data={"user_id": ceo2.pk, "role": "superuser"},
                            content_type="application/json")
    assert resp.status_code == 201, resp.content
    assert wsvc.member_role(ceo2, "dimagi") == M.SUPERUSER


def test_a_superuser_is_not_an_owner_for_who_is_told(tree):
    jj, coo, _ed, root, kid, _other = tree
    assert wsvc.owner_user_ids(root) == {jj.pk}
    assert wsvc.owner_user_ids(kid) == {jj.pk}
    assert wsvc.mail_recipients([jj, coo], kid) == [jj]
    assert wsvc.mail_recipients([coo], kid) == [coo]  # never down to nobody


def test_access_request_mail_goes_to_the_owner_not_the_superuser(tree, settings):
    from django.core import mail

    from apps.workspaces import access_requests
    from apps.workspaces.models import WorkspaceAccessRequest

    asker = User.objects.create_user("new", "new@dimagi.com", "pw")
    req = WorkspaceAccessRequest.objects.create(workspace_id="dimagi", user=asker)
    access_requests.notify_admins(req)
    assert sorted(to for m in mail.outbox for to in m.to) == ["jj@dimagi.com"]


# --- the workspace controls ----------------------------------------------------------


def _runner(ws, owner):
    return Runner.objects.create(
        name="box", kind=Runner.EMDASH, workspace=ws, owner=owner, host="h",
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities={"projects": ["repo"]},
    )


def _queued(ws_slug, key):
    return Turn.objects.create(project="repo", workspace_id=ws_slug, status=Turn.QUEUED, origin="api",
                               idempotency_key=key)


def test_a_superuser_holds_the_tree_and_the_claim_respects_it(tree):
    jj, coo, _ed, root, kid, other = tree
    in_kid = _queued("connect", "k1")
    elsewhere = _queued("other", "o1")
    c = _client(coo)

    body = c.post("/api/harness/workspaces/dimagi/hold", data={"note": "audit"},
                  content_type="application/json").json()
    assert body["held"] is True and body["can_control"] is True

    kid_runner = _runner(kid, jj)
    assert claim.claim_next_turn(kid_runner) is None
    other_runner = _runner(other, jj)
    got = claim.claim_next_turn(other_runner)
    assert got is not None and got.pk == elsewhere.pk  # another tenant is not held

    child_view = c.get("/api/harness/workspaces/connect/hold").json()
    assert child_view["held"] is True and child_view["held_by_workspace"] == "dimagi"
    rows = claim.unclaimable_queued_turns(jj)
    assert any(r["kind"] == "workspace_hold" and r["turn_id"] == str(in_kid.pk) for r in rows) or rows == []

    assert c.post("/api/harness/workspaces/dimagi/hold/release").json()["held"] is False
    claimed = claim.claim_next_turn(kid_runner)
    assert claimed is not None and claimed.pk == in_kid.pk


def test_an_editor_may_read_the_hold_but_not_set_it(tree):
    _jj, _coo, ed, _root, _kid, _other = tree
    c = _client(ed)
    assert c.get("/api/harness/workspaces/connect/hold").json()["can_control"] is False
    assert c.post("/api/harness/workspaces/connect/hold", data={},
                  content_type="application/json").status_code == 403
    assert c.post("/api/harness/workspaces/connect/stop").status_code == 403


def test_a_stranger_gets_a_404(tree):
    _jj, coo, _ed, _root, _kid, _other = tree
    assert _client(coo).get("/api/harness/workspaces/other/hold").status_code == 404


def test_stop_reaches_only_the_workspace_tree(tree, monkeypatch):
    jj, coo, _ed, root, kid, other = tree
    from apps.realtime import groups

    sent = []
    monkeypatch.setattr(groups, "publish", lambda group, msg: sent.append(msg))
    runner = _runner(kid, jj)
    mine = _queued("connect", "k1")
    theirs = _queued("other", "o1")
    Turn.objects.filter(pk__in=[mine.pk, theirs.pk]).update(status=Turn.RUNNING, claimed_by=runner)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=kid, owner=jj)
    session = Session.objects.create(agent=agent, workspace=kid, origin=Session.ORIGIN_RUNNER, title="t")
    RunnerBinding.objects.create(session=session, runner=runner, session_key="hal-1", agent_status="working")
    other_agent = Agent.objects.create(slug="eva", name="Eva", workspace=other, owner=jj)
    other_session = Session.objects.create(agent=other_agent, workspace=other, origin=Session.ORIGIN_RUNNER,
                                           title="t")
    RunnerBinding.objects.create(session=other_session, runner=runner, session_key="eva-1", agent_status="working")

    body = _client(coo).post("/api/harness/workspaces/dimagi/stop").json()

    assert body == {"stopped_sessions": 1, "stopped_turns": 1}
    cancelled = {m["turn_id"] for m in sent if m["type"] == "runner.cancel"}
    assert cancelled == {str(mine.pk)}
    interrupted = {m["session_id"] for m in sent if m["type"] == "runner.session_interrupt"}
    assert interrupted == {str(session.id)}
