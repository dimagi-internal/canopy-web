"""A viewer who attaches before the runner binds the session is still streamed to.

The widget's opening sequence is create -> send -> open socket -> attach, all
within a second, and the runner binds the session only when it claims the turn
a moment later. `attach_session` marks `stream_desired` on the 0->1 viewer edge
and only on an existing binding, so that edge fired into nothing, the new
binding started unwatched, and `post_session_stream` persisted the whole reply
and pushed none of it. Seen on connect-labs, 2026-09-25: "Thinking…" and then
neither the question nor the answer.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache

from apps.agents.models import Agent
from apps.canopy_sessions import services as chat_services
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness import services
from apps.harness.models import Runner
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


def _world():
    user = get_user_model().objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=user)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws)
    runner = Runner.objects.create(name="laptop", workspace=ws, host="jj@mbp", location=Runner.LOCAL)
    chat = Session.objects.create(workspace=ws, created_by=user, agent=agent)
    return agent, runner, chat


def test_a_viewer_who_attached_before_the_binding_is_streamed_to():
    agent, runner, chat = _world()

    chat_services.attach_session(chat)          # the widget's socket, first
    assert not RunnerBinding.objects.filter(session=chat).exists()

    services.record_session(agent, str(chat.id), runner=runner,  # the runner, a moment later
                            emdash_task_id="ace-task-1")

    assert RunnerBinding.objects.get(session=chat).stream_desired is True


def test_nobody_watching_still_means_nothing_is_pushed():
    agent, runner, chat = _world()

    services.record_session(agent, str(chat.id), runner=runner, emdash_task_id="ace-task-1")

    assert RunnerBinding.objects.get(session=chat).stream_desired is False


def test_a_viewer_who_left_before_the_binding_is_not_streamed_to():
    agent, runner, chat = _world()
    chat_services.attach_session(chat)
    chat_services.detach_session(chat)

    services.record_session(agent, str(chat.id), runner=runner, emdash_task_id="ace-task-1")

    assert RunnerBinding.objects.get(session=chat).stream_desired is False
