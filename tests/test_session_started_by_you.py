"""`SessionOut.started_by_you` — the "Started by me" filter on the session lists.

A session is yours when you started it, or when it was found on your own box and
is not an agent's own work: `access.sessions_of_q`, asked for one row by
`access.is_own_session`. The two must agree. It is also yours when an agent you
asked dispatched it from a conversation of yours (`parent_session`), however deep.
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


def test_a_session_an_agent_dispatched_from_your_chat_is_yours(w):
    hal = User.objects.create_user("halbot", "hal@dimagi-ai.com", "pw")
    agent = Agent.objects.get(slug="hal")
    child = Session.objects.create(workspace=agent.workspace, created_by=hal, agent=agent,
                                   title="child", parent_session=w["mine"])
    grandchild = Session.objects.create(workspace=agent.workspace, created_by=hal, agent=agent,
                                        title="grandchild", parent_session=child)
    under_bo = Session.objects.create(workspace=agent.workspace, created_by=hal, agent=agent,
                                      title="under-bo", parent_session=w["bos"])
    for s in (child, grandchild, under_bo):
        SessionParticipant.objects.create(session=s, user=w["jj"], role=SessionParticipant.EDITOR)
    c = Client()
    c.force_login(w["jj"])
    listed = {s["title"]: s["started_by_you"] for s in c.get("/api/canopy-sessions/?state=all").json()}
    assert listed["child"] and listed["grandchild"]
    assert not listed["under-bo"]  # its root is bo's chat, not jj's
    assert access.started_by_you_among(w["jj"], [child, grandchild, under_bo]) == {child.pk, grandchild.pk}


def test_dispatched_from_your_chat_grants_no_access(w):
    """A display rule only: `sessions_of_q` (what review grants and coaching read) is unchanged."""
    hal = User.objects.create_user("halbot", "hal@dimagi-ai.com", "pw")
    agent = Agent.objects.get(slug="hal")
    child = Session.objects.create(workspace=agent.workspace, created_by=hal, agent=agent,
                                   title="child", parent_session=w["mine"])
    assert not access.is_own_session(w["jj"], child)
    assert not Session.objects.filter(access.sessions_of_q([w["jj"].pk]), pk=child.pk).exists()


def _scheduled_session(w, title, *, by, origin=None):
    """A session an agent's turn drove, the turn fired by `by`'s schedule (or by `origin`)."""
    from apps.harness import initiator as who
    from apps.harness.models import Turn

    hal = User.objects.get_or_create(username="halbot", defaults={"email": "hal@dimagi-ai.com"})[0]
    agent = Agent.objects.get(slug="hal")
    s = Session.objects.create(workspace=agent.workspace, created_by=hal, agent=agent, title=title)
    SessionParticipant.objects.create(session=s, user=w["jj"], role=SessionParticipant.EDITOR)
    init = who.system(via="schedule", accountable=by)
    Turn.objects.create(agent=agent, origin=origin or Turn.ORIGIN_CANOPY_SCHEDULER, prompt="/hal:turn",
                        idempotency_key=f"t:{title}", status=Turn.DONE,
                        canopy_session=s, initiator_kind=init.kind, initiator_via=init.via,
                        initiator_user=by)
    return s


def test_a_session_your_schedule_fired_is_yours(w):
    bo = User.objects.get(username="bo")
    mine = _scheduled_session(w, "my-schedule", by=w["jj"])
    theirs = _scheduled_session(w, "bos-schedule", by=bo)
    not_scheduled = _scheduled_session(w, "api-turn", by=w["jj"], origin="api")
    hal = User.objects.get(username="halbot")
    dispatched = Session.objects.create(workspace=mine.workspace, created_by=hal, agent=mine.agent,
                                        title="dispatched", parent_session=mine)
    SessionParticipant.objects.create(session=dispatched, user=w["jj"], role=SessionParticipant.EDITOR)
    c = Client()
    c.force_login(w["jj"])
    listed = {s["title"]: s["started_by_you"] for s in c.get("/api/canopy-sessions/?state=all").json()}
    assert listed["my-schedule"] and listed["dispatched"]
    assert not listed["bos-schedule"] and not listed["api-turn"]
    # Still a display rule: a schedule's session grants nothing.
    assert not Session.objects.filter(access.sessions_of_q([w["jj"].pk]), pk=mine.pk).exists()
    assert access.started_by_you_among(w["jj"], [mine, theirs, not_scheduled, dispatched]) == {
        mine.pk, dispatched.pk}
