"""Widget arrival (who-is-asking §2): an existing canopy account arrives as itself.

A connected site signs an assertion about its visitor. canopy resolves: an
already-linked contact → that user; else a site-signed VERIFIED email held
verified by exactly one existing canopy user who is a member of the site's
workspace → link and arrive as that user; else a contact. Never a new account (D1).
"""
from __future__ import annotations

import datetime as dt
import uuid

import jwt
import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from apps.contacts.models import Contact
from apps.tokens import assertions
from apps.tokens.models import DelegatedToken
from apps.workspaces.models import WorkspaceMembership
from tests.test_contact_websocket import _world

pytestmark = pytest.mark.django_db
M = WorkspaceMembership


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def w():
    owner, ws, app, priv = _world()
    alice = User.objects.create_user("alice", "alice@dimagi.com", "pw")
    EmailAddress.objects.create(user=alice, email=alice.email, verified=True, primary=True)
    M.objects.create(user=alice, workspace=ws, role=M.EDITOR)
    return {"owner": owner, "ws": ws, "app": app, "priv": priv, "alice": alice}


def _arrive(priv, sub="u-42", **claims):
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    assertion = jwt.encode({"iss": "connect-labs", "sub": sub, "aud": assertions.audience(),
                            "iat": now, "exp": now + 60, "jti": str(uuid.uuid4()), **claims},
                           priv, algorithm="EdDSA")
    r = Client().post("/api/auth/contact-token", data={"assertion": assertion},
                      content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()


def test_a_member_with_a_verified_email_arrives_as_the_user_with_no_setting(w):
    """No per-site opt-in: being allowed in the workspace is the whole gate."""
    got = _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)
    assert got["kind"] == "user"
    tok = DelegatedToken.lookup(got["token"])
    assert tok.user == w["alice"] and tok.assurance == "host_signed"
    assert Contact.objects.get(pk=got["contact_id"]).user == w["alice"]     # linked


def test_a_linked_contact_arrives_as_the_user_without_repeating_the_email(w):
    _arrive(w["priv"], sub="u-7", email="alice@dimagi.com", email_verified=True)
    got = _arrive(w["priv"], sub="u-7")                                     # no email this time
    assert got["kind"] == "user"


@pytest.mark.parametrize("claims", [
    {"email": "alice@dimagi.com"},                                   # not verified
    {"email": "alice@dimagi.com", "email_verified": "true"},         # a string is not True
    {"email": "alice@dimagi.com", "email_verified": False},
    {"email": "alice@other.org", "email_verified": True},            # nobody holds it
])
def test_anything_less_is_a_contact(w, claims):
    assert _arrive(w["priv"], **claims)["kind"] == "contact"


def test_arrival_never_creates_an_account(w):
    before = User.objects.count()
    got = _arrive(w["priv"], email="newcomer@dimagi.com", email_verified=True)
    assert got["kind"] == "contact" and User.objects.count() == before


def test_an_unverified_canopy_address_proves_nothing(w):
    EmailAddress.objects.filter(user=w["alice"]).update(verified=False)
    assert _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)["kind"] == "contact"


def test_a_canopy_user_outside_the_sites_workspace_stays_a_contact(w):
    M.objects.filter(user=w["alice"]).delete()
    assert _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)["kind"] == "contact"


def test_the_user_token_reaches_the_user_surface_not_the_contact_one(w):
    raw = _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)["token"]
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert c.get("/api/embed/agents").status_code == 200
    assert c.get("/api/contact/me").status_code in (401, 403)


def test_a_turn_a_resolved_user_starts_says_host_signed(w):
    from apps.harness.models import Turn

    raw = _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)["token"]
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    sid = c.post("/api/canopy-sessions/", {"agent_slug": "echo"},
                 content_type="application/json").json()["id"]
    r = c.post(f"/api/canopy-sessions/{sid}/send", {"text": "hi", "client_id": "c1"},
               content_type="application/json")
    assert r.status_code in (200, 201), r.content
    t = Turn.objects.filter(chat_session_id=sid).latest("created_at")
    assert (t.initiator_user_id, t.initiator_assurance) == (w["alice"].pk, "host_signed")


# --- An agent's OWN login (Agent.user, #983) arriving through a host. -------
# Observed 2026-10-01, turn 2727e227: ace-web started an ACE run as
# ace@dimagi-ai.com — ACE's own login, a member of the workspace — and it came in
# as a contact, confined to `ask`, which refused the run. Agent logins are minted
# by `create_token --create-user`, so they have no allauth EmailAddress row, and
# arrival only ever looked there.


def _agent_login(w, *, member=True, bind=True):
    from apps.agents.models import Agent

    bot = User.objects.create_user("ace@dimagi-ai.com", "ace@dimagi-ai.com")   # create_token's shape
    assert not EmailAddress.objects.filter(user=bot).exists()
    if member:
        M.objects.create(user=bot, workspace=w["ws"], role=M.EDITOR)
    agent = Agent.objects.get(slug="echo")
    if bind:
        agent.user = bot
        agent.save(update_fields=["user"])
    return bot, agent


def _publish_ask_interface(agent):
    from apps.agents.interface import parse

    agent.interface = parse({"capabilities": {"ask": {"callers": ["contact", "member"]}}})
    agent.save(update_fields=["interface"])


def _turn_as(raw, agent_slug="echo"):
    from apps.harness.models import Turn

    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    sid = c.post("/api/canopy-sessions/", {"agent_slug": agent_slug},
                 content_type="application/json").json()["id"]
    r = c.post(f"/api/canopy-sessions/{sid}/send", {"text": "/ace:run", "client_id": "c1"},
               content_type="application/json")
    assert r.status_code in (200, 201), r.content
    return Turn.objects.filter(chat_session_id=sid).latest("created_at")


def test_an_agents_own_login_arrives_as_itself_and_runs_unconfined(w):
    from apps.agents.interface import FULL
    from apps.harness import caller_context

    bot, agent = _agent_login(w)
    _publish_ask_interface(agent)
    got = _arrive(w["priv"], sub="ace-owner", email="ace@dimagi-ai.com", email_verified=True)
    assert got["kind"] == "user"
    assert DelegatedToken.lookup(got["token"]).user == bot
    t = _turn_as(got["token"])
    assert t.initiator_user_id == bot.pk
    env = caller_context.build(t)
    assert env["relationship"] == "system"
    assert t.capability == FULL and env["profile"] == "full" and env["granted_by"] == "system"


def test_the_contact_it_already_fell_to_is_promoted_on_the_next_arrival(w):
    """The incident's contact (#29) was recorded with user=None. Once the login
    resolves, step 2 links it — the same contact, not a new one."""
    bot, agent = _agent_login(w, bind=False)
    first = _arrive(w["priv"], sub="ace-owner", email="ace@dimagi-ai.com", email_verified=True)
    assert first["kind"] == "contact"                       # not an agent's login yet
    agent.user = bot
    agent.save(update_fields=["user"])
    again = _arrive(w["priv"], sub="ace-owner", email="ace@dimagi-ai.com", email_verified=True)
    assert again["kind"] == "user" and again["contact_id"] == first["contact_id"]
    assert Contact.objects.get(pk=first["contact_id"]).user == bot


def test_an_agents_login_outside_the_sites_workspace_stays_a_contact(w):
    _agent_login(w, member=False)
    got = _arrive(w["priv"], email="ace@dimagi-ai.com", email_verified=True)
    assert got["kind"] == "contact"


def test_an_agents_login_still_needs_the_site_to_say_verified(w):
    _agent_login(w)
    assert _arrive(w["priv"], email="ace@dimagi-ai.com")["kind"] == "contact"


def test_a_human_with_no_verified_email_row_is_still_a_contact(w):
    """No loosening for people: `User.email` alone proves nothing unless the
    account is bound as some agent's own login."""
    bob = User.objects.create_user("bob", "bob@dimagi.com", "pw")
    M.objects.create(user=bob, workspace=w["ws"], role=M.EDITOR)
    assert _arrive(w["priv"], email="bob@dimagi.com", email_verified=True)["kind"] == "contact"


def test_a_human_verified_holder_of_the_address_wins_over_an_agent_login(w):
    bot, _ = _agent_login(w)
    EmailAddress.objects.create(user=w["alice"], email="ace@dimagi-ai.com", verified=True)
    got = _arrive(w["priv"], email="ace@dimagi-ai.com", email_verified=True)
    assert got["kind"] == "user" and DelegatedToken.lookup(got["token"]).user == w["alice"]


def test_two_agent_logins_on_one_address_is_ambiguous(w):
    from apps.agents.models import Agent
    from apps.contacts.services import user_for_verified_email

    _agent_login(w)
    twin = User.objects.create_user("ace-twin", "ACE@dimagi-ai.com")
    Agent.objects.create(slug="ace2", name="Ace2", workspace=w["ws"], user=twin)
    assert user_for_verified_email("ace@dimagi-ai.com") is None
