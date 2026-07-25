"""The 0009 backfill: today's labs list is entirely rows nobody can retire, because
until now nothing could. Apply the new rule once so the list starts clean."""
import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(
        name="jj-air", workspace=ws, location=Runner.LOCAL, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), paired_by=user,
    )
    return user, ws, runner


def test_backfill_archives_only_stale_runner_sessions():
    from apps.canopy_sessions.staleness import archive_stale_sessions

    user, ws, runner = _ctx()
    fresh = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="live")
    RunnerBinding.objects.create(session=fresh, runner=runner, session_key="live",
                                 live_seen_at=timezone.now())
    stale = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="gone")
    RunnerBinding.objects.create(session=stale, runner=runner, session_key="gone",
                                 live_seen_at=timezone.now() - dt.timedelta(days=9))
    orphan = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="orphan")
    web = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web")

    archive_stale_sessions(Session)

    assert Session.objects.get(pk=fresh.pk).status == Session.ACTIVE
    assert Session.objects.get(pk=stale.pk).status == Session.ARCHIVED
    assert Session.objects.get(pk=orphan.pk).status == Session.ARCHIVED  # no binding = unseen
    assert Session.objects.get(pk=web.pk).status == Session.ACTIVE       # web is exempt
