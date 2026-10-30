"""`SessionOut.started_by_you` — the "Started by me" filter on the session lists.

A session is yours when you started it, or when it was found on your own box and
is not an agent's own work: `access.sessions_of_q`, asked for one row by
`access.is_own_session`. The two must agree.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions import access
from apps.canopy_sessions.models import RunnerBinding, Session, SessionParticipant
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    bo = User.objects.create_user("bo", "bo@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    WorkspaceMembership.objects.create(user=jj, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=bo, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=jj)
    box = Runner.objects.create(name="jj-mbp", workspace=ws, kind=Runner.EMDASH, location=Runner.LOCAL,
                                status=Runner.ONLINE, owner=jj, host="jj@mbp")

    def discovered(title, **kw):
        s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title=title, **kw)
        RunnerBinding.objects.create(session=s, runner=box, session_key=title, host=box.host,
                                     thread_key=str(s.id))
        return s

    bos = Session.objects.create(workspace=ws, created_by=bo, agent=agent, title="bos")
    SessionParticipant.objects.create(session=bos, user=jj, role=SessionParticipant.EDITOR)  # jj was added
    return {
        "jj": jj,
        "mine": Session.objects.create(workspace=ws, created_by=jj, agent=agent, title="mine"),
        "bos": bos,
        "on_my_box": discovered("on-my-box", project="canopy-web"),
        "agents_own": discovered("agents-own", agent=agent),
    }


def test_started_by_you_marks_only_your_own_sessions(w):
    c = Client()
    c.force_login(w["jj"])
    listed = {s["title"]: s["started_by_you"] for s in c.get("/api/canopy-sessions/?state=all").json()}
    assert listed == {"mine": True, "on-my-box": True, "bos": False, "agents-own": False}


def test_one_row_agrees_with_the_query(w):
    own = set(Session.objects.filter(access.sessions_of_q([w["jj"].pk])).values_list("pk", flat=True))
    for s in Session.objects.select_related("runner_binding__runner"):
        assert access.is_own_session(w["jj"], s) == (s.pk in own), s.title
