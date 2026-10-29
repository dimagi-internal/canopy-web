"""A turn's result says which closeout verdict it ended on — lifted off its result note.

An unattended incident turn ends on a status line canopy's `closeout record` computed,
tokened `[closeout: <VERDICT>]` (canopy_transcript.closeout). The turn result carries it
as `closeout_verdict` / `closeout_line`, so a session can be auto-closed on CLOSED alone and
a NEEDS_YOU shows its owner exactly the ask.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership

ASK = ("⏸ Session paused — waiting on you for: @jjackson: run `aws sso login --profile labs`. "
       "[closeout: NEEDS_YOU]")


@pytest.fixture
def hal():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    M.objects.create(workspace=ws, user=jj, role=M.OWNER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=jj)
    client = Client()
    client.force_login(jj)
    return agent, client


def _finished(agent, note, key):
    return Turn.objects.create(agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key=key,
                               status=Turn.DONE, result_note=note)


def test_the_turn_result_names_the_verdict_and_its_line(hal):
    agent, client = hal
    turn = _finished(agent, "Commented on #2768.\n\n" + ASK, "k1")
    body = client.get(f"/api/harness/turns/{turn.pk}").json()
    assert body["result_note"].endswith(ASK), body
    assert body["closeout_verdict"] == "NEEDS_YOU"
    assert body["closeout_line"] == ASK


def test_a_turn_with_no_closeout_line_has_none(hal):
    agent, client = hal
    turn = _finished(agent, "✅ Session complete — safe to close.", "k2")
    body = client.get(f"/api/harness/turns/{turn.pk}").json()
    assert body["closeout_verdict"] == "" and body["closeout_line"] == ""


def test_the_agents_turn_list_carries_it_too(hal):
    agent, client = hal
    _finished(agent, "Done.\n✅ Session complete — safe to close. [closeout: CLOSED]", "k3")
    res = client.get("/api/agents/hal/turns/")
    assert res.status_code == 200, res.content
    rows = res.json()
    rows = rows.get("items", rows) if isinstance(rows, dict) else rows
    assert [r["closeout_verdict"] for r in rows] == ["CLOSED"]
