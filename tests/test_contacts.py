"""A person canopy knows, and the line between knowing and admitting.

The use case: someone at a partner organisation emails `ace@dimagi-ai.com`. For
the agent to remember them next time, and for routing to ever depend on who they
are, canopy needs a place to put that person — without handing an external
partner the tenant.

Two properties carry the weight here, and both are security properties:

1. **Recording a contact grants nothing.** This codebase spent a release
   removing every path where writing a row produced a `WorkspaceMembership` as a
   side effect. "An inbound email creates a user" is that same pattern arriving
   through the mail slot, so it is asserted directly rather than assumed.
2. **A forged `Authentication-Results` header must not authenticate anyone.**
   It is an ordinary header; anyone can add one. Only our own receiver's counts.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.agents.models import Agent
from apps.contacts import services as contacts
from apps.contacts.email_auth import grade_from_headers, grade_of
from apps.contacts.models import Contact
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db

GOOGLE = "mx.google.com"
PASS_ALL = (
    "mx.google.com; dkim=pass header.i=@llo-foo.org; "
    "spf=pass (google.com: domain of fatima@llo-foo.org designates 1.2.3.4 as "
    "permitted sender) smtp.mailfrom=fatima@llo-foo.org; "
    "dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=llo-foo.org"
)


@pytest.fixture
def ws():
    return a_workspace("contacts-ws")


# --- the property that matters most -------------------------------------------

def test_recording_a_sender_grants_no_membership(ws):
    """The whole point of a Contact rather than a User.

    An external partner emailing an agent must not become a member of the
    tenant that runs it — that would hand them every agent, project and
    shareout in the workspace. Being known and being let in are different
    things, and only the first one happens automatically.
    """
    contact = contacts.record_inbound_sender(
        workspace=ws, address="Fatima <fatima@llo-foo.org>", display_name="Fatima",
    )
    assert contact is not None
    assert contact.email == "fatima@llo-foo.org"
    assert not WorkspaceMembership.objects.exists()
    assert contact.user is None


def test_linking_a_contact_to_a_user_still_grants_no_membership(ws):
    """Even the deliberate promotion step is not a grant.

    Someone can authenticate for real — prove they own the address — and still
    not be a member of anything. Membership stays a separate decision made by
    an owner, through the workspaces service like every other grant.
    """
    contact = contacts.record_inbound_sender(workspace=ws, address="fatima@llo-foo.org")
    user = get_user_model().objects.create_user(username="fatima", email="fatima@llo-foo.org")
    contacts.promote_to_user(contact, user)
    contact.refresh_from_db()
    assert contact.user_id == user.pk
    assert not WorkspaceMembership.objects.exists()


# --- email authentication ------------------------------------------------------

def test_a_forged_authentication_results_header_authenticates_nobody():
    """THE security test for this module.

    `Authentication-Results` is an ordinary header — a sender can put
    `dmarc=pass` in a message they compose. A parser that scans every header
    believes them, and the answer to "is this sender authenticated" then comes
    from the sender. Only the header written by OUR receiver may be read.
    """
    headers = [
        # What an attacker puts in the message they send.
        ("Authentication-Results", "evil.example; dmarc=pass header.from=llo-foo.org"),
        ("From", "Fatima <fatima@llo-foo.org>"),
    ]
    grade, raw = grade_from_headers(headers, authserv_id=GOOGLE, from_address="fatima@llo-foo.org")
    assert grade == Contact.AUTH_NONE
    assert raw == ""


def test_our_own_receivers_verdict_is_read(ws):
    headers = [("Authentication-Results", PASS_ALL), ("From", "Fatima <fatima@llo-foo.org>")]
    grade, raw = grade_from_headers(headers, authserv_id=GOOGLE, from_address="fatima@llo-foo.org")
    assert grade == Contact.AUTH_DMARC
    assert "dmarc=pass" in raw


def test_the_attackers_header_does_not_win_by_being_first():
    """RFC 8601 puts the newest header first, and ours is the newest — but an
    attacker's is in the message body from the start. Selection is by
    authserv-id, not by position, so ordering cannot decide the outcome."""
    forged = ("Authentication-Results", "evil.example; dmarc=pass header.from=llo-foo.org")
    ours = ("Authentication-Results", PASS_ALL)
    for headers in ([forged, ours], [ours, forged]):
        grade, _ = grade_from_headers(
            headers, authserv_id=GOOGLE, from_address="fatima@llo-foo.org")
        assert grade == Contact.AUTH_DMARC


def test_no_authserv_id_means_nothing_is_trusted():
    """Fail closed. A deployment that has not said which receiver it trusts
    cannot have a trusted verdict, and falling back to "read whatever is there"
    would make the unconfigured case the insecure one."""
    grade, _ = grade_from_headers([("Authentication-Results", PASS_ALL)], authserv_id="")
    assert grade == Contact.AUTH_NONE


@pytest.mark.parametrize("value,expected", [
    ("mx.google.com; spf=pass smtp.mailfrom=x@llo-foo.org", Contact.AUTH_SPF),
    ("mx.google.com; dkim=pass header.i=@llo-foo.org", Contact.AUTH_DKIM),
    ("mx.google.com; spf=softfail; dkim=fail; dmarc=fail", Contact.AUTH_NONE),
    ("", Contact.AUTH_NONE),
])
def test_each_mechanism_earns_its_own_tier(value, expected):
    """Graded, not boolean. Partner organisations run mail of wildly varying
    quality and refusing to talk to the ones without DMARC is not an option —
    so every message yields a tier and "unverified" stays workable."""
    assert grade_of(value) == expected


def test_dmarc_only_counts_when_it_is_about_the_right_domain():
    """Alignment is the property DMARC exists to provide.

    A genuine `dmarc=pass` for `attacker.example` says nothing about a message
    attributed to `fatima@llo-foo.org`. Honouring it because the word "pass"
    appears would throw away the only check that ties a verdict to the visible
    `From:`. It falls back to the DKIM/SPF tier rather than being discarded.
    """
    misaligned = "mx.google.com; dkim=pass; dmarc=pass header.from=attacker.example"
    assert grade_of(misaligned, from_address="fatima@llo-foo.org") == Contact.AUTH_DKIM


# --- accumulating what we know -------------------------------------------------

def test_the_grade_is_a_high_water_mark_and_the_drop_stays_visible(ws):
    """A forwarded message breaks SPF, and that must not downgrade a
    correspondent who has always passed DMARC. But a genuine drop is the
    interesting signal, so the latest grade is kept beside the best one."""
    contacts.record_inbound_sender(
        workspace=ws, address="fatima@llo-foo.org",
        headers=[("Authentication-Results", PASS_ALL)], authserv_id=GOOGLE)
    contacts.record_inbound_sender(
        workspace=ws, address="fatima@llo-foo.org",
        headers=[("Authentication-Results", "mx.google.com; spf=softfail")], authserv_id=GOOGLE)

    c = Contact.objects.get(workspace=ws, email="fatima@llo-foo.org")
    assert c.auth_result == Contact.AUTH_DMARC
    assert c.last_auth_result == Contact.AUTH_NONE
    assert c.message_count == 2
    assert c.auth_at_least(Contact.AUTH_DKIM)


def test_a_later_sender_cannot_relabel_an_established_contact(ws):
    """Display name is attacker-supplied on every message. Letting each one
    overwrite would let someone who learns an address rename that contact in
    the tenant's records."""
    contacts.record_inbound_sender(
        workspace=ws, address="fatima@llo-foo.org", display_name="Fatima Diallo")
    contacts.record_inbound_sender(
        workspace=ws, address="fatima@llo-foo.org", display_name="Jonathan Jackson")
    assert Contact.objects.get(email="fatima@llo-foo.org").display_name == "Fatima Diallo"


def test_the_same_person_in_two_tenants_is_two_contacts(ws):
    """Deliberate. Merging them would leak one tenant's dealings into another —
    the profile is the workspace's knowledge, not a global record of a human."""
    other = a_workspace("contacts-ws-2")
    contacts.record_inbound_sender(workspace=ws, address="fatima@llo-foo.org")
    contacts.record_inbound_sender(workspace=other, address="fatima@llo-foo.org")
    assert Contact.objects.filter(email="fatima@llo-foo.org").count() == 2
    assert contacts.for_workspace(ws.slug).count() == 1


def test_unusable_senders_are_skipped_rather_than_stored(ws):
    """Mail with no usable address is mail, not an error. A row with no
    matchable address could never be found again."""
    for bad in ("", "not-an-address", "<>", "@nohost"):
        assert contacts.record_inbound_sender(workspace=ws, address=bad) is None
    assert not Contact.objects.exists()


# --- the inbound wiring ---------------------------------------------------------

def test_an_email_turn_records_its_sender_in_the_agents_workspace(ws):
    """The tenant is the AGENT's, not the enqueuer's.

    `Turn.enqueued_by` on an email turn is the runner's own account — the inbox
    watcher authenticates as it — so keying the contact off the caller would
    file every correspondent in the fleet under the runner owner. The sender
    lives in `origin_ref["from"]`.
    """
    from apps.harness.models import Turn
    from apps.harness.services import enqueue_turn

    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws)
    enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key="m-1",
        prompt="a question",
        origin_ref={"from": "Fatima <fatima@llo-foo.org>", "thread_id": "t1"},
    )
    c = Contact.objects.get(email="fatima@llo-foo.org")
    assert c.workspace_id == ws.slug
    assert not WorkspaceMembership.objects.exists()


def test_a_turn_still_happens_when_the_contact_cannot_be_recorded(ws, monkeypatch):
    """Mail must be answerable even when the bookkeeping fails. A contact is a
    convenience for later turns; a turn that fails to EXIST is a regression on
    a surface that does not tolerate one."""
    from apps.harness.models import Turn
    from apps.harness.services import enqueue_turn

    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws)
    monkeypatch.setattr(
        contacts, "record_inbound_sender",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    turn, _created = enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key="m-2",
        prompt="a question", origin_ref={"from": "fatima@llo-foo.org"},
    )
    assert turn.pk
    assert not Contact.objects.exists()


# --- the API surface -----------------------------------------------------------

def _member(ws, email, role):
    from apps.workspaces.testing import a_member
    from django.test import Client

    user = a_member(ws, email=email, role=role)
    c = Client()
    c.force_login(user)
    return c


def test_a_viewer_can_read_contacts_but_not_edit_them(ws):
    """The role ladder, applied. Answering an agent's question about a
    correspondent needs to see who they are; changing the tenant's record of a
    person changes what the agent will believe next turn."""
    contact = contacts.record_inbound_sender(workspace=ws, address="fatima@llo-foo.org")
    viewer = _member(ws, "v@dimagi.com", WorkspaceMembership.VIEWER)

    assert viewer.get(f"/api/contacts/{contact.pk}/").status_code == 200
    res = viewer.patch(f"/api/contacts/{contact.pk}/",
                       data={"notes": "rewritten"}, content_type="application/json")
    assert res.status_code == 403, res.content
    contact.refresh_from_db()
    assert contact.notes == ""


def test_an_editor_can_correct_the_record(ws):
    contact = contacts.record_inbound_sender(workspace=ws, address="fatima@llo-foo.org")
    editor = _member(ws, "e@dimagi.com", WorkspaceMembership.EDITOR)
    res = editor.patch(f"/api/contacts/{contact.pk}/",
                       data={"notes": "LLO Foo, runs the Kano site"},
                       content_type="application/json")
    assert res.status_code == 200, res.content
    contact.refresh_from_db()
    assert contact.notes == "LLO Foo, runs the Kano site"


def test_another_tenants_contact_is_404_not_403(ws):
    """A contact is a person's identity. This API must never confirm that a
    given address is known to a tenant the caller cannot see — so a non-member
    gets the same answer as for a contact that does not exist."""
    other = a_workspace("contacts-ws-3")
    theirs = contacts.record_inbound_sender(workspace=other, address="fatima@llo-foo.org")
    outsider = _member(ws, "o@dimagi.com", WorkspaceMembership.OWNER)

    assert outsider.get(f"/api/contacts/{theirs.pk}/").status_code == 404
    res = outsider.patch(f"/api/contacts/{theirs.pk}/",
                         data={"notes": "x"}, content_type="application/json")
    assert res.status_code == 404, res.content


def test_the_list_is_scoped_to_the_callers_tenants(ws):
    other = a_workspace("contacts-ws-4")
    contacts.record_inbound_sender(workspace=ws, address="mine@llo-foo.org")
    contacts.record_inbound_sender(workspace=other, address="theirs@llo-bar.org")
    client = _member(ws, "m@dimagi.com", WorkspaceMembership.VIEWER)

    body = client.get("/api/contacts/").json()
    assert [r["email"] for r in body["items"]] == ["mine@llo-foo.org"]


def test_the_email_cannot_be_edited(ws):
    """It is the identity the record is keyed on. Editing it would silently
    re-attribute a whole correspondence history to a different person."""
    contact = contacts.record_inbound_sender(workspace=ws, address="fatima@llo-foo.org")
    editor = _member(ws, "e2@dimagi.com", WorkspaceMembership.EDITOR)
    res = editor.patch(f"/api/contacts/{contact.pk}/",
                       data={"email": "attacker@evil.example"},
                       content_type="application/json")
    assert res.status_code == 422, res.content
    contact.refresh_from_db()
    assert contact.email == "fatima@llo-foo.org"
