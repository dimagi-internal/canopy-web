"""claim_next_turn, widened to chat-session turns (SP2b).

A chat "send" makes a session turn (agent=NULL, project="", chat_session set). A
session-capable runner (capabilities.sessions=true — a cloud runner with claude)
claims it for its tenant; a plain runner never does. Sessions serialize like
agents (one executing turn per session), but distinct sessions run in parallel.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.canopy_sessions.models import Session
from apps.harness import services
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _ws(slug, owner):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    return ws


def _runner(pairer, **kw):
    defaults = dict(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", paired_by=pairer,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"projects": ["canopy-web"]},
    )
    defaults.update(kw)
    return Runner.objects.create(**defaults)


def _session_runner(pairer, **kw):
    return _runner(pairer, name="cloud-1", kind=Runner.CLOUD, host="",
                   capabilities={"sessions": True}, **kw)


def _session(ws, user):
    return Session.objects.create(workspace=ws, created_by=user)


def _session_turn(session, key, **kw):
    return Turn.objects.create(
        chat_session=session, origin=Turn.ORIGIN_API, idempotency_key=key, **kw
    )


def test_session_capable_runner_claims_a_session_turn():
    jj = _user("jj")
    ws = _ws("canopy", jj)
    runner = _session_runner(jj)
    turn = _session_turn(_session(ws, jj), "s1", prompt="hi")

    claimed = services.claim_next_turn(runner)

    assert claimed is not None and claimed.pk == turn.pk


def test_plain_runner_never_claims_a_session_turn():
    jj = _user("jj")
    ws = _ws("canopy", jj)
    runner = _runner(jj)  # no sessions capability
    _session_turn(_session(ws, jj), "s1")

    assert services.claim_next_turn(runner) is None


def test_session_turn_is_tenant_gated():
    jj = _user("jj")
    other_owner = _user("o2")
    ws_other = _ws("other", other_owner)  # jj is NOT a member
    runner = _session_runner(jj)
    _session_turn(_session(ws_other, other_owner), "s1")

    assert services.claim_next_turn(runner) is None


def test_busy_session_is_not_reclaimed():
    jj = _user("jj")
    ws = _ws("canopy", jj)
    runner = _session_runner(jj)
    session = _session(ws, jj)
    _session_turn(session, "s1", status=Turn.RUNNING)  # already executing
    _session_turn(session, "s2", status=Turn.QUEUED)   # same session, queued

    assert services.claim_next_turn(runner) is None


def test_distinct_sessions_claim_in_parallel():
    jj = _user("jj")
    ws = _ws("canopy", jj)
    runner = _session_runner(jj)
    t1 = _session_turn(_session(ws, jj), "s1")
    t2 = _session_turn(_session(ws, jj), "s2")

    c1 = services.claim_next_turn(runner)
    c2 = services.claim_next_turn(runner)
    assert c1 is not None and c2 is not None
    assert {c1.pk, c2.pk} == {t1.pk, t2.pk}


def test_busy_session_exclude_does_not_strand_project_turns():
    """The .exclude(chat_session_id__in=busy_sessions) must NOT NULL-propagate away
    agent/project turns (chat_session_id NULL) when a session is executing."""
    jj = _user("jj")
    ws = _ws("canopy", jj)
    runner = _runner(jj, kind=Runner.CLOUD, capabilities={"projects": ["canopy-web"], "sessions": True})
    _session_turn(_session(ws, jj), "sx", status=Turn.RUNNING)  # a busy session exists
    pturn = Turn.objects.create(
        project="canopy-web", workspace=ws, origin=Turn.ORIGIN_API, idempotency_key="p1"
    )

    claimed = services.claim_next_turn(runner)
    assert claimed is not None and claimed.pk == pturn.pk
