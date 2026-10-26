""""Who can see this" agrees with the session ACL, for every session shape.

`canopy_sessions.audience` reads each leg of `access.visible_session_q` for ONE
session; this file holds it to the predicate itself: for every person and every
shape, being among the audience's readers is exactly `access.can_read`. A leg
changed in one place and not the other fails here.
"""
from __future__ import annotations

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import AgentProject, ProjectParticipant
from apps.canopy_sessions import access, audience
from apps.canopy_sessions.models import ProjectSessionShare, RunnerBinding, Session, SessionParticipant
from apps.contacts import services as contacts
from apps.harness.models import Runner
from tests.fixtures.session_world import session_world as _world

pytestmark = pytest.mark.django_db


def _more_shapes(people, shapes, ws):
    """Add the shapes `_world` lacks: an email thread, a project-shared laptop
    session, a dispatched session, a sensitive laptop session."""
    from apps.agents.models import Agent

    hal = Agent.objects.get(slug="hal")
    email = Session.objects.create(workspace=ws, agent=hal, origin=Session.ORIGIN_RUNNER,
                                   metadata={"email_thread_key": "email:t1", "source": "email"})
    SessionParticipant.objects.create(session=email, user=people["viewer"], role=SessionParticipant.EDITOR)
    shapes["email"] = email

    project = AgentProject.objects.create(workspace=ws, handle="supply", name="Supply", agent=hal,
                                          repo_slug="connect-labs", owner_user=people["viewer"])
    ProjectParticipant.objects.create(project=project, person=contacts.person_for(user=people["outsider"]),
                                      source=ProjectParticipant.MANUAL)
    ProjectParticipant.objects.create(project=project, person=contacts.person_for(user=people["hal_login"]),
                                      source=ProjectParticipant.MANUAL)
    mates_box = Runner.objects.get(name="m")
    shared = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="shared laptop")
    RunnerBinding.objects.create(session=shared, runner=mates_box, session_key="sl",
                                 emdash_project="connect-labs", live_seen_at=timezone.now())
    ProjectSessionShare.objects.create(user=people["mate"], project=project)
    shapes["project_shared"] = shared

    sealed = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="sealed",
                                    metadata={"runner_requirements": ["sensitive_data"]})
    RunnerBinding.objects.create(session=sealed, runner=Runner.objects.get(name="r"), session_key="sx")
    shapes["sealed_laptop"] = sealed

    dispatched = Session.objects.create(workspace=ws, created_by=people["owner"],
                                        parent_session=shapes["shared_web"])
    SessionParticipant.objects.create(session=dispatched, user=people["viewer"], role=SessionParticipant.EDITOR)
    shapes["dispatched"] = dispatched


def test_the_audience_is_exactly_who_the_acl_lets_read():
    people, shapes, ws = _world()
    _more_shapes(people, shapes, ws)
    for label, s in shapes.items():
        readers = audience.readers(s)
        for who, user in people.items():
            assert (user.pk in readers) is access.can_read(user, s), f"{who} on {label}"
        assert audience.summary(s, people["owner"])["count"] == len(readers), label


def test_the_line_names_what_people_recognise():
    people, shapes, ws = _world()
    _more_shapes(people, shapes, ws)
    say = lambda key, who="owner": audience.summary(shapes[key], people[who])  # noqa: E731
    assert say("own_web")["label"] == "Only you"
    assert say("shared_web")["label"] == "Only you and 1 person you added"
    assert say("shared_web", "viewer")["label"].endswith("and 1 person they added")
    assert say("discovered")["label"] == "Only you"
    assert "This machine's admins can read it, to move it" in say("discovered")["extras"]
    assert "Trellis may review (coaching)" in say("own_web")["extras"]
    assert "Trellis may review (coaching)" not in say("sealed_laptop")["extras"]   # never a sensitive one
    assert say("email", "viewer")["label"] == "Everyone on this email thread (1)"
    assert say("agent_thread")["label"] == "Hal's admins (the agent's own work)"
    assert say("project_shared", "mate")["label"] == "Only you"
    assert "The Supply project team" in say("project_shared", "mate")["extras"]
    assert say("dispatched")["label"].startswith("You and the people of the conversation it came from")


def test_the_session_view_carries_it():
    people, shapes, _ws = _world()
    c = Client()
    c.force_login(people["owner"])
    body = c.get(f"/api/canopy-sessions/{shapes['shared_web'].id}").json()
    assert body["audience"]["label"] == "Only you and 1 person you added"
    assert body["audience"]["count"] == 3     # owner, the viewer added, Trellis reviewing
