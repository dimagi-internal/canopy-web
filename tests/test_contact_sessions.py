"""A contact's conversation: theirs, and only theirs.

The routes are mirrored under `/api/contact/` rather than opened up on
`/api/canopy-sessions/`, and the duplication is the security property: those
routes stay strictly user-only, so a contact cannot reach one by a view
forgetting to ask who is calling.

What these tests are really pinning is that the two visibility predicates are
DISJOINT. A contact must not see a member's conversation, a member must not see
a contact's, and neither predicate should have to know the other exists for
that to hold.
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
from apps.canopy_sessions.models import Session
from apps.contacts.models import Contact
from apps.tokens import assertions
from apps.tokens.models import AppCredential, AppCredentialAgent
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


def _keypair():
    priv = ed25519.Ed25519PrivateKey.generate()
    return (
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode(),
    )


def _world():
    owner = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    offered = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    private = Agent.objects.create(slug="hal", name="Hal", workspace=ws)
    priv, pub = _keypair()
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=ws)
    app.workspace = ws
    app.public_keys = [pub]
    app.save(update_fields=["workspace", "public_keys"])
    AppCredentialAgent.objects.create(app=app, agent=offered)
    return owner, ws, app, priv, offered, private


def _contact_headers(priv, sub="u-42"):
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    token = jwt.encode(
        {"iss": "connect-labs", "sub": sub, "aud": assertions.audience(),
         "iat": now, "exp": now + 60, "jti": str(uuid.uuid4())},
        priv, algorithm="EdDSA",
    )
    body = Client().post("/api/auth/contact-token", data={"assertion": token},
                         content_type="application/json").json()
    return {"HTTP_AUTHORIZATION": f"Bearer {body['token']}"}


# --- the loop -----------------------------------------------------------------


def test_a_contact_can_start_a_conversation_and_speak():
    _owner, ws, app, priv, offered, _private = _world()
    c, hdr = Client(), _contact_headers(priv)

    started = c.post("/api/contact/sessions", data={"agent_slug": "echo"},
                     content_type="application/json", **hdr)
    assert started.status_code == 200, started.content
    sid = started.json()["id"]

    sent = c.post(f"/api/contact/sessions/{sid}/send", data={"text": "hello"},
                  content_type="application/json", **hdr)
    assert sent.status_code == 200, sent.content

    session = Session.objects.get(pk=sid)
    assert session.contact.external_id == "u-42"
    assert session.created_by is None, "a contact session has no user owner"


def test_only_agents_the_site_was_allowed_to_offer():
    """Not "agents the contact can reach" — a contact reaches nothing, having
    no membership. The app's allowlist is the whole gate."""
    _owner, _ws, _app, priv, _offered, _private = _world()
    c, hdr = Client(), _contact_headers(priv)

    r = c.post("/api/contact/sessions", data={"agent_slug": "hal"},
               content_type="application/json", **hdr)

    assert r.status_code == 404
    assert not Session.objects.exists()


def test_a_contact_lists_only_their_own_conversations():
    _owner, _ws, _app, priv, _offered, _private = _world()
    c = Client()
    mine = _contact_headers(priv, sub="u-42")
    theirs = _contact_headers(priv, sub="u-99")

    c.post("/api/contact/sessions", data={"agent_slug": "echo"},
           content_type="application/json", **mine)
    c.post("/api/contact/sessions", data={"agent_slug": "echo"},
           content_type="application/json", **theirs)

    assert len(c.get("/api/contact/sessions", **mine).json()) == 1
    assert Session.objects.count() == 2


def test_one_contact_cannot_open_another_contacts_session_by_id():
    _owner, _ws, _app, priv, _offered, _private = _world()
    c = Client()
    mine = _contact_headers(priv, sub="u-42")
    theirs = _contact_headers(priv, sub="u-99")
    sid = c.post("/api/contact/sessions", data={"agent_slug": "echo"},
                 content_type="application/json", **theirs).json()["id"]

    assert c.get(f"/api/contact/sessions/{sid}", **mine).status_code == 404
    assert c.post(f"/api/contact/sessions/{sid}/send", data={"text": "hi"},
                  content_type="application/json", **mine).status_code == 404


# --- the two predicates are disjoint ------------------------------------------


def test_a_members_conversation_is_invisible_to_a_contact():
    owner, ws, _app, priv, offered, _private = _world()
    theirs = Session.objects.create(workspace=ws, created_by=owner, agent=offered, title="private")
    c, hdr = Client(), _contact_headers(priv)

    assert c.get(f"/api/contact/sessions/{theirs.id}", **hdr).status_code == 404
    assert c.get("/api/contact/sessions", **hdr).json() == []


def test_a_contacts_conversation_is_invisible_to_the_workspace_owner():
    """The Contact model's whole property, at the conversation level: knowing
    somebody is not being able to read what they said."""
    owner, _ws, _app, priv, _offered, _private = _world()
    c = Client()
    hdr = _contact_headers(priv)
    sid = c.post("/api/contact/sessions", data={"agent_slug": "echo"},
                 content_type="application/json", **hdr).json()["id"]

    member = Client()
    member.force_login(owner)

    assert member.get(f"/api/canopy-sessions/{sid}").status_code == 404
    listed = member.get("/api/canopy-sessions/").json()
    assert all(row["id"] != sid for row in listed)


def test_a_member_cannot_use_the_contact_routes_with_their_session_cookie():
    """`contact_auth` requires the principal, not merely an authenticated
    request."""
    owner, _ws, _app, _priv, _offered, _private = _world()
    member = Client()
    member.force_login(owner)

    assert member.get("/api/contact/sessions").status_code in (401, 403)


# --- refusals still apply mid-conversation ------------------------------------


def test_blocking_ends_the_conversation_immediately():
    _owner, _ws, _app, priv, _offered, _private = _world()
    from apps.contacts import services

    c, hdr = Client(), _contact_headers(priv)
    sid = c.post("/api/contact/sessions", data={"agent_slug": "echo"},
                 content_type="application/json", **hdr).json()["id"]

    services.block(Contact.objects.get(), reason="abuse")

    assert c.post(f"/api/contact/sessions/{sid}/send", data={"text": "hi"},
                  content_type="application/json", **hdr).status_code == 401


def test_disconnecting_the_site_ends_it_too():
    from django.utils import timezone

    _owner, _ws, app, priv, _offered, _private = _world()
    c, hdr = Client(), _contact_headers(priv)
    sid = c.post("/api/contact/sessions", data={"agent_slug": "echo"},
                 content_type="application/json", **hdr).json()["id"]

    app.revoked_at = timezone.now()
    app.save(update_fields=["revoked_at"])

    assert c.get(f"/api/contact/sessions/{sid}", **hdr).status_code == 401


def test_an_empty_message_is_refused():
    _owner, _ws, _app, priv, _offered, _private = _world()
    c, hdr = Client(), _contact_headers(priv)
    sid = c.post("/api/contact/sessions", data={"agent_slug": "echo"},
                 content_type="application/json", **hdr).json()["id"]

    r = c.post(f"/api/contact/sessions/{sid}/send", data={"text": "   "},
               content_type="application/json", **hdr)

    assert r.status_code == 422


def test_the_contact_routes_refuse_a_request_with_no_token_at_all():
    """Found by omitting the header in the test above: the surface answers 401
    rather than falling through to anything. Worth keeping, because the whole
    boundary is "no principal, no access"."""
    _owner, _ws, _app, _priv, _offered, _private = _world()

    r = Client().post("/api/contact/sessions", data={"agent_slug": "echo"},
                      content_type="application/json")

    assert r.status_code == 401
    assert not Session.objects.exists()


# --- the page the contact is looking at ---------------------------------------
# A contact could hold a conversation but never say what was on their screen,
# because neither declaration route existed under `/api/contact/`. The agent's
# own read was never the obstacle: the session driving the chat reads its page
# with the chat's key, which does not depend on who created the session.


def _started(c, hdr):
    r = c.post("/api/contact/sessions", data={"agent_slug": "echo"},
               content_type="application/json", **hdr)
    assert r.status_code == 200, r.content
    return r.json()["id"]


def test_a_contact_declares_what_they_are_looking_at():
    _owner, _ws, _app, priv, _offered, _private = _world()
    c, hdr = Client(), _contact_headers(priv)
    sid = _started(c, hdr)

    state = {"resource": "labs-marketplace://orgs", "backing_tool": "marketplace_orgs_get",
             "visible_ids": ["acme-health", "beta-care"]}
    r = c.put(f"/api/contact/sessions/{sid}/page-state", data={"state": state},
              content_type="application/json", **hdr)

    assert r.status_code == 200, r.content
    assert r.json()["state"]["resource"] == "labs-marketplace://orgs"
    assert r.json()["version"] == 1
    assert Session.objects.get(pk=sid).page_state["visible_ids"] == ["acme-health", "beta-care"]


def test_the_agent_can_read_a_contacts_page():
    """The point of the whole feature: the panel is useless if the declaration
    lands somewhere the agent cannot see. The agent reads it through the chat
    key canopy issues the session driving this chat."""
    from unittest import mock

    from apps.canopy_sessions import chat_keys
    from apps.canopy_sessions.models import Session
    from apps.mcp.tools.page import _page_of_chat

    _owner, _ws, _app, priv, _offered, _private = _world()
    c, hdr = Client(), _contact_headers(priv)
    sid = _started(c, hdr)
    c.put(f"/api/contact/sessions/{sid}/page-state",
          data={"state": {"resource": "labs-marketplace://orgs"}},
          content_type="application/json", **hdr)

    key = chat_keys.mint(Session.objects.get(pk=sid))
    with mock.patch("apps.mcp.chat_scope.get_http_headers",
                    return_value={chat_keys.HEADER.lower(): key}):
        pages = _page_of_chat()
    assert [p["session_id"] for p in pages] == [sid]
    assert pages[0]["state"]["resource"] == "labs-marketplace://orgs"


def test_one_contact_cannot_declare_a_page_on_anothers_session():
    _owner, _ws, _app, priv, _offered, _private = _world()
    c = Client()
    mine, theirs = _contact_headers(priv, sub="u-42"), _contact_headers(priv, sub="u-99")
    victim = _started(c, theirs)

    r = c.put(f"/api/contact/sessions/{victim}/page-state",
              data={"state": {"resource": "x://"}},
              content_type="application/json", **mine)

    assert r.status_code == 404
    assert Session.objects.get(pk=victim).page_state in ({}, None)


def test_a_contacts_oversized_page_state_is_refused_like_a_users():
    """The 8 KiB cap is a design guard — send the selection, not the rows — and
    it must not be reachable-around by arriving as a contact."""
    _owner, _ws, _app, priv, _offered, _private = _world()
    c, hdr = Client(), _contact_headers(priv)
    sid = _started(c, hdr)

    r = c.put(f"/api/contact/sessions/{sid}/page-state",
              data={"state": {"rows": ["x" * 200 for _ in range(60)]}},
              content_type="application/json", **hdr)

    assert r.status_code == 422, r.content
    assert "too_large" in r.json()["detail"]


def test_a_contact_declares_and_resolves_a_page_action():
    from apps.canopy_sessions.models import PageAction

    _owner, _ws, _app, priv, _offered, _private = _world()
    c, hdr = Client(), _contact_headers(priv)
    sid = _started(c, hdr)

    declared = c.put(
        f"/api/contact/sessions/{sid}/page-actions",
        data={"actions": [{"name": "scrollToOrg", "description": "Scroll to an org",
                           "parameters": {"type": "object",
                                          "properties": {"slug": {"type": "string"}},
                                          "required": ["slug"]}}]},
        content_type="application/json", **hdr,
    )
    assert declared.status_code == 200, declared.content
    assert [a["name"] for a in declared.json()] == ["scrollToOrg"]

    action = PageAction.objects.create(session=Session.objects.get(pk=sid),
                                       name="scrollToOrg", args={"slug": "acme-health"})
    resolved = c.post(f"/api/contact/sessions/{sid}/page-actions/{action.id}/result",
                      data={"result": {"ok": True}}, content_type="application/json", **hdr)

    assert resolved.status_code == 200, resolved.content
    assert resolved.json()["status"] == PageAction.DONE


def test_a_contact_cannot_resolve_an_action_on_anothers_session():
    from apps.canopy_sessions.models import PageAction

    _owner, _ws, _app, priv, _offered, _private = _world()
    c = Client()
    mine, theirs = _contact_headers(priv, sub="u-42"), _contact_headers(priv, sub="u-99")
    victim = _started(c, theirs)
    action = PageAction.objects.create(session=Session.objects.get(pk=victim),
                                       name="scrollToOrg", args={})

    r = c.post(f"/api/contact/sessions/{victim}/page-actions/{action.id}/result",
               data={"result": {"ok": True}}, content_type="application/json", **mine)

    assert r.status_code == 404
    action.refresh_from_db()
    assert action.status == PageAction.PENDING


def test_a_member_cannot_declare_a_page_through_the_contact_routes():
    """The mirrored routes must stay contact-only, exactly as the rest do."""
    _owner, _ws, _app, priv, _offered, _private = _world()
    c, hdr = Client(), _contact_headers(priv)
    sid = _started(c, hdr)

    member = Client()
    member.force_login(User.objects.get(username="boss"))
    r = member.put(f"/api/contact/sessions/{sid}/page-state",
                   data={"state": {"resource": "x://"}}, content_type="application/json")

    assert r.status_code in (401, 403), r.content
