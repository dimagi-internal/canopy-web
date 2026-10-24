"""The doors around `sensitivity.reads_content`: every way a sensitive conversation's
words could reach a reader the gate would refuse — an MCP tool, a session minted
from a token, an owner who signs as someone else, a push, a renamed tab — is
closed, one test per door.

docs/architecture/tenancy.md § Sensitive conversations.
"""
from __future__ import annotations

import uuid
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions.models import Attachment, RunnerBinding, Session, SessionParticipant
from apps.harness import caller_context, sensitivity
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.sensitive import ASKED, REPLY, SENSITIVE, _converse, _socket, _turn, _unowned_thread, minted, world

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    return world()


def _keypair_pub() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    return ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()

QUESTION = "Which of Amina's visits should I flag?"
MENU = {"question": QUESTION, "title": "Amina", "body": "", "selected": None,
        "options": [{"number": 1, "label": "Amina 2026-10-01", "description": "HIV+"},
                    {"number": 2, "label": "none", "description": ""}],
        "source": "acp", "observed_at": 1.0}


def _minted(user) -> Client:
    """A session cookie minted from a token (`/api/debug/mint-session/`)."""
    return minted(user)


# --- 1. who_is_asking ---------------------------------------------------------------


def test_who_is_asking_refuses_a_members_read_of_a_sensitive_turn(w):
    from apps.mcp.tools.caller import TurnNotFound, _envelope_for_caller_token, _envelope_sync

    _converse(w["hot"])
    hot = _turn(w["hot"], prompt="and last month?")
    with pytest.raises(TurnNotFound):
        _envelope_sync(w["user"].pk, str(hot.pk))
    # An ordinary turn still answers.
    assert _envelope_sync(w["user"].pk, str(_turn(w["cold"]).pk))["turn_id"]
    # The turn's own box (its caller token) re-reads its envelope, with none of the
    # conversation's words in it: the agent remembers them in its own session.
    env = _envelope_for_caller_token(str(hot.pk))
    assert ASKED not in str(env) and REPLY not in str(env)


def test_neither_the_claim_nor_the_envelope_carries_the_words(w):
    from apps.harness.schemas import ClaimedTurnOut

    _converse(w["hot"])
    t = _turn(w["hot"], prompt="and last month?")
    for env in (caller_context.build(t), ClaimedTurnOut.resolve_caller_context(t)):
        assert "history" not in env["conversation"]
        assert ASKED not in str(env) and REPLY not in str(env)


# --- 2. app views ------------------------------------------------------------------


def test_app_view_routes_refuse_a_minted_session(w):
    r = _minted(w["user"]).get(f"/api/canopy-sessions/{w['hot'].pk}/apps/tu1/resource")
    assert r.status_code == 403, r.content
    assert "token" in r.json()["detail"]


def test_a_views_arguments_follow_the_real_surface(w):
    from apps.mcp_apps_host import mcp_apps_views

    _converse(w["hot"])
    row = mock.Mock(app=mock.Mock(name="labs"), ref={"resource_uri": "ui://x", "tool": "data_query",
                                                            "path": "gateway"})
    calls = []

    def reads(session, *, chat_surface, user=None, contact=None):
        calls.append(chat_surface)
        return False

    with mock.patch.object(mcp_apps_views, "find", return_value=row), \
            mock.patch.object(mcp_apps_views, "resolve_for_viewer", return_value=None), \
            mock.patch.object(mcp_apps_views.mcp_apps, "cached_resource",
                              return_value={"html": "<p/>"}), \
            mock.patch.object(mcp_apps_views, "_audit"), \
            mock.patch.object(mcp_apps_views.sensitivity, "reads_content", side_effect=reads), \
            mock.patch.object(row.app, "refresh_from_db"):
        out = mcp_apps_views.resource(w["hot"], mcp_apps_views.Viewer(user=w["user"]), "tu1")
    assert calls == [False] and out["tool_input"] == {} and out["tool_input_sealed"] is None


# --- 3. the panel's own token -----------------------------------------------------------


def test_the_panel_token_is_never_minted_from_a_minted_session(w):
    r = _minted(w["user"]).post("/api/embed/token", data={},
                                content_type="application/json")
    assert r.status_code == 403, r.content


# --- 4. attachments -------------------------------------------------------------------


def _attachment(session, user):
    return Attachment.objects.create(
        session=session, uploaded_by=user, filename="scan.png", content_type="image/png",
        size_bytes=4, storage_key=f"sessions/{session.id}/{uuid.uuid4()}/scan.png")


def test_a_sensitive_attachment_reaches_its_people_on_a_chat_surface_only(w, settings):
    from apps.tokens.models import PersonalToken

    settings.CANOPY_ATTACHMENTS_BUCKET = "test-bucket"
    row = _attachment(w["hot"], w["user"])
    url = f"/api/canopy-sessions/attachments/{row.id}/content"
    with mock.patch("apps.canopy_sessions.attachment_storage.get") as get:
        get.return_value = mock.Mock(body=b"\x89PNG", content_type="image/png")
        assert w["c"].get(url).status_code == 200
        raw, _ = PersonalToken.create_for_user(user=w["user"], label="cli")
        assert Client(HTTP_AUTHORIZATION=f"Bearer {raw}").get(url).status_code == 404
        assert _minted(w["user"]).get(url).status_code == 404
        # The agent's owner reads a thread nobody started, but is not in it.
        other = _attachment(_unowned_thread(w), w["user"])
        assert w["c"].get(f"/api/canopy-sessions/attachments/{other.id}/content").status_code == 404


# --- 5. naming a site's signer ------------------------------------------------------------


def test_registering_a_site_or_changing_its_signer_needs_a_passkey(w):
    from apps.tokens.models import AppCredential, PersonalToken

    pub = _keypair_pub()
    body = {"name": "evil", "origins": ["https://evil.example"], "public_keys": [pub]}
    r = w["c"].post("/api/workspaces/w1/connected-apps", data=body,
                    content_type="application/json")
    assert r.status_code == 403 and r.json()["error"] == "step_up_required", r.content
    raw, _ = PersonalToken.create_for_user(user=w["user"], label="cli")
    pat = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    r = pat.post("/api/workspaces/w1/connected-apps", data=body, content_type="application/json")
    assert r.status_code == 403 and r.json().get("error") != "step_up_required", r.content
    assert not AppCredential.objects.filter(name="evil").exists()

    grant_step_up(w["c"])
    r = w["c"].post("/api/workspaces/w1/connected-apps", data=body, content_type="application/json")
    assert r.status_code == 201, r.content
    app_id = r.json()["id"]

    # Anything but the signer needs no passkey; the signer always does.
    fresh = Client()
    fresh.force_login(w["user"])
    url = f"/api/workspaces/w1/connected-apps/{app_id}"
    same = {"public_keys": [pub], "show_on_canopy_pages": False}
    assert fresh.patch(url, data=same, content_type="application/json").status_code == 200
    for change in ({"jwks_url": "https://evil.example/other.json"},
                   {"public_keys": [_keypair_pub()]},
                   {"host_issuer": "https://evil.example"}):
        r = fresh.patch(url, data=change, content_type="application/json")
        assert r.status_code == 403, (change, r.content)
        r = pat.patch(url, data=change, content_type="application/json")
        assert r.status_code == 403, (change, r.content)
    assert AppCredential.objects.get(pk=app_id).public_keys == [pub.strip()]


# --- 6. a blocked agent's question ----------------------------------------------------------


def test_a_sensitive_dialog_keeps_its_shape_never_its_words(w):
    from apps.harness import session_sync
    from apps.push import services as push

    binding = RunnerBinding.objects.create(session=w["hot"], runner=w["runner"], session_key="k")
    pushed, relayed = [], []
    with mock.patch.object(push, "send_to_user", side_effect=lambda u, **kw: pushed.append(kw) or 1), \
            mock.patch.object(push, "_question_audience", return_value=w["user"]), \
            mock.patch("apps.slack.relay.status_mod", create=True), \
            mock.patch("apps.slack.relay.session_destination",
                       side_effect=lambda s: relayed.append(s) or ("i", "C1", "1")), \
            mock.patch("django.db.transaction.on_commit", lambda fn: fn()):
        assert session_sync.report_session_menu(w["runner"], w["hot"].pk, MENU)
    binding.refresh_from_db()
    stored = binding.pending_question
    assert "Amina" not in str(stored) and "HIV" not in str(stored) and QUESTION not in str(stored)
    assert [o["number"] for o in stored["options"]] == [1, 2] and stored["source"] == "acp"
    served = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()["menu"]
    assert "Amina" not in str(served)
    assert pushed and "Amina" not in str(pushed) and push.SENSITIVE_QUESTION_BODY in pushed[0]["body"]
    from apps.slack import relay

    with mock.patch.object(relay, "session_destination", return_value=("i", "C1", "1")), \
            mock.patch.object(relay, "menus") as menus:
        assert relay.relay_menu(w["hot"].pk, MENU) is False
    menus.to_text.assert_not_called()


def test_a_new_sensitive_ask_is_still_told_from_the_last(w):
    first = sensitivity.menu_metadata(MENU)
    second = sensitivity.menu_metadata({**MENU, "question": "Which of Baraka's?"})
    assert first["sha256"] != second["sha256"]
    assert sensitivity.menu_metadata(first) == first and sensitivity.menu_metadata(None) is None


# --- 7. the live socket, after it connected ---------------------------------------------------


def _recheck(session, user, *, auth_method="session", become=None, then=None):
    """Connect, change something, deliver the recheck frame; did the socket close?"""
    from asgiref.sync import async_to_sync
    from channels.testing import WebsocketCommunicator

    from apps.canopy_sessions.consumers import SessionConsumer

    async def go():
        comm = WebsocketCommunicator(SessionConsumer.as_asgi(), f"/ws/canopy-sessions/{session.id}/")
        comm.scope.update(user=user, auth_method=auth_method, session={},
                          url_route={"kwargs": {"session_id": str(session.id)}})
        connected, _ = await comm.connect()
        assert connected
        while not await comm.receive_nothing(timeout=0.2):
            await comm.receive_output()
        from channels.db import database_sync_to_async

        frames = []
        with mock.patch("apps.realtime.groups.publish", lambda g, m: frames.append((g, m))):
            await database_sync_to_async(then)()
        assert frames, "no recheck was asked for"
        for _group, frame in frames:
            await comm.send_input(frame)  # what the channel layer would deliver
        closed = False
        while not closed and not await comm.receive_nothing(timeout=0.5):
            closed = (await comm.receive_output()).get("type") == "websocket.close"
        await comm.disconnect()
        return closed
    return async_to_sync(go)()


def test_a_token_socket_closes_when_its_conversation_becomes_sensitive(w):
    from apps.canopy_sessions import services

    with mock.patch("django.db.transaction.on_commit", lambda fn: fn()):
        closed = _recheck(w["cold"], w["user"], auth_method="pat",
                          then=lambda: services.add_runner_requirements(w["cold"], {"sensitive_data"}))
    assert closed
    # Its own person, at a browser, stays.
    s = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["user"])
    with mock.patch("django.db.transaction.on_commit", lambda fn: fn()):
        assert not _recheck(s, w["user"],
                            then=lambda: services.add_runner_requirements(s, {"sensitive_data"}))


def _give_copy(w, peer):
    from apps.canopy_sessions import conversation_keys
    from tests.fixtures.sensitive import rewrap_for, vault_key

    vault_key(peer)
    wrap = rewrap_for(w["hot"], w["user"], w["key"], peer)
    conversation_keys.add_wrap(w["hot"], peer, {k: v for k, v in wrap.items() if k != "recipient_jkt"},
                               recipient_jkt=wrap["recipient_jkt"], by=w["user"])


def test_a_removed_participants_socket_closes(w):
    peer = User.objects.create_user("peer", "peer@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=peer, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    SessionParticipant.objects.create(session=w["hot"], user=peer, role=SessionParticipant.VIEWER)
    _give_copy(w, peer)
    assert _socket(w["hot"], peer)

    def remove():
        r = w["c"].delete(f"/api/canopy-sessions/{w['hot'].pk}/participants/{peer.pk}")
        assert r.status_code == 200, r.content

    with mock.patch("django.db.transaction.on_commit", lambda fn: fn()):
        assert _recheck(w["hot"], peer, then=remove)


# --- 8. titles ---------------------------------------------------------------------------


def test_a_sensitive_conversation_keeps_no_title(w):
    from apps.canopy_sessions import services
    from apps.harness import provenance

    r = w["c"].put(f"/api/canopy-sessions/{w['hot'].pk}/title", data={"title": "Amina's visits"},
                   content_type="application/json")
    assert r.status_code == 422 and "sensitive" in r.json()["detail"], r.content
    assert w["c"].put(f"/api/canopy-sessions/{w['hot'].pk}/title", data={"title": ""},
                      content_type="application/json").status_code == 200
    born = services.create_session(workspace=w["ws"], created_by=w["user"], agent=w["agent"],
                                   title="Amina's visits", metadata=dict(SENSITIVE))
    assert born.title == "" and provenance.session_record(born)["title"] == ""
    # A conversation that becomes sensitive drops the title it had.
    services.add_runner_requirements(w["cold"], {"sensitive_data"})
    w["cold"].refresh_from_db()
    assert w["cold"].title == ""
    assert services.create_session(workspace=w["ws"], created_by=w["user"],
                                   title="plain").title == "plain"
