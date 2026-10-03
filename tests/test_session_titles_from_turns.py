"""A session a turn starts is named for a person, never by the command it ran.

2026-10-03: ACE's cloud session for an email from Ali was titled
"/ace:turn --thread 1a0f24bf9b830273" — the cloud runner proposed the prompt's
first line, which for an email turn is the command — while the email's subject,
"Latest on workflows", sat on the turn unused.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness import services
from apps.harness.models import Runner, Turn
from apps.harness.services import readable_title
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("prompt, title", [
    ("/ace:turn --thread 1a0f24bf9b830273", ""),
    ("/eva:turn — catch-up brief from Jonathan (owner), 2026-10-03. Five emails were never worked",
     "Catch-up brief from Jonathan (owner), 2026-10-03"),
    ("/echo:turn", ""),
    ("\nFix brief from Ada\nmore", "Fix brief from Ada"),
    ("Review the " + "very " * 30 + "long thing", None),
])
def test_readable_title(prompt, title):
    got = readable_title(prompt)
    if title is None:
        assert got.endswith("…") and len(got) <= 81
    else:
        assert got == title


def test_the_server_names_a_cloud_email_session_by_its_subject():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="C", created_by=u)
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.OWNER)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=u)
    box = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, host="", owner=u, workspace=ws,
                                status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    turn = Turn.objects.create(agent=ace, origin=Turn.ORIGIN_EMAIL, idempotency_key="e1",
                               prompt="/ace:turn --thread 1a0f24bf9b830273", claimed_by=box,
                               status=Turn.RUNNING,
                               origin_ref={"thread_id": "1a0f", "subject": "Latest on workflows"})
    c = Client()
    c.force_login(u)
    r = c.post(f"/api/harness/runners/{box.pk}/record-session", {
        "thread_key": f"ace:{turn.pk}", "session_key": "cli-1", "session_id": "cli-1",
        "title": "/ace:turn --thread 1a0f24bf9b830273", "turn_id": str(turn.pk), "agent_slug": "ace",
    }, content_type="application/json")
    assert r.status_code == 200, r.content
    assert Session.objects.get(runner_binding__session_key="cli-1").title == "Latest on workflows"


def test_with_nothing_readable_it_is_the_agents_turn():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="C", created_by=u)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=u)
    turn = Turn.objects.create(agent=ace, origin=Turn.ORIGIN_API, idempotency_key="a1",
                               prompt="/ace:turn --thread 1a0f")
    assert services.turn_session_title(turn, "/ace:turn --thread 1a0f") == "ACE turn"
