"""Domain-wide access, aligned DKIM, and the interface held on canopy-web.

* `full: [contact@dimagi.com:verified]` gives verified senders at a trusted
  domain the agent's WHOLE profile — the old allowlist, enforced by canopy on
  proof about THIS message.
* A domain that DKIM-signs its own mail but publishes no DMARC record
  (dimagi-associate.com) can still prove a message: aligned DKIM.
* The interface is live state on canopy-web, saved as the YAML its editor wrote.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.interface import ASK, FULL, InterfaceError, parse
from apps.agents.models import Agent
from apps.contacts.email_auth import grade_of
from apps.contacts.models import Contact
from apps.harness import caller_context, services
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
DOMAINS = ["dimagi.com", "dimagi-ai.com", "dimagi-associate.com"]
ACE = {"full": [f"contact@{d}:verified" for d in DOMAINS] + [f"member@{d}" for d in DOMAINS],
       "capabilities": {"ask": {"callers": ["contact"]}}}


def _hdr(from_domain, *, dmarc=None, dkim_d=None):
    parts = ["mx.google.com", f"spf=pass smtp.mailfrom={from_domain}"]
    if dkim_d:
        parts.append(f"dkim=pass header.i=@{dkim_d} header.s=google header.b=abc")
    if dmarc:
        parts.append(f"dmarc={dmarc} (p=NONE) header.from={from_domain}")
    return [{"name": "Authentication-Results", "value": "; ".join(parts)}]


@pytest.fixture()
def w():
    op = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=op)
    M.objects.create(user=op, workspace=ws, role=M.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=op, interface=parse(ACE))
    return {"op": op, "ws": ws, "agent": agent}


def _email(agent, frm, key, headers):
    t, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key=key,
                                 origin_ref={"from": frm, "headers": headers})
    return t


# --- aligned DKIM -------------------------------------------------------------------

def test_dkim_signed_by_the_from_domain_is_aligned():
    v = _hdr("dimagi-associate.com", dkim_d="dimagi-associate.com")[0]["value"]
    assert grade_of(v, from_address="nlesh@dimagi-associate.com") == Contact.AUTH_DKIM_ALIGNED


def test_dkim_signed_by_another_domain_is_not():
    v = _hdr("dimagi-associate.com", dkim_d="sendgrid.net")[0]["value"]
    assert grade_of(v, from_address="nlesh@dimagi-associate.com") == Contact.AUTH_DKIM


def test_header_d_is_read_too():
    v = "mx.google.com; dkim=pass header.d=dimagi-associate.com header.s=google"
    assert grade_of(v, from_address="a@dimagi-associate.com") == Contact.AUTH_DKIM_ALIGNED


# --- domain-wide access -------------------------------------------------------------

@pytest.mark.parametrize("domain, headers", [
    ("dimagi.com", _hdr("dimagi.com", dmarc="pass", dkim_d="dimagi.com")),
    ("dimagi-ai.com", _hdr("dimagi-ai.com", dmarc="pass", dkim_d="dimagi-ai.com")),
    ("dimagi-associate.com", _hdr("dimagi-associate.com", dkim_d="dimagi-associate.com")),
])
def test_a_verified_sender_at_each_trusted_domain_gets_the_whole_agent(w, domain, headers):
    t = _email(w["agent"], f"someone@{domain}", domain, headers)
    assert t.capability == FULL and t.status == Turn.QUEUED
    env = caller_context.build(t)
    assert env["profile"] == "full"
    assert env["granted_by"] == f"full:contact@{domain}:verified"


def test_an_unverified_message_from_a_trusted_domain_is_only_a_caller(w):
    t = _email(w["agent"], "someone@dimagi.com", "spoof", _hdr("dimagi.com", dmarc="fail"))
    assert t.capability == ASK
    assert caller_context.build(t)["granted_by"] == "capability:ask"


@pytest.mark.parametrize("addr", ["x@dimagi.com.evil.org", "x@evil-dimagi.com", "x@mail.dimagi.com"])
def test_the_domain_is_matched_exactly(w, addr):
    dom = addr.split("@")[1]
    t = _email(w["agent"], addr, addr, _hdr(dom, dmarc="pass", dkim_d=dom))
    assert t.capability == ASK


def test_an_outsider_is_a_caller(w):
    t = _email(w["agent"], "ppi@povertyindex.org", "p", _hdr("povertyindex.org", dmarc="pass"))
    assert t.capability == ASK


def test_a_member_at_a_trusted_domain_gets_every_mcp_tool(w):
    from apps.agents.interface import offered_to

    agent = w["agent"]
    agent.interface = parse({**ACE, "capabilities": {"ask": {"callers": ["contact"]},
                                                      "report": {"callers": ["contact"]}}})
    agent.save(update_fields=["interface"])
    mem = User.objects.create_user("mem", "mem@dimagi-ai.com", "pw")
    M.objects.create(user=mem, workspace=w["ws"], role=M.EDITOR)
    assert offered_to(mem, agent) == ["ask", "report"]


def test_full_rules_are_validated():
    with pytest.raises(InterfaceError, match="not a caller class"):
        parse({"full": ["everyone"]})
    with pytest.raises(InterfaceError, match="not a caller class"):
        parse({"full": ["contact@*.com"]})


# --- held on canopy-web as YAML ------------------------------------------------------

YAML = """# Who gets all of ACE.
full:
  - contact@dimagi.com:verified   # staff
capabilities:
  ask:
    callers: [contact]
callers_default: none
"""


def _client(u):
    c = Client()
    c.force_login(u)
    return c


def test_the_yaml_is_saved_verbatim_and_parsed(w):
    r = _client(w["op"]).put("/api/agents/ace/interface", {"source": YAML},
                             content_type="application/json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["source"].strip() == YAML.strip()                     # comments kept
    assert body["interface"]["full"] == ["contact@dimagi.com:verified"]
    assert _client(w["op"]).get("/api/agents/ace/interface").json()["source"].strip() == YAML.strip()


def test_bad_yaml_is_a_422_that_says_why(w):
    r = _client(w["op"]).put("/api/agents/ace/interface", {"source": "full: [unclosed"},
                             content_type="application/json")
    assert r.status_code == 422 and "YAML" in r.content.decode()


def test_yaml_cannot_construct_objects(w):
    r = _client(w["op"]).put("/api/agents/ace/interface",
                             {"source": "!!python/object/apply:os.system ['id']"},
                             content_type="application/json")
    assert r.status_code == 422


def test_exactly_one_of_source_or_interface(w):
    c = _client(w["op"])
    assert c.put("/api/agents/ace/interface", {}, content_type="application/json").status_code == 422
    assert c.put("/api/agents/ace/interface", {"source": YAML, "interface": {}},
                 content_type="application/json").status_code == 422
