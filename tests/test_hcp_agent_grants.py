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
import importlib
import json

import pytest
from django.apps import apps as django_apps
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


def test_the_migration_retires_every_presumed_grant(w):
    keep = PersonGrant.objects.create(
        person=w["person"], workspace=w["ws"], agent=w["ada"], client_key="k1", client_name="Ada",
        scopes=hcp.default_scopes(), modality="canopy-chat")
    presumed = PersonGrant.objects.create(
        person=w["person"], workspace=w["ws"], agent=w["eva"], client_key="k2", client_name="Eva",
        scopes=hcp.default_scopes(), modality="canopy-control-plane")
    mod = importlib.import_module("apps.contacts.migrations.0022_retire_presumed_grants")
    mod.forwards(django_apps, None)
    presumed.refresh_from_db()
    keep.refresh_from_db()
    assert (presumed.status, keep.status) == ("revoked", "active")
    ev = PersonAuditEvent.objects.get(person=w["person"], event_type="grant.revoked")
    assert ev.grant_id == presumed.grant_id and ev.actor_type == "system"
    assert "only ever given by you" in ev.detail
