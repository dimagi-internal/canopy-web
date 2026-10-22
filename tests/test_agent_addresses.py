"""An agent may have several email addresses, apart from its Google identity.

Ada keeps her Gmail mailbox ada@dimagi-ai.com and also holds ada@dimagi.ai (an SES
route); a new agent may have ONLY a canopy-hosted <slug>@dimagi.ai address while it signs
in to Drive/Docs/gog as <slug>@dimagi-ai.com (`Agent.google_account`)."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import Turn
from apps.inbound.models import InboundMailbox
from apps.inbound.signals import _mailbox_for
from apps.mail import agent_mail, domains, parse, ses
from apps.mail.models import MailDomain, MailMessage, MailRoute, MailThread
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
FIX = Path(__file__).parent / "fixtures" / "mail"


@pytest.fixture()
def w(settings, monkeypatch):
    settings.MAIL_ENABLED = True
    sent = []
    monkeypatch.setattr(ses, "send_raw", lambda **kw: sent.append(kw) or f"ses-{len(sent)}")
    monkeypatch.setattr(ses, "keep", lambda k: "kept/" + k.split("/", 1)[-1])
    monkeypatch.setattr(ses, "put_object", lambda *a, **k: None)
    owner = User.objects.create_user("jj", "jjackson@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="fleet", display_name="F", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
    ai = MailDomain.objects.create(workspace=ws, name="dimagi.ai", status="verified")
    other = MailDomain.objects.create(workspace=ws, name="canopy.test", status="verified")
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=owner,
                               email="ada@dimagi-ai.com")
    return type("W", (), dict(owner=owner, ws=ws, ai=ai, other=other, ada=ada, sent=sent))


def _c(u):
    c = Client()
    c.force_login(u)
    return c


def _route(w, local, domain=None, agent=None, **kw):
    return MailRoute.objects.create(domain=domain or w.ai, local_part=local, kind="agent",
                                    agent=agent or w.ada, **kw)


def _send(w, **body):
    return _c(w.owner).post("/api/mail/send/fleet", {"agent": "ada", "to": ["x@x.org"], "subject": "Hi",
                                                     "text": "x", **body},
                            content_type="application/json")


# --- two agent routes for one agent ------------------------------------------------

def test_an_agent_with_an_address_can_be_given_a_second_one(w):
    c = _c(w.owner)
    for domain, local in ((w.ai, "ada"), (w.other, "ada-two")):
        r = c.put(f"/api/mail/domains/fleet/{domain.pk}/routes",
                  {"local_part": local, "kind": "agent", "agent_slug": "ada"},
                  content_type="application/json")
        assert r.status_code == 200, r.content
    assert sorted(MailRoute.objects.filter(agent=w.ada).values_list("local_part", flat=True)) == [
        "ada", "ada-two"]


# --- from_address --------------------------------------------------------------------

def test_from_address_picks_one_of_the_agents_own_addresses(w):
    _route(w, "ada")
    _route(w, "ada", domain=w.other)
    r = _send(w, from_address="ADA@canopy.test")
    assert r.status_code == 200, r.content
    assert w.sent[-1]["from_addr"] == "ada@canopy.test"
    assert MailThread.objects.get(subject="Hi").address == "ada@canopy.test"


@pytest.mark.parametrize("bad", ["eve@dimagi.ai", "ada@dimagi-ai.com", "off@dimagi.ai", "nonsense"])
def test_from_address_must_be_a_live_route_of_this_agent(w, bad):
    _route(w, "ada")
    eve = Agent.objects.create(slug="eve", name="Eve", workspace=w.ws, owner=w.owner)
    _route(w, "eve", agent=eve)
    _route(w, "off", enabled=False)
    r = _send(w, from_address=bad)
    assert r.status_code == 422 and "not one of this agent's addresses" in r.json()["detail"]
    assert w.sent == []


def test_from_address_is_refused_on_a_domain_no_longer_shared(w):
    guest = Workspace.objects.create(slug="guest", display_name="G", created_by=w.owner)
    WorkspaceMembership.objects.create(workspace=guest, user=w.owner, role="owner")
    g = Agent.objects.create(slug="gee", name="Gee", workspace=guest, owner=w.owner)
    MailRoute.objects.create(domain=w.ai, workspace=guest, local_part="gee", kind="agent", agent=g)
    r = _c(w.owner).post("/api/mail/send/guest", {"agent": "gee", "to": ["x@x.org"], "subject": "s",
                                                  "text": "x", "from_address": "gee@dimagi.ai"},
                         content_type="application/json")
    assert r.status_code == 422


def test_without_from_address_the_agents_own_email_wins_else_the_first(w):
    _route(w, "ada", domain=w.other)       # canopy.test sorts first
    _route(w, "ada")                       # dimagi.ai
    assert _send(w).status_code == 200
    assert w.sent[-1]["from_addr"] == "ada@canopy.test", "agent.email is Gmail: first route"
    Agent.objects.filter(pk=w.ada.pk).update(email="ada@dimagi.ai")
    assert _send(w).status_code == 200
    assert w.sent[-1]["from_addr"] == "ada@dimagi.ai", "the route for agent.email wins"
    listed = _c(w.owner).get("/api/mail/threads/fleet", {"agent": "ada"}).json()
    assert listed["agent_address"] == "ada@dimagi.ai"


# --- resolve?thread ------------------------------------------------------------------

def test_resolve_a_thread_the_caller_may_see(w):
    r = _route(w, "ada")
    t = MailThread.objects.create(workspace=w.ws, route=r, subject="s")
    got = _c(w.owner).get("/api/mail/resolve", {"thread": str(t.uuid)})
    assert got.status_code == 200
    assert got.json() == {"backend": "ses", "workspace": "fleet", "agent": "ada", "address": "ada@dimagi.ai"}
    by_address = _c(w.owner).get("/api/mail/resolve", {"address": "ada@dimagi.ai"}).json()
    assert by_address["address"] == "ada@dimagi.ai"


def test_resolve_a_thread_is_404_for_a_stranger_a_gmail_id_or_nothing(w):
    t = MailThread.objects.create(workspace=w.ws, route=_route(w, "ada"), subject="s")
    stranger = User.objects.create_user("s", "s@s.com", "pw")
    assert _c(stranger).get("/api/mail/resolve", {"thread": str(t.uuid)}).status_code == 404
    assert _c(w.owner).get("/api/mail/resolve", {"thread": "18c2f0a1b2c3d4e5"}).status_code == 404
    assert _c(w.owner).get("/api/mail/resolve",
                           {"thread": "00000000-0000-0000-0000-000000000000"}).status_code == 404
    assert _c(w.owner).get("/api/mail/resolve").status_code == 422


# --- the agent's addresses -----------------------------------------------------------

def test_the_mailbox_lists_every_address_once_primary_first(w):
    _route(w, "ada")
    _route(w, "ada", domain=w.other)
    _route(w, "old", enabled=False)
    InboundMailbox.objects.create(address="ada@dimagi-ai.com", agent=w.ada)
    InboundMailbox.objects.create(address="ada.alt@dimagi-ai.com", agent=w.ada)
    got = _c(w.owner).get("/api/mail/agents/ada/mailbox").json()["addresses"]
    assert got == [
        {"address": "ada@dimagi-ai.com", "backend": "gmail", "primary": True},
        {"address": "ada@canopy.test", "backend": "ses", "primary": False},
        {"address": "ada@dimagi.ai", "backend": "ses", "primary": False},
        {"address": "ada.alt@dimagi-ai.com", "backend": "gmail", "primary": False},
    ]


def test_a_hosted_primary_is_ses(w):
    Agent.objects.filter(pk=w.ada.pk).update(email="Ada@Dimagi.ai")
    _route(w, "ada")
    w.ada.refresh_from_db()
    assert domains.agent_addresses(w.ada) == [{"address": "ada@dimagi.ai", "backend": "ses", "primary": True}]


# --- google identity -----------------------------------------------------------------

def test_google_identity_rules(w):
    a = w.ada
    assert a.google_identity() == "ada@dimagi-ai.com", "a Google mailbox is its own login"
    a.email = "ada@dimagi.ai"
    assert a.google_identity() == "", "a canopy-hosted address is no Google account"
    a.google_account = "Ada@Dimagi-AI.com"
    assert a.google_identity() == "ada@dimagi-ai.com", "the explicit account wins"
    a.email, a.google_account = "", ""
    assert a.google_identity() == ""


def test_upsert_takes_google_account_and_keeps_it_when_omitted(w):
    c = _c(w.owner)
    body = {"slug": "nova", "name": "Nova", "email": "nova@dimagi.ai", "workspace": "fleet",
            "google_account": "Nova@dimagi-ai.com"}
    r = c.post("/api/agents/", body, content_type="application/json")
    assert r.status_code == 201, r.content
    assert r.json()["google_account"] == "nova@dimagi-ai.com"
    body.pop("google_account")
    assert c.post("/api/agents/", body, content_type="application/json").status_code == 201
    assert Agent.objects.get(slug="nova").google_account == "nova@dimagi-ai.com"
    assert c.get("/api/agents/nova/").json()["google_account"] == "nova@dimagi-ai.com"
    bad = c.post("/api/agents/", {**body, "google_account": "nope"}, content_type="application/json")
    assert bad.status_code == 422


def test_scaffold_input_accepts_google_account():
    from apps.agents.schemas import AgentScaffoldIn

    s = AgentScaffoldIn(workspace="fleet", slug="nova", name="Nova", mandate="m", repo="o/r",
                        token="t", mailbox="nova@dimagi.ai", google_account="nova@dimagi-ai.com")
    assert s.google_account == "nova@dimagi-ai.com"


def _bearer(user) -> Client:
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    c = Client()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {raw}"
    return c


def test_credentials_resolve_names_the_google_account(w):
    nova = Agent.objects.create(slug="nova", name="Nova", workspace=w.ws, owner=w.owner,
                                email="nova@dimagi.ai", google_account="nova@dimagi-ai.com")
    _route(w, "nova", agent=nova)
    out = _bearer(w.owner).get("/api/agents/nova/credentials/resolve").json()
    assert out["mailbox"] == "nova@dimagi.ai" and out["mailbox_backend"] == "ses"
    assert out["google_account"] == "nova@dimagi-ai.com"
    ada = _bearer(w.owner).get("/api/agents/ada/credentials/resolve").json()
    assert ada["google_account"] == "ada@dimagi-ai.com"


def test_the_google_mint_hints_the_google_account(w, monkeypatch):
    from apps.agents.credentials import google_oauth

    monkeypatch.setattr(google_oauth, "google_client_credentials", lambda: ("cid", "secret"))
    Agent.objects.filter(pk=w.ada.pk).update(email="ada@dimagi.ai", google_account="ada.g@dimagi-ai.com")

    def hint(**params):
        r = _c(w.owner).get("/api/agents/ada/google/authorize", params)
        assert r.status_code == 200, r.content
        return parse_qs(urlparse(r.json()["url"]).query)["login_hint"][0]

    assert hint() == "ada.g@dimagi-ai.com"
    assert hint(login_hint="x@y.com") == "x@y.com"
    Agent.objects.filter(pk=w.ada.pk).update(google_account="")
    assert hint() == "ada@dimagi-ai.com", "hosted mailbox, no Google account: the slug default"


# --- origin_ref names the mailbox ----------------------------------------------------

def test_an_ses_turn_names_the_mailbox_it_arrived_at(w):
    r = _route(w, "ada")
    assert agent_mail.deliver(r, parse.parse((FIX / "plain.eml").read_bytes()), "incoming/abc") == "agent"
    ref = Turn.objects.get(origin=Turn.ORIGIN_EMAIL).origin_ref
    assert ref["backend"] == "ses" and ref["mailbox"] == "ada@dimagi.ai"
    assert ref["thread_id"] == str(MailMessage.objects.get().thread.uuid)


def test_push_miss_audit_picks_the_mailbox_the_turn_names(w):
    first = InboundMailbox.objects.create(address="ada@dimagi-ai.com", agent=w.ada)
    second = InboundMailbox.objects.create(address="ada.alt@dimagi-ai.com", agent=w.ada)
    turn = Turn(agent=w.ada, origin=Turn.ORIGIN_EMAIL, origin_ref={"mailbox": "Ada.Alt@dimagi-ai.com"})
    assert _mailbox_for(turn) == second
    turn.origin_ref = {}
    assert _mailbox_for(turn) == first
    turn.origin_ref = {"mailbox": "elsewhere@x.org"}
    assert _mailbox_for(turn) is None
