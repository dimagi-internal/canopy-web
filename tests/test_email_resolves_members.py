"""A DMARC-aligned email from an existing member is THAT member, not a contact.

Without this, every email sender is a contact, so an agent that publishes a
declared interface would confine its own staff's emailed instructions as a
caller's. The arrival rule (who-is-asking §2, D1): existing accounts only,
never created, and only on proof about THIS message.
"""
from __future__ import annotations

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User

from apps.agents.interface import ASK, FULL, parse
from apps.agents.models import Agent, AgentAdmin
from apps.contacts.models import Contact
from apps.harness import caller_context, services
from apps.harness import initiator as who
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership


def _hdr(result: str, domain: str = "dimagi.com"):
    return [{"name": "Authentication-Results",
             "value": f"mx.google.com; dkim=pass; spf=pass; dmarc={result} header.from={domain}"}]


@pytest.fixture()
def w():
    op = User.objects.create_user("op", "op@dimagi.com", "pw")
    sam = User.objects.create_user("sam", "sam@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=op)
    M.objects.create(user=op, workspace=ws, role=M.EDITOR)
    M.objects.create(user=sam, workspace=ws, role=M.EDITOR)
    for u in (op, sam):
        EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, owner=op)
    return {"op": op, "sam": sam, "ws": ws, "agent": agent}


def _email(agent, frm, key="e", headers=None):
    ref = {"from": frm}
    if headers is not None:
        ref["headers"] = headers
    t, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key=key,
                                 origin_ref=ref)
    return t


def test_the_owner_emailing_is_the_owner(w):
    t = _email(w["agent"], "Op <op@dimagi.com>", headers=_hdr("pass"))
    assert (t.initiator_kind, t.initiator_user_id) == (who.USER, w["op"].pk)
    assert t.initiator_contact.email == "op@dimagi.com"      # profile still rides along
    env = caller_context.build(t)
    assert env["relationship"] == "owner" and env["verified"] is True
    assert Contact.objects.get(email="op@dimagi.com").user_id == w["op"].pk   # linked


def test_a_member_is_a_member_and_an_admin_is_an_admin(w):
    assert caller_context.build(_email(w["agent"], "sam@dimagi.com", "a", _hdr("pass")))["relationship"] == "member"
    AgentAdmin.objects.create(agent=w["agent"], user=w["sam"])
    assert caller_context.build(_email(w["agent"], "sam@dimagi.com", "b", _hdr("pass")))["relationship"] == "admin"


@pytest.mark.parametrize("headers", [None, _hdr("fail"), _hdr("pass", domain="evil.example")])
def test_without_dmarc_alignment_it_stays_a_contact(w, headers):
    """No headers, a failed DMARC, or a pass for a different domain: the From:
    could be forged, so the owner's address earns nothing."""
    t = _email(w["agent"], "op@dimagi.com", headers=headers)
    assert t.initiator_kind == who.CONTACT and t.initiator_user_id is None


def test_a_spoof_after_a_real_message_is_still_a_contact(w):
    _email(w["agent"], "op@dimagi.com", "real", _hdr("pass"))
    spoof = _email(w["agent"], "op@dimagi.com", "spoof", _hdr("fail"))
    assert spoof.initiator_kind == who.CONTACT


def test_a_canopy_user_outside_the_workspace_stays_a_contact(w):
    out = User.objects.create_user("x", "x@dimagi.com", "pw")
    EmailAddress.objects.create(user=out, email=out.email, verified=True, primary=True)
    assert _email(w["agent"], "x@dimagi.com", headers=_hdr("pass")).initiator_kind == who.CONTACT


def test_an_unverified_email_address_proves_nothing(w):
    EmailAddress.objects.filter(user=w["sam"]).update(verified=False)
    assert _email(w["agent"], "sam@dimagi.com", headers=_hdr("pass")).initiator_kind == who.CONTACT


def test_user_email_alone_without_a_verified_address_proves_nothing(w):
    """`User.email` is a free field; only allauth's VERIFIED record counts."""
    EmailAddress.objects.filter(user=w["sam"]).delete()
    assert _email(w["agent"], "sam@dimagi.com", headers=_hdr("pass")).initiator_kind == who.CONTACT


def test_arrival_never_creates_an_account(w):
    before = User.objects.count()
    _email(w["agent"], "new@dimagi.com", headers=_hdr("pass"))
    assert User.objects.count() == before


def test_with_an_interface_the_owner_by_email_stays_full_and_a_stranger_is_confined(w):
    w["agent"].interface = parse({"capabilities": {"ask": {"callers": ["contact:verified"]}}})
    w["agent"].save(update_fields=["interface"])
    assert _email(w["agent"], "op@dimagi.com", "o", _hdr("pass")).capability == FULL
    partner = _email(w["agent"], "fatima@llo-foo.org", "p", _hdr("pass", domain="llo-foo.org"))
    assert partner.capability == ASK
