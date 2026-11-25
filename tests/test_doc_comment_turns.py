"""A Google Docs comment that tags an agent runs in canopy's `doc_comment` profile: that one Doc, read and
answered in, as the agent's own mailbox, and nothing else, whoever the comment says it is from.

Why (apps/agents/doc_comment.py): Google sends every comment from `comments-noreply@docs.google.com`, and the
commenter's email is in no header and not in the body (eva@ thread 1a20810ba581b0a9). Anyone can make a
Doc, share it with an agent and tag it under any name, so a comment is never run as the person it names.
"""
from __future__ import annotations

import pytest

from apps.agents import doc_comment, published_profiles, turn_context
from apps.agents.models import AgentGrant
from apps.agents.published_profiles import InterfaceError, parse
from apps.contacts.models import Contact
from apps.harness import caller_context
from apps.harness import initiator as who
from apps.harness.models import Turn

from .fixtures.turn_context import make_world

pytestmark = pytest.mark.django_db

FID = "1QDxfhS_gWTYbu31_vhT6gdFxin1WL3XmX8q_B-_QAhE"
OTHER = "1OTHERotherOTHERotherOTHERotherOTHERother00"
MAILBOX = "agent@dimagi-ai.com"
_keys = iter(range(10**6))


@pytest.fixture()
def w():
    world = make_world()
    world.agent.email = MAILBOX
    world.agent.save(update_fields=["email"])
    # What every fleet agent's list holds today for Google's notifications: they get read mode.
    AgentGrant.objects.create(agent=world.agent, kind=AgentGrant.DOMAIN, domain="docs.google.com",
                              role="read_only")
    return world


def _docs_contact(w, grade="dmarc", email=doc_comment.DOCS_COMMENTS):
    return Contact.objects.create(workspace=w.ws, email=email, last_auth_result=grade, auth_result=grade)


def _ref(**doc):
    return {"thread_id": "1a20810ba581b0a9", "backend": "gmail", "mailbox": MAILBOX,
            "from": '"Shayoni Mazumdar (Google Docs)" <comments-noreply@docs.google.com>',
            "inbox_row": "wanted/doc-interaction",
            "doc_comment": {"file_id": FID, "kind": "document", "comment_hint": "doco.I7zRfRBEMbUpGaSUsMVuLQ",
                            "commenter_name": "Shayoni Mazumdar", **doc}}


def _turn(w, contact, ref=None, **extra):
    return Turn.objects.create(agent=w.agent, origin=Turn.ORIGIN_EMAIL, initiator_kind=who.CONTACT,
                               initiator_contact=contact, initiator_assurance=contact.last_auth_result,
                               prompt="/agent:turn --thread 1a20810ba581b0a9", idempotency_key=f"k-{next(_keys)}",
                               origin_ref=_ref() if ref is None else ref, **extra)


def test_a_tagged_comment_runs_in_the_doc_comment_profile_bound_to_its_doc(w):
    ctx = turn_context.for_turn(_turn(w, _docs_contact(w)), w.agent)
    assert ctx.access == turn_context.CONFINED
    assert ctx.profile.kind == turn_context.DOC_COMMENT and ctx.stamped == "doc_comment"
    cap = ctx.profile.as_envelope()
    assert cap["tools"] == ["Read", "Write", "ToolSearch", "mcp__*canopy-web__who_is_asking"]
    gog = [rule for rule in cap["bash"] if rule.startswith("gog ")]
    assert gog and all(f" {FID} " in rule and f"--account {MAILBOX}" in rule for rule in gog)
    assert {rule.split(FID)[0] for rule in gog} == {"gog docs cat ", "gog drive comments list ",
                                                     "gog drive comments reply "}
    assert set(cap["bash"]) - set(gog) == {"canopy email client --repo ."}   # no mail, no other file
    assert FID in cap["entry"] and "Never reply by email" in cap["entry"]


def test_no_standing_widens_it(w):
    # Even a line that would drive the whole agent: a comment's author is never established.
    AgentGrant.objects.filter(agent=w.agent, kind=AgentGrant.DOMAIN).update(role="read_write")
    ctx = turn_context.for_turn(_turn(w, _docs_contact(w)), w.agent)
    assert ctx.access == turn_context.CONFINED and ctx.profile.kind == turn_context.DOC_COMMENT
    assert ctx.review.may_request_auto is False and ctx.review.may_pin_runner is False


def test_the_commenter_is_never_the_initiator_even_with_a_members_name(w):
    turn = _turn(w, _docs_contact(w), ref=_ref(commenter_name=w.member.get_full_name() or "member"))
    ctx = turn_context.for_turn(turn, w.agent)
    assert turn.initiator_kind == who.CONTACT and turn.initiator_user_id is None
    assert ctx.profile.kind == turn_context.DOC_COMMENT


def test_it_still_refuses_what_the_list_refuses(w):
    AgentGrant.objects.filter(agent=w.agent, kind=AgentGrant.DOMAIN).delete()
    assert turn_context.for_turn(_turn(w, _docs_contact(w)), w.agent).refused


@pytest.mark.parametrize("change", [
    {"grade": "spf"},                                                # this message is not proven Google's
    {"email": "mallory@example.org"},                                # not Google's address
])
def test_an_unproven_notification_is_not_bound_to_a_doc(w, change):
    AgentGrant.objects.create(agent=w.agent, kind=AgentGrant.DOMAIN, domain="example.org", role="read_only",
                              verified=False)
    AgentGrant.objects.filter(agent=w.agent, domain="docs.google.com").update(verified=False)
    turn = _turn(w, _docs_contact(w, **change))
    assert doc_comment.of_turn(turn) is None
    ctx = turn_context.for_turn(turn, w.agent)
    assert ctx.refused or ctx.profile.kind != turn_context.DOC_COMMENT


@pytest.mark.parametrize("ref_change", [
    {"doc_comment": {"file_id": f"{FID} --account x@y.org"}},        # not a file id
    {"doc_comment": {"file_id": "*"}},
    {"doc_comment": {"file_id": FID, "kind": "drive"}},
    {"doc_comment": "nope"},
    {"backend": "ses"},                                              # no Google account to answer as
])
def test_a_malformed_or_hosted_doc_comment_binds_nothing(w, ref_change):
    turn = _turn(w, _docs_contact(w), ref={**_ref(), **ref_change})
    assert doc_comment.of_turn(turn) is None
    assert turn_context.for_turn(turn, w.agent).profile.kind == turn_context.READ_MODE   # as before


def test_the_mailbox_is_always_the_agents_own(w):
    turn = _turn(w, _docs_contact(w), ref={**_ref(), "mailbox": "someone-else@dimagi.com"})
    assert doc_comment.of_turn(turn).mailbox == MAILBOX


def test_the_stamp_is_bound_to_the_turns_own_doc_at_claim(w):
    google = _docs_contact(w)
    turn = _turn(w, google, capability="doc_comment")
    ctx = turn_context.of_turn(turn, w.agent)
    assert ctx.profile.kind == turn_context.DOC_COMMENT and FID in " ".join(ctx.profile.bash)
    # A turn stamped `doc_comment` that proves no Doc opens nothing, never the whole agent.
    bare = _turn(w, google, ref={**_ref(), "doc_comment": None}, capability="doc_comment")
    assert turn_context.of_turn(bare, w.agent).profile.kind == turn_context.DENY_ALL
    assert published_profiles.profile(w.agent, "doc_comment")["bash"] == []


def test_the_envelope_names_the_doc_and_says_the_commenter_is_unverified(w):
    google = _docs_contact(w)
    env = caller_context.build(_turn(w, google, capability="doc_comment"))
    assert env["doc_comment"]["file_id"] == FID and env["doc_comment"]["commenter_verified"] is False
    assert env["doc_comment"]["commenter_name"] == "Shayoni Mazumdar"
    assert env["capability"]["name"] == "doc_comment"
    other = caller_context.build(_turn(w, google, ref={**_ref(), "doc_comment": None}))
    assert other["doc_comment"] is None


def test_no_agent_can_publish_its_name():
    with pytest.raises(InterfaceError):
        parse({"capabilities": {"doc_comment": {"description": "x", "tools": ["Read"]}}})


def test_a_comment_id_never_reaches_a_rule():
    doc = doc_comment.DocComment(file_id=FID, kind="document", mailbox=MAILBOX, comment_hint="AAAA")
    agent = type("A", (), {"slug": "agent"})()
    bash = published_profiles.doc_comment_profile(agent, doc)["bash"]
    assert not any("AAAA" in rule for rule in bash) and not any(OTHER in rule for rule in bash)
