import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.contrib.auth import get_user_model
from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, WorkspaceRunnerOrder
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

def test_count():
    jj = get_user_model().objects.create_user(username="jj", email="jj@x.com")
    d = Workspace.objects.create(slug="dimagi", display_name="D", created_by=jj)
    WorkspaceMembership.objects.create(workspace=d, user=jj, role="owner")
    kids = [Workspace.objects.create(slug=s, display_name=s, created_by=jj, parent=d) for s in ("connect","commcare","strategy","ops","gs")]
    boxes = [Runner.objects.create(name=n, kind=Runner.EMDASH, host=n, owner=jj, status=Runner.ONLINE, last_heartbeat_at=timezone.now(), workspace=d, capabilities={"projects":["hal","ada","ace","eva"]}) for n in ("jj","hal","ace","cloud")]
    for i,r in enumerate(boxes[:3]): WorkspaceRunnerOrder.objects.create(workspace=d, runner=r, rank=i)
    for i in range(9):
        Agent.objects.create(slug=f"a{i}", name=f"A{i}", workspace=kids[i % 5])
    with CaptureQueriesContext(connection) as q:
        services.claim_next_turn(boxes[1])
    print("CLAIM QUERIES", len(q))
    with CaptureQueriesContext(connection) as q:
        services.agents_following_runner(boxes[1])
    print("FOLLOWING QUERIES", len(q))
