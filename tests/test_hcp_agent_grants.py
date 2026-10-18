"""First-party grants are per agent and only ever the person's act (HCP 4.1.4,
4.1.6) — Jonathan, 2026-10-09: "each agent session / entry should obtain the grant
explicitly or due to previous granting to this agent."

* Policy (`Person.hcp_*_available/default`) issues nothing; it bounds what a grant
  may carry and what a session offers.
* The person allows the session's agent "for this session" (temporary — the
  default outcome, lapsing with the session or after the cap); keeping it "always"
  is a SEPARATE later act that only elects persistence (4.1.4: one act never both
  authorizes and elects persistence). Each is a `grant.issued` carrying the
  modality and what was shown.
* A prior "always" grant to that agent is reused; another agent gets nothing from
  it; widening needs a new act; revoking ends it for good.
* Until granted, the agent is served as if the feature were off and the envelope
  says the person is being asked.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.contacts import hcp, people
from apps.contacts import services as contacts
from apps.contacts.models import PersonAuditEvent, PersonGrant
from apps.harness import caller_context, services
from apps.harness import initiator as who
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _agent(ws, slug, owner):
    agent = Agent.objects.create(slug=slug, name=slug.title(), workspace=ws, owner=owner)
    login = User.objects.create_user(f"{slug}-login", f"{slug}@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=login, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent.user = login
    agent.save(update_fields=["user"])
    return agent


@pytest.fixture()
def w(agent_memory_on):
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=lili, workspace=ws, role=WorkspaceMembership.EDITOR)
    person = contacts.person_for(user=lili)
    people.record_fact(person=person, workspace=ws, kind="role", statement="KC metrics lead.",
                       basis="declared")
    return {"ws": ws, "owner": owner, "lili": lili, "person": person,
            "ada": _agent(ws, "ada", owner), "eva": _agent(ws, "eva", owner)}


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _json(c, method, path, body):
    return getattr(c, method)(path, data=json.dumps(body), content_type="application/json")


def _session(user, agent):
    from apps.canopy_sessions.models import Session

    return Session.objects.create(agent=agent, created_by=user, workspace=agent.workspace)


def _turn(agent, user, session, key, prompt="KC metrics"):
    turn, _ = services.enqueue_turn(
        session=session, origin=Turn.ORIGIN_API, idempotency_key=key, prompt=prompt,
        initiator=who.for_user(user, via="chat", assurance=who.SESSION))
    return turn


def _grant(user, session, features, duration, surface="chat"):
    return _json(_as(user), "post", f"/api/people/me/sessions/{session.pk}/agent-grants/",
                 {"features": features, "duration": duration, "surface": surface})


def _keep(user, session, features):
    """Allow for this session, then — a separate act — keep it always."""
    assert _grant(user, session, features, "session").status_code == 200
    return _grant(user, session, features, "always")


def _search(agent, turn):
    return _json(_as(agent.user), "post", f"/api/hcp/v1/preferences/search?turn={turn.pk}",
                 {"query": "KC metrics", "categories": ["work_context"], "purpose": "answer"})


def _add(agent, turn):
    return _json(_as(agent.user), "post", f"/api/hcp/v1/preferences/add?turn={turn.pk}",
                 {"category": "work_context", "preference": "Works on KC.",
                  "declarationType": "user-declared", "sourceContext": f"turn:{turn.pk}"})


def _issued(person):
    return list(PersonAuditEvent.objects.filter(person=person, event_type="grant.issued")
                .order_by("pk"))


def test_nothing_is_granted_without_the_persons_act(w):
    session = _session(w["lili"], w["ada"])
    turn = _turn(w["ada"], w["lili"], session, "t1")
    block = caller_context.build(turn)["person"]
    assert block["hcp"]["record"] is False and block["hcp"]["use"] is False
    assert block["hcp"]["granted"] == {"record": False, "use": False}
    assert block["hcp"]["awaiting_grant"] == ["record", "use"]      # the UI asks; the agent doesn't
    assert block["facts"] == [] and block["grant"] is None
    assert block["recall"] is None and block["record"] is None
    r = _search(w["ada"], turn)
    assert r.status_code == 403 and hcp.NOT_GRANTED in r.json()["detail"]
    assert _add(w["ada"], turn).status_code == 403
    assert not PersonGrant.objects.filter(person=w["person"]).exists()    # nothing presumed
    assert not _issued(w["person"])


def test_a_session_grant_is_temporary_shown_logged_and_works(w):
    session = _session(w["lili"], w["ada"])
    turn = _turn(w["ada"], w["lili"], session, "t1")
    before = timezone.now()
    r = _grant(w["lili"], session, ["use"], "session")
    assert r.status_code == 200
    body = r.json()
    assert body["agent"] == {"slug": "ada", "name": "Ada"}
    assert body["use"]["granted"] and body["use"]["grant"]["type"] == "temporary"
    assert not body["record"]["granted"]
    g = PersonGrant.objects.get(person=w["person"], status="active")
    assert (g.agent, g.grant_type, g.modality) == (w["ada"], "temporary", "canopy-chat")
    assert before + hcp.SESSION_GRANT_CAP <= g.expires_at <= timezone.now() + hcp.SESSION_GRANT_CAP
    assert g.scopes == [hcp.scope(c, "read") for c in hcp.HELD_CATEGORIES]
    (issued,) = _issued(w["person"])
    assert "modality=canopy-chat" in issued.detail and "type=temporary" in issued.detail
    assert "shown: agent=ada" in issued.detail and "actions=read" in issued.detail
    assert issued.actor_type == "user"                                  # the person's act
    assert _search(w["ada"], turn).json()["entries"]
    assert _add(w["ada"], turn).status_code == 403                      # record not granted


def test_a_session_grant_lapses_with_the_session_and_after_the_cap(w):
    session = _session(w["lili"], w["ada"])
    turn = _turn(w["ada"], w["lili"], session, "t1")
    _grant(w["lili"], session, ["use"], "session")
    session.status = "archived"
    session.save(update_fields=["status"])
    assert _search(w["ada"], turn).status_code == 403
    assert PersonGrant.objects.get(person=w["person"]).status == "expired"
    assert PersonAuditEvent.objects.filter(person=w["person"], event_type="grant.expired").exists()

    other = _session(w["lili"], w["ada"])
    t2 = _turn(w["ada"], w["lili"], other, "t2")
    _grant(w["lili"], other, ["use"], "session")
    PersonGrant.objects.filter(person=w["person"], status="active").update(
        expires_at=timezone.now() - dt.timedelta(seconds=1))
    assert _search(w["ada"], t2).status_code == 403                    # past the cap
    # A grant for one session never reaches another of the same agent.
    third = _session(w["lili"], w["ada"])
    assert _search(w["ada"], _turn(w["ada"], w["lili"], third, "t3")).status_code == 403


def test_an_always_grant_is_reused_in_later_sessions_without_asking(w):
    s1 = _session(w["lili"], w["ada"])
    assert _keep(w["lili"], s1, ["record", "use"]).status_code == 200
    g = PersonGrant.objects.get(person=w["person"], status="active", grant_type="persistent")
    assert g.expires_at is None
    assert "electing persistence after allowing it for this session" in _issued(w["person"])[-1].detail
    s2 = _session(w["lili"], w["ada"])
    t2 = _turn(w["ada"], w["lili"], s2, "t2")
    block = caller_context.build(t2)["person"]
    assert block["hcp"]["granted"] == {"record": True, "use": True}
    assert block["hcp"]["awaiting_grant"] == []
    assert block["facts"] and block["recall"] and block["record"]
    assert _search(w["ada"], t2).status_code == 200
    assert len(_issued(w["person"])) == 2                     # allow + keep; nothing asked in s2


def test_granting_one_agent_grants_no_other(w):
    s = _session(w["lili"], w["ada"])
    _keep(w["lili"], s, ["record", "use"])
    s_eva = _session(w["lili"], w["eva"])
    t = _turn(w["eva"], w["lili"], s_eva, "e1")
    assert _search(w["eva"], t).status_code == 403
    assert _add(w["eva"], t).status_code == 403
    assert caller_context.build(t)["person"]["hcp"]["granted"] == {"record": False, "use": False}
    # And Ada cannot name Eva's turn.
    assert _search(w["ada"], t).status_code == 403


def test_widening_is_its_own_act_and_replaces_the_grant(w):
    s = _session(w["lili"], w["ada"])
    t = _turn(w["ada"], w["lili"], s, "t1")
    _keep(w["lili"], s, ["record"])
    first = PersonGrant.objects.get(person=w["person"], status="active", grant_type="persistent")
    assert _search(w["ada"], t).status_code == 403                     # read never granted
    assert _grant(w["lili"], s, ["record"], "always").status_code == 200   # already held: no-op
    assert len(_issued(w["person"])) == 2
    # Widening: use is not in this session's grant, so keeping it is refused until allowed.
    assert _grant(w["lili"], s, ["use"], "always").status_code == 422
    _keep(w["lili"], s, ["use"])
    first.refresh_from_db()
    assert first.status == "revoked"
    second = PersonGrant.objects.get(person=w["person"], status="active", grant_type="persistent")
    assert set(hcp.features_of(second)) == {"record", "use"}
    assert _search(w["ada"], t).status_code == 200


def test_one_act_never_both_allows_and_keeps_it(w):
    # 4.1.4: temporary is the default outcome; persistence is a separate act.
    s = _session(w["lili"], w["ada"])
    r = _grant(w["lili"], s, ["record"], "always")
    assert r.status_code == 422 and "separate choice" in r.json()["detail"]
    assert not PersonGrant.objects.filter(person=w["person"]).exists()
    _grant(w["lili"], s, ["record"], "session")
    assert list(PersonGrant.objects.filter(person=w["person"]).values_list("grant_type", flat=True)) \
        == ["temporary"]


def test_policy_bounds_what_can_be_granted(w):
    hcp.set_agent_memory(w["person"], actor=hcp.user_actor(w["lili"]), use={"available": False})
    s = _session(w["lili"], w["ada"])
    r = _grant(w["lili"], s, ["use"], "always")
    assert r.status_code == 403
    assert not PersonGrant.objects.filter(person=w["person"]).exists()
    assert _grant(w["lili"], s, ["record"], "maybe").status_code == 422


def test_granting_a_default_off_feature_turns_it_on_for_that_session(w):
    hcp.set_agent_memory(w["person"], actor=hcp.user_actor(w["lili"]), use={"default": False})
    s = _session(w["lili"], w["ada"])
    t = _turn(w["ada"], w["lili"], s, "t1")
    assert caller_context.build(t)["person"]["hcp"]["awaiting_grant"] == ["record"]  # use not offered
    body = _grant(w["lili"], s, ["use"], "session").json()
    assert body["use"]["override"] is True and body["use"]["effective"] is True
    assert _search(w["ada"], t).status_code == 200


def test_only_the_person_grants(w):
    s = _session(w["lili"], w["ada"])
    assert _grant(w["ada"].user, s, ["use"], "always").status_code == 403       # the agent itself
    assert _grant(w["owner"], s, ["use"], "always").status_code == 404          # not their session
    assert not PersonGrant.objects.filter(person=w["person"]).exists()


def test_a_revoked_grant_is_not_revived(w):
    s = _session(w["lili"], w["ada"])
    t = _turn(w["ada"], w["lili"], s, "t1")
    _keep(w["lili"], s, ["use"])
    for g in PersonGrant.objects.filter(person=w["person"], status="active"):
        r = _as(w["lili"]).delete(f"/api/hcp/v1/grants/urn:uuid:{g.grant_id}")
        assert r.status_code == 200
    assert _search(w["ada"], t).status_code == 403
    assert caller_context.build(t)["person"]["hcp"]["awaiting_grant"] == ["record", "use"]
    assert PersonGrant.objects.filter(person=w["person"], status="active").count() == 0


@pytest.fixture()
def org(w):
    """Dimagi above two divisions — Connect (Lili's entry) and Strategy (Hal) —
    and Acme, another organisation, with its own agent. Lili also has a personal
    entry."""
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=w["owner"])
    Workspace.objects.filter(pk="connect").update(parent=dimagi)
    strategy = Workspace.objects.create(slug="strategy", display_name="Strategy", parent=dimagi,
                                        created_by=w["owner"])
    acme = Workspace.objects.create(slug="acme", display_name="Acme", created_by=w["owner"])
    people.record_fact(person=w["person"], workspace=None, kind="preference",
                       statement="Likes KC metrics as tables.", basis="declared",
                       category="work_context")
    return {**w, "dimagi": dimagi, "strategy": strategy, "acme": acme,
            "hal": _agent(strategy, "hal", w["owner"]), "zed": _agent(acme, "zed", w["owner"])}


def _found(agent, turn) -> list[str]:
    r = _search(agent, turn)
    assert r.status_code == 200, r.content
    return sorted(e["claim"]["subject"]["preference"] for e in r.json()["entries"])


def _live(person, **kw) -> PersonGrant:
    return PersonGrant.objects.get(person=person, status="active", **kw)


def _collection(user, name, sources):
    r = _json(_as(user), "post", "/api/people/me/memory-collections/",
              {"name": name, "sources": sources})
    assert r.status_code == 200, r.content
    return r.json()


def _point(user, grant, collection_id, confirm=None):
    body = {"collection_id": collection_id}
    if confirm is not None:
        body["confirm_widening"] = confirm
    return _json(_as(user), "put", f"/api/people/me/grants/{hcp.entry_urn(grant.grant_id)}/collection/",
                 body)


def test_an_agent_in_another_division_reads_org_wide_but_not_personal(org):
    """Jonathan, 2026-10-11: an agent serving me in another division "should have
    my Dimagi-wide context" — derived from the agent's organisation, never ticked."""
    session = _session(org["lili"], org["hal"])
    turn = _turn(org["hal"], org["lili"], session, "t1")
    shown = _as(org["lili"]).get(f"/api/people/me/sessions/{session.pk}/agent-memory/").json()
    assert shown["reach"] == "all of Dimagi (every workspace in it, including new ones)"   # shown before the act
    assert shown["org"] == "Dimagi"
    assert _grant(org["lili"], session, ["use"], "session").status_code == 200
    g = _live(org["person"])
    # 4.1.5.1 Rule 3: the object, the log and what is shown all name the reach.
    assert g.restrictions == [{"scope": s, "narrowedTo": {"sources": ["tree:dimagi"]}}
                              for s in g.scopes]
    (issued,) = _issued(org["person"])
    assert '"tree:dimagi"' in issued.detail
    assert "reach=only all of Dimagi (every workspace in it, including new ones)" in issued.detail
    assert hcp.grant_dict(g)["canopy"]["reach"] == "all of Dimagi (every workspace in it, including new ones)"
    # ...and resolved now, as names.
    assert hcp.grant_dict(g)["canopy"]["reach_names"] == ["Connect", "Dimagi", "Strategy"]
    # The same reach as data, for the page to mark what this agent sees: the tree, never personal.
    assert hcp.grant_dict(g)["canopy"]["reaches"] == ["connect", "dimagi", "strategy"]
    listed = _as(org["lili"]).get("/api/hcp/v1/grants").json()["grants"]
    assert listed[0]["restrictions"] == g.restrictions
    assert _found(org["hal"], turn) == ["KC metrics lead."]          # Connect's; not personal
    block = caller_context.build(turn)["person"]
    assert [f["statement"] for f in block["facts"]] == ["KC metrics lead."]
    assert block["grant"]["reach"] == "all of Dimagi (every workspace in it, including new ones)"


def test_the_person_cannot_pick_a_reach_in_the_prompt(org):
    session = _session(org["lili"], org["hal"])
    r = _json(_as(org["lili"]), "post", f"/api/people/me/sessions/{session.pk}/agent-grants/",
              {"features": ["use"], "duration": "session", "reach": ["personal"]})
    assert r.status_code == 422
    assert not PersonGrant.objects.filter(person=org["person"]).exists()


def test_a_collection_of_one_workspace_narrows_an_agent_to_it(org):
    """Collections are built from workspaces too, not only whole organisations:
    pointing an agent at one narrows it at once (no confirmation owed)."""
    session = _session(org["lili"], org["hal"])
    assert _grant(org["lili"], session, ["use"], "session").status_code == 200
    assert _grant(org["lili"], session, ["use"], "always").status_code == 200
    kept = _live(org["person"], grant_type="persistent")
    c = _collection(org["lili"], "Just Connect", ["workspace:connect"])
    assert c["reach"] == "Connect"
    assert _point(org["lili"], kept, c["id"]).status_code == 200
    kept.refresh_from_db()
    assert hcp.reach_of(kept) == {"connect"}
    assert hcp.grant_dict(kept)["canopy"]["reach_names"] == ["Connect"]


def test_personal_is_reached_only_through_a_collection_with_confirmation(org):
    session = _session(org["lili"], org["hal"])
    turn = _turn(org["hal"], org["lili"], session, "t1")
    assert _grant(org["lili"], session, ["use"], "session").status_code == 200
    assert _grant(org["lili"], session, ["use"], "always").status_code == 200
    kept = _live(org["person"], grant_type="persistent")
    listed = _as(org["lili"]).get("/api/people/me/memory-collections/").json()
    # Per organisation, the whole of it and then each workspace; entries in no workspace last.
    assert [(c["source"], c["label"], c["group"]) for c in listed["contexts"]] == [
        ("tree:dimagi", "All of Dimagi", "Dimagi"),
        ("workspace:connect", "Connect", "Dimagi"),
        ("personal", "No workspace", ""),
    ]
    c = _collection(org["lili"], "Work and me", ["tree:dimagi", "personal"])
    before = len(_issued(org["person"]))
    r = _point(org["lili"], kept, c["id"])                         # a widening, unconfirmed
    assert r.status_code == 409
    body = r.json()["extras"]
    assert body["agents"] == ["Hal"] and "entries in no workspace" in body["adds"]
    assert len(body["confirm_widening"]) == 2                        # the kept grant and the session's
    assert _found(org["hal"], turn) == ["KC metrics lead."]          # nothing changed
    assert _point(org["lili"], kept, c["id"], body["confirm_widening"]).status_code == 200
    kept.refresh_from_db()
    assert kept.collection_id == c["id"]
    assert hcp.named_sources(kept) == [f"collection:{c['id']}", "tree:dimagi", "personal"]
    assert len(_issued(org["person"])) == before + 2                 # one grant.issued per grant
    assert _found(org["hal"], turn) == ["KC metrics lead.", "Likes KC metrics as tables."]
    # a later session grant to Hal carries the collection
    s2 = _session(org["lili"], org["hal"])
    assert _grant(org["lili"], s2, ["use"], "session").status_code == 200
    temp = PersonGrant.objects.filter(person=org["person"], status="active",
                                      grant_type="temporary").order_by("-pk").first()
    assert temp.collection_id == c["id"]


def test_editing_a_collection_needs_confirmation_to_widen_and_narrows_at_once(org):
    session = _session(org["lili"], org["hal"])
    turn = _turn(org["hal"], org["lili"], session, "t1")
    assert _grant(org["lili"], session, ["use"], "session").status_code == 200
    g = _live(org["person"])
    c = _collection(org["lili"], "Just Dimagi", ["tree:dimagi"])
    assert _point(org["lili"], g, c["id"]).status_code == 200        # same reach: no confirm
    url = f"/api/people/me/memory-collections/{c['id']}/"
    r = _json(_as(org["lili"]), "put", url, {"sources": ["tree:dimagi", "personal"]})
    assert r.status_code == 409 and r.json()["extras"]["agents"] == ["Hal"]
    assert "this will also let Hal use entries in no workspace" in r.json()["detail"]
    assert _found(org["hal"], turn) == ["KC metrics lead."]
    before = len(_issued(org["person"]))
    r = _json(_as(org["lili"]), "put", url, {"sources": ["tree:dimagi", "personal"],
                                             "confirm_widening": r.json()["extras"]["confirm_widening"]})
    assert r.status_code == 200
    assert len(_issued(org["person"])) == before + 1
    assert _found(org["hal"], turn) == ["KC metrics lead.", "Likes KC metrics as tables."]
    r = _json(_as(org["lili"]), "put", url, {"sources": ["personal"]})   # narrowing: at once
    assert r.status_code == 200
    assert PersonAuditEvent.objects.filter(person=org["person"], event_type="grant.narrowed").exists()
    assert _found(org["hal"], turn) == ["Likes KC metrics as tables."]
    assert _as(org["lili"]).delete(url).status_code == 409             # still used by Hal


def test_a_collection_holds_only_the_persons_own_contexts(org):
    r = _json(_as(org["lili"]), "post", "/api/people/me/memory-collections/",
              {"name": "x", "sources": ["tree:acme"]})
    assert r.status_code == 422
    assert _json(_as(org["hal"].user), "post", "/api/people/me/memory-collections/",
                 {"name": "x", "sources": ["personal"]}).status_code in (403, 404)


def test_a_new_division_is_covered_without_a_new_grant(org):
    session = _session(org["lili"], org["hal"])
    assert _grant(org["lili"], session, ["use"], "session").status_code == 200
    Workspace.objects.create(slug="labs", display_name="Labs", parent=org["dimagi"],
                             created_by=org["owner"])
    assert hcp.reach_of(_live(org["person"])) == {"dimagi", "connect", "strategy", "labs"}


def test_another_organisations_agent_never_reaches_dimagis_memory(org):
    session = _session(org["lili"], org["zed"])
    turn = _turn(org["zed"], org["lili"], session, "t1")
    shown = _as(org["lili"]).get(f"/api/people/me/sessions/{session.pk}/agent-memory/").json()
    assert shown["reach"] == "all of Acme (every workspace in it, including new ones)"
    assert _grant(org["lili"], session, ["use"], "session").status_code == 200
    g = _live(org["person"])
    assert hcp.named_sources(g) == ["tree:acme"]
    assert _found(org["zed"], turn) == []
    # Rule 1: even a row naming Dimagi's tree reaches nothing outside Acme's.
    g.restrictions = hcp.restrictions_for(g.scopes, ["tree:dimagi", "workspace:connect"])
    g.save(update_fields=["restrictions"])
    assert hcp.reach_of(g) == set()
    assert _found(org["zed"], turn) == []


def test_an_entry_recorded_through_a_site_carries_the_site(org):
    from apps.canopy_sessions.models import Session

    session = Session.objects.create(agent=org["hal"], created_by=org["lili"],
                                     workspace=org["strategy"],
                                     metadata={"embed_app": "connect-labs"},
                                     page_state={"path": "/opportunities/12"})
    turn = _turn(org["hal"], org["lili"], session, "t1")
    assert _grant(org["lili"], session, ["record"], "session").status_code == 200
    r = _add(org["hal"], turn)
    assert r.status_code == 201, r.content
    assert "site" not in r.json()["entry"]["record"]["provenance"]   # redacted for the agent
    me = _as(org["lili"]).get("/api/people/me/").json()
    fact = next(f for f in me["facts"] if f["statement"] == "Works on KC.")
    assert fact["provenance_site"] == {"name": "connect-labs", "page": "/opportunities/12"}
    assert fact["learned_by"] == "Hal"
    export = _as(org["lili"]).get("/api/hcp/v1/export").json()
    entries = export.get("entries") or export.get("@graph") or []
    mine = [e for e in entries if e["claim"]["subject"]["preference"] == "Works on KC."]
    assert mine[0]["record"]["provenance"]["site"] == {"name": "connect-labs",
                                                       "page": "/opportunities/12"}
    # in canopy itself: no site
    plain = _session(org["lili"], org["hal"])
    t2 = _turn(org["hal"], org["lili"], plain, "t2")
    assert _grant(org["lili"], plain, ["record"], "session").status_code == 200
    _json(_as(org["hal"].user), "post", f"/api/hcp/v1/preferences/add?turn={t2.pk}",
          {"category": "work_context", "preference": "Reviews on Fridays.",
           "declarationType": "user-declared", "sourceContext": f"turn:{t2.pk}"})
    from apps.contacts.models import PersonFact
    assert PersonFact.objects.get(statement="Reviews on Fridays.").provenance_site == {}
