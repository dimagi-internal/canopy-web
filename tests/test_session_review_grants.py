"""Opting into coaching, and who coaches — `CoachingOptIn` + `CoachingRole`.

* only the person opts in (naming no agent); each opt-in and opt-out is on their audit; they are shown
  who coaches them now, which is what they agree to;
* a coach (an agent's own login or a designated person, for a workspace and everything below it) then
  reads their sessions through the one session ACL (`canopy_sessions.access`, leg 5), read-only, and
  lists who opted in within its reach;
* never a sensitive conversation; another agent, an admin with no role, or a co-tenant gains nothing;
* opting out, or removing the role, ends it at once.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client, override_settings

from apps.agents.models import Agent
from apps.canopy_sessions import access
from apps.canopy_sessions.models import CoachingOptIn, Session
from apps.contacts import services as contacts
from apps.contacts.models import PersonAuditEvent
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    elsewhere = Workspace.objects.create(slug="other", display_name="Other", created_by=owner)
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    colleague = User.objects.create_user("bo", "bo@dimagi.com", "pw")
    for u in (lili, colleague):
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    agents = {}
    for slug, space in (("trellis", ws), ("hal", ws), ("stranger", elsewhere)):
        a = Agent.objects.create(slug=slug, name=slug.title(), workspace=space, owner=owner)
        a.user = User.objects.create_user(f"{slug}-login", f"{slug}@dimagi-ai.com", "pw")
        a.save(update_fields=["user"])
        WorkspaceMembership.objects.create(user=a.user, workspace=space, role=WorkspaceMembership.EDITOR)
        agents[slug] = a
    from apps.canopy_sessions.models import CoachingRole

    CoachingRole.objects.create(workspace=ws, agent=agents["trellis"])        # the program's coach today
    chat = Session.objects.create(workspace=ws, created_by=lili, title="lili's chat")
    sealed = Session.objects.create(workspace=ws, created_by=lili, title="",
                                    metadata={"runner_requirements": ["sensitive_data"]})
    bos = Session.objects.create(workspace=ws, created_by=colleague, title="bo's chat")
    return {"ws": ws, "lili": lili, "bo": colleague, "chat": chat, "sealed": sealed, "bos": bos,
            "person": contacts.person_for(user=lili), **agents}


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _let_in(w):
    r = _as(w["lili"]).post("/api/people/me/session-reviewers/", data=json.dumps({}),
                            content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()


def _sweep(agent, for_user=None) -> Client:
    """The agent's login inside a running turn canopy started: nobody asked, so it has the
    roster, and with `for_user` the one grantor it was dispatched for (who may ask is
    tests/test_agent_session_reads_bind_to_the_asker.py)."""
    from django.utils import timezone

    from apps.harness import turn_agent_tokens
    from apps.harness.models import Runner, Turn

    box = Runner.objects.create(name=f"box-{agent.slug}-{Runner.objects.count()}", kind=Runner.CLOUD, owner=agent.owner,
                                workspace=agent.workspace, last_heartbeat_at=timezone.now(),
                                status=Runner.ONLINE)
    turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="sweep",
                               idempotency_key=f"sweep-{agent.slug}-{Turn.objects.count()}",
                               status=Turn.RUNNING, claimed_by=box, initiator_kind="system",
                               initiator_via="schedule", initiator_assurance="internal",
                               origin_ref={"reads_for": {"user_id": for_user.pk}} if for_user else {})
    bearer = turn_agent_tokens.issue(turn, box)["token"]
    return Client(HTTP_AUTHORIZATION=f"Bearer {bearer}")


def _reads(agent, session) -> bool:
    return _sweep(agent, session.created_by).get(f"/api/canopy-sessions/{session.pk}").status_code == 200


def test_the_person_opts_in_and_it_is_audited(w):
    before = _as(w["lili"]).get("/api/people/me/session-reviewers/").json()
    assert before["on"] is False and before["reviewers"] == []
    # What they agree to is shown first: who coaches them. Not an agent of a workspace she isn't in.
    assert [(c["kind"], c["slug"], c["workspace"]) for c in before["coaches"]] == [("agent", "trellis", "dimagi")]
    assert before["coach"] == {"agent": "trellis", "name": "Trellis", "workspace": "dimagi",
                               "member": True, "on": False}

    after = _let_in(w)
    _let_in(w)                                                             # again: still one
    assert after["on"] is True and after["since"] is not None
    assert [r["agent"] for r in after["reviewers"]] == ["trellis"]
    assert after["coach"]["on"] is True
    assert CoachingOptIn.objects.filter(user=w["lili"], revoked_at__isnull=True).count() == 1
    assert PersonAuditEvent.objects.filter(person=w["person"], event_type="sessionReviewer.granted").count() == 1

    r = _as(w["lili"]).delete("/api/people/me/session-reviewers/")
    assert r.status_code == 200 and r.json()["on"] is False and r.json()["reviewers"] == []
    assert PersonAuditEvent.objects.filter(person=w["person"], event_type="sessionReviewer.revoked").count() == 1
    assert _as(w["lili"]).delete("/api/people/me/session-reviewers/").status_code == 404
    # The old per-agent path still opts out (deprecated alias).
    _let_in(w)
    assert _as(w["lili"]).delete("/api/people/me/session-reviewers/trellis/").json()["on"] is False


def test_only_the_person_opts_in(w):
    # An agent's own login cannot opt itself (or anyone) in.
    r = _as(w["trellis"].user).post("/api/people/me/session-reviewers/", data=json.dumps({}),
                                    content_type="application/json")
    assert r.status_code == 403
    assert not CoachingOptIn.objects.exists()


def test_the_agent_enumerates_only_its_own_granters(w):
    _let_in(w)
    people = _sweep(w["trellis"]).get("/api/people/session-reviewers/granted-to-me/").json()["people"]
    assert [(p["user_id"], p["email"]) for p in people] == [(w["lili"].pk, "lili@dimagi.com")]
    assert _sweep(w["hal"]).get("/api/people/session-reviewers/granted-to-me/").json() == {"people": []}
    assert _as(w["bo"]).get("/api/people/session-reviewers/granted-to-me/").json() == {"people": []}


def test_reading_works_while_live_and_stops_on_revoke(w):
    trellis = w["trellis"]
    assert not _reads(trellis, w["chat"])                                 # no grant, no read
    _let_in(w)
    assert _reads(trellis, w["chat"])
    assert not _reads(trellis, w["bos"])                                  # only the granter's sessions
    listed = _sweep(trellis, w["lili"]).get(f"/api/canopy-sessions/?of_user={w['lili'].pk}").json()
    assert {s["id"] for s in listed} == {str(w["chat"].pk)}
    # Read-only: the grant never lets it write, send or share.
    from apps.harness import turn_asker

    setattr(trellis.user, "_canopy_turn_standing", turn_asker.AnsweringTo("t", person_id=0, operator=True))
    assert access.role_for(trellis.user, w["chat"]) == "viewer"
    assert not access.can_write(trellis.user, w["chat"])
    assert not access.can_share(trellis.user, w["chat"])

    _as(w["lili"]).delete("/api/people/me/session-reviewers/")
    assert not _reads(trellis, w["chat"])


def test_sensitive_sessions_stay_sealed(w):
    _let_in(w)
    assert not _reads(w["trellis"], w["sealed"])
    assert not access.can_read(w["trellis"].user, w["sealed"])
    assert _as(w["lili"]).get(f"/api/canopy-sessions/{w['sealed'].pk}").status_code == 200   # still hers


def test_only_a_coach_reads_and_a_coach_is_a_role_not_an_admin(w):
    from apps.canopy_sessions.models import CoachingRole

    _let_in(w)
    assert not _reads(w["hal"], w["chat"])                               # an agent that is no coach
    assert not _reads(w["stranger"], w["chat"])
    assert _as(w["bo"]).get(f"/api/canopy-sessions/{w['chat'].pk}").status_code == 404
    # The agent's owner administers Trellis but holds no coaching role: administering is not coaching.
    jj = User.objects.get(username="jj")
    WorkspaceMembership.objects.create(user=jj, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    assert not access.can_read(jj, w["chat"])
    # Designated a coach for the workspace, they read as themselves; never a sensitive one.
    role = CoachingRole.objects.create(workspace=w["ws"], user=jj)
    assert access.can_read(jj, w["chat"]) and not access.can_read(jj, w["sealed"])
    assert not access.can_read(jj, w["bos"])                             # bo never opted in
    assert access.role_for(jj, w["chat"]) == "viewer" and not access.can_write(jj, w["chat"])
    # A plain member who coaches nothing gains nothing; removing the role ends it at once.
    assert not access.can_read(User.objects.get(username="bo"), w["chat"])
    from django.utils import timezone

    role.revoked_at = timezone.now()
    role.save(update_fields=["revoked_at"])
    assert not access.can_read(jj, w["chat"])


def test_a_coach_for_one_workspace_does_not_reach_a_person_in_another(w):
    from apps.canopy_sessions.models import CoachingRole

    elsewhere = Workspace.objects.get(slug="other")
    mia = User.objects.create_user("mia", "mia@other.org", "pw")
    WorkspaceMembership.objects.create(user=mia, workspace=elsewhere, role=WorkspaceMembership.EDITOR)
    theirs = Session.objects.create(workspace=elsewhere, created_by=mia, title="mia's chat")
    CoachingOptIn.objects.create(user=mia)
    jj = User.objects.get(username="jj")
    WorkspaceMembership.objects.create(user=jj, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    CoachingRole.objects.create(workspace=w["ws"], user=jj)
    assert not access.can_read(jj, theirs)                               # her workspace is outside jj's scope
    CoachingRole.objects.create(workspace=elsewhere, user=jj)            # now designated there too
    WorkspaceMembership.objects.create(user=jj, workspace=elsewhere, role=WorkspaceMembership.EDITOR)
    assert access.can_read(jj, theirs)


def test_a_coach_designated_above_covers_the_workspaces_below(w):
    from apps.canopy_sessions.models import CoachingRole

    division = Workspace.objects.create(slug="division", display_name="Division", parent=w["ws"],
                                        created_by=w["lili"])
    nik = User.objects.create_user("nik", "nik@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=nik, workspace=division, role=WorkspaceMembership.EDITOR)
    theirs = Session.objects.create(workspace=division, created_by=nik, title="nik's chat")
    CoachingOptIn.objects.create(user=nik)
    md = User.objects.create_user("md", "md@dimagi.com", "pw")
    # The tenant gate is unchanged: md is in both. An editor, so only the coach role reads (an owner or admin
    # would read a learning-mode person anyway: `access._learning_mode_q`).
    WorkspaceMembership.objects.create(user=md, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(user=md, workspace=division, role=WorkspaceMembership.EDITOR)
    CoachingRole.objects.create(workspace=w["ws"], user=md)              # designated for dimagi and below
    assert access.can_read(md, theirs)
    CoachingRole.objects.filter(user=md).update(workspace=division)      # the role's own scope still counts
    assert access.can_read(md, theirs)
    mia = User.objects.create_user("mia2", "mia2@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=mia, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    other = Session.objects.create(workspace=w["ws"], created_by=mia, title="in the parent")
    CoachingOptIn.objects.create(user=mia)
    assert not access.can_read(md, other)                                # a role for the division is not for dimagi


@override_settings(COACHING_AGENT_SLUG="")
def test_no_coach_is_offered_when_the_setting_is_blank(w):
    assert _as(w["lili"]).get("/api/people/me/session-reviewers/").json()["coach"] is None


def test_the_coach_is_offered_only_where_the_person_can_reach_it(w):
    outsider = User.objects.create_user("zed", "zed@example.org", "pw")
    WorkspaceMembership.objects.create(user=outsider, workspace=Workspace.objects.get(slug="other"),
                                       role=WorkspaceMembership.EDITOR)
    assert _as(outsider).get("/api/people/me/session-reviewers/").json()["coach"] is None
    # A newcomer whose domain may ask to join is offered it, not yet on.
    w["ws"].access_request_domains = ["example.org"]
    w["ws"].save(update_fields=["access_request_domains"])
    coach = _as(outsider).get("/api/people/me/session-reviewers/").json()["coach"]
    assert coach == {"agent": "trellis", "name": "Trellis", "workspace": "dimagi", "member": False, "on": False}
