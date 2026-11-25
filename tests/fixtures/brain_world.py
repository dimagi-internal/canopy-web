"""A small world for the brain's tests: one workspace, three people, one session two of them are in. Its workspace
has "Learning reads" on (`reads`), so what a test judges is consent; the setting's own tests turn it off."""
from __future__ import annotations

import datetime as dt
import itertools

from django.contrib.auth.models import User
from django.utils import timezone

from apps.brain import consent, learning_reads, registry, sources, targets
from apps.brain.models import BrainCapability, BrainRun
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.workspaces.models import Workspace, WorkspaceMembership


def user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


def member(u, ws, role=WorkspaceMembership.EDITOR):
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=role)


def reads(ws, on: bool | None = True, by=None):
    """Set a workspace's "Learning reads" (None: inherit)."""
    learning_reads.set_reads(ws, on, by=by)


def aim(cap, workspace, project=None, by=None, **kw):
    """Aim a learner at a workspace (or a project of it): a target with its own schedule (`targets.add`)."""
    return targets.add(cap, workspace=workspace, project=project, by=by or cap.registered_by, **kw)


_slots = itertools.count(1)


def fire(*aimed) -> list:
    """Fire each target's schedule at a new slot, as canopy's clock does; the runs that started."""
    from apps.harness import schedule_turns

    out = []
    for t in aimed:
        slot = (timezone.now() + dt.timedelta(minutes=next(_slots))).replace(second=0, microsecond=0)
        t.schedule.refresh_from_db()
        schedule_turns.fire_schedule(t.schedule, slot)
        run = BrainRun.objects.filter(target=t, slot=slot).first()
        if run is not None:
            out.append(run)
    return out


def capability(slug, cls, purposes=("learn",), enabled=True, **kw):
    kw.setdefault("audience", list(registry.CLASS_AUDIENCE[cls]))        # as `registry.register` gives it
    return BrainCapability.objects.create(slug=slug, name=slug, purpose_class=cls, purposes=list(purposes),
                                          enabled=enabled, subject_type="project" if cls == "project" else "person",
                                          source_kinds=["session"], **kw)


def brain_world():
    registry.ensure_builtin()          # what migration 0005 seeds; a database built without migrations has no row
    owner = user("jj")
    ws = Workspace.objects.create(slug="acme", display_name="Acme", created_by=owner)
    member(owner, ws, WorkspaceMembership.OWNER)
    reads(ws)
    alice, bob, carol = user("alice"), user("bob"), user("carol")
    for u in (alice, bob, carol):
        member(u, ws)
    session = Session.objects.create(workspace=ws, created_by=alice, origin=Session.ORIGIN_WEB)
    SessionParticipant.objects.create(session=session, user=bob)
    src = sources.for_session(session)
    return {"owner": owner, "ws": ws, "alice": alice, "bob": bob, "carol": carol, "session": session,
            "source": src, "coach": capability("coach", "individual", ("coach",)),
            "proj": capability("proj", "project"), "sys": capability("sys", "system"),
            "mem": capability("mem", "memory", ("remember",))}


def agree(u, *classes, **kw):
    return consent.set_consent(u, classes=classes or ["individual"], **kw)
