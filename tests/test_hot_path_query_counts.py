"""Query budgets for the endpoints every box and every page hit constantly.

Labs serves everything from one process. On 2026-10-03 the SLOW_REQUEST log named
the requests stalling it: a runner's session report (~160 queries, ~1.1s, sent by
every box every ~10s), the turn list (2,468 queries, 3s) and a chat send (~300).
Each was a per-row loop of lookups. These tests pin the counts so a new per-row
query in the loop fails here instead of in production.

The budgets are deliberately NOT a function of the row count: a test that
grows its budget with N is the N+1 it exists to catch.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext

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
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mbp", owner=user, workspace=ws
    )
    return user, ws, runner


def _report_queries(runner, ws, n):
    sessions = [_Reported(f"task-{i}") for i in range(n)]
    services.replace_reported_sessions(runner, ws, sessions, complete=True)  # creates
    with CaptureQueriesContext(connection) as q:
        services.replace_reported_sessions(runner, ws, sessions, complete=True)  # the steady state
    return len(q)


def test_a_steady_state_session_report_does_not_query_per_session():
    """The ~10s report from every box. The steady state — every session already
    known — is what runs all day, so that is what is budgeted."""
    _user, ws, runner = _ctx()
    few = _report_queries(runner, ws, 3)
    Runner.objects.filter(pk=runner.pk).update(host="jj-mbp")
    _user2 = User.objects.create_user("jj2", "jj2@dimagi.com", "pw")
    runner2 = Runner.objects.create(name="jj2-mbp", kind=Runner.EMDASH, host="jj2-mbp",
                                    owner=runner.owner, workspace=ws)
    many = _report_queries(runner2, ws, 20)
    # One save per session is the floor while each binding carries its own fresh
    # observation; everything else must be a constant.
    assert many - few <= (20 - 3) * 1, (few, many)


def _turn_list_queries(n_agent, n_chat):
    from django.test import Client

    from apps.agents.models import Agent
    from apps.canopy_sessions.models import Session
    from apps.harness import initiator
    from apps.harness.models import Turn

    tag = f"{n_agent}x{n_chat}"
    owner = User.objects.create_user(f"o{tag}", f"o{tag}@dimagi.com", "pw")
    ws = Workspace.objects.create(slug=f"t{tag}", display_name="T", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    ed = User.objects.create_user(f"e{tag}", f"e{tag}@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=ed, workspace=ws, role=WorkspaceMembership.EDITOR)
    a = Agent.objects.create(slug=f"hal{tag}", name="Hal", workspace=ws, owner=owner)
    by = initiator.system(via="test")
    for i in range(n_agent):
        services.enqueue_turn(initiator=by, agent=a, origin=Turn.ORIGIN_API, idempotency_key=f"a{i}")
    for i in range(n_chat):
        sess = Session.objects.create(agent=a, workspace=ws, created_by=ed, title=f"s{i}")
        services.enqueue_turn(initiator=by, session=sess, origin=Turn.ORIGIN_API, idempotency_key=f"c{i}")
    c = Client()
    c.force_login(ed)  # a member, not an admin: every row is permission-checked
    with CaptureQueriesContext(connection) as q:
        assert c.get(f"/api/w/{ws.slug}/harness/turns/").status_code == 200
    return len(q)


def test_the_turn_list_does_not_query_per_turn():
    # Doubling the page (agent turns AND chat turns, every one permission-checked)
    # must not add queries. Was ~3 a turn: 2,468 for one page on labs.
    _turn_list_queries(1, 1)  # the process's first request warms caches; not the subject
    small = _turn_list_queries(10, 8)
    big = _turn_list_queries(60, 40)  # limit is 100: a full page
    assert big <= small + 2, (small, big)


def test_an_idle_claim_poll_is_cheap_however_big_the_tree():
    """Every box polls `claim` every few seconds and almost always finds nothing.
    It was 72 queries on a labs-shaped tree: a membership walk per workspace to
    find the tenant, and inherited-order resolution for every agent."""
    from django.utils import timezone

    from apps.agents.models import Agent
    from apps.harness.models import WorkspaceRunnerOrder

    def poll(n_kids, tag):
        u = User.objects.create_user(f"p{tag}", f"p{tag}@dimagi.com", "pw")
        root = Workspace.objects.create(slug=f"root{tag}", display_name="R", created_by=u)
        WorkspaceMembership.objects.create(user=u, workspace=root, role=WorkspaceMembership.OWNER)
        kids = [Workspace.objects.create(slug=f"k{tag}{i}", display_name="K", created_by=u, parent=root)
                for i in range(n_kids)]
        box = Runner.objects.create(name=f"b{tag}", kind=Runner.EMDASH, host=f"b{tag}", owner=u,
                                    status=Runner.ONLINE, last_heartbeat_at=timezone.now(), workspace=root)
        WorkspaceRunnerOrder.objects.create(workspace=root, runner=box, rank=0)
        for i, k in enumerate(kids * 2):
            Agent.objects.create(slug=f"ag{tag}{i}", name="A", workspace=k)
        services.claim_next_turn(box)
        with CaptureQueriesContext(connection) as q:
            assert services.claim_next_turn(box) is None
        return len(q)

    small, big = poll(1, "s"), poll(8, "b")
    assert big <= small + 3, (small, big)
    assert big <= 20, big


def test_member_roles_agrees_with_member_role_everywhere():
    """The batched read must give exactly `member_role`'s answer — including an
    inherited ownership outranking a weaker direct row."""
    from apps.workspaces import services as wsvc

    u = User.objects.create_user("r", "r@dimagi.com", "pw")
    org = Workspace.objects.create(slug="org", display_name="O", created_by=u)
    div = Workspace.objects.create(slug="div", display_name="D", created_by=u, parent=org)
    team = Workspace.objects.create(slug="team", display_name="T", created_by=u, parent=div)
    other = Workspace.objects.create(slug="other", display_name="X", created_by=u)
    WorkspaceMembership.objects.create(user=u, workspace=org, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=u, workspace=div, role=WorkspaceMembership.VIEWER)
    WorkspaceMembership.objects.create(user=u, workspace=other, role=WorkspaceMembership.EDITOR)
    Workspace.objects.create(slug="stranger", display_name="S", created_by=u)
    roles = wsvc.member_roles(u)
    for slug in ("org", "div", "team", "other", "stranger"):
        assert roles.get(slug) == wsvc.member_role(u, slug), slug
    assert roles["div"] == WorkspaceMembership.OWNER and roles["team"] == WorkspaceMembership.OWNER
