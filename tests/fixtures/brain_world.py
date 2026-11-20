"""A small world for the brain's tests: one workspace, three people, one session two of them are in."""
from __future__ import annotations

from django.contrib.auth.models import User

from apps.brain import consent, sources
from apps.brain.models import BrainCapability
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.workspaces.models import Workspace, WorkspaceMembership


def user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


def member(u, ws, role=WorkspaceMembership.EDITOR):
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=role)


def capability(slug, cls, purposes=("learn",), enabled=True, **kw):
    return BrainCapability.objects.create(slug=slug, name=slug, purpose_class=cls, purposes=list(purposes),
                                          enabled=enabled, subject_type="project" if cls == "project" else "person",
                                          source_kinds=["session"], **kw)


def brain_world():
    owner = user("jj")
    ws = Workspace.objects.create(slug="acme", display_name="Acme", created_by=owner)
    member(owner, ws, WorkspaceMembership.OWNER)
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
