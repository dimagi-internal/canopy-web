"""A cloud turn that ends waiting on a person is on that person's feed (#1165).

The cloud never sets `RunnerBinding.pending_question` (only the laptop's report
loop does), and its turns always finish `done`. So for a cloud turn, "waiting on
you" can't come from a dialog. It comes from the feed's other half
(`apps/canopy_sessions/feed.py`): the agent had the last word and stopped, in a
session that started MANUAL. #1277 made such a session stay listed, and made a
cloud session with no creator belong to its agent's owner. This pins the whole
of #1165's "done when" on the real email-turn path:

* a manual cloud email turn that finished with a draft is on the owner's feed,
  and the owner is the one pushed when it finishes;
* it leaves the feed once someone replies;
* an AUTO cloud turn is never on it.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import feed
from apps.canopy_sessions.models import Message, RunnerBinding
from apps.harness import services
from apps.harness.models import Runner, Turn
from apps.push import services as push_services
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def world():
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(user=jj, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=jj)
    # Paired by somebody else: nobody sits at a cloud box.
    ops = User.objects.create_user("ops", "ops@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=ops, workspace=ws, role=WorkspaceMembership.EDITOR)
    cloud = Runner.objects.create(
        name="cloud-ec2-1", kind=Runner.CLOUD, host="ec2", owner=ops, workspace=ws,
        location=Runner.CLOUD, status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    return jj, agent, cloud


def _email_turn_finished(agent, cloud, *, mode, said="Draft reply to Ali. Approve to send?"):
    """An ACE email turn the cloud ran to completion, as the runner leaves it. The
    turn is `done` with its mode stamped at claim, the thread's session is bound to
    the cloud box under the Claude session id, and the agent's draft is its last word."""
    turn, created = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key=f"email-ace-{mode}",
        prompt="/ace:turn --thread t1",
        origin_ref={"thread_id": f"t-{mode}", "subject": "Latest on workflows",
                    "from": "Ali <ali@example.org>"},
    )
    assert created and turn.chat_session_id, "precondition: an email turn targets the thread's session"
    session = turn.chat_session
    Turn.objects.filter(pk=turn.pk).update(
        status=Turn.DONE, turn_mode=mode, claimed_by=cloud, session_key=f"uuid-{mode}",
        claimed_at=timezone.now(), finished_at=timezone.now(),
    )
    RunnerBinding.objects.update_or_create(
        session=session,
        defaults={"runner": cloud, "session_key": f"uuid-{mode}", "live_seen_at": timezone.now()},
    )
    Message.objects.create(session=session, turn_index=1, role=Message.USER,
                           plaintext="/ace:turn --thread t1", content={})
    Message.objects.create(session=session, turn_index=2, role=Message.ASSISTANT,
                           plaintext=said, content={})
    turn.refresh_from_db()
    return session, turn


def _row(user, session):
    c = Client()
    c.force_login(user)
    rows = c.get("/api/canopy-sessions/?reply=true").json()
    return next((r for r in rows if r["id"] == str(session.id)), None)


def test_a_manual_cloud_turn_left_waiting_is_on_the_owners_feed(world):
    jj, agent, cloud = world
    session, turn = _email_turn_finished(agent, cloud, mode="manual")
    row = _row(jj, session)
    assert row is not None, "the finished cloud session must stay listed"
    assert row["feed_status"] == feed.WAITING
    assert row["turn_mode"] == "manual"
    assert "Approve to send" in row["last_reply"]
    # And the "is done" push goes to the same person the feed shows it to.
    assert push_services._finish_audience(turn) == jj


def test_it_leaves_the_feed_once_someone_replies(world):
    jj, agent, cloud = world
    session, _turn = _email_turn_finished(agent, cloud, mode="manual")
    Message.objects.create(session=session, turn_index=3, role=Message.USER,
                           plaintext="Send it", content={})
    assert _row(jj, session)["feed_status"] == ""


def test_an_auto_cloud_turn_is_never_on_the_feed(world):
    jj, agent, cloud = world
    session, turn = _email_turn_finished(agent, cloud, mode="auto", said="Replied to Ali.")
    assert _row(jj, session)["feed_status"] == feed.AUTO
    assert push_services._finish_audience(turn) is None
