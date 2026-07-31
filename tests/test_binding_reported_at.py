"""`reported_at` answers ONE question: is a runner reporting an emdash task for
this session? It exists because no other field can — `record_session` is called by
BOTH runners and stamps `session_key` and `live_seen_at`, so a cloud binding is
indistinguishable from a laptop one by either."""
import pytest
from django.contrib.auth.models import User

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness import services
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


class _Reported:
    """Duck-types ReportedSessionIn — services reads attributes, not dict keys."""

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
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mbp", paired_by=user, workspace=ws
    )
    return user, ws, runner


def test_the_report_loop_stamps_reported_at():
    _user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd")])
    binding = RunnerBinding.objects.get(session_key="ddd")
    assert binding.reported_at is not None


def test_record_session_stamps_live_seen_at_but_not_reported_at():
    """The whole point of the field. `record_session` is the CLOUD runner's only
    binding write, so if it stamped this, a cloud session would take the local
    branch and canopy would relay a close to a box with no emdash to close."""
    user, ws, runner = _ctx()
    session = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="cloud chat"
    )
    binding = RunnerBinding.objects.create(
        session=session, runner=runner, host=runner.host, session_key="", thread_key=str(session.id)
    )
    services.record_session(
        agent=None, thread_key=str(session.id), runner=runner, project="canopy-web",
        workspace=ws, emdash_task_id="0d6f2c1e-1111-2222-3333-444455556666",
    )
    binding.refresh_from_db()
    assert binding.live_seen_at is not None
    assert binding.reported_at is None
