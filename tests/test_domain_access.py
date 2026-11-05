"""Domain-wide access and aligned DKIM.

* An access-list line "anyone at dimagi.com, verified -> editor" gives verified senders at a trusted
  domain the agent's WHOLE profile, enforced by canopy on proof about THIS message.
* A domain that DKIM-signs its own mail but publishes no DMARC record
  (dimagi-associate.com) can still prove a message: aligned DKIM.

The interface saved as YAML is tested in tests/test_declared_interface.py.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.agents import roles
from apps.agents.interface import FULL, READ_MODE_STAMP
from apps.agents.models import Agent
from apps.contacts.email_auth import grade_of
from apps.contacts.models import Contact
from apps.harness import caller_context, services
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

from .access_helpers import allow, declare

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
DOMAINS = ["dimagi.com", "dimagi-ai.com", "dimagi-associate.com"]


def _ace(agent, *extra_contact_sites):
    """Verified contacts and members of each trusted domain are editors; any contact may use read mode
    (and use `extra_contact_sites`)."""
    declare(agent, {name: {} for name in ("ask", *extra_contact_sites)})
    for d in DOMAINS:
        allow(agent, role=roles.EDITOR, contact=True, domain=d, verified=True)
        allow(agent, role=roles.EDITOR, domain=d)
    allow(agent, role=roles.READ_ONLY, contact=True)
    for name in extra_contact_sites:
        allow(agent, capability=name, contact=True)


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
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=op)
    _ace(agent)
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
    assert env["role"] == "editor" and env["source"].startswith("list:") and domain in env["source"]


def test_an_unverified_message_from_a_trusted_domain_is_only_a_caller(w):
    t = _email(w["agent"], "someone@dimagi.com", "spoof", _hdr("dimagi.com", dmarc="fail"))
    assert t.capability == READ_MODE_STAMP
    assert caller_context.build(t)["granted_by"] == "capability:ask"


@pytest.mark.parametrize("addr", ["x@dimagi.com.evil.org", "x@evil-dimagi.com", "x@mail.dimagi.com"])
def test_the_domain_is_matched_exactly(w, addr):
    dom = addr.split("@")[1]
    t = _email(w["agent"], addr, addr, _hdr(dom, dmarc="pass", dkim_d=dom))
    assert t.capability == READ_MODE_STAMP


def test_an_outsider_is_a_caller(w):
    t = _email(w["agent"], "ppi@povertyindex.org", "p", _hdr("povertyindex.org", dmarc="pass"))
    assert t.capability == READ_MODE_STAMP


def test_a_member_at_a_trusted_domain_gets_every_mcp_tool(w):
    from apps.agents.interface import offered_to

    agent = w["agent"]
    _ace(agent, "report")
    mem = User.objects.create_user("mem", "mem@dimagi-ai.com", "pw")
    M.objects.create(user=mem, workspace=w["ws"], role=M.EDITOR)
    assert offered_to(mem, agent) == ["ask", "report"]
