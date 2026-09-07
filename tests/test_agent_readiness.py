"""What a BOX reports, as distinct from what canopy-web stored.

The distinction is the whole point. On 2026-09-07 canopy-web held a valid
gog-token for ACE — `credentials/status` would have shown it set, with a green
tick — while every gmail call on the box failed, because the OAuth client
id+secret the token is useless without had not materialized. Both true at once,
and only one of them visible outside journald.

So readiness is POSTED by the box, from a call it actually made, and it is
gated to a caller pairing a live runner the agent routes to: a readiness signal
anyone could write is one nobody can trust, and this one is meant to be trusted
over the control plane's own record.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAssignment
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet(client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws)
    runner = Runner.objects.create(
        name="cloud-ec2-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    client.force_login(jj)
    return {"client": client, "agent": agent, "user": jj, "ws": ws}


def _pat(user) -> str:
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return raw


def _post(user, body):
    return Client().post(
        "/api/agents/ace/bootstrap-report", data=body,
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {_pat(user)}",
    )


DEAD = {
    "runner_name": "cloud-ec2-1", "client_creds_ok": False, "mailbox_ok": False,
    "gog_client": "canopy-web",
    "detail": "op read op://Canopy-Shared/gog-oauth-client-web/credential failed",
}


def test_a_box_reports_a_dead_mailbox_and_it_is_visible(fleet):
    """The 2026-09-07 shape: everything stored, nothing working."""
    assert _post(fleet["user"], DEAD).status_code == 200
    rows = fleet["client"].get("/api/agents/ace/readiness").json()
    assert len(rows) == 1
    assert rows[0]["mailbox_ok"] is False
    assert rows[0]["client_creds_ok"] is False
    assert "gog-oauth-client-web" in rows[0]["detail"]
    assert rows[0]["gog_client"] == "canopy-web"


def test_the_latest_report_replaces_the_previous_one(fleet):
    """Current state, not a log. A history of every boot would bury the one
    answer this is for: can the agent run right now."""
    _post(fleet["user"], DEAD)
    _post(fleet["user"], {**DEAD, "client_creds_ok": True, "mailbox_ok": True, "detail": ""})
    rows = fleet["client"].get("/api/agents/ace/readiness").json()
    assert len(rows) == 1
    assert rows[0]["mailbox_ok"] is True


def test_two_boxes_are_reported_separately(fleet):
    """Per-box, because two boxes running one agent can differ — and 'which box'
    is the first thing you need when one of them is broken."""
    _post(fleet["user"], DEAD)
    _post(fleet["user"], {**DEAD, "runner_name": "jj-mbp-cdp", "mailbox_ok": True})
    rows = fleet["client"].get("/api/agents/ace/readiness").json()
    assert {r["runner_name"] for r in rows} == {"cloud-ec2-1", "jj-mbp-cdp"}


def test_a_non_member_cannot_even_see_the_agent(fleet):
    """404, not 403 — agent lookup is workspace-scoped, so an outsider cannot
    tell a real agent from a typo. Same no-existence-leak rule as the rest of
    the surface, and stronger than the gate this test originally asserted."""
    outsider = get_user_model().objects.create_user(username="out", email="out@dimagi.com")
    assert _post(outsider, DEAD).status_code == 404


def test_a_member_who_pairs_no_runner_may_not_report(fleet):
    """The gate that actually matters here, and the same one credentials/resolve
    uses: being able to SEE an agent is not being able to speak for a box that
    runs it. A readiness signal anyone in the workspace could write is one
    nobody can trust — and this one is meant to outrank the control plane's own
    record of what it stored."""
    mate = get_user_model().objects.create_user(username="mate", email="mate@dimagi.com")
    WorkspaceMembership.objects.create(
        workspace=fleet["ws"], user=mate, role=WorkspaceMembership.EDITOR)
    assert _post(mate, DEAD).status_code == 403


def test_an_agent_no_box_has_reported_is_empty_not_healthy(fleet):
    """Absence must read as 'nobody has said', never as a pass. Silence being
    indistinguishable from health is the failure mode this replaces."""
    assert fleet["client"].get("/api/agents/ace/readiness").json() == []
