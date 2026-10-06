"""RunnerBinding as the reuse authority (SessionLink fold, Plan 3 Task 2).

Replaces tests/test_harness_session_link.py: SessionLink is gone, and its
DB-constraint tests (unique-per-agent-thread, the agent-XOR-project
CheckConstraint) tested a schema RunnerBinding does not have — a binding's
identity is its OneToOne `session`, not a unique `thread_key` column, so
"one binding per thread" is a SERVICE-layer guarantee (`_binding_for_thread`
finds-before-creates) exercised here through resolve_session/record_session,
not a constraint test. See services._binding_for_thread / _thread_session.
"""
from __future__ import annotations

import pytest

from apps.harness import services
from apps.harness.models import Runner
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _user(name="jj"):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _ws(slug="w1"):
    return Workspace.objects.create(slug=slug, display_name=slug.upper(), created_by=_user(f"u-{slug}"))


def _agent(ws, slug="echo"):
    from apps.agents.models import Agent

    return Agent.objects.create(slug=slug, name=slug.title(), workspace=ws)


def test_resolve_new_thread_when_no_binding():
    ws = _ws("w1")
    a = _agent(ws)
    r = Runner.objects.create(name="laptop", workspace=ws, host="jj@air", location=Runner.LOCAL)
    plan = services.resolve_session(a, "phone:jj:echo", r)
    assert plan["new_thread"] is True
    assert plan["reuse"] is False
    assert plan["link_id"] is None


def test_record_then_resolve_reuses_for_same_runner_host():
    ws = _ws("w1")
    a = _agent(ws)
    r = Runner.objects.create(name="laptop", workspace=ws, host="jj@air", location=Runner.LOCAL)
    services.record_session(a, "phone:jj:echo", runner=r,
                            session_key="echo-1234", summary="rolling ctx",
                            agent_task_ext_id="T-9")
    plan = services.resolve_session(a, "phone:jj:echo", r)
    assert plan["reuse"] is True
    assert plan["session_key"] == "echo-1234"
    assert plan["summary"] == "rolling ctx"
    assert plan["agent_task_ext_id"] == "T-9"
    # exactly one durable Session was created for the thread
    assert Session.objects.filter(agent=a, origin=Session.ORIGIN_RUNNER).count() == 1


def test_record_is_idempotent_per_thread():
    ws = _ws("w1")
    a = _agent(ws)
    r = Runner.objects.create(name="laptop", workspace=ws, host="jj@air", location=Runner.LOCAL)
    services.record_session(a, "phone:jj:echo", runner=r, session_key="echo-1")
    services.record_session(a, "phone:jj:echo", runner=r, session_key="echo-2")
    assert Session.objects.filter(agent=a).count() == 1
    b = RunnerBinding.objects.get(thread_key="phone:jj:echo")
    assert b.session_key == "echo-2"  # re-pointed at the newest live task


def _owned_runner(ws, name="laptop", host="jj@air"):
    """A paired runner: its owner is the pairing human, a member of `ws`."""
    from apps.workspaces.models import WorkspaceMembership

    owner = _user(f"owner-{name}-{ws.slug}")
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.EDITOR)
    return Runner.objects.create(name=name, workspace=ws, host=host, location=Runner.LOCAL,
                                 owner=owner)


def test_record_binds_existing_chat_session_by_uuid_thread_key():
    ws = _ws("w1")
    a = _agent(ws)
    r = _owned_runner(ws)
    chat = Session.objects.create(workspace=ws, agent=a, origin=Session.ORIGIN_WEB, title="web chat")
    services.record_session(a, str(chat.id), runner=r, session_key="echo-9")
    # binds the EXISTING web session, does not fork a new runner session
    assert Session.objects.filter(agent=a).count() == 1
    b = RunnerBinding.objects.get(session=chat)
    assert b.session_key == "echo-9"
    assert b.thread_key == str(chat.id)


def test_reuse_denied_for_different_host():
    ws = _ws("w1")
    a = _agent(ws)
    r = Runner.objects.create(name="laptop", workspace=ws, host="jj@air", location=Runner.LOCAL)
    services.record_session(a, "phone:jj:echo", runner=r, session_key="echo-1")
    r.host = "jj@studio"  # other macOS account claims the same runner id
    plan = services.resolve_session(a, "phone:jj:echo", r)
    assert plan["reuse"] is False
    assert plan["session_key"] == "echo-1"  # hint still returned for rehydration context


def test_project_reuse_is_workspace_scoped():
    ws = _ws("w1")
    ws2 = _ws("w2")
    r = Runner.objects.create(name="laptop", workspace=ws, host="jj@air", location=Runner.LOCAL)
    services.record_session(None, "phone:jj:canopy-web", runner=r, project="canopy-web",
                            workspace=ws, session_key="cw-1")
    # a guessed thread_key from another workspace must NOT hijack the link
    other = services.resolve_session(None, "phone:jj:canopy-web", r, project="canopy-web", workspace=ws2)
    assert other["new_thread"] is True
    same = services.resolve_session(None, "phone:jj:canopy-web", r, project="canopy-web", workspace=ws)
    assert same["reuse"] is True


def test_sessionlink_is_gone():
    import apps.harness.models as m
    assert not hasattr(m, "SessionLink")


def test_report_does_not_clobber_agent_thread_reuse():
    """The runner's ambient session-report sweep reports EVERY open emdash task
    (agent- or project-driven, no filter) within ~10s of record_session binding
    a phone/agent thread. replace_reported_sessions finds that SAME binding via
    (runner, session_key) and must NOT stamp over its thread_key/host — doing so
    orphans the agent thread's binding, so the next resolve_session forks a
    duplicate session instead of reusing the live one (regression)."""
    from types import SimpleNamespace

    ws = _ws("w1")
    a = _agent(ws)
    r = Runner.objects.create(name="laptop", workspace=ws, host="jj@air", location=Runner.LOCAL)

    # The agent chat/phone thread's binding is created first by record_session.
    services.record_session(a, "phone:jj:echo", runner=r, session_key="echo-1234")

    # The runner's ambient sweep then reports the SAME emdash task (no agent/
    # project filter) as part of its ordinary open-session report.
    reported = SimpleNamespace(
        emdash_task="echo-1234", project="echo", status="in_progress",
        last_interacted_at=None, recent_messages=[],
    )
    services.replace_reported_sessions(r, ws, [reported])

    plan = services.resolve_session(a, "phone:jj:echo", r)
    assert plan["reuse"] is True
    assert plan["new_thread"] is False
    assert plan["session_key"] == "echo-1234"


# --- a session id is not a key to someone else's conversation ---------------------
#
# `_thread_session` binds an existing Session named by a UUID thread_key. Before,
# ANY session id bound, whatever its agent or tenant, re-pointing someone else's
# chat (its live hint, streams and next turns) at the caller's box.


def test_record_refuses_another_agents_session_by_uuid():
    ws = _ws("w1")
    echo, hal = _agent(ws, "echo"), _agent(ws, "hal")
    r = _owned_runner(ws)
    hals_chat = Session.objects.create(workspace=ws, agent=hal, origin=Session.ORIGIN_WEB, title="hal")
    with pytest.raises(services.ThreadSessionNotFound):
        services.record_session(echo, str(hals_chat.id), runner=r, session_key="echo-9")
    assert not RunnerBinding.objects.filter(session=hals_chat).exists()


def test_record_refuses_a_session_in_a_tenant_the_runner_owner_is_not_in():
    home, other = _ws("w1"), _ws("w2")
    a = _agent(other, "echo")
    r = _owned_runner(home)          # owner is a member of w1 only
    chat = Session.objects.create(workspace=other, agent=a, origin=Session.ORIGIN_WEB, title="x")
    with pytest.raises(services.ThreadSessionNotFound):
        services.record_session(a, str(chat.id), runner=r, session_key="echo-9")
    assert not RunnerBinding.objects.filter(session=chat).exists()


def test_record_refuses_an_unowned_runner_binding_a_session_by_uuid():
    ws = _ws("w1")
    a = _agent(ws)
    r = Runner.objects.create(name="stray", workspace=ws, host="h", location=Runner.LOCAL)
    chat = Session.objects.create(workspace=ws, agent=a, origin=Session.ORIGIN_WEB, title="x")
    with pytest.raises(services.ThreadSessionNotFound):
        services.record_session(a, str(chat.id), runner=r, session_key="echo-9")


def test_record_refuses_a_project_thread_naming_an_agent_or_other_repo_session():
    ws = _ws("w1")
    a = _agent(ws)
    r = _owned_runner(ws)
    agent_chat = Session.objects.create(workspace=ws, agent=a, origin=Session.ORIGIN_WEB, title="a")
    other_repo = Session.objects.create(workspace=ws, project="ace", origin=Session.ORIGIN_WEB,
                                        title="ace")
    for chat in (agent_chat, other_repo):
        with pytest.raises(services.ThreadSessionNotFound):
            services.record_session(None, str(chat.id), runner=r, project="canopy-web",
                                    workspace=ws, session_key="t-1")
        assert not RunnerBinding.objects.filter(session=chat).exists()


def test_record_binds_its_own_project_session_by_uuid():
    ws = _ws("w1")
    r = _owned_runner(ws)
    chat = Session.objects.create(workspace=ws, project="canopy-web", origin=Session.ORIGIN_WEB,
                                  title="repo chat")
    services.record_session(None, str(chat.id), runner=r, project="canopy-web", workspace=ws,
                            session_key="t-1")
    assert RunnerBinding.objects.get(session=chat).session_key == "t-1"
