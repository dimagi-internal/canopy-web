"""Repo targets: a Turn addresses EITHER an agent or a repo.

The session you want to revise from the phone is working on canopy-web — a repo.
Of 22 emdash projects roughly 5 are agents, and `cdp_control.create_task` was
always project-generic; only the data model insisted on an agent.
"""
from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.agents.models import Agent
from apps.harness.models import Turn
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _ws(slug="canopy"):
    from django.contrib.auth import get_user_model

    u = get_user_model().objects.create_user(username=f"u-{slug}", email=f"{slug}@d.com")
    return Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=u)


def _agent(slug="echo", ws=None):
    from apps.workspaces.testing import a_workspace

    return Agent.objects.create(slug=slug, name=slug.title(), workspace=ws or a_workspace())


def test_a_turn_cannot_target_both_an_agent_and_a_project():
    with pytest.raises(IntegrityError):
        Turn.objects.create(
            agent=_agent(), project="canopy-web",
            origin=Turn.ORIGIN_MANUAL, idempotency_key="both",
        )


def test_a_turn_must_target_something():
    with pytest.raises(IntegrityError):
        Turn.objects.create(origin=Turn.ORIGIN_MANUAL, idempotency_key="neither")


def test_a_project_turn_needs_no_agent():
    t = Turn.objects.create(
        project="canopy-web", workspace=_ws(),
        origin=Turn.ORIGIN_MANUAL, idempotency_key="p1", prompt="fix the header",
    )
    assert t.agent_id is None
    assert t.target == "canopy-web"
    assert t.status == Turn.QUEUED


def test_two_project_turns_for_the_same_repo_both_execute():
    """The reason a pseudo-agent-per-repo was rejected, and the reason
    one_executing_turn_per_agent stays agent-only.

    emdash gives every task its own worktree, so repo work is MEANT to
    parallelize. This passes because agent is NULL on both rows and NULLs never
    compare equal in a UniqueConstraint — load-bearing behaviour, hence a test
    rather than a comment.
    """
    ws = _ws()
    Turn.objects.create(
        project="canopy-web", workspace=ws, origin=Turn.ORIGIN_MANUAL,
        idempotency_key="p1", status=Turn.RUNNING,
    )
    Turn.objects.create(  # must not raise
        project="canopy-web", workspace=ws, origin=Turn.ORIGIN_MANUAL,
        idempotency_key="p2", status=Turn.RUNNING,
    )
    assert Turn.objects.filter(project="canopy-web", status=Turn.RUNNING).count() == 2


def test_one_executing_turn_per_agent_still_holds_for_agents():
    """The negative half of the test above: widening the model must not have
    loosened the agent invariant it was protecting."""
    a = _agent()
    Turn.objects.create(
        agent=a, origin=Turn.ORIGIN_BOARD, idempotency_key="a1", status=Turn.RUNNING
    )
    with pytest.raises(IntegrityError):
        Turn.objects.create(
            agent=a, origin=Turn.ORIGIN_BOARD, idempotency_key="a2", status=Turn.RUNNING
        )


def test_str_does_not_crash_on_a_project_turn():
    """__str__ read self.agent.slug unconditionally before agent could be NULL."""
    t = Turn.objects.create(
        project="canopy-web", workspace=_ws(), origin=Turn.ORIGIN_MANUAL,
        idempotency_key="p1",
    )
    assert "canopy-web" in str(t)


def test_target_prefers_the_agent_for_agent_turns():
    t = Turn.objects.create(
        agent=_agent("hal"), origin=Turn.ORIGIN_BOARD, idempotency_key="a1"
    )
    assert t.target == "hal"


def test_turn_out_serializes_a_project_turn_without_dereferencing_a_null_agent():
    """TurnOut.resolve_agent_slug read obj.agent.slug unconditionally. The moment
    agent could be NULL that became a 500 on every project turn."""
    from apps.harness.schemas import TurnOut

    t = Turn.objects.create(
        project="canopy-web", workspace=_ws(), origin=Turn.ORIGIN_MANUAL,
        idempotency_key="p1", prompt="fix the header",
    )
    out = TurnOut.from_orm(t)
    assert out.agent_slug is None
    assert out.project == "canopy-web"
    assert out.target == "canopy-web"


def test_turn_out_still_serializes_an_agent_turn():
    from apps.harness.schemas import TurnOut

    out = TurnOut.from_orm(
        Turn.objects.create(agent=_agent("hal"), origin=Turn.ORIGIN_BOARD, idempotency_key="a1")
    )
    assert out.agent_slug == "hal"
    assert out.project == ""
    assert out.target == "hal"


# ---- RunnerBinding-backed reuse: the same agent-XOR-project treatment ----
#
# The direct SessionLink model-constraint tests that used to live here
# (two-link-for-the-same-thread rejected, thread-key-collision-across-projects
# is fine, workspace-scoped tenant isolation, the CheckConstraint pairs) tested
# a DB shape that no longer exists: RunnerBinding has no unique-per-thread_key
# constraint at all — thread identity is enforced at the SERVICE layer
# (`_binding_for_thread` finds-before-creates), not by a DB constraint, because
# a binding's real identity is its OneToOne `session`, not `thread_key`. Those
# constraint tests are pruned rather than ported onto a schema they don't
# apply to; the underlying BEHAVIOUR they protected (idempotent-per-thread,
# workspace-scoped project reuse) is covered by
# tests/test_harness_session_reuse.py::test_record_is_idempotent_per_thread and
# ::test_project_reuse_is_workspace_scoped.


def test_record_then_resolve_a_project_session_reuses_it():
    """The whole point of Phase 3's session input: the phone owns a persistent
    thread per target, so message 2 must resolve to REUSE rather than fork a new
    Claude session. Nothing new is built for this — a stable thread_key lands on
    the existing reuse path."""
    from apps.harness import services
    from apps.harness.models import Runner

    ws = _ws()
    runner = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mac",
        capabilities={"projects": ["canopy-web"]},
    )
    services.record_session(
        None, "phone:jj:canopy-web", runner=runner, project="canopy-web", workspace=ws,
        emdash_task_id="task-1", session_id="sess-1",
    )
    plan = services.resolve_session(
        None, "phone:jj:canopy-web", runner, project="canopy-web", workspace=ws
    )

    assert plan["reuse"] is True
    assert plan["emdash_task_id"] == "task-1"
    assert plan["new_thread"] is False


def test_resolving_a_project_thread_in_the_wrong_workspace_finds_nothing():
    """The tenant gate. Even holding the exact thread_key, a runner scoped to a
    DIFFERENT workspace resolves new_thread — never the victim's summary/task."""
    from apps.harness import services
    from apps.harness.models import Runner

    owner_ws, other_ws = _ws("canopy"), _ws("dimagi")
    r = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, host="jj-mac")
    services.record_session(
        None, "phone:jj:canopy-web", runner=r, project="canopy-web", workspace=owner_ws,
        emdash_task_id="secret-task", summary="secret context",
    )
    plan = services.resolve_session(
        None, "phone:jj:canopy-web", r, project="canopy-web", workspace=other_ws
    )

    assert plan["new_thread"] is True
    assert plan["emdash_task_id"] == ""
    assert plan["summary"] == ""


def test_recording_the_same_project_thread_twice_updates_one_link():
    """get_or_create must find the existing row, not trip the unique constraint
    or spawn a second link (which would mean a second session)."""
    from apps.canopy_sessions.models import RunnerBinding
    from apps.harness import services
    from apps.harness.models import Runner

    ws = _ws()
    r = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, host="jj-mac")
    services.record_session(None, "phone:jj:canopy-web", runner=r, project="canopy-web",
                            workspace=ws, emdash_task_id="task-1")
    services.record_session(None, "phone:jj:canopy-web", runner=r, project="canopy-web",
                            workspace=ws, emdash_task_id="task-2")

    assert RunnerBinding.objects.filter(session__project="canopy-web").count() == 1
    assert RunnerBinding.objects.get(session__project="canopy-web").session_key == "task-2"


def test_a_session_link_target_must_be_exactly_one_thing():
    from apps.harness import services
    from apps.harness.models import Runner

    r = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH)
    with pytest.raises(ValueError):
        services.resolve_session(_agent(), "t", r, project="canopy-web")
    with pytest.raises(ValueError):
        services.resolve_session(None, "t", r)
