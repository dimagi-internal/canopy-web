"""The report path upserts a durable Session(origin=runner) + RunnerBinding
per reported emdash session, keyed by (runner, session_key) — plus (host,
session_key) recovery for a binding this runner previously released — replacing
the deleted EmdashSession model."""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from django.contrib.auth import get_user_model

from apps.harness.services import replace_reported_sessions
from apps.harness.models import Runner
from apps.canopy_sessions.models import Session, RunnerBinding
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _reported(task, msgs):
    return SimpleNamespace(
        emdash_task=task, project="canopy-web", status="running",
        last_interacted_at=None, recent_messages=msgs,
    )


def _user():
    return get_user_model().objects.create(username="jj", email="jj@dimagi.com")


def test_report_creates_session_and_binding():
    jj = _user()
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    n = replace_reported_sessions(runner, ws, [_reported("feat-x", [{"role": "assistant", "text": "hi"}])])
    assert n == 1
    b = RunnerBinding.objects.get(runner=runner, session_key="feat-x")
    assert b.session.origin == Session.ORIGIN_RUNNER
    assert b.tail == [{"role": "assistant", "text": "hi"}]


def test_report_is_idempotent_and_updates_tail():
    jj = _user()
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    replace_reported_sessions(runner, ws, [_reported("feat-x", [{"role": "user", "text": "a"}])])
    replace_reported_sessions(runner, ws, [_reported("feat-x", [{"role": "assistant", "text": "b"}])])
    assert Session.objects.filter(runner_binding__session_key="feat-x").count() == 1
    b = RunnerBinding.objects.get(runner=runner, session_key="feat-x")
    assert b.tail == [{"role": "assistant", "text": "b"}]


def test_dropped_session_clears_live_but_keeps_session():
    jj = _user()
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    replace_reported_sessions(runner, ws, [_reported("feat-x", [])])
    replace_reported_sessions(runner, ws, [])  # feat-x no longer open
    b = RunnerBinding.objects.get(session_key="feat-x")
    assert b.runner_id is None       # live pointer cleared
    assert Session.objects.filter(runner_binding=b).exists()  # session kept


def test_a_dropped_session_reappearing_revives_the_same_row():
    """The clear nulls the live FK, so the upsert lookup must not key on it: a task
    that drops off a report and comes back is the SAME session, not a fork. Recovery
    is scoped by host — emdash task names collide across machines."""
    jj = _user()
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    runner = Runner.objects.create(
        name="laptop", workspace=ws, location=Runner.LOCAL, host="jj@air"
    )
    replace_reported_sessions(runner, ws, [_reported("feat-x", [])])
    original = RunnerBinding.objects.get(session_key="feat-x")

    replace_reported_sessions(runner, ws, [])          # dropped: live pointer cleared
    replace_reported_sessions(runner, ws, [_reported("feat-x", [])])  # and back

    assert Session.objects.count() == 1                # no fork
    revived = RunnerBinding.objects.get(session_key="feat-x")
    assert revived.pk == original.pk
    assert revived.session_id == original.session_id
    assert revived.runner_id == runner.id


def test_recovery_of_a_released_binding_is_host_scoped():
    """A DIFFERENT machine reporting the same task name must not claim the released
    row — it gets its own session."""
    jj = _user()
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    air = Runner.objects.create(name="air", workspace=ws, location=Runner.LOCAL, host="jj@air")
    mini = Runner.objects.create(name="mini", workspace=ws, location=Runner.LOCAL, host="jj@mini")

    replace_reported_sessions(air, ws, [_reported("feat-x", [])])
    replace_reported_sessions(air, ws, [])  # air releases it
    replace_reported_sessions(mini, ws, [_reported("feat-x", [])])

    assert Session.objects.count() == 2
    assert RunnerBinding.objects.get(runner=mini, session_key="feat-x").host == "jj@mini"
    assert RunnerBinding.objects.get(host="jj@air", session_key="feat-x").runner_id is None


def test_recovery_does_not_fuse_two_runners_with_blank_host():
    """Two distinct runners that both have host="" (legacy/unheartbeated) each
    report the same task name and then release it. Runner B's fresh report must
    NOT recover runner A's released binding — the null-recovery branch requires a
    non-blank host, so a blank host never matches. Runner B gets its OWN session."""
    jj = _user()
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    a = Runner.objects.create(name="a", workspace=ws, location=Runner.LOCAL, host="")
    b = Runner.objects.create(name="b", workspace=ws, location=Runner.LOCAL, host="")

    replace_reported_sessions(a, ws, [_reported("feat-x", [])])
    binding_a = RunnerBinding.objects.get(runner=a, session_key="feat-x")
    session_a = binding_a.session_id

    replace_reported_sessions(a, ws, [])  # a releases it (runner FK nulled)

    replace_reported_sessions(b, ws, [_reported("feat-x", [])])

    assert Session.objects.count() == 2  # no fusion: b got its own session

    binding_a.refresh_from_db()
    assert binding_a.runner_id is None
    assert binding_a.session_id == session_a  # a's binding/session untouched

    binding_b = RunnerBinding.objects.get(runner=b, session_key="feat-x")
    assert binding_b.pk != binding_a.pk
    assert binding_b.session_id != session_a


def test_list_visible_sessions_maps_to_wire_shape():
    from django.utils import timezone

    from apps.harness.services import list_visible_sessions
    from apps.workspaces.services import ensure_member
    from apps.workspaces.models import WorkspaceMembership

    jj = _user()
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    ensure_member(ws, jj, WorkspaceMembership.OWNER)
    runner = Runner.objects.create(
        name="laptop", workspace=ws, location=Runner.LOCAL,
        status=Runner.ONLINE, paired_by=jj, last_heartbeat_at=timezone.now(),
    )
    replace_reported_sessions(runner, ws, [_reported("feat-x", [{"role": "assistant", "text": "hi"}])])
    rows = list_visible_sessions(jj)
    assert len(rows) == 1
    r = rows[0]
    assert r.emdash_task == "feat-x"
    assert r.recent_messages == [{"role": "assistant", "text": "hi"}]
    assert r.runner_name == "laptop"
