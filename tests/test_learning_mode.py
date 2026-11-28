"""Learning mode: one per-person switch for "coaching agents and admins may read and analyze my sessions".

The person turns it on or off (`/api/people/me/learning-mode/`), or an owner of a workspace they are in does it for
them there (`/api/coaching/workspaces/{slug}/learning-mode/{user_id}/`). While it holds, the session ACL lets the
owners and admins of the session's workspace and the admins of the agent it was with read it (leg 8,
`access._learning_mode_q`), read-only, never a sensitive one, never a pull, held to a turn's reach; REST, the list,
the socket and the "who can see this" line agree. It is the same yes as the brain's `individual` class."""
from __future__ import annotations

import json

import pytest
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.db.models import Q
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.brain import consent
from apps.brain.models import LearningConsent
from apps.canopy_sessions import access, audience
from apps.canopy_sessions.consumers import SessionConsumer
from apps.canopy_sessions.models import CoachingOptIn, CoachingRole, Session, SessionParticipant
from apps.contacts import learning_mode
from apps.contacts.services import person_for
from apps.workspaces.models import Workspace
from apps.workspaces.models import WorkspaceMembership as M

pytestmark = pytest.mark.django_db(transaction=True)


def _u(name):
    u = User.objects.create_user(name, f"{name}@example.org", "pw")
    person_for(user=u)
    return u


def _m(user, ws, role=M.EDITOR):
    M.objects.create(user=user, workspace=ws, role=role)


@pytest.fixture()
def w():
    parent, kid, admin, sib, aa, bb, teacher = (_u(n) for n in ("parent", "kid", "admin", "sib", "aa", "bb", "teacher"))
    home = Workspace.objects.create(slug="home", display_name="Home", created_by=parent)
    den = Workspace.objects.create(slug="den", display_name="Den", created_by=parent, parent=home)
    school = Workspace.objects.create(slug="school", display_name="School", created_by=teacher)
    _m(parent, home, M.OWNER)
    _m(admin, home, M.ADMIN)
    _m(kid, home)
    _m(sib, home)
    _m(aa, home)
    _m(bb, home)
    _m(kid, den)
    _m(teacher, school, M.OWNER)
    _m(kid, school)
    a = Agent.objects.create(slug="tutor", name="Tutor", workspace=home)
    b = Agent.objects.create(slug="games", name="Games", workspace=home)
    AgentAdmin.objects.create(agent=a, user=aa)
    AgentAdmin.objects.create(agent=b, user=bb)
    coach = Agent.objects.create(slug="trellis", name="Trellis", workspace=home)
    coach.user = _u("coach_login")
    coach.save(update_fields=["user"])
    _m(coach.user, home)
    CoachingRole.objects.create(workspace=home, agent=coach)
    s = {
        "with_a": Session.objects.create(workspace=home, created_by=kid, agent=a, origin=Session.ORIGIN_WEB),
        "with_b": Session.objects.create(workspace=home, created_by=kid, agent=b, origin=Session.ORIGIN_WEB),
        "plain": Session.objects.create(workspace=home, created_by=kid, origin=Session.ORIGIN_WEB),
        "sealed": Session.objects.create(workspace=home, created_by=kid, agent=a, origin=Session.ORIGIN_WEB,
                                         metadata={"runner_requirements": ["sensitive_data"]}),
        "deleted": Session.objects.create(workspace=home, created_by=kid, origin=Session.ORIGIN_WEB,
                                          content_deleted_at=timezone.now()),
        "den": Session.objects.create(workspace=den, created_by=kid, origin=Session.ORIGIN_WEB),
        "school": Session.objects.create(workspace=school, created_by=kid, origin=Session.ORIGIN_WEB),
        "sibs": Session.objects.create(workspace=home, created_by=sib, agent=a, origin=Session.ORIGIN_WEB),
    }
    return {"parent": parent, "kid": kid, "admin": admin, "sib": sib, "aa": aa, "bb": bb, "teacher": teacher,
            "coach": coach.user, "home": home, "a": a, "b": b, "s": s}


def _c(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _put(user, path, on):
    return _c(user).put(path, data=json.dumps({"on": on}), content_type="application/json")


def _mine(user, on):
    return _put(user, "/api/people/me/learning-mode/", on)


def _for(owner, member, on, ws="home"):
    return _put(owner, f"/api/coaching/workspaces/{ws}/learning-mode/{member.pk}/", on)


def _socket(user, session):
    async def go():
        comm = WebsocketCommunicator(SessionConsumer.as_asgi(), f"/ws/canopy-sessions/{session.id}/")
        comm.scope["user"] = user
        comm.scope["url_route"] = {"kwargs": {"session_id": str(session.id)}}
        connected, _ = await comm.connect(timeout=10)
        await comm.disconnect()
        return connected
    return async_to_sync(go)()


# ---- turning it on and off -------------------------------------------------------------------------------------

def test_the_person_turns_it_on_and_off_and_it_is_the_individual_class(w):
    kid = w["kid"]
    r = _c(kid).get("/api/people/me/learning-mode/")
    assert r.status_code == 200 and r.json()["on"] is False
    on = _mine(kid, True).json()
    assert on["on"] is True and on["set_by"]["is_you"] is True and on["set_at"]
    assert set(on["workspaces"]) == {"home", "den", "school"}
    assert "individual" in LearningConsent.objects.get(user=kid, revoked_at__isnull=True).classes
    readers = on["readers"]
    assert [c["slug"] for c in readers["coaches"]] == ["trellis"]
    home = next(x for x in readers["workspace_admins"] if x["workspace"] == "home")
    assert {p["email"] for p in home["people"]} == {"parent@example.org", "admin@example.org"}
    tutor = next(x for x in readers["agent_admins"] if x["agent"] == "tutor")
    assert "aa@example.org" in {p["email"] for p in tutor["people"]}
    off = _mine(kid, False).json()
    assert off["on"] is False and off["workspaces"] == []
    row = CoachingOptIn.objects.get(user=kid)
    assert row.revoked_at is not None and row.revoked_by_id == kid.pk
    assert not any("individual" in c.classes for c in LearningConsent.objects.filter(user=kid, revoked_at__isnull=True))


def test_an_owner_turns_it_on_and_off_for_a_member_and_the_person_sees_who(w, monkeypatch):
    told = []
    monkeypatch.setattr("apps.push.services.send_to_user", lambda u, title, body, url, **k: told.append((u, body)) or 1)
    kid, parent = w["kid"], w["parent"]
    LearningConsent.objects.create(user=kid, classes=["project"], workspaces=["home", "den"], include_new=False,
                                   known_workspaces=["home", "den", "school"])
    r = _for(parent, kid, True)
    assert r.status_code == 200, r.content
    assert set(r.json()["changed"]) == {"home", "den"} and r.json()["member"]["on"] is True
    mine = _c(kid).get("/api/people/me/learning-mode/").json()
    assert mine["on"] is True and mine["set_by"]["email"] == "parent@example.org" and mine["set_by"]["is_you"] is False
    assert set(mine["workspaces"]) == {"home", "den"}                    # never school: the parent does not own it
    assert told and told[0][0] == kid and "on" in told[0][1]
    assert access.can_read(w["admin"], w["s"]["plain"]) is True
    # Off again, by the owner: it reaches only workspaces they own, so the class goes.
    r = _for(parent, kid, False)
    assert r.status_code == 200 and set(r.json()["changed"]) == {"home", "den"}
    assert _c(kid).get("/api/people/me/learning-mode/").json()["on"] is False
    assert CoachingOptIn.objects.get(user=kid).revoked_by_id == parent.pk
    assert access.can_read(w["admin"], w["s"]["plain"]) is False
    assert len(told) == 2


def test_an_owner_turning_it_off_closes_it_only_in_their_workspaces(w, monkeypatch):
    monkeypatch.setattr("apps.push.services.send_to_user", lambda *a, **k: 1)
    kid = w["kid"]
    _mine(kid, True)                                                     # everywhere: home, den and school
    r = _for(w["teacher"], kid, False, ws="school")
    assert r.status_code == 200 and r.json()["changed"] == ["school"]
    assert set(learning_mode.workspaces_on(kid)) == {"home", "den"}
    assert access.can_read(w["admin"], w["s"]["plain"]) is True
    assert access.can_read(w["teacher"], w["s"]["school"]) is False
    # The teacher turns it back on there; the person may turn it on everywhere again too.
    assert _for(w["teacher"], kid, True, ws="school").json()["changed"] == ["school"]
    assert access.can_read(w["teacher"], w["s"]["school"]) is True


def test_an_owner_cannot_widen_it_where_they_do_not_own(w):
    kid = w["kid"]
    LearningConsent.objects.create(user=kid, classes=["project"])        # every workspace, school included
    r = _for(w["parent"], kid, True)
    assert r.status_code == 422 and "do not manage" in r.json()["detail"]
    assert learning_mode.workspaces_on(kid) == []


def test_only_an_owner_sets_it_for_someone_else(w):
    kid = w["kid"]
    for who in ("admin", "sib", "aa"):
        assert _for(w[who], kid, True).status_code == 403, who
    assert _for(w["teacher"], kid, True).status_code == 404               # not a member of home
    assert _for(w["parent"], w["teacher"], True).status_code == 404       # not in home or below
    assert _c(w["admin"]).get("/api/coaching/workspaces/home/learning-mode/").status_code == 403
    assert not CoachingOptIn.objects.filter(user=kid).exists()
    listing = _c(w["parent"]).get("/api/coaching/workspaces/home/learning-mode/").json()
    assert {r["email"] for r in listing} >= {"kid@example.org", "sib@example.org"}
    assert all(r["on"] is False for r in listing)


def test_an_agent_login_cannot_change_a_person_s_learning_mode(w):
    assert _mine(w["coach"], True).status_code == 403
    assert not CoachingOptIn.objects.exists()


# ---- who reads -------------------------------------------------------------------------------------------------

def test_each_reader_reads_a_learning_mode_person_s_non_sensitive_sessions_read_only(w):
    _mine(w["kid"], True)
    s = w["s"]
    for who in ("parent", "admin"):
        user = w[who]
        for label in ("with_a", "with_b", "plain", "den"):
            assert access.can_read(user, s[label]) is True, (who, label)
            assert access.role_for(user, s[label]) == SessionParticipant.VIEWER, (who, label)
            assert access.can_write(user, s[label]) is False and access.may_pull(user, s[label]) is False
        assert access.can_read(user, s["sealed"]) is False, who
        assert access.can_read(user, s["deleted"]) is False, who
        assert access.can_read(user, s["school"]) is False, who            # not in school: the tenant gate
    assert access.can_read(w["aa"], s["with_a"]) is True                   # agent A's admin, A's session
    assert access.can_read(w["aa"], s["sealed"]) is False
    assert access.can_read(w["sib"], s["with_a"]) is False                 # a plain member reads nothing
    assert access.can_read(w["coach"], s["with_a"]) is False               # an agent coach: only inside a turn


def test_an_agent_admin_reads_only_sessions_with_their_agent(w):
    _mine(w["kid"], True)
    s = w["s"]
    assert access.can_read(w["aa"], s["with_a"]) is True
    assert access.can_read(w["aa"], s["with_b"]) is False
    assert access.can_read(w["aa"], s["plain"]) is False
    assert access.can_read(w["bb"], s["with_b"]) is True and access.can_read(w["bb"], s["with_a"]) is False
    w["home"].memberships.filter(user=w["aa"]).update(role=M.ADMIN)        # and once a workspace admin too
    assert access.can_read(w["aa"], s["with_b"]) is True


def test_a_workspace_admin_reads_nobody_not_in_learning_mode(w):
    s = w["s"]
    for label in ("with_a", "plain", "den"):
        assert access.can_read(w["admin"], s[label]) is False
        assert access.can_read(w["parent"], s[label]) is False
        assert access.can_read(w["aa"], s[label]) is False
    assert access.can_read(w["admin"], s["sibs"]) is False                 # sib never turned it on
    _mine(w["kid"], True)
    assert access.can_read(w["admin"], s["sibs"]) is False


def test_a_session_taken_out_of_learning_is_not_read(w):
    from apps.brain import sources

    _mine(w["kid"], True)
    consent.exclude(w["kid"], sources.for_session(w["s"]["plain"]))
    assert access.can_read(w["admin"], w["s"]["plain"]) is False
    assert access.can_read(w["admin"], w["s"]["with_a"]) is True


def test_reach_still_holds(w):
    _mine(w["kid"], True)
    admin = w["admin"]
    access.limit_reach(admin, access.Reach(Q(agent_id=w["a"].pk), agent_id=w["a"].pk))
    assert access.can_read(admin, w["s"]["with_a"]) is True
    assert access.can_read(admin, w["s"]["with_b"]) is False
    assert access.can_read(admin, w["s"]["plain"]) is False
    access.limit_reach(admin, access.NO_REACH)
    assert access.can_read(admin, w["s"]["with_a"]) is False


def test_rest_the_list_the_socket_and_the_audience_agree(w):
    _mine(w["kid"], True)
    for who in ("parent", "admin", "sib", "aa", "bb", "teacher", "kid"):
        user = w[who]
        c = _c(user)
        listed = set()
        for ws in ("home", "school"):                # one read per organisation (the kid is in two)
            resp = c.get(f"/api/w/{ws}/canopy-sessions/?state=all")
            if resp.status_code == 200:
                listed |= {r["id"] for r in resp.json()}
        for label, s in w["s"].items():
            expected = access.can_read(user, s)
            assert (c.get(f"/api/canopy-sessions/{s.id}").status_code == 200) is expected, (who, label)
            assert (str(s.id) in listed) is expected, (who, label)
            if label != "sealed":   # a sensitive chat's socket wants its own people's keys: not this leg's question
                assert _socket(user, s) is expected, (who, label)
            assert (user.pk in audience.readers(s)) is expected, (who, label)
    line = audience.summary(w["s"]["with_a"], w["kid"])
    assert any("learning mode" in e for e in line["extras"])
