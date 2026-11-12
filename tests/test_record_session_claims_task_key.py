"""`record_session` claims its emdash task even when the report loop filed it first.

A laptop resumes an email thread in an emdash task. If the report loop sees that
task before `record_session` reaches the server, it files the task as a NEW session
with its own binding holding the task's (runner, project, name). Every later
`record_session` for the thread then tried to give the same key to the thread's
binding and hit `one_binding_per_runner_project_session_key`: a 500, retried every
few seconds for hours. The runner's `record_session` is the authoritative "this task
serves this thread", so the key moves to the thread and the stray row lets go of it.
"""
import pytest
from django.contrib.auth.models import User

from apps.canopy_sessions.models import RunnerBinding
from apps.harness import session_sync
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


class _Reported:
    def __init__(self, task, project="canopy-web"):
        self.emdash_task = task
        self.project = project
        self.status = ""
        self.last_interacted_at = None
        self.recent_messages = []


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mbp", owner=user, workspace=ws
    )
    return ws, runner


def _record(ws, runner, key):
    return session_sync.record_session(
        None, "thread-d718", runner=runner, project="canopy-web", workspace=ws, session_key=key,
    )


def test_record_session_takes_the_key_from_a_row_the_report_loop_filed_first():
    ws, runner = _ctx()
    thread = _record(ws, runner, "")
    session_sync.replace_reported_sessions(runner, ws, [_Reported("c-demo-d718")])
    stray = RunnerBinding.objects.get(session_key="c-demo-d718")
    assert stray.pk != thread.pk

    claimed = _record(ws, runner, "c-demo-d718")

    assert claimed.pk == thread.pk
    assert RunnerBinding.objects.get(pk=thread.pk).session_key == "c-demo-d718"
    assert RunnerBinding.objects.get(pk=stray.pk).session_key == ""
    # And the next report finds the thread's row, not a new one.
    session_sync.replace_reported_sessions(runner, ws, [_Reported("c-demo-d718")])
    assert list(RunnerBinding.objects.filter(session_key="c-demo-d718").values_list("pk", flat=True)) == [thread.pk]


def test_a_namesake_in_another_project_is_left_alone():
    ws, runner = _ctx()
    _record(ws, runner, "")
    session_sync.replace_reported_sessions(runner, ws, [_Reported("c-demo-d718", project="ace")])
    other = RunnerBinding.objects.get(session_key="c-demo-d718")

    _record(ws, runner, "c-demo-d718")

    assert RunnerBinding.objects.get(pk=other.pk).session_key == "c-demo-d718"
