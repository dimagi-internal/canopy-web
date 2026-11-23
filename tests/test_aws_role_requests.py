"""A fenced AWS role is requested when needed and approved by a person (`apps/agents/api_aws.py`).

Pinned: the agent asks (`POST /api/agents/{slug}/aws-role-requests`), which files an `agent.aws_role`
approval only an owner of the agent's workspace or a superuser may approve, with a passkey, never an
agent; approving makes a grant `aws-credentials` vends under, for no longer than the grant has left;
an expired or revoked grant, or none, is a 403 `approval_required` naming the request route; a role
vended on ask cannot be requested; no agent holds a fenced role standing (hal and ada get
`labs-readwrite` only by an approved request, and `labs-read` on ask).
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents import api_aws
from apps.agents.models import Agent, AgentAdmin, AgentAwsRoleRequest
from apps.approvals import kinds, services
from apps.approvals.models import ApprovalRequest
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import WorkspaceMembership as M
from apps.workspaces.testing import a_member, a_workspace
from tests.token_helpers import pat_client

pytestmark = pytest.mark.django_db

J = "application/json"
READ_ARN = "arn:aws:iam::858923557655:role/labs-jj-observe-readonly"
RW_ARN = "arn:aws:iam::858923557655:role/labs-jj-agent-readwrite"
CONFIG_ARN = "arn:aws:iam::858923557655:role/labs-jj-agent-config"


class FakeSts:
    def __init__(self):
        self.calls: list[dict] = []

    def assume_role(self, **kw):
        self.calls.append(kw)
        return {"Credentials": {
            "AccessKeyId": "ASIAEXAMPLE", "SecretAccessKey": "secret", "SessionToken": "tok",
            "Expiration": timezone.now() + dt.timedelta(seconds=kw["DurationSeconds"])}}


@pytest.fixture()
def sts(monkeypatch, settings):
    settings.CANOPY_AWS_ROLES = {"labs-read": READ_ARN, "labs-readwrite": RW_ARN, "labs-config": CONFIG_ARN}
    settings.CANOPY_AWS_ROLE_AGENTS = {"labs-readwrite": [], "labs-config": []}
    settings.CANOPY_AWS_ROLE_SUMMARIES = {"labs-config": "Create and update labs app secrets named labs-jj-app-*."}
    fake = FakeSts()
    monkeypatch.setattr(api_aws, "sts_client", lambda: fake)
    return fake


@pytest.fixture()
def w():
    class W:
        pass

    w = W()
    ws = a_workspace("dimagi")
    w.owner = a_member(ws, email="jj@dimagi.com", role=M.OWNER)
    w.editor = a_member(ws, email="ed@dimagi.com", role=M.EDITOR)
    w.admin = a_member(ws, email="ad@dimagi.com", role=M.ADMIN)
    # A canopy superuser reaches the approval as a member of the workspace (here only a viewer).
    w.root = a_member(ws, email="root@dimagi.com", role=M.VIEWER)
    w.root.is_superuser = True
    w.root.save()
    w.wsuper = a_member(ws, email="su@dimagi.com", role=M.SUPERUSER)
    w.ace_login = a_member(ws, email="ace@dimagi-ai.com", role=M.VIEWER)
    w.hal_login = a_member(ws, email="hal@dimagi-ai.com", role=M.OWNER)  # an agent, even as an owner
    w.ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=w.editor, user=w.ace_login)
    w.hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=w.owner, user=w.hal_login)
    # The agent's own admin is not enough: these are AWS account rights.
    AgentAdmin.objects.create(agent=w.ace, user=w.admin)
    return w


def _ask(c, slug="ace", role="labs-config", reason="rotate the labs app's Stripe key", minutes=None):
    body = {"role": role, "reason": reason} | ({"minutes": minutes} if minutes is not None else {})
    return c.post(f"/api/agents/{slug}/aws-role-requests", data=json.dumps(body), content_type=J)


def _creds(c, slug="ace", role="labs-config"):
    return c.post(f"/api/agents/{slug}/aws-credentials", data=json.dumps({"role": role}), content_type=J)


def _decide(user, approval_id, *, step_up=True, decision="approve"):
    c = Client()
    c.force_login(user)
    if step_up:
        grant_step_up(c)
    return c.post(f"/api/approvals/{approval_id}/decide", {"decision": decision}, content_type=J)


def _requested(w, **kw):
    r = _ask(pat_client(w.ace_login), **kw)
    assert r.status_code == 201, r.content
    return r.json()


def test_no_grant_is_a_403_naming_how_to_ask(w, sts):
    r = _creds(pat_client(w.ace_login))
    assert r.status_code == 403
    body = r.json()
    assert body["error"] == "approval_required"
    assert body["request_with"] == "POST /api/agents/ace/aws-role-requests"
    assert sts.calls == []


def test_request_then_passkey_approval_then_vend(w, sts):
    card = _requested(w, minutes=120)
    assert card["status"] == "pending" and card["role"] == "labs-config" and card["minutes"] == 120
    assert card["approval_url"].endswith(f"/approvals/{card['approval_id']}")
    approval = ApprovalRequest.objects.get(pk=card["approval_id"])
    assert approval.kind == "agent.aws_role" and approval.requested_by_agent == w.ace
    # The preview names the agent, the role, what it can do and the duration; the reason is the agent's own.
    assert "ACE" in approval.title and "labs-config" in approval.title and "2 hours" in approval.title
    assert any("labs-jj-app-*" in line for line in approval.plan)
    assert any(CONFIG_ARN in line for line in approval.plan)
    assert approval.reason == "rotate the labs app's Stripe key"
    # Still pending: no keys.
    assert _creds(pat_client(w.ace_login)).status_code == 403
    # Approving without a fresh passkey is refused.
    assert _decide(w.owner, approval.pk, step_up=False).status_code == 403
    r = _decide(w.owner, approval.pk)
    assert r.status_code == 200 and r.json()["status"] == "done", r.content
    row = AgentAwsRoleRequest.objects.get(pk=card["id"])
    assert row.status == "approved" and row.approved_by == w.owner
    assert abs((row.expires_at - row.approved_at) - dt.timedelta(minutes=120)) < dt.timedelta(seconds=1)
    r = _creds(pat_client(w.ace_login))
    assert r.status_code == 200, r.content
    (call,) = sts.calls
    assert call["RoleArn"] == CONFIG_ARN and call["SourceIdentity"] == "canopy-agent-ace"
    assert call["DurationSeconds"] == 3600  # two hours left, but a chained role is an hour at most


def test_the_session_never_outlasts_the_grant(w, sts):
    card = _requested(w)
    _decide(w.owner, card["approval_id"])
    row = AgentAwsRoleRequest.objects.get(pk=card["id"])
    row.expires_at = timezone.now() + dt.timedelta(minutes=20)
    row.save()
    assert _creds(pat_client(w.ace_login)).status_code == 200
    assert 19 * 60 <= sts.calls[-1]["DurationSeconds"] <= 20 * 60
    # Under STS's 15-minute floor, nothing can be vended inside the grant: ask again.
    row.expires_at = timezone.now() + dt.timedelta(minutes=10)
    row.save()
    r = _creds(pat_client(w.ace_login))
    assert r.status_code == 403 and r.json()["error"] == "approval_required"
    assert len(sts.calls) == 1


def test_an_expired_grant_vends_nothing(w, sts):
    card = _requested(w)
    _decide(w.owner, card["approval_id"])
    AgentAwsRoleRequest.objects.filter(pk=card["id"]).update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    r = _creds(pat_client(w.ace_login))
    assert r.status_code == 403 and r.json()["error"] == "approval_required"
    listed = pat_client(w.ace_login).get("/api/agents/ace/aws-role-requests").json()
    assert listed[0]["status"] == "expired"
    assert sts.calls == []


def test_a_revoked_grant_vends_nothing_and_only_the_approver_or_an_owner_revokes(w, sts):
    card = _requested(w)
    _decide(w.owner, card["approval_id"])
    url = f"/api/agents/ace/aws-role-requests/{card['id']}"
    assert pat_client(w.editor).delete(url).status_code == 403
    assert pat_client(w.admin).delete(url).status_code == 403    # the agent's admin is not enough
    assert pat_client(w.ace_login).delete(url).status_code == 403  # nor the agent itself
    r = pat_client(w.owner).delete(url)
    assert r.status_code == 200 and r.json()["status"] == "revoked" and r.json()["revoked_by"] == "jj@dimagi.com"
    r = _creds(pat_client(w.ace_login))
    assert r.status_code == 403 and r.json()["error"] == "approval_required"
    assert pat_client(w.owner).delete(url).status_code == 409
    assert sts.calls == []


def test_a_superuser_may_approve_and_revoke(w, sts):
    card = _requested(w)
    assert _decide(w.root, card["approval_id"]).json()["status"] == "done"
    assert _creds(pat_client(w.ace_login)).status_code == 200
    r = pat_client(w.wsuper).delete(f"/api/agents/ace/aws-role-requests/{card['id']}")
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    again = _requested(w)
    assert _decide(w.wsuper, again["approval_id"]).json()["status"] == "done"


def test_an_agent_never_approves_nor_does_a_non_owner(w, sts):
    card = _requested(w)
    approval = ApprovalRequest.objects.get(pk=card["approval_id"])
    k, p = kinds.parse(approval.kind, approval.params)
    d = k.describe(p)
    assert not k.may_approve(w.hal_login, p, d)   # another agent, though an owner of the workspace
    assert not k.may_approve(w.ace_login, p, d)   # the agent asking
    assert not k.may_approve(w.admin, p, d)       # the agent's own admin, a workspace admin
    assert not k.may_approve(w.editor, p, d)      # the agent's owner, an editor of the workspace
    assert k.may_approve(w.owner, p, d) and k.may_approve(w.root, p, d)
    for who in (w.hal_login, w.admin, w.editor):
        assert _decide(who, approval.pk).status_code == 403
    assert AgentAwsRoleRequest.objects.get(pk=card["id"]).status == "pending"
    approvers = {u.pk for u in k.approvers(p, d)}
    assert {w.owner.pk, w.root.pk, w.wsuper.pk} <= approvers
    assert not approvers & {w.hal_login.pk, w.ace_login.pk, w.admin.pk, w.editor.pk}


def test_a_declined_request_vends_nothing(w, sts):
    card = _requested(w)
    assert _decide(w.owner, card["approval_id"], step_up=False, decision="decline").json()["status"] == "declined"
    assert _creds(pat_client(w.ace_login)).status_code == 403
    assert pat_client(w.ace_login).get("/api/agents/ace/aws-role-requests").json()[0]["status"] == "declined"


def test_withdrawing_a_pending_request_closes_its_approval(w, sts):
    card = _requested(w)
    r = pat_client(w.ace_login).delete(f"/api/agents/ace/aws-role-requests/{card['id']}")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert ApprovalRequest.objects.get(pk=card["approval_id"]).status == "cancelled"


def test_asking_again_while_pending_returns_the_same_request(w, sts):
    first = _requested(w)
    again = _ask(pat_client(w.ace_login))
    assert again.status_code == 200 and again.json()["id"] == first["id"]
    assert ApprovalRequest.objects.filter(kind="agent.aws_role").count() == 1


def test_what_cannot_be_requested(w, sts):
    c = pat_client(w.ace_login)
    assert _ask(c, role="labs-admin").status_code == 404                   # not a role canopy vends
    r = _ask(c, role="labs-read")                                          # vended on ask
    assert r.status_code == 422 and "no approval" in r.json()["detail"]
    assert _ask(c, minutes=8 * 60 + 1).status_code == 422
    assert _ask(c, minutes=5).status_code == 422
    assert _ask(c, reason="").status_code == 422
    assert not AgentAwsRoleRequest.objects.exists()


def test_who_may_ask(w, sts):
    viewer = a_member(w.ace.workspace, email="vi@dimagi.com", role=M.VIEWER)
    assert _ask(pat_client(viewer)).status_code == 403
    r = _ask(pat_client(w.editor))
    assert r.status_code == 201
    assert ApprovalRequest.objects.get(pk=r.json()["approval_id"]).requested_by_user == w.editor


def test_the_approval_preview_comes_only_from_the_server_made_request(w, sts):
    """Filing the kind by hand for someone else's request cannot approve it: the handler refuses."""
    card = _requested(w)
    stray, _ = services.create(kind="agent.aws_role", params={"request": card["id"]}, requested_by_user=w.editor)
    assert _decide(w.owner, stray.pk).json()["status"] == "failed"
    assert AgentAwsRoleRequest.objects.get(pk=card["id"]).status == "pending"


def test_hal_and_ada_get_labs_read_on_ask_and_labs_readwrite_only_by_request(w, sts):
    ada_login = a_member(w.ace.workspace, email="ada@dimagi-ai.com", role=M.VIEWER)
    Agent.objects.create(slug="ada", name="Ada", workspace=w.ace.workspace, owner=w.owner, user=ada_login)
    for slug, login in (("hal", w.hal_login), ("ada", ada_login)):
        r = _creds(pat_client(login), slug=slug, role="labs-readwrite")
        assert r.status_code == 403, r.content
        assert r.json()["error"] == "approval_required"
        assert r.json()["request_with"] == f"POST /api/agents/{slug}/aws-role-requests"
        r = _creds(pat_client(login), slug=slug, role="labs-read")
        assert r.status_code == 200, r.content
        assert sts.calls[-1]["RoleArn"] == READ_ARN
    assert all(call["RoleArn"] == READ_ARN for call in sts.calls)
    # hal asks, an owner approves with a passkey, and hal gets labs-readwrite for the grant.
    r = _ask(pat_client(w.hal_login), slug="hal", role="labs-readwrite", reason="restore a rehearsal copy")
    assert r.status_code == 201, r.content
    assert _decide(w.owner, r.json()["approval_id"]).json()["status"] == "done"
    r = _creds(pat_client(w.hal_login), slug="hal", role="labs-readwrite")
    assert r.status_code == 200, r.content
    assert sts.calls[-1]["RoleArn"] == RW_ARN and sts.calls[-1]["SourceIdentity"] == "canopy-agent-hal"
    # A grant is its agent's and its role's only.
    assert _creds(pat_client(ada_login), slug="ada", role="labs-readwrite").status_code == 403
    assert _creds(pat_client(w.hal_login), slug="hal", role="labs-config").status_code == 403


def test_an_agent_listed_for_a_role_needs_no_request(w, sts, settings):
    settings.CANOPY_AWS_ROLE_AGENTS = {"labs-readwrite": ["hal"], "labs-config": []}
    assert _creds(pat_client(w.hal_login), slug="hal", role="labs-readwrite").status_code == 200
    r = _ask(pat_client(w.hal_login), slug="hal", role="labs-readwrite")
    assert r.status_code == 422 and "no approval" in r.json()["detail"]
    assert _creds(pat_client(w.ace_login), role="labs-readwrite").status_code == 403


def test_the_shipped_settings_fence_every_write_role_with_no_standing_agent():
    from django.conf import settings as live

    assert live.CANOPY_AWS_ROLE_AGENTS == {"labs-readwrite": [], "labs-config": []}
    for role in live.CANOPY_AWS_ROLES:
        assert live.CANOPY_AWS_ROLE_SUMMARIES.get(role), f"{role} has no summary for its approver"
