"""Every chat-session shape and every kind of person who might knock on it — the
world `tests/test_session_acl.py` and `tests/test_session_audience.py` both judge."""
from __future__ import annotations

from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import CoachingOptIn, CoachingRole, RunnerBinding, Session, SessionParticipant
from apps.harness.models import Runner, RunnerAdmin
from apps.workspaces.models import Workspace, WorkspaceMembership


def grant_coaching(user, agent):
    """Opt `user` into coaching and make `agent` a coach for its own workspace (and below): the
    program-level shape that replaced one grant per agent. Returns the opt-in."""
    from apps.canopy_sessions.models import CoachingOptIn, CoachingRole

    opt, _ = CoachingOptIn.objects.get_or_create(user=user, revoked_at=None)
    CoachingRole.objects.get_or_create(workspace_id=agent.workspace_id, agent=agent, revoked_at=None)
    return opt


def make_user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


def make_member(user, ws, role=WorkspaceMembership.EDITOR):
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=role)


def session_world():
    """Every session shape, and every kind of person who might knock.

    `owner` owns the workspace AND the laptop `r`; `mate` is a plain editor with a
    laptop of their own; `box_admin` was made an admin of `r`; `hal_login` is an
    agent's own login (an editor member, like every agent login); `coach` is
    Trellis's login, which `owner` let review their sessions (leg 5)."""
    owner, mate, viewer, outsider = make_user("owner"), make_user("mate"), make_user("viewer"), make_user("outsider")
    box_admin = make_user("box_admin")
    ws = Workspace.objects.create(slug="acme", display_name="Acme", created_by=owner)
    make_member(owner, ws, WorkspaceMembership.OWNER)
    make_member(mate, ws)
    make_member(viewer, ws)
    make_member(box_admin, ws)
    agent = Agent.objects.create(slug="hal", name="Hal", owner=owner, workspace=ws)
    agent.user = make_user("hal_login")
    agent.save(update_fields=["user"])
    make_member(agent.user, ws)
    trellis = Agent.objects.create(slug="trellis", name="Trellis", owner=owner, workspace=ws)
    trellis.user = make_user("coach")
    trellis.save(update_fields=["user"])
    make_member(trellis.user, ws)
    CoachingOptIn.objects.create(user=owner)
    CoachingRole.objects.create(workspace=ws, agent=trellis)
    runner = Runner.objects.create(name="r", kind=Runner.EMDASH, host="h", owner=owner,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    RunnerAdmin.objects.create(runner=runner, user=box_admin, granted_by=owner)
    mates_box = Runner.objects.create(name="m", kind=Runner.EMDASH, host="m", owner=mate,
                                      status=Runner.ONLINE, last_heartbeat_at=timezone.now())

    def bound(session, key, box=runner):
        RunnerBinding.objects.create(session=session, runner=box, session_key=key,
                                     live_seen_at=timezone.now())
        return session

    shapes = {
        "own_web": Session.objects.create(workspace=ws, created_by=owner, origin=Session.ORIGIN_WEB),
        "shared_web": Session.objects.create(workspace=ws, created_by=owner, origin=Session.ORIGIN_WEB),
        "discovered": bound(Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER), "d"),
        "mates_laptop": bound(Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER), "ml",
                              mates_box),
        "agent_thread": Session.objects.create(workspace=ws, agent=agent, origin=Session.ORIGIN_RUNNER),
        "bound_web": bound(Session.objects.create(workspace=ws, created_by=owner,
                                                  origin=Session.ORIGIN_WEB), "b"),
        "unbound_discovered": Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER),
    }
    SessionParticipant.objects.create(session=shapes["own_web"], user=owner, role=SessionParticipant.OWNER)
    SessionParticipant.objects.create(session=shapes["shared_web"], user=viewer,
                                      role=SessionParticipant.VIEWER)
    _brain_shapes(shapes, ws, owner, mate, agent)
    people = {"owner": owner, "mate": mate, "viewer": viewer, "outsider": outsider,
              "box_admin": box_admin, "hal_login": agent.user, "coach": trellis.user}
    return people, shapes, ws


def _brain_shapes(shapes, ws, owner, mate, agent):
    """The brain's project leg (inert unless `BRAIN_READS_ENABLED`): a chat tied to a workspace project
    whose people both agreed to project learning, one where a participant did not, and one tied only by a
    weak (candidate) link. Tests that judge every shape run with the gate on and off."""
    from apps.agents.models import AgentProject
    from apps.brain import associate, sources
    from apps.brain.models import LearningConsent

    project = AgentProject.objects.create(workspace=ws, handle="brain-p", name="Brain P", agent=agent)
    for u in (owner, mate):   # directly: `consent.set_consent` would also move coaching's opt-in
        LearningConsent.objects.create(user=u, classes=["project"])
    agreed = Session.objects.create(workspace=ws, created_by=mate, origin=Session.ORIGIN_WEB, title="project chat")
    SessionParticipant.objects.create(session=agreed, user=owner, role=SessionParticipant.EDITOR)
    mixed = Session.objects.create(workspace=ws, created_by=mate, origin=Session.ORIGIN_WEB, title="not everyone agreed")
    weak = Session.objects.create(workspace=ws, created_by=mate, origin=Session.ORIGIN_WEB, title="a candidate")
    SessionParticipant.objects.create(session=mixed, user=agent.user, role=SessionParticipant.VIEWER)  # an agent: not a person
    other = User.objects.create_user("undecided", "undecided@dimagi.com", "pw")
    make_member(other, ws)
    SessionParticipant.objects.create(session=mixed, user=other, role=SessionParticipant.EDITOR)
    for s, tier in ((agreed, "core"), (mixed, "core"), (weak, "candidate")):
        associate.put(sources.for_session(s), project, how="signals", tier=tier)
    shapes.update({"brain_project": agreed, "brain_not_everyone_agreed": mixed, "brain_candidate_only": weak})
