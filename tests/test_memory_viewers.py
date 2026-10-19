"""Letting an agent see your memory page, read-only (apps/contacts/viewers.py).

* only the person chooses, and only an agent of a workspace they belong to;
* the agent's own login then reads the page — what is held, the grants, the
  audit, the searches — and changes nothing;
* anyone else (another agent, a caller token, the person's colleague) is 404;
* every look is in the person's audit, once per window; revoking ends it at once.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.contacts import people
from apps.contacts import services as contacts
from apps.contacts.models import PersonAccess, PersonAuditEvent
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    elsewhere = Workspace.objects.create(slug="other", display_name="Other", created_by=owner)
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=lili, workspace=ws, role=WorkspaceMembership.EDITOR)
    agents = {}
    for slug, space in (("hal", ws), ("ace", ws), ("stranger", elsewhere)):
        a = Agent.objects.create(slug=slug, name=slug.upper(), workspace=space, owner=owner)
        a.user = User.objects.create_user(f"{slug}-login", f"{slug}@dimagi-ai.com", "pw")
        a.save(update_fields=["user"])
        WorkspaceMembership.objects.create(user=a.user, workspace=space, role=WorkspaceMembership.EDITOR)
        agents[slug] = a
    person = contacts.person_for(user=lili)
    people.record_fact(person=person, workspace=ws, kind="instance", statement="Owns the budget dashboard.",
                       category="work_context")
    return {"lili": lili, "person": person, **agents}


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _let_in(w, slug="hal"):
    r = _as(w["lili"]).post("/api/people/me/viewers/", data=json.dumps({"agent": slug}),
                            content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()


def test_the_person_lets_an_agent_in_and_it_sees_the_page(w):
    before = _as(w["lili"]).get("/api/people/me/viewers/").json()
    assert before["viewers"] == []
    assert {a["slug"] for a in before["agents"]} == {"hal", "ace"}      # not an agent of a workspace she isn't in

    after = _let_in(w)
    assert [v["agent"] for v in after["viewers"]] == ["hal"]
    assert "hal" not in {a["slug"] for a in after["agents"]}
    assert PersonAuditEvent.objects.filter(person=w["person"], event_type="viewer.granted").count() == 1

    hal = _as(w["hal"].user)
    shared = hal.get("/api/people/pages/shared-with-me/").json()["pages"]
    assert [(p["person_id"], p["href"]) for p in shared] == [(w["person"].pk, f"/people/{w['person'].pk}/page")]

    page = hal.get(f"/api/people/{w['person'].pk}/page/")
    assert page.status_code == 200, page.content
    assert [f["statement"] for f in page.json()["me"]["facts"]] == ["Owns the budget dashboard."]
    assert "grants" in page.json() and "collections" in page.json()
    audit = hal.get(f"/api/people/{w['person'].pk}/page/audit/").json()
    assert audit["events"][0]["eventType"] == "page.viewed"
    assert hal.get(f"/api/people/{w['person'].pk}/page/searches/").status_code == 200


def test_a_page_load_is_one_look_in_the_audit(w):
    _let_in(w)
    hal = _as(w["hal"].user)
    for path in ("", "audit/", "searches/", ""):
        assert hal.get(f"/api/people/{w['person'].pk}/page/{path}").status_code == 200
    looks = PersonAuditEvent.objects.filter(person=w["person"], event_type="page.viewed")
    assert [(e.actor_id, e.actor_type) for e in looks] == [("agent:hal", "agent")]
    assert PersonAccess.objects.filter(person=w["person"], reader_agent=w["hal"]).count() == 1


def test_no_one_else_sees_it_and_the_viewer_changes_nothing(w):
    _let_in(w)
    pid = w["person"].pk
    assert _as(w["ace"].user).get(f"/api/people/{pid}/page/").status_code == 404     # not let in
    jj = User.objects.get(username="jj")
    assert _as(jj).get(f"/api/people/{pid}/page/").status_code == 404                # a person, not an agent
    assert _as(w["lili"]).get(f"/api/people/{pid}/page/").status_code == 404         # her own is /me/
    assert _as(w["hal"].user).get("/api/people/999999/page/").status_code == 404
    hal = _as(w["hal"].user)
    # Its own login cannot choose who sees the page, nor read the person's own routes.
    assert hal.post("/api/people/me/viewers/", data=json.dumps({"agent": "ace"}),
                    content_type="application/json").status_code == 403
    assert hal.get("/api/people/me/memory-searches/").status_code == 403
    assert hal.get("/api/hcp/v1/audit").status_code == 403


def test_only_an_agent_of_your_workspaces(w):
    r = _as(w["lili"]).post("/api/people/me/viewers/", data=json.dumps({"agent": "stranger"}),
                            content_type="application/json")
    assert r.status_code == 404


def test_revoking_ends_it_at_once(w):
    _let_in(w)
    _let_in(w)                                     # again: still one
    lili = _as(w["lili"])
    r = lili.delete("/api/people/me/viewers/hal/")
    assert r.status_code == 200 and r.json()["viewers"] == []
    assert _as(w["hal"].user).get(f"/api/people/{w['person'].pk}/page/").status_code == 404
    assert _as(w["hal"].user).get("/api/people/pages/shared-with-me/").json()["pages"] == []
    assert lili.delete("/api/people/me/viewers/hal/").status_code == 404
    kinds = list(PersonAuditEvent.objects.filter(person=w["person"], event_type__startswith="viewer.")
                 .order_by("pk").values_list("event_type", flat=True))
    assert kinds == ["viewer.granted", "viewer.revoked"]


def test_each_entry_says_how_often_agents_were_told_it_over_all_time(w):
    """The page used to count only its first page of audit rows ("used 9×" of an
    entry told in 84 conversations); the count now comes from the server."""
    from apps.contacts import hcp

    fact = w["person"].facts.get()
    for _ in range(3):                      # three looks outside any turn
        hcp.audit(w["person"], "preference.read", actor=hcp.agent_actor(w["ace"]), entry=fact, purpose="t")
    _let_in(w)
    page = _as(w["hal"].user).get(f"/api/people/{w['person'].pk}/page/").json()
    used = page["me"]["facts"][0]["used"]
    assert used["conversations"] == 3 and used["by"] == ["ace"] and used["last_at"]
    mine = _as(w["lili"]).get("/api/people/me/").json()
    assert mine["facts"][0]["used"]["conversations"] == 3
