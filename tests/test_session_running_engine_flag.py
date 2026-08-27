"""`running` when emdash's engine flag has gone stale.

emdash derives `agent_status` from three Claude Code hooks, and only UserPromptSubmit
ever reaches "working". Claude Code fires `Stop` whenever the MAIN LOOP's turn ends —
including a turn that ends only to hand off to a background subagent — and the wake-up
afterwards is a task-notification, not a prompt. So from the first background dispatch
onward the flag is pinned at "completed" while the session churns on, and a server that
trusts a non-blank flag outright calls a working session finished.

Measured 2026-08-27 on `hh4`: background Agent dispatched 12:59:34, wake-ups at 13:06
and 13:22, transcript still growing at 13:23:33, API saying running=False throughout.
"""
import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.canopy_sessions.services import is_session_running
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    c = Client(); c.force_login(user)
    return user, ws, c


def _binding(user, ws, *, agent_status, stale=False, interacted=None, key="echo-1"):
    session = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="disc")
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                   paired_by=user)
    return session, RunnerBinding.objects.create(
        session=session, runner=runner, session_key=key,
        agent_status=agent_status, agent_status_stale=stale,
        last_interacted_at=interacted or timezone.now(),
        live_seen_at=timezone.now(),
    )


def test_a_stale_completed_flag_still_reads_as_running():
    """The reported bug. The flag says the turn ended; the runner has watched the
    session keep writing since it said so, which no finished turn does."""
    user, ws, _ = _ctx()
    # Deliberately also past RUNNING_WINDOW: the recency fallback would say False too,
    # so nothing but the dissent can carry this.
    stale_ts = timezone.now() - dt.timedelta(seconds=600)
    _, binding = _binding(user, ws, agent_status="completed", stale=True, interacted=stale_ts)
    assert is_session_running(binding) is True


def test_a_finished_turn_still_retires_immediately():
    """The property the engine flag was adopted for must survive: no dissent, so a
    "completed" flag retires the badge at once even though the write was seconds ago."""
    user, ws, _ = _ctx()
    _, binding = _binding(user, ws, agent_status="completed", stale=False)
    assert is_session_running(binding) is False


def test_dissent_cannot_resurrect_an_offline_runner():
    """A box that is gone is not running whatever it last reported — the offline check
    comes first and the dissent must not reach past it."""
    user, ws, _ = _ctx()
    _, binding = _binding(user, ws, agent_status="completed", stale=True)
    # live_status is derived from the heartbeat, not the stored column.
    binding.runner.last_heartbeat_at = timezone.now() - dt.timedelta(hours=2)
    binding.runner.save(update_fields=["last_heartbeat_at"])
    assert binding.runner.live_status != Runner.ONLINE
    assert is_session_running(binding) is False


def test_a_working_flag_does_not_need_the_dissent():
    user, ws, _ = _ctx()
    _, binding = _binding(user, ws, agent_status="working", stale=False)
    assert is_session_running(binding) is True


def test_a_blank_flag_still_falls_back_to_recency():
    """An older runner cannot dissent, so blank+False must behave exactly as before."""
    user, ws, _ = _ctx()
    _, fresh = _binding(user, ws, agent_status="", stale=False)
    assert is_session_running(fresh) is True
    _, old = _binding(user, ws, agent_status="", stale=False, key="echo-2",
                      interacted=timezone.now() - dt.timedelta(seconds=150))
    assert is_session_running(old) is False


def test_the_api_row_reflects_the_dissent():
    user, ws, c = _ctx()
    session, _ = _binding(user, ws, agent_status="completed", stale=True,
                          interacted=timezone.now() - dt.timedelta(seconds=600))
    row = next(r for r in c.get("/api/canopy-sessions/").json() if r["id"] == str(session.id))
    assert row["running"] is True
