"""Contacts build up HCP when THEY opt in, from a site canopy trusts for email.

Jonathan, 2026-10-09: "allow contacts to start building up HCP interactions if they
opt in from a trusted system like labs where we do trust their e-mail identity."

What these pin:
* identity — only a superuser-trusted site's signed `email_verified: true` makes
  the address a contact's HCP identity; any other site, or an unverified address,
  gets nothing; an address with a canopy account is refused (sign in instead);
* the act — every route needs a frame proof only canopy's own frame can get, so
  the site that holds the contact token cannot opt its visitor in, grant, keep,
  change their policy, revoke, read or export;
* the grant — opting in sets the contact's policy and gives a SESSION grant to this
  conversation's agent; keeping it for the agent is a separate act;
* revoke and export work from the panel, and revoking ends the agent's access.
"""

import datetime as dt
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from apps.agents.models import Agent
from apps.contacts import hcp
from apps.contacts.models import Contact, Person, PersonAuditEvent, PersonGrant
from apps.tokens import assertions
from apps.tokens.models import AppCredential, AppCredentialAgent, ContactToken
from apps.tokens.views_embed import FRAME_COOKIE
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SITE = "https://labs.example"


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


def _keypair():
    priv = ed25519.Ed25519PrivateKey.generate()
    return (
        priv.private_bytes(encoding=serialization.Encoding.PEM,
                           format=serialization.PrivateFormat.PKCS8,
                           encryption_algorithm=serialization.NoEncryption()).decode(),
        priv.public_key().public_bytes(encoding=serialization.Encoding.PEM,
                                       format=serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
    )


@pytest.fixture()
def world():
    owner = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    other = Agent.objects.create(slug="hal", name="Hal", workspace=ws)
    priv, pub = _keypair()
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=ws)
    app.public_keys = [pub]
    app.allowed_frame_origins = [SITE]
    app.asserts_verified_email = True
    app.save(update_fields=["public_keys", "allowed_frame_origins", "asserts_verified_email"])
    AppCredentialAgent.objects.create(app=app, agent=agent)
    AppCredentialAgent.objects.create(app=app, agent=other)
    return {"owner": owner, "ws": ws, "app": app, "priv": priv, "agent": agent, "other": other}


def _token(priv, *, sub="u-42", email="visitor@partner.org", verified=True):
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    claims = {"iss": "connect-labs", "sub": sub, "aud": assertions.audience(),
              "iat": now, "exp": now + 60, "jti": str(uuid.uuid4())}
    if email:
        claims.update(email=email, email_verified=verified)
    assertion = jwt.encode(claims, priv, algorithm="EdDSA")
    r = Client().post("/api/auth/contact-token", data={"assertion": assertion, "agent_slug": "echo"},
                      content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()["token"]


class Frame:
    """canopy's own frame: it loaded the shell (so it holds the cookie), and the
    browser marks its calls same-origin."""

    def __init__(self, token, *, load_shell=True):
        self.c = Client(HTTP_SEC_FETCH_SITE="same-origin", HTTP_ORIGIN="http://testserver")
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        if load_shell:
            shell = self.c.get("/embed/chat?app=connect-labs&agent=echo")
            assert shell.status_code == 200, shell.content
            assert FRAME_COOKIE in self.c.cookies

    def proof(self):
        r = self.c.post("/api/contact/hcp/proof", **self.auth)
        assert r.status_code == 200, r.content
        return r.json()["proof"]

    def call(self, method, path, data=None, proof=True):
        extra = dict(self.auth)
        if proof:
            extra["HTTP_X_CANOPY_FRAME_PROOF"] = self.proof()
        fn = getattr(self.c, method)
        if data is None:
            return fn(path, **extra)
        return fn(path, data=data, content_type="application/json", **extra)

    def session(self, agent="echo"):
        r = self.c.post("/api/contact/sessions", data={"agent_slug": agent},
                        content_type="application/json", **self.auth)
        assert r.status_code == 200, r.content
        return r.json()["id"]


def _email_person(email="visitor@partner.org"):
    return Person.objects.filter(issuer="", signer="", external_id="", email=email).first()


# --- identity ---------------------------------------------------------------------------


def test_a_trusted_sites_verified_address_is_recorded_on_the_token(world):
    raw = _token(world["priv"])
    assert ContactToken.lookup(raw).verified_email == "visitor@partner.org"


def test_an_untrusted_site_records_no_identity_and_cannot_opt_in(world):
    AppCredential.objects.filter(pk=world["app"].pk).update(asserts_verified_email=False)
    raw = _token(world["priv"])
    assert ContactToken.lookup(raw).verified_email == ""
    f = Frame(raw)
    sid = f.session()
    r = f.call("post", "/api/contact/hcp/opt-in", {"session_id": sid})
    assert r.status_code == 403
    state = f.call("get", "/api/contact/hcp/state").json()
    assert state["eligible"] is False and state["reason"] == "untrusted_site"
    assert _email_person() is None


def test_an_unverified_address_is_not_an_identity(world):
    raw = _token(world["priv"], verified=False)
    assert ContactToken.lookup(raw).verified_email == ""
    f = Frame(raw)
    r = f.call("post", "/api/contact/hcp/opt-in", {"session_id": f.session()})
    assert r.status_code == 403


def test_an_address_with_a_canopy_account_is_refused(world):
    User.objects.create_user("v", "visitor@partner.org", "pw")
    f = Frame(_token(world["priv"]))
    r = f.call("post", "/api/contact/hcp/opt-in", {"session_id": f.session()})
    assert r.status_code == 403
    assert "sign in to canopy" in r.json()["detail"]
    state = f.call("get", "/api/contact/hcp/state").json()
    assert state["reason"] == "has_account"


# --- the act is the contact's, in canopy's frame ----------------------------------------


@pytest.mark.parametrize("method,path,data", [
    ("post", "/api/contact/hcp/opt-in", {"session_id": "SID"}),
    ("post", "/api/contact/hcp/sessions/SID/agent-grants", {"features": ["record"], "duration": "session"}),
    ("post", "/api/contact/hcp/sessions/SID/agent-grants", {"features": ["record"], "duration": "always"}),
    ("put", "/api/contact/hcp/policy", {"use": {"available": True, "default": True}}),
    ("put", "/api/contact/hcp/sessions/SID/memory", {"record": "off"}),
    ("delete", "/api/contact/hcp/grants/GID", None),
    ("get", "/api/contact/hcp/state", None),
    ("get", "/api/contact/hcp/export", None),
    ("delete", "/api/contact/hcp/entries/EID", None),
])
def test_the_site_holding_the_token_can_do_none_of_it(world, method, path, data):
    raw = _token(world["priv"])
    frame = Frame(raw)
    sid = frame.session()
    frame.call("post", "/api/contact/hcp/opt-in", {"session_id": sid})
    gid = PersonGrant.objects.get(person=_email_person(), status="active").grant_id
    fact = hcp.add_entry(person=_email_person(), workspace=world["ws"], category="work_context",
                         statement="Runs the Kano pilot.", declaration="user-declared",
                         actor=hcp.SYSTEM)
    path = path.replace("SID", sid).replace("GID", str(gid)).replace("EID", hcp.entry_urn(fact.entry_id))
    if data and data.get("session_id") == "SID":
        data = {"session_id": sid}
    before = PersonAuditEvent.objects.count()

    auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
    # 1. the host page's script: cross-site, no canopy cookie, no proof
    host = Client(HTTP_SEC_FETCH_SITE="cross-site", HTTP_ORIGIN=SITE)
    # 2. the host page's script holding a proof it somehow got: still cross-site, no cookie
    stolen = {"HTTP_X_CANOPY_FRAME_PROOF": frame.proof()}
    # 3. the host's server: forges the headers but never loaded canopy's frame (no cookie)
    server = Client(HTTP_SEC_FETCH_SITE="same-origin", HTTP_ORIGIN="http://testserver")
    for client, extra in ((host, {}), (host, stolen), (server, {}), (server, stolen)):
        fn = getattr(client, method)
        r = (fn(path, **auth, **extra) if data is None
             else fn(path, data=data, content_type="application/json", **auth, **extra))
        assert r.status_code == 403, (method, path, r.status_code)
    # 4. canopy's frame without a proof
    r = frame.call(method, path, data, proof=False)
    assert r.status_code == 403
    assert PersonAuditEvent.objects.count() == before
    assert PersonGrant.objects.filter(person=_email_person(), status="active").count() == 1


def test_the_proof_cannot_be_minted_outside_the_frame(world):
    raw = _token(world["priv"])
    auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
    no_cookie = Client(HTTP_SEC_FETCH_SITE="same-origin", HTTP_ORIGIN="http://testserver")
    assert no_cookie.post("/api/contact/hcp/proof", **auth).status_code == 403
    f = Frame(raw)
    f.c.defaults["HTTP_SEC_FETCH_SITE"] = "same-site"
    assert f.c.post("/api/contact/hcp/proof", **f.auth).status_code == 403


def test_a_proof_is_spent_by_the_call_it_authorizes(world):
    f = Frame(_token(world["priv"]))
    proof = f.proof()
    hdr = {**f.auth, "HTTP_X_CANOPY_FRAME_PROOF": proof}
    assert f.c.get("/api/contact/hcp/state", **hdr).status_code == 200
    assert f.c.get("/api/contact/hcp/state", **hdr).status_code == 403


def test_a_proof_is_bound_to_its_frame_cookie(world):
    raw = _token(world["priv"])
    a, b = Frame(raw), Frame(raw)
    proof = a.proof()
    r = b.c.get("/api/contact/hcp/state", **b.auth, HTTP_X_CANOPY_FRAME_PROOF=proof)
    assert r.status_code == 403


def test_canopy_on_the_sites_own_origin_is_not_a_frame(world):
    AppCredential.objects.filter(pk=world["app"].pk).update(
        allowed_frame_origins=[SITE, "http://testserver"])
    f = Frame(_token(world["priv"]))
    assert f.c.post("/api/contact/hcp/proof", **f.auth).status_code == 403


# --- opting in --------------------------------------------------------------------------


def test_opting_in_sets_policy_and_grants_this_conversations_agent_for_the_session(world):
    f = Frame(_token(world["priv"]))
    sid = f.session()
    r = f.call("post", "/api/contact/hcp/opt-in", {"session_id": sid})
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["opted_in"] is True and body["email"] == "visitor@partner.org"
    person = _email_person()
    assert person.hcp_record_available and person.hcp_record_default
    assert not person.hcp_use_available, "use stays off unless they ticked it"
    contact = Contact.objects.get(external_id="u-42")
    assert contact.person_id == person.pk
    grant = PersonGrant.objects.get(person=person, status="active")
    assert grant.grant_type == PersonGrant.TEMPORARY and grant.agent == world["agent"]
    assert grant.modality == "canopy-embed-trusted-email"
    assert hcp.features_of(grant) == {"record"}
    issued = PersonAuditEvent.objects.get(person=person, event_type="grant.issued")
    assert "site=connect-labs" in issued.detail
    assert issued.actor_type == PersonAuditEvent.USER and issued.actor_id.startswith("contact:")
    assert body["session"]["record"]["granted"] is True


def test_ticking_use_makes_use_available_and_grants_it(world):
    f = Frame(_token(world["priv"]))
    r = f.call("post", "/api/contact/hcp/opt-in", {"session_id": f.session(), "use": True})
    assert r.status_code == 200
    person = _email_person()
    assert person.hcp_use_available
    grant = PersonGrant.objects.get(person=person, status="active")
    assert hcp.features_of(grant) == {"record", "use"}


def test_keeping_it_for_the_agent_is_a_separate_act(world):
    f = Frame(_token(world["priv"]))
    sid = f.session()
    f.call("post", "/api/contact/hcp/opt-in", {"session_id": sid})
    assert not PersonGrant.objects.filter(grant_type=PersonGrant.PERSISTENT).exists()
    r = f.call("post", f"/api/contact/hcp/sessions/{sid}/agent-grants",
               {"features": ["record"], "duration": "always"})
    assert r.status_code == 200, r.content
    keep = PersonGrant.objects.get(grant_type=PersonGrant.PERSISTENT, status="active")
    assert keep.agent == world["agent"]


def test_a_grant_to_one_agent_is_not_a_grant_to_another(world):
    f = Frame(_token(world["priv"]))
    f.call("post", "/api/contact/hcp/opt-in", {"session_id": f.session()})
    other = f.session("hal")
    state = f.call("get", f"/api/contact/hcp/state?session_id={other}").json()
    assert state["session"]["record"]["granted"] is False


def test_the_same_visitor_on_a_second_arrival_is_the_same_person(world):
    f = Frame(_token(world["priv"]))
    f.call("post", "/api/contact/hcp/opt-in", {"session_id": f.session()})
    g = Frame(_token(world["priv"]))
    state = g.call("get", "/api/contact/hcp/state").json()
    assert state["opted_in"] is True and len(state["grants"]) == 1


# --- the person controls it from the panel ----------------------------------------------


def test_revoking_from_the_panel_ends_the_agents_access(world):
    f = Frame(_token(world["priv"]))
    sid = f.session()
    f.call("post", "/api/contact/hcp/opt-in", {"session_id": sid})
    person = _email_person()
    assert hcp.granted(person, world["agent"], _session(sid))["record"]
    gid = hcp.entry_urn(PersonGrant.objects.get(person=person, status="active").grant_id)
    r = f.call("delete", f"/api/contact/hcp/grants/{gid}")
    assert r.status_code == 200, r.content
    assert r.json()["grants"] == []
    assert not hcp.granted(person, world["agent"], _session(sid))["record"]


def test_opting_out_turns_policy_off_and_deletes_nothing(world):
    f = Frame(_token(world["priv"]))
    f.call("post", "/api/contact/hcp/opt-in", {"session_id": f.session()})
    person = _email_person()
    hcp.add_entry(person=person, workspace=world["ws"], category="work_context",
                  statement="Runs the Kano pilot.", declaration="user-declared",
                  actor=hcp.SYSTEM)
    r = f.call("put", "/api/contact/hcp/policy",
               {"record": {"available": False}, "use": {"available": False}})
    assert r.status_code == 200
    assert r.json()["opted_in"] is False
    person.refresh_from_db()
    assert not person.hcp_record_available
    assert person.facts.exists()


def test_export_from_the_panel(world):
    f = Frame(_token(world["priv"]))
    f.call("post", "/api/contact/hcp/opt-in", {"session_id": f.session()})
    hcp.add_entry(person=_email_person(), workspace=world["ws"], category="work_context",
                  statement="Runs the Kano pilot.", declaration="user-declared",
                  actor=hcp.SYSTEM)
    r = f.call("get", "/api/contact/hcp/export")
    assert r.status_code == 200, r.content
    body = r.json()
    assert [e["preference"] if "preference" in e else e for e in body["entries"]]
    assert any(a["eventType"] == "grant.issued" for a in body["audit"])


# --- the site's registration ------------------------------------------------------------


def test_only_a_superuser_trusts_a_site(world):
    c = Client()
    c.force_login(world["owner"])
    url = f"/api/workspaces/w1/connected-apps/{world['app'].pk}"
    r = c.patch(url, data={"asserts_verified_email": False}, content_type="application/json")
    assert r.status_code == 403
    root = User.objects.create_superuser("root", "root@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=root, workspace=world["ws"], role=WorkspaceMembership.OWNER)
    c.force_login(root)
    r = c.patch(url, data={"asserts_verified_email": False}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["asserts_verified_email"] is False


def test_changing_a_trusted_sites_keys_clears_the_trust(world):
    c = Client()
    c.force_login(world["owner"])
    _priv, pub = _keypair()
    r = c.patch(f"/api/workspaces/w1/connected-apps/{world['app'].pk}",
                data={"public_keys": [pub]}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["asserts_verified_email"] is False


def _session(sid):
    from apps.canopy_sessions.models import Session

    return Session.objects.get(pk=sid)


def test_the_agent_sees_the_same_hcp_block_for_an_opted_in_contact(world):
    from apps.contacts import people
    from apps.harness.models import Turn

    f = Frame(_token(world["priv"]))
    sid = f.session()
    f.call("post", "/api/contact/hcp/opt-in", {"session_id": sid})
    r = f.c.post(f"/api/contact/sessions/{sid}/send", data={"text": "hello"},
                 content_type="application/json", **f.auth)
    assert r.status_code == 200, r.content
    turn = Turn.objects.filter(chat_session_id=sid).latest("created_at")
    block = people.envelope_block(turn, agent=world["agent"], workspace_slug="w1")
    assert block["hcp"]["record"] is True and block["hcp"]["use"] is False
    assert block["hcp"]["granted"]["record"] is True


def test_a_contact_who_never_opted_in_gets_no_memory(world):
    from apps.contacts import people
    from apps.harness.models import Turn

    f = Frame(_token(world["priv"]))
    sid = f.session()
    f.c.post(f"/api/contact/sessions/{sid}/send", data={"text": "hello"},
             content_type="application/json", **f.auth)
    turn = Turn.objects.filter(chat_session_id=sid).latest("created_at")
    block = people.envelope_block(turn, agent=world["agent"], workspace_slug="w1")
    assert block is None or not block.get("hcp", {}).get("record")


def test_a_contact_turns_learning_off_for_one_conversation(world):
    f = Frame(_token(world["priv"]))
    sid = f.session()
    f.call("post", "/api/contact/hcp/opt-in", {"session_id": sid})
    r = f.call("put", f"/api/contact/hcp/sessions/{sid}/memory", {"record": "off"})
    assert r.status_code == 200, r.content
    assert r.json()["session"]["record"]["effective"] is False
    r = f.call("put", f"/api/contact/hcp/sessions/{sid}/memory", {"use": "on"})
    assert r.status_code == 403, "use is not available, so a session cannot turn it on"


def test_a_contact_lists_and_removes_an_entry_from_the_panel(world):
    f = Frame(_token(world["priv"]))
    f.call("post", "/api/contact/hcp/opt-in", {"session_id": f.session()})
    fact = hcp.add_entry(person=_email_person(), workspace=world["ws"], category="work_context",
                         statement="Runs the Kano pilot.", declaration="user-declared",
                         actor=hcp.SYSTEM)
    state = f.call("get", "/api/contact/hcp/state").json()
    assert [e["statement"] for e in state["entries"]] == ["Runs the Kano pilot."]
    r = f.call("delete", f"/api/contact/hcp/entries/{hcp.entry_urn(fact.entry_id)}")
    assert r.status_code == 200, r.content
    assert r.json()["entries"] == []
    fact.refresh_from_db()
    assert fact.status == "deleted"
