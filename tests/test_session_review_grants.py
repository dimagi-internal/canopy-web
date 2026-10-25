"""Letting an agent review your sessions (a coach) — `SessionReviewGrant`.

* only the person chooses, only an agent of a workspace they belong to, and each
  grant and revoke is on their audit;
* the agent's own login then reads their sessions through the one session ACL
  (`canopy_sessions.access`, leg 5), read-only, and lists who let it in;
* never a sensitive conversation; another agent, or a person, gains nothing;
* revoking ends it at once.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client, override_settings

from apps.agents.models import Agent
from apps.canopy_sessions import access
from apps.canopy_sessions.models import Session, SessionReviewGrant
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


def _let_in(w, slug="trellis"):
    r = _as(w["lili"]).post("/api/people/me/session-reviewers/", data=json.dumps({"agent": slug}),
                            content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()


def _reads(agent, session) -> bool:
    return _as(agent.user).get(f"/api/canopy-sessions/{session.pk}").status_code == 200


def test_the_person_lets_the_coach_in_and_it_is_audited(w):
    before = _as(w["lili"]).get("/api/people/me/session-reviewers/").json()
    assert before["reviewers"] == []
    assert {a["slug"] for a in before["agents"]} == {"trellis", "hal"}   # not an agent of a workspace she isn't in
    assert before["coach"] == {"agent": "trellis", "name": "Trellis", "workspace": "dimagi",
                               "member": True, "on": False}

    after = _let_in(w)
    _let_in(w)                                                             # again: still one
    assert [r["agent"] for r in after["reviewers"]] == ["trellis"]
    assert after["coach"]["on"] is True
    assert SessionReviewGrant.objects.filter(user=w["lili"], revoked_at__isnull=True).count() == 1
    assert PersonAuditEvent.objects.filter(person=w["person"], event_type="sessionReviewer.granted").count() == 1

    r = _as(w["lili"]).delete("/api/people/me/session-reviewers/trellis/")
    assert r.status_code == 200 and r.json()["reviewers"] == []
    assert PersonAuditEvent.objects.filter(person=w["person"], event_type="sessionReviewer.revoked").count() == 1
    assert _as(w["lili"]).delete("/api/people/me/session-reviewers/trellis/").status_code == 404


def test_only_the_person_grants_and_only_an_agent_of_their_workspaces(w):
    r = _as(w["lili"]).post("/api/people/me/session-reviewers/", data=json.dumps({"agent": "stranger"}),
                            content_type="application/json")
    assert r.status_code == 404
    # An agent's own login cannot grant itself (or anyone) a person's sessions.
    r = _as(w["trellis"].user).post("/api/people/me/session-reviewers/", data=json.dumps({"agent": "trellis"}),
                                    content_type="application/json")
    assert r.status_code == 403
    assert not SessionReviewGrant.objects.exists()


def test_the_agent_enumerates_only_its_own_granters(w):
    _let_in(w)
    people = _as(w["trellis"].user).get("/api/people/session-reviewers/granted-to-me/").json()["people"]
    assert [(p["user_id"], p["email"]) for p in people] == [(w["lili"].pk, "lili@dimagi.com")]
    assert _as(w["hal"].user).get("/api/people/session-reviewers/granted-to-me/").json() == {"people": []}
    assert _as(w["bo"]).get("/api/people/session-reviewers/granted-to-me/").json() == {"people": []}


def test_reading_works_while_live_and_stops_on_revoke(w):
    trellis = w["trellis"]
    assert not _reads(trellis, w["chat"])                                 # no grant, no read
    _let_in(w)
    assert _reads(trellis, w["chat"])
    assert not _reads(trellis, w["bos"])                                  # only the granter's sessions
    listed = _as(trellis.user).get(f"/api/canopy-sessions/?of_user={w['lili'].pk}").json()
    assert {s["id"] for s in listed} == {str(w["chat"].pk)}
    # Read-only: the grant never lets it write, send or share.
    assert access.role_for(trellis.user, w["chat"]) == "viewer"
    assert not access.can_write(trellis.user, w["chat"])
    assert not access.can_share(trellis.user, w["chat"])

    _as(w["lili"]).delete("/api/people/me/session-reviewers/trellis/")
    assert not _reads(trellis, w["chat"])


def test_sensitive_sessions_stay_sealed(w):
    _let_in(w)
    assert not _reads(w["trellis"], w["sealed"])
    assert not access.can_read(w["trellis"].user, w["sealed"])
    assert _as(w["lili"]).get(f"/api/canopy-sessions/{w['sealed'].pk}").status_code == 200   # still hers


def test_another_agent_or_a_person_cannot_use_the_coachs_grant(w):
    _let_in(w)
    assert not _reads(w["hal"], w["chat"])
    assert not _reads(w["stranger"], w["chat"])
    assert _as(w["bo"]).get(f"/api/canopy-sessions/{w['chat'].pk}").status_code == 404
    # The agent's owner administers it, but is not its login: the grant is not theirs.
    jj = User.objects.get(username="jj")
    WorkspaceMembership.objects.create(user=jj, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    assert not access.can_read(jj, w["chat"])


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
