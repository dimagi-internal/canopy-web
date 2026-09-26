"""Every turn records WHO asked, on every channel — phase 1 of the who-is-asking spec.

Record-only: nothing here changes what a turn may do. These tests drive each
channel through its real entry point (the REST route, the socket, the Slack
handler, the scheduler) rather than calling `enqueue_turn` directly, because the
bug this phase exists to end was a caller-level one: the generic enqueue route
stamped the CALLER as `enqueued_by`, so an email posted by a runner read as
"launched by" the runner's owner.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentTask
from apps.harness import initiator as who
from apps.harness import services
from apps.harness.models import AgentSchedule, Turn
from apps.tokens.models import AppCredential, DelegatedToken, PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

# Slack's own fixtures, reused rather than re-built, so this file cannot drift
# from how Slack is actually set up in its tests.
from tests.test_slack import (  # noqa: F401
    BOT, alice, configured, hal, installation, linked, mention, slack, ws,
)

pytestmark = pytest.mark.django_db
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def ctx():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")
    workspace = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=workspace,
                                       role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=workspace, owner=owner)
    return owner, workspace, agent


def _session_client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _bearer_client(raw: str) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _new_session(client) -> str:
    r = client.post("/api/canopy-sessions/", data={"agent_slug": "echo"},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()["id"]


# --- the REST enqueue route ------------------------------------------------------


def test_a_turn_posted_from_a_signed_in_browser_is_the_user_by_session(ctx):
    owner, _ws, _agent = ctx
    r = _session_client(owner).post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "api", "idempotency_key": "k1"},
        content_type="application/json",
    )
    assert r.status_code == 201, r.content

    turn = Turn.objects.get()
    assert (turn.initiator_kind, turn.initiator_user, turn.initiator_assurance) == (
        who.USER, owner, who.SESSION)


def test_a_turn_posted_with_a_personal_token_says_so(ctx):
    """Same person, different claim: a PAT is a machine acting as its owner."""
    owner, _ws, _agent = ctx
    raw, _tok = PersonalToken.create_for_user(user=owner, label="cli")
    r = _bearer_client(raw).post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "api", "idempotency_key": "k2"},
        content_type="application/json",
    )
    assert r.status_code == 201, r.content

    turn = Turn.objects.get()
    assert (turn.initiator_user, turn.initiator_assurance) == (owner, who.PAT)


def test_an_email_turn_is_the_SENDER_not_the_runner_that_posted_it(ctx):
    """The bug: the runner posts email turns with its owner's PAT, and
    `enqueued_by` records that owner. The initiator is the person who wrote in."""
    owner, _ws, _agent = ctx
    raw, _tok = PersonalToken.create_for_user(user=owner, label="runner")
    r = _bearer_client(raw).post(
        "/api/harness/turns/",
        {
            "agent_slug": "echo", "origin": "email", "idempotency_key": "email-1",
            "origin_ref": {"from": "partner@example.org", "from_name": "Pat Partner",
                           "thread_id": "t-1", "subject": "hello"},
        },
        content_type="application/json",
    )
    assert r.status_code == 201, r.content

    turn = Turn.objects.get()
    assert turn.enqueued_by == owner                    # unchanged: the caller
    assert turn.initiator_kind == who.CONTACT           # new: the asker
    assert turn.initiator_contact.email == "partner@example.org"
    assert turn.initiator_user is None
    assert turn.initiator_via == "email"
    # Nothing in the request proves the address, so the grade says so.
    assert turn.initiator_assurance == "none"


def test_an_email_with_no_sender_is_unknown_rather_than_the_runner(ctx):
    owner, _ws, agent = ctx
    turn, _ = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key="e2",
        origin_ref={"thread_id": "t-2"}, enqueued_by=owner,
    )
    assert turn.initiator_kind == who.UNKNOWN
    assert turn.initiator_user is None


# --- chat ------------------------------------------------------------------------


def test_a_chat_message_from_canopy_is_the_signed_in_user(ctx):
    owner, _ws, _agent = ctx
    client = _session_client(owner)
    sid = _new_session(client)
    r = client.post(f"/api/canopy-sessions/{sid}/send", data={"text": "hi"},
                    content_type="application/json")
    assert r.status_code == 200, r.content

    turn = Turn.objects.get()
    assert (turn.initiator_kind, turn.initiator_user, turn.initiator_via,
            turn.initiator_assurance) == (who.USER, owner, "chat", who.SESSION)


def test_a_chat_message_through_a_hosts_widget_names_the_host(ctx):
    """A delegated token means an embedding host's widget: the channel says which
    host, and the assurance says the token was minted by an app."""
    from apps.tokens.models import AppCredentialAgent

    owner, workspace, agent = ctx
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=workspace)
    AppCredentialAgent.objects.create(app=app, agent=agent)
    raw, _tok = DelegatedToken.issue(app=app, user=owner, ttl_seconds=600)
    client = _bearer_client(raw)
    sid = _new_session(client)
    r = client.post(f"/api/canopy-sessions/{sid}/send", data={"text": "hi"},
                    content_type="application/json")
    assert r.status_code == 200, r.content

    turn = Turn.objects.get()
    assert (turn.initiator_user, turn.initiator_via, turn.initiator_assurance) == (
        owner, "widget:connect-labs", who.DELEGATED)


def test_a_widget_contact_is_the_contact_with_its_own_grade(ctx):
    """A contact token resolves to no user at all; the turn names the contact and
    carries what the HOST could prove about them, not a flat "contact"."""
    from types import SimpleNamespace

    from apps.contacts.models import Contact

    owner, workspace, _agent = ctx
    app = AppCredential.create_credential(name="connect-labs", created_by=owner,
                                                   workspace=workspace)
    contact = Contact.objects.create(workspace=workspace, app=app, external_id="42",
                                     email="visitor@partner.org",
                                     auth_result=Contact.AUTH_APP_SIGNED,
                                     # Both, as `record_embed_visitor` writes them:
                                     # the turn reads THIS arrival's grade.
                                     last_auth_result=Contact.AUTH_APP_SIGNED)
    request = SimpleNamespace(contact=contact, delegated_app=app, user=None,
                              auth_method="contact")

    got = who.for_request(request, via=who.channel(request, "contact"))

    assert (got.kind, got.contact, got.via, got.assurance) == (
        who.CONTACT, contact, "widget:connect-labs", Contact.AUTH_APP_SIGNED)
    assert got.user is None


def test_a_socket_scope_is_graded_the_same_way():
    """The socket's sends go through `for_scope`; a delegated query token and a
    session cookie are different claims there too."""
    user = User.objects.create_user("s", "s@dimagi.com", "pw")
    via_session = who.for_scope({"user": user, "auth_method": "session"}, via="chat")
    via_token = who.for_scope({"user": user, "auth_method": "delegated"}, via="chat")

    assert (via_session.kind, via_session.assurance) == (who.USER, who.SESSION)
    assert (via_token.kind, via_token.assurance) == (who.USER, who.DELEGATED)


# --- Slack -----------------------------------------------------------------------


def test_a_slack_mention_is_the_linked_user_who_sent_it(slack, linked, hal, alice):  # noqa: F811
    resp = mention(f"<@{BOT}> hal summarise this thread")
    assert resp.status_code == 200

    turn = Turn.objects.get()
    assert (turn.initiator_kind, turn.initiator_user, turn.initiator_assurance) == (
        who.USER, alice, who.SLACK_LINKED)
    assert turn.initiator_via.startswith("slack:")


# --- canopy starting it ----------------------------------------------------------


def test_a_schedule_is_system_with_its_creator_accountable(ctx):
    owner, _ws, agent = ctx
    schedule = AgentSchedule.objects.create(
        agent=agent, name="digest", cron="0 9 * * 1", prompt="/echo:turn",
        created_by=owner,
    )
    turn, _ = services.fire_schedule(schedule, timezone.now().replace(microsecond=0))

    assert (turn.initiator_kind, turn.initiator_assurance) == (who.SYSTEM, who.INTERNAL)
    assert turn.initiator_user == owner          # accountable, per spec D3
    assert turn.initiator_via == f"schedule:{schedule.id}"


def test_dispatched_work_is_the_person_who_approved_it(ctx):
    owner, _ws, agent = ctx
    item = AgentTask(agent=agent, ext_id="T1", title="t", ask_kind=AgentTask.ASK_REVIEW, origin="api",
                idempotency_key="i1", decided_by_user=owner)
    item.save()
    assert who.for_user(owner, via="x", assurance=who.APPROVAL).kind == who.USER

    from apps.harness.dispatch import _dispatch_initiator

    approved = _dispatch_initiator(item)
    assert (approved.kind, approved.user, approved.assurance) == (who.USER, owner, who.APPROVAL)

    item.decided_by_user = None
    raised = _dispatch_initiator(item)
    assert (raised.kind, raised.agent_slug) == (who.AGENT, agent.slug)


# --- what the runner and the UI see ----------------------------------------------


def test_the_turn_api_carries_the_initiator_by_name(ctx):
    """The agent is going to address this person, so it gets a name, not an id."""
    owner, _ws, _agent = ctx
    client = _session_client(owner)
    client.post("/api/harness/turns/",
                {"agent_slug": "echo", "origin": "api", "idempotency_key": "k9"},
                content_type="application/json")

    (row,) = client.get("/api/harness/turns/").json()
    assert row["initiator"]["kind"] == "user"
    assert row["initiator"]["user"]["name"] == "Jonathan"
    assert row["initiator"]["user"]["email"] == "jj@dimagi.com"


# --- nothing slips through -------------------------------------------------------

ENTRY_POINTS = {"enqueue_turn", "send_message", "transfer_session"}


def _calls_missing_initiator() -> list[str]:
    missing = []
    for path in (REPO / "apps").rglob("*.py"):
        rel = path.relative_to(REPO).as_posix()
        if "/tests/" in rel or "/migrations/" in rel:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in ENTRY_POINTS:
                continue
            if not any(kw.arg == "initiator" for kw in node.keywords):
                missing.append(f"{rel}:{node.lineno} {name}(...)")
    return missing


def test_every_production_caller_says_who_asked():
    """A new channel that forgets to pass `initiator` would record its turns as
    `unknown` (or, via the chat fallback, a person with no assurance) — silently.
    So every call to a turn-creating entry point must name its asker. For email
    the enqueue route passes `initiator=None` explicitly, which is still a
    decision someone made rather than an omission."""
    assert _calls_missing_initiator() == []


# --- an embedded site's visitor tops out at tier 2 ----------------------------
# Not an accident to be fixed later: the mint grades `app_signed` deliberately,
# "because it proves the SITE said this, not that the human is who the site
# thinks". These pin the ceiling, and that nothing claims otherwise — a tier-3
# grade nobody can earn made `contact:verified` read as "this visitor failed"
# when it meant "this rule can never pass".


def test_no_grade_above_tier_2_exists_for_an_embedded_site():
    """Every tier-3 grade on the ladder is a MAIL grade, or a full member of
    our own Slack — where the organisation that owns the Slack provisioned the
    email. Never an embedded site's."""
    from apps.contacts.models import Contact

    top = {g for g, rank in Contact.AUTH_RANK.items()
           if rank >= Contact.AUTH_RANK[Contact.TIER_SIGNED_ALIGNED]}

    assert top == {Contact.AUTH_DMARC, Contact.AUTH_DKIM_ALIGNED, Contact.AUTH_SLACK_MEMBER}, (
        f"the top of the ladder changed: {top}. A grade an embedded site could "
        "hold up here needs canopy to verify the PERSON, which it does not do — "
        "it verifies the host's signature. See tokens/contact_api.py."
    )
    assert all(g in dict(Contact.AUTH_CHOICES) for g in Contact.AUTH_RANK), (
        "a ranked grade is missing from AUTH_CHOICES"
    )
    assert set(dict(Contact.AUTH_CHOICES)) == set(Contact.AUTH_RANK), (
        "AUTH_CHOICES and AUTH_RANK disagree, so some grade is either unrankable "
        "or unstorable"
    )


@pytest.mark.django_db
def test_an_embedded_visitor_is_never_verified():
    """So `contact:verified` cannot admit one, and the rule that reads the grade
    agrees with the mint that writes it."""
    from types import SimpleNamespace

    from apps.contacts.models import Contact
    from apps.harness.caller_context import _verified

    owner = User.objects.create_user("o2", "o2@dimagi.com", "pw")
    workspace = Workspace.objects.create(slug="w9", display_name="W9", created_by=owner)
    app = AppCredential.create_credential(name="connect-labs", created_by=owner,
                                                   workspace=workspace)
    contact = Contact.objects.create(workspace=workspace, app=app, external_id="42",
                                     email="visitor@partner.org",
                                     auth_result=Contact.AUTH_APP_SIGNED,
                                     last_auth_result=Contact.AUTH_APP_SIGNED)

    turn = SimpleNamespace(initiator_kind=who.CONTACT,
                           initiator_assurance=contact.last_auth_result)
    assert _verified(turn) is False

    # And a contact graded by MAIL still is, so this is a ceiling on the embed
    # channel rather than on contacts.
    mailed = SimpleNamespace(initiator_kind=who.CONTACT,
                             initiator_assurance=Contact.AUTH_DMARC)
    assert _verified(mailed) is True


def test_a_grade_that_is_no_longer_on_the_ladder_reads_as_unverified():
    """The fail-closed direction, for a value stored before a grade was dropped."""
    from types import SimpleNamespace

    from apps.harness.caller_context import _verified

    turn = SimpleNamespace(initiator_kind=who.CONTACT,
                           initiator_assurance="app_signed_origin")

    assert _verified(turn) is False
