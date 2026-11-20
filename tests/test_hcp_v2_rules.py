"""Standing rules and directives (HCP v2 6.3, 6.4): set once by the person, enforced on every grant.

* a rule set AFTER a grant fails that grant's next read, and where it covers everything the grant
  reaches the grant is revoked;
* a grant is never issued reaching a category a rule denies its operator;
* an entry with any denied category is withheld whichever category a request names;
* directives ride with every result a client receives, and are logged on the read;
* both are the person's own act, over HTTP, never an agent's.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.contacts import hcp, people
from apps.contacts import services as contacts
from apps.contacts.models import HcpClient, PersonAuditEvent, PersonGrant, StandingRule
from apps.harness import initiator as who
from apps.harness import services
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w(agent_memory_on):
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=lili, workspace=ws, role=WorkspaceMembership.EDITOR)
    person = contacts.person_for(user=lili)
    agent = Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=owner)
    login = User.objects.create_user("ada-login", "ada@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=login, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent.user = login
    agent.save(update_fields=["user"])
    return {"ws": ws, "lili": lili, "person": person, "ada": agent}


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _json(c, method, path, body=None):
    return getattr(c, method)(path, data=json.dumps(body or {}), content_type="application/json")


def _session(w):
    from apps.canopy_sessions.models import Session

    return Session.objects.create(agent=w["ada"], created_by=w["lili"], workspace=w["ws"])


def _allow(w, session):
    r = _json(_as(w["lili"]), "post", f"/api/people/me/sessions/{session.pk}/agent-grants/",
              {"features": ["record", "use"], "duration": "session", "surface": "chat"})
    return r


def _turn(w, session, key="t1"):
    turn, _ = services.enqueue_turn(
        session=session, origin=Turn.ORIGIN_API, idempotency_key=key, prompt="KC metrics",
        initiator=who.for_user(w["lili"], via="chat", assurance=who.SESSION))
    return turn


def _search(w, turn, categories=("work_context",)):
    return _json(_as(w["ada"].user), "post", f"/api/hcp/v1/preferences/search?turn={turn.pk}",
                 {"query": "KC metrics", "categories": list(categories), "purpose": "answer"})


def _fact(w, statement="KC metrics lead.", category="work_context", kind="role", **kw):
    return people.record_fact(person=w["person"], workspace=w["ws"], kind=kind, statement=statement,
                              basis="declared", category=category, **kw)


def _rule(w, **body):
    return _json(_as(w["lili"]), "post", "/api/hcp/v2/rules", body)


def test_a_rule_set_after_a_grant_fails_the_grants_next_read(w):
    _fact(w)
    session = _session(w)
    assert _allow(w, session).status_code == 200
    turn = _turn(w, session)
    assert len(_search(w, turn).json()["entries"]) == 1
    r = _rule(w, operators=[hcp.rules.PROVIDER_OPERATOR], categories=["work_context"])
    assert r.status_code == 201, r.content
    denied = _search(w, turn)
    assert denied.status_code == 403
    # Other categories the grant reaches still work.
    _fact(w, "Prefers short replies.", category="general_preferences", kind="preference")
    assert _search(w, turn, ("general_preferences",)).status_code == 200


def test_a_rule_that_covers_everything_a_grant_reaches_revokes_it(w):
    session = _session(w)
    _allow(w, session)
    grant = PersonGrant.objects.get(person=w["person"])
    assert _rule(w, operators=["dimagi.com"]).status_code == 201       # a domain matches subdomains
    assert _rule(w, operators=["canopy.dimagi.com"]).status_code == 201
    grant.refresh_from_db()
    assert grant.status == "revoked"
    assert PersonAuditEvent.objects.filter(person=w["person"], event_type="rule.set").count() == 2
    assert "grant.revoked" in {e.event_type for e in PersonAuditEvent.objects.filter(person=w["person"])}


def test_a_grant_is_never_issued_reaching_a_category_a_rule_denies_its_operator(w):
    _rule(w, operators=[hcp.rules.PROVIDER_OPERATOR], categories=["work_context"])
    session = _session(w)
    assert _allow(w, session).status_code == 200
    grant = PersonGrant.objects.get(person=w["person"])
    assert not any("work_context" in s for s in grant.scopes) and any("general_preferences" in s for s in grant.scopes)
    issued = PersonAuditEvent.objects.get(person=w["person"], event_type="grant.issued")
    assert "excluded by your standing rules=work_context" in issued.detail
    # Denying everything refuses the grant outright, naming why.
    _rule(w, operators=[hcp.rules.PROVIDER_OPERATOR])
    refused = _allow(w, _session(w))
    assert refused.status_code == 403 and "standing rule" in refused.json()["detail"]


def test_an_entry_with_a_denied_category_is_withheld_whichever_category_is_asked(w):
    both = _fact(w, "KC metrics lead.")
    both.extra_categories = ["general_preferences"]
    both.save(update_fields=["extra_categories"])
    session = _session(w)
    _allow(w, session)
    turn = _turn(w, session)
    assert len(_search(w, turn).json()["entries"]) == 1
    _rule(w, operators=["canopy.dimagi.com"], categories=["general_preferences"])
    assert _search(w, turn).json()["entries"] == []                 # asked for work_context; withheld anyway


def test_requires_denies_only_clients_that_do_not_make_every_claim(w):
    follows = HcpClient.objects.create(client_id="a", name="A", operator="Acme", operator_id="acme.example",
                                       redirect_uris=["https://a.acme.example/cb"],
                                       claims=[hcp.rules.CLAIM_DIRECTIVES])
    ignores = HcpClient.objects.create(client_id="b", name="B", operator="Acme", operator_id="acme.example",
                                       redirect_uris=["https://b.acme.example/cb"])
    other = HcpClient.objects.create(client_id="c", name="C", operator="Other", redirect_uris=["https://c.other.example/cb"])
    assert _rule(w, operators=["acme.example"], requires=[hcp.rules.CLAIM_DIRECTIVES],
                 categories=["work_context"]).status_code == 201
    op = lambda c: hcp.rules.operator_of(PersonGrant(hcp_client=c))      # noqa: E731
    assert not hcp.rules.denial(w["person"], *op(follows)).covers(["work_context"])
    assert hcp.rules.denial(w["person"], *op(ignores)).covers(["work_context"])
    assert not hcp.rules.denial(w["person"], *op(other)).covers(["work_context"])
    # An operator-less rule needs requires, and one with neither is refused.
    assert _rule(w, requires=[hcp.rules.CLAIM_DIRECTIVES]).status_code == 201
    assert _rule(w).status_code == 422
    assert StandingRule.objects.filter(operators=[]).count() == 1


def test_a_domain_matches_its_subdomains_and_an_identifier_matches_exactly():
    assert hcp.rules.matches("acme.example", "https://api.acme.example")
    assert hcp.rules.matches("acme.example", "acme.example")
    assert not hcp.rules.matches("acme.example", "https://notacme.example")
    assert hcp.rules.matches("urn:op:1", "urn:op:1") and not hcp.rules.matches("urn:op:1", "urn:op:12")


def test_an_oauth_grant_leaves_out_what_a_rule_denies_and_the_screen_says_so(w):
    from apps.contacts import hcp_oauth

    client = HcpClient.objects.create(client_id="c1", name="App", operator="Acme", operator_id="acme.example",
                                      redirect_uris=["https://a.acme.example/cb"],
                                      allowed_scopes=["hcp:work_context:read", "hcp:general_preferences:read"])
    _rule(w, operators=["acme.example"], categories=["work_context"])
    pending = {"client": client.pk, "scopes": ["hcp:work_context:read", "hcp:general_preferences:read"],
               "sources": [], "expiry": "4h"}
    grant = hcp_oauth.issue_grant(pending, person=w["person"], user=w["lili"], persistent=False)
    assert grant.scopes == ["hcp:general_preferences:read"]
    _rule(w, operators=["acme.example"])
    with pytest.raises(hcp.HcpError):
        hcp_oauth.issue_grant(pending, person=w["person"], user=w["lili"], persistent=False)


def test_directives_ride_with_every_result_and_the_log_names_them(w):
    _fact(w)
    session = _session(w)
    _allow(w, session)
    turn = _turn(w, session)
    assert _search(w, turn).json()["directives"] == []
    r = _json(_as(w["lili"]), "post", "/api/hcp/v2/directives",
              {"action": "share", "effect": "deny", "text": "Never pass my work context to another agent.",
               "categories": ["work_context"]})
    assert r.status_code == 201, r.content
    directive_id = r.json()["directiveId"]
    body = _search(w, turn).json()
    assert [d["text"] for d in body["directives"]] == ["Never pass my work context to another agent."]
    assert directive_id in PersonAuditEvent.objects.filter(
        person=w["person"], event_type="preference.read").order_by("-pk").first().detail
    # A directive about another category is not delivered with this one's entries (6.4).
    _json(_as(w["lili"]), "post", "/api/hcp/v2/directives",
          {"action": "sign", "effect": "ask-first", "text": "Ask me before signing.", "categories": ["goals_and_constraints"]})
    assert len(_search(w, turn).json()["directives"]) == 1
    # The turn-start envelope carries them too.
    from apps.harness import caller_context
    assert [d["effect"] for d in caller_context.build(turn)["person"]["directives"]] == ["deny"]


def test_removing_a_rule_or_directive_is_audited_and_restores_the_read(w):
    _fact(w)
    session = _session(w)
    _allow(w, session)
    turn = _turn(w, session)
    rule = _rule(w, operators=["canopy.dimagi.com"], categories=["work_context"]).json()
    assert _search(w, turn).status_code == 403
    gone = _as(w["lili"]).delete(f"/api/hcp/v2/rules/{rule['ruleId']}")
    assert gone.status_code == 204
    assert _search(w, turn).status_code == 200
    assert PersonAuditEvent.objects.filter(person=w["person"], event_type="rule.removed").exists()
    d = _json(_as(w["lili"]), "post", "/api/hcp/v2/directives",
              {"action": "share", "effect": "deny", "text": "No."}).json()
    assert _as(w["lili"]).get("/api/hcp/v2/directives").json()["directives"][0]["directiveId"] == d["directiveId"]
    assert _as(w["lili"]).delete(f"/api/hcp/v2/directives/{d['directiveId']}").status_code == 204
    assert PersonAuditEvent.objects.filter(person=w["person"], event_type="directive.removed").exists()
    assert _as(w["lili"]).delete(f"/api/hcp/v2/rules/{rule['ruleId']}").status_code == 403    # gone, not disclosed


def test_rules_and_directives_are_the_persons_and_an_agent_login_is_refused(w):
    ada = _as(w["ada"].user)
    assert _json(ada, "post", "/api/hcp/v2/rules", {"operators": ["x.example"]}).status_code == 403
    assert ada.get("/api/hcp/v2/rules").status_code == 403
    assert _json(ada, "post", "/api/hcp/v2/directives", {"action": "share", "effect": "deny", "text": "x"}).status_code == 403
    with pytest.raises(hcp.HcpError):
        hcp.rules.set_rule(w["person"], operators=["x.example"], actor=hcp.agent_actor(w["ada"]))
    assert not StandingRule.objects.exists()


def test_a_bad_directive_or_rule_is_refused(w):
    c = _as(w["lili"])
    assert _json(c, "post", "/api/hcp/v2/directives", {"action": "nope", "effect": "deny", "text": "x"}).status_code == 422
    assert _json(c, "post", "/api/hcp/v2/directives", {"action": "share", "effect": "maybe", "text": "x"}).status_code == 422
    assert _json(c, "post", "/api/hcp/v2/directives", {"action": "share", "effect": "deny", "text": "  "}).status_code == 422
    assert _rule(w, operators=["x.example"], categories=["not_a_category"]).status_code == 422
