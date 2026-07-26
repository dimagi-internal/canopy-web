"""A queued turn nothing can claim must be LOUD, not silent.

Observed: a `project=ace` turn sat QUEUED for 12 hours because every online
runner declared `projects: ['canopy-web']`. `enqueue_turn` accepted it happily
and nothing ever said the turn was unrunnable.

The detector shares `runner_target_q` with `claim_next_turn`, so "can anyone run
this?" cannot disagree with what claiming actually does.
"""
import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx(*, agents=(), projects=(), sessions=False, online=True):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    Runner.objects.create(
        name="jj-mbp", workspace=ws, location=Runner.LOCAL, paired_by=user, host="jj@mbp",
        status=Runner.ONLINE if online else Runner.DISCONNECTED,
        last_heartbeat_at=timezone.now() if online else None,
        capabilities={"agents": list(agents), "projects": list(projects), "sessions": sessions},
    )
    return user, ws


def _project_turn(ws, project, key="k1", *, aged=True):
    t = services.enqueue_turn(
        project=project, workspace=ws, origin=Turn.ORIGIN_API,
        idempotency_key=key, prompt="Go",
    )[0]
    if aged:
        _age(t)
    return t


def _age(turn):
    """Push a turn past UNCLAIMABLE_GRACE — a just-enqueued turn is NOT stuck."""
    Turn.objects.filter(pk=turn.pk).update(
        created_at=timezone.now() - services.UNCLAIMABLE_GRACE - dt.timedelta(seconds=5)
    )


def test_flags_a_project_turn_no_runner_declares():
    """The exact 12-hour stall."""
    user, ws = _ctx(agents=["ace"], projects=["canopy-web"])
    _project_turn(ws, "ace")
    rows = services.unclaimable_queued_turns(user)
    assert len(rows) == 1
    assert rows[0]["target"] == "project ace"
    assert "declares the repo 'ace'" in rows[0]["reason"]
    assert rows[0]["prompt"] == "Go"


def test_silent_when_the_runner_declares_the_repo():
    user, ws = _ctx(agents=["ace"], projects=["canopy-web", "ace"])
    _project_turn(ws, "ace")
    assert services.unclaimable_queued_turns(user) == []


def test_flags_an_agent_turn_no_runner_declares():
    user, ws = _ctx(agents=["ace"], projects=["canopy-web"])
    other = Agent.objects.create(slug="ghost", name="Ghost", workspace=ws)
    _age(services.enqueue_turn(agent=other, origin=Turn.ORIGIN_API, idempotency_key="k2", prompt="hi")[0])
    rows = services.unclaimable_queued_turns(user)
    assert [r["target"] for r in rows] == ["agent ghost"]


def test_an_offline_runner_does_not_count_as_coverage():
    """A declared-but-dead runner must not mask the stall."""
    user, ws = _ctx(agents=["ace"], projects=["ace"], online=False)
    _project_turn(ws, "ace")
    rows = services.unclaimable_queued_turns(user)
    assert [r["target"] for r in rows] == ["project ace"]


def test_a_degraded_runner_does_count():
    """DEGRADED = CDP down, still polling and still able to claim once CDP returns."""
    user, ws = _ctx(agents=["ace"], projects=["ace"])
    Runner.objects.update(status=Runner.DEGRADED)
    _project_turn(ws, "ace")
    assert services.unclaimable_queued_turns(user) == []


def test_session_turns_need_a_session_capable_runner():
    from apps.canopy_sessions.models import Session
    user, ws = _ctx(agents=["ace"], projects=["canopy-web"], sessions=False)
    s = Session.objects.create(workspace=ws, created_by=user, title="chat")
    _age(services.enqueue_turn(session=s, origin=Turn.ORIGIN_API, idempotency_key="k3", prompt="hi")[0])
    assert [r["target"] for r in services.unclaimable_queued_turns(user)] == ["session"]

    Runner.objects.update(capabilities={"agents": [], "projects": [], "sessions": True})
    assert services.unclaimable_queued_turns(user) == []


def test_endpoint_returns_the_rows(client):
    user, ws = _ctx(agents=["ace"], projects=["canopy-web"])
    _project_turn(ws, "ace")
    client.force_login(user)
    body = client.get("/api/harness/turns/unclaimable").json()
    assert [r["target"] for r in body] == ["project ace"]


# --- false positives that fired on healthy traffic ------------------------

def test_a_just_enqueued_turn_is_not_stuck():
    """Every normal send is queued for a few seconds while a runner polls.

    Regression: a phone chat send was flagged instantly, then claimed and answered
    seconds later — the banner cried wolf on healthy traffic.
    """
    user, ws = _ctx(agents=["ace"], projects=["canopy-web"])
    _project_turn(ws, "ace", aged=False)
    assert services.unclaimable_queued_turns(user) == []


def test_a_stale_heartbeat_reports_OFFLINE_not_misconfiguration():
    """A flaky laptop network (555 DNS failures in 3h) lapses the heartbeat, so the
    runner reads STALE. That is transient — it must not look like a config error."""
    user, ws = _ctx(agents=["ace"], projects=["ace"])          # runner DOES declare it
    Runner.objects.update(last_heartbeat_at=timezone.now() - dt.timedelta(minutes=10))
    _project_turn(ws, "ace")
    rows = services.unclaimable_queued_turns(user)
    assert len(rows) == 1
    assert rows[0]["kind"] == "offline"
    assert "none are reachable" in rows[0]["reason"]


def test_a_genuinely_undeclared_target_still_reports_CONFIG():
    user, ws = _ctx(agents=["ace"], projects=["canopy-web"])
    _project_turn(ws, "ace")
    rows = services.unclaimable_queued_turns(user)
    assert rows[0]["kind"] == "config"
    assert rows[0]["reason"] == "no runner declares the repo 'ace'"


# ── Directed routing (RunnerAssignment) coverage — spec 2026-07-24 ─────────────
# The detector shares runner_target_q with claim_next_turn, which now routes
# agent turns by RunnerAssignment, NOT capabilities.agents. These pin the two
# sides of that: declared-but-unassigned is a CONFIG problem (claiming would
# never happen), and assigned-but-offline is an OFFLINE problem.


def test_capabilities_without_assignment_reports_CONFIG():
    """A runner that merely DECLARES the agent in capabilities no longer covers
    it — assignment is the routing authority, and the warning must agree."""
    from apps.agents.models import Agent as _Agent

    user, ws = _ctx(agents=["ace"], projects=["canopy-web"])
    ace = _Agent.objects.create(slug="ace", name="Ace", workspace=ws)
    _age(services.enqueue_turn(agent=ace, origin=Turn.ORIGIN_API, idempotency_key="ka1", prompt="hi")[0])
    rows = services.unclaimable_queued_turns(user)
    assert [r["kind"] for r in rows] == ["config"]
    assert "is assigned the agent 'ace'" in rows[0]["reason"]


def test_assignment_with_offline_runner_reports_OFFLINE():
    from apps.agents.models import Agent as _Agent
    from apps.harness.models import RunnerAssignment

    user, ws = _ctx(agents=[], projects=[], online=False)
    ace = _Agent.objects.create(slug="ace", name="Ace", workspace=ws)
    RunnerAssignment.objects.create(agent=ace, runner=Runner.objects.get(), rank=0)
    _age(services.enqueue_turn(agent=ace, origin=Turn.ORIGIN_API, idempotency_key="ka2", prompt="hi")[0])
    rows = services.unclaimable_queued_turns(user)
    assert [r["kind"] for r in rows] == ["offline"]


# ── Tenant scope for "could ANY runner take this?" ─────────────────────────
# Regression: scoping candidate runners to `paired_by=user` made every stuck
# turn read CONFIG for anyone who didn't personally pair a runner — a
# delegated identity, or a teammate in a workspace someone ELSE's runner
# serves. The candidate set must match the tenancy rule the rest of this file
# already uses elsewhere (`runner_tenant_slugs`, paired_by-derived).


def test_teammates_runner_counts_even_if_caller_paired_none():
    """user2 paired no runners at all, but shares a workspace with user1 who
    paired one (currently offline, assigned to the agent). The turn must read
    OFFLINE — "a runner could take this, wait" — not CONFIG — "no runner is
    assigned, fix your routing"."""
    from apps.agents.models import Agent as _Agent
    from apps.harness.models import RunnerAssignment

    user1 = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    user2 = User.objects.create_user("teammate", "teammate@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user1)
    WorkspaceMembership.objects.create(user=user1, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=user2, workspace=ws, role=WorkspaceMembership.EDITOR)
    runner = Runner.objects.create(
        name="jj-mbp", workspace=ws, location=Runner.LOCAL, paired_by=user1, host="jj@mbp",
        status=Runner.DISCONNECTED, last_heartbeat_at=None,
        capabilities={"agents": [], "projects": [], "sessions": False},
    )
    ace = _Agent.objects.create(slug="ace", name="Ace", workspace=ws)
    RunnerAssignment.objects.create(agent=ace, runner=runner, rank=0)
    _age(services.enqueue_turn(agent=ace, origin=Turn.ORIGIN_API, idempotency_key="kteam", prompt="hi")[0])

    rows = services.unclaimable_queued_turns(user2)
    assert [r["kind"] for r in rows] == ["offline"]


def test_genuinely_no_runner_in_the_tenant_still_reports_CONFIG():
    """Sanity check the other direction: if nothing in the caller's tenant
    could EVER take the turn, it's still CONFIG, not spuriously OFFLINE just
    because the tenant scope widened."""
    from apps.agents.models import Agent as _Agent

    user = User.objects.create_user("solo", "solo@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w2", display_name="W2", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    ghost = _Agent.objects.create(slug="ghost2", name="Ghost2", workspace=ws)
    _age(services.enqueue_turn(agent=ghost, origin=Turn.ORIGIN_API, idempotency_key="ksolo", prompt="hi")[0])

    rows = services.unclaimable_queued_turns(user)
    assert [r["kind"] for r in rows] == ["config"]
