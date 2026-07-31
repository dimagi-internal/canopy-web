"""Closing a session. Two branches on ONE question — is a runner reporting an
emdash task for this session? — because a server-only archive does not survive
a local session: replace_reported_sessions un-archives anything re-reported as
open, and the runner re-reports every ~10s."""
import pytest
from unittest.mock import patch
from django.contrib.auth.models import User
from django.utils import timezone

from apps.canopy_sessions import services
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    return user, ws


def _runner(user, ws, *, status=Runner.ONLINE, paused=False):
    return Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mbp", paired_by=user, workspace=ws,
        status=status, last_heartbeat_at=timezone.now(), paused=paused,
    )


def _local_session(user, ws, runner, *, key="ddd"):
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_RUNNER, title=key
    )
    RunnerBinding.objects.create(
        session=s, runner=runner, host=runner.host, session_key=key,
        thread_key=f"emdash:{key}", live_seen_at=timezone.now(),
        reported_at=timezone.now(),
    )
    return s


def _turn(session, *, key, status=Turn.QUEUED):
    return Turn.objects.create(
        chat_session=session, status=status, prompt="hi",
        origin=Turn.ORIGIN_API, idempotency_key=key,
    )


def test_a_reported_session_relays_and_writes_nothing():
    """The emdash task is the truth for a local session. Writing here would make
    canopy a second source of truth that the next report can disagree with."""
    user, ws = _ctx()
    s = _local_session(user, ws, _runner(user, ws))
    with patch("apps.realtime.groups.publish") as pub:
        assert services.close_session(session=s) == "closing"
    s.refresh_from_db()
    assert s.status == Session.ACTIVE
    frame = pub.call_args[0][1]
    assert frame["type"] == "runner.close_session"
    assert frame["session_key"] == "ddd"
    assert frame["session_id"] == str(s.id)


def test_an_unreported_session_archives_here_and_sticks():
    """Cloud sessions and never-bound web chats. Nothing on a box to delete, and
    nothing will ever report them back, so the write is safe and final."""
    user, ws = _ctx()
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="cloud chat"
    )
    with patch("apps.realtime.groups.publish") as pub:
        assert services.close_session(session=s) == "closed"
    s.refresh_from_db()
    assert s.status == Session.ARCHIVED
    assert pub.call_count == 0


def test_a_cloud_binding_takes_the_unreported_branch():
    """A cloud runner calls record_session too, so the binding exists and carries a
    session_key. reported_at is what tells them apart."""
    user, ws = _ctx()
    runner = _runner(user, ws)
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="cloud chat"
    )
    RunnerBinding.objects.create(
        session=s, runner=runner, host=runner.host,
        session_key="0d6f2c1e-1111-2222-3333-444455556666",
        thread_key=str(s.id), live_seen_at=timezone.now(), reported_at=None,
    )
    with patch("apps.realtime.groups.publish") as pub:
        assert services.close_session(session=s) == "closed"
    assert pub.call_count == 0


def test_a_stale_report_takes_the_unreported_branch():
    """Reported three minutes ago is not reported now. Relaying to a box that is not
    listening would archive nothing and report success."""
    user, ws = _ctx()
    runner = _runner(user, ws)
    s = _local_session(user, ws, runner)
    binding = s.runner_binding
    binding.reported_at = timezone.now() - timezone.timedelta(hours=1)
    binding.save(update_fields=["reported_at"])
    with patch("apps.realtime.groups.publish") as pub:
        assert services.close_session(session=s) == "closed"
    assert pub.call_count == 0


def test_an_unreachable_runner_refuses_up_front():
    """Never queue a close. A close that sits until a box comes back is
    indistinguishable from one that worked."""
    user, ws = _ctx()
    runner = _runner(user, ws, paused=True)
    s = _local_session(user, ws, runner)
    with patch("apps.realtime.groups.publish") as pub:
        assert services.close_session(session=s) == "unavailable"
    s.refresh_from_db()
    assert s.status == Session.ACTIVE
    assert pub.call_count == 0


def test_already_archived_is_a_refusal_not_a_second_close():
    user, ws = _ctx()
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB,
        title="done", status=Session.ARCHIVED,
    )
    assert services.close_session(session=s) == "already_closed"


def test_the_unreported_branch_cancels_non_terminal_turns():
    """A queued turn on a closed session would wake it up again."""
    user, ws = _ctx()
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="cloud chat"
    )
    turn = _turn(s, key="k-unreported-1")
    services.close_session(session=s)
    turn.refresh_from_db()
    assert turn.status not in Turn.NON_TERMINAL


def test_the_reported_branch_cancels_turns_too_before_relaying():
    """Deleting the emdash task kills the process the turn is running in. Cancel
    first or canopy is left holding an EXECUTING turn whose runner will never
    finish it — it sits until the lease sweep, wedging the agent via
    one_executing_turn_per_agent."""
    user, ws = _ctx()
    s = _local_session(user, ws, _runner(user, ws))
    turn = _turn(s, key="k-reported-1")
    with patch("apps.realtime.groups.publish"):
        assert services.close_session(session=s) == "closing"
    turn.refresh_from_db()
    assert turn.status not in Turn.NON_TERMINAL
    s.refresh_from_db()
    assert s.status == Session.ACTIVE   # still not archived here — the report does that


def test_a_closed_name_never_retires_a_still_open_namesake():
    """emdash task names are not unique. The closing signal this feature finally
    produces must not retire a DIFFERENT, still-open task that happens to share a
    name — `now_keys` wins over `archived` (apps/harness/services.py)."""
    from apps.harness import services as harness_services

    user, ws = _ctx()
    runner = _runner(user, ws)

    class _Reported:
        def __init__(self, task):
            self.emdash_task = task
            self.project = "canopy-web"
            self.status = ""
            self.last_interacted_at = None
            self.recent_messages = []

    harness_services.replace_reported_sessions(runner, ws, [_Reported("ddd")])
    # The runner deleted one "ddd" and re-reports another still open under the
    # same name in the SAME wholesale call.
    harness_services.replace_reported_sessions(
        runner, ws, [_Reported("ddd")], archived=["ddd"]
    )
    assert Session.objects.get(runner_binding__session_key="ddd").status == Session.ACTIVE
