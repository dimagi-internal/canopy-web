"""A private (sensitive) conversation's key stays with its people: the starter's browser
makes it, a person given the conversation before it became private is given a copy by
someone in it, a person who replaces their vault key carries their copies over, and the
live socket hears only someone who holds one. Plus what such a conversation does not
take: a file, a draft kept on the server (spec
2026-10-23-sensitive-conversations-sealed-to-their-people §2).

The tests stand in for the browser with the vault private key only it holds
(`tests/fixtures/sensitive.py::vault_key`).
"""
from __future__ import annotations

import json
import uuid
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.canopy_sessions import conversation_keys
from apps.canopy_sessions import services as chat_services
from apps.canopy_sessions.models import Attachment, Message, SessionKey, SessionKeyWrap, SessionParticipant
from apps.common import ecdh_es
from apps.passkeys.models import UserVaultKeyset
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.sensitive import ASKED, REPLY, _converse, browser_said, rewrap_for, unwrap, vault_key, world

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    out = world()
    # As every conversation started through `create_session`: its starter is its owner.
    for s in (out["hot"], out["cold"]):
        SessionParticipant.objects.create(session=s, user=out["user"], role=SessionParticipant.OWNER)
    return out


def _peer(w, name="peer"):
    u = User.objects.create_user(name, f"{name}@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=u, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    priv = vault_key(u)
    c = Client()
    c.force_login(u)
    return u, priv, c


def _browser_key(user, priv=None):
    """What the starter's browser sends with a new chat: a fresh key pair, its private
    half wrapped to their own vault key. Returns (offer, the conversation private key)."""
    conv = ec.generate_private_key(ec.SECP256R1())
    scalar = conv.private_numbers().private_value.to_bytes(32, "big")
    mine = UserVaultKeyset.objects.get(user=user).public_jwk
    conversation = str(uuid.uuid4())
    offer = {"conversation": conversation, "public_jwk": ecdh_es.jwk_of(conv.public_key()),
             "wrap": ecdh_es.wrap(scalar, mine, conversation_keys.wrap_info(conversation, user.pk)),
             "recipient_jkt": ecdh_es.jkt(mine)}
    return offer, conv


def _socket_frames(session, user):
    """Connect as `user`; every frame until the socket closes (or goes quiet)."""
    from asgiref.sync import async_to_sync
    from channels.testing import WebsocketCommunicator

    from apps.canopy_sessions.consumers import SessionConsumer

    async def go():
        comm = WebsocketCommunicator(SessionConsumer.as_asgi(), f"/ws/canopy-sessions/{session.id}/")
        comm.scope.update(user=user, auth_method="session", session={},
                          url_route={"kwargs": {"session_id": str(session.id)}})
        connected, _ = await comm.connect()
        out = []
        while connected and not await comm.receive_nothing(timeout=0.3):
            out.append(await comm.receive_output())
        await comm.disconnect()
        return connected, out

    return async_to_sync(go)()


def _admitted(session, user) -> bool:
    connected, frames = _socket_frames(session, user)
    return connected and not any(f.get("type") == "websocket.close" for f in frames)


# --- 1. a file is not taken into a private conversation ------------------------------


def test_a_file_is_refused_in_a_private_conversation_and_nothing_is_stored(w, settings):
    settings.ATTACHMENT_ALLOWED_CONTENT_TYPES = ["image/png"]
    with mock.patch("apps.canopy_sessions.attachment_storage.put") as put, \
            mock.patch("apps.canopy_sessions.attachment_storage.is_configured", return_value=True):
        r = w["c"].post(f"/api/canopy-sessions/{w['hot'].pk}/attachments",
                        {"file": SimpleUploadedFile("amina-chart.png", b"\x89PNG" + b"x" * 64, "image/png")})
        assert r.status_code == 422 and "private conversation" in r.json()["detail"], r.content
        assert not put.called and not Attachment.objects.filter(session=w["hot"]).exists()
        # An ordinary conversation still takes one.
        ok = w["c"].post(f"/api/canopy-sessions/{w['cold'].pk}/attachments",
                         {"file": SimpleUploadedFile("chart.png", b"\x89PNG" + b"x" * 64, "image/png")})
        assert ok.status_code == 201, ok.content


# --- 2. the starter's browser makes the key -------------------------------------------


def test_a_chat_started_in_a_browser_seals_to_the_key_that_browser_made(w):
    offer, conv = _browser_key(w["user"])
    with mock.patch.object(ecdh_es, "generate", side_effect=AssertionError("made on the server")):
        r = w["c"].post("/api/canopy-sessions/", {"agent_slug": w["agent"].slug, "conversation_key": offer},
                        content_type="application/json")
        assert r.status_code == 200, r.content
        from apps.canopy_sessions.models import Session

        s = Session.objects.get(pk=r.json()["id"])
        # Kept while it is ordinary; it is the key once it becomes private.
        chat_services.add_runner_requirements(s, ["sensitive_data"])
    key = SessionKey.objects.get(session=s)
    assert key.public_jwk == offer["public_jwk"] and str(key.conversation) == offer["conversation"]
    _converse(s)
    sealed = Message.objects.get(session=s, role=Message.ASSISTANT).sealed
    body = json.loads(ecdh_es.unb64u(sealed[len(conversation_keys.PREFIX):]))
    words = json.loads(unwrap(body, conv, conversation_keys.message_info(body["c"], body["i"], body["r"])))
    assert words["plaintext"] == REPLY
    detail = w["c"].get(f"/api/canopy-sessions/{s.pk}").json()
    assert REPLY in browser_said(detail["messages"], detail["sealed_key"], w["key"], w["user"].pk)


@pytest.mark.parametrize("spoil", ["jkt", "point", "wrap"])
def test_a_browser_key_that_is_not_for_the_starter_is_refused(w, spoil):
    offer, _ = _browser_key(w["user"])
    if spoil == "jkt":
        offer["recipient_jkt"] = "x" * 43
    elif spoil == "point":
        offer["public_jwk"] = {**offer["public_jwk"], "y": offer["public_jwk"]["x"]}
    else:
        offer["wrap"] = {**offer["wrap"], "ct": "AAAA"}
    r = w["c"].post("/api/canopy-sessions/", {"agent_slug": w["agent"].slug, "conversation_key": offer},
                    content_type="application/json")
    assert r.status_code == 422, r.content


def test_with_no_browser_the_server_still_makes_one(w):
    """A conversation made private with no browser present (mid-turn, Slack, a host's
    grant): the documented limit — the server makes the pair and lets the private half go."""
    chat_services.add_runner_requirements(w["cold"], ["sensitive_data"])
    assert SessionKey.objects.get(session=w["cold"]).conversation == w["cold"].pk


# --- 3. given it before it became private -------------------------------------------


def test_someone_given_it_before_it_became_private_waits_for_a_copy(w):
    peer, peer_priv, peer_c = _peer(w)
    SessionParticipant.objects.create(session=w["cold"], user=peer, role=SessionParticipant.EDITOR)
    chat_services.add_runner_requirements(w["cold"], ["sensitive_data"])
    people = w["c"].get(f"/api/canopy-sessions/{w['cold'].pk}/participants").json()
    assert {p["email"]: p["needs_key"] for p in people} == {"jj@dimagi.com": False, "peer@dimagi.com": True}
    # Not on the live socket, whose frames carry the words, until they hold one — and told why.
    connected, frames = _socket_frames(w["cold"], peer)
    assert any("needs_key" in json.dumps(f) for f in frames)
    assert any(f.get("type") == "websocket.close" for f in frames)
    assert _admitted(w["cold"], w["user"])

    # The owner's browser gives them one in a click.
    recipient = w["c"].get(f"/api/canopy-sessions/{w['cold'].pk}/participants/recipient?email=peer@dimagi.com")
    assert recipient.status_code == 200
    wrap = rewrap_for(w["cold"], w["user"], w["key"], peer)
    r = w["c"].post(f"/api/canopy-sessions/{w['cold'].pk}/key-copies", {"user_id": peer.pk, "wrap": wrap},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    assert {p["email"]: p["needs_key"] for p in r.json()}["peer@dimagi.com"] is False
    assert _admitted(w["cold"], peer)
    _converse(w["cold"])
    detail = peer_c.get(f"/api/canopy-sessions/{w['cold'].pk}").json()
    assert REPLY in browser_said(detail["messages"], detail["sealed_key"], peer_priv, peer.pk)


def test_a_copy_goes_only_to_someone_already_in_it(w):
    outsider, _priv, _c = _peer(w, "outsider")
    wrap = rewrap_for(w["hot"], w["user"], w["key"], outsider)
    r = w["c"].post(f"/api/canopy-sessions/{w['hot'].pk}/key-copies", {"user_id": outsider.pk, "wrap": wrap},
                    content_type="application/json")
    assert r.status_code == 404
    assert not SessionKeyWrap.objects.filter(session=w["hot"], user=outsider).exists()
    # Nor does asking for their key let a non-owner find out who to add.
    peer, _p, peer_c = _peer(w)
    SessionParticipant.objects.create(session=w["hot"], user=peer, role=SessionParticipant.EDITOR)
    assert peer_c.get(f"/api/canopy-sessions/{w['hot'].pk}/participants/recipient"
                      "?email=outsider@dimagi.com").status_code == 403


# --- 4. replacing a vault key ---------------------------------------------------------


def test_a_new_vault_key_carries_its_copies_over(w):
    _converse(w["hot"])
    mine = w["c"].get("/api/canopy-sessions/my-keys")
    assert mine.status_code == 200, mine.content
    [held] = [k for k in mine.json() if k["session_id"] == str(w["hot"].pk)]
    assert held["agent_name"] == "labsdata" and ASKED not in json.dumps(mine.json())
    # The browser opens each copy with the old key, then sets the new key ...
    scalar = unwrap(held["wrap"], w["key"], conversation_keys.wrap_info(held["conversation"], w["user"].pk))
    new = ec.generate_private_key(ec.SECP256R1())
    new_jwk = ecdh_es.jwk_of(new.public_key())
    UserVaultKeyset.objects.filter(user=w["user"]).update(public_jwk=new_jwk)
    stale = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()["sealed_key"]
    assert stale["current"] is False
    # ... and stores the copies re-wrapped to it.
    copy = {"session_id": held["session_id"], "recipient_jkt": ecdh_es.jkt(new_jwk),
            "wrap": ecdh_es.wrap(scalar, new_jwk, conversation_keys.wrap_info(held["conversation"], w["user"].pk))}
    bad = w["c"].put("/api/canopy-sessions/my-keys", {"copies": [{**copy, "recipient_jkt": "x" * 43}]},
                     content_type="application/json")
    assert bad.status_code == 422
    r = w["c"].put("/api/canopy-sessions/my-keys", {"copies": [copy]}, content_type="application/json")
    assert r.status_code == 200 and r.json()["stored"] == 1, r.content
    detail = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()
    assert detail["sealed_key"]["current"] is True
    assert REPLY in browser_said(detail["messages"], detail["sealed_key"], new, w["user"].pk)


def test_a_copy_is_replaced_only_where_one_is_held(w):
    from apps.canopy_sessions.models import Session

    other = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["user"],
                                   metadata={"runner_requirements": ["sensitive_data"]})
    SessionKeyWrap.objects.filter(session=other).delete()
    jwk = UserVaultKeyset.objects.get(user=w["user"]).public_jwk
    copy = {"session_id": str(other.pk), "recipient_jkt": ecdh_es.jkt(jwk),
            "wrap": ecdh_es.wrap(b"\x01" * 32, jwk, "x")}
    r = w["c"].put("/api/canopy-sessions/my-keys", {"copies": [copy]}, content_type="application/json")
    assert r.status_code == 200 and r.json()["stored"] == 0
    assert not SessionKeyWrap.objects.filter(session=other).exists()


def test_someone_else_in_it_refreshes_the_starters_stale_copy(w):
    peer, peer_priv, peer_c = _peer(w)
    SessionParticipant.objects.create(session=w["hot"], user=peer, role=SessionParticipant.EDITOR)
    give = rewrap_for(w["hot"], w["user"], w["key"], peer)
    conversation_keys.add_wrap(w["hot"], peer, {k: v for k, v in give.items() if k != "recipient_jkt"},
                               recipient_jkt=give["recipient_jkt"], by=w["user"])
    new = vault_key(w["user"])  # the starter replaced their vault key and kept no copy
    people = peer_c.get(f"/api/canopy-sessions/{w['hot'].pk}/participants").json()
    assert {p["email"]: p["needs_key"] for p in people}["jj@dimagi.com"] is True
    # The starter cannot hand on what their own stale copy no longer opens.
    third, _t, _c = _peer(w, "third")
    SessionParticipant.objects.create(session=w["hot"], user=third, role=SessionParticipant.EDITOR)
    refused = w["c"].post(f"/api/canopy-sessions/{w['hot'].pk}/key-copies",
                          {"user_id": third.pk, "wrap": give}, content_type="application/json")
    assert refused.status_code == 409 and "your own copy" in refused.json()["detail"]
    # The peer can: the old "that person owns this chat" 409 is not in the way.
    recipient = peer_c.get(f"/api/canopy-sessions/{w['hot'].pk}/participants/recipient?email=jj@dimagi.com")
    assert recipient.status_code == 200
    wrap = rewrap_for(w["hot"], peer, peer_priv, w["user"])
    r = peer_c.post(f"/api/canopy-sessions/{w['hot'].pk}/key-copies", {"user_id": w["user"].pk, "wrap": wrap},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    _converse(w["hot"])
    detail = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()
    assert REPLY in browser_said(detail["messages"], detail["sealed_key"], new, w["user"].pk)


# --- 10. what an envelope binds ---------------------------------------------------------


def test_an_envelope_binds_its_speaker(w):
    _converse(w["hot"])
    row = Message.objects.get(session=w["hot"], role=Message.ASSISTANT)
    body = json.loads(ecdh_es.unb64u(row.sealed[len(conversation_keys.PREFIX):]))
    assert body["r"] == Message.ASSISTANT
    key = SessionKey.objects.get(session=w["hot"])
    wrap = SessionKeyWrap.objects.get(session=w["hot"], user=w["user"])
    scalar = unwrap(wrap.wrap, w["key"], conversation_keys.wrap_info(key.conversation, w["user"].pk))
    conv = ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())
    # Served as the person's row instead, it does not open.
    with pytest.raises(Exception):
        unwrap(body, conv, conversation_keys.message_info(body["c"], body["i"], Message.USER))
    assert w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()["sealed_key"]["index_offsets"] == []


def test_my_vault_key_is_its_public_half_only(w):
    r = w["c"].get("/api/canopy-sessions/my-vault-key")
    assert r.status_code == 200
    jwk = UserVaultKeyset.objects.get(user=w["user"]).public_jwk
    body = r.json()
    assert {k: body[k] for k in ("user_id", "public_jwk", "jkt")} == {
        "user_id": str(w["user"].pk), "public_jwk": jwk, "jkt": ecdh_es.jkt(jwk)}
    # The person's own browser also gets what opens it (public half and seals, never a
    # private key), to derive the key itself before trusting this one; a token does not.
    assert body["keyset"]["public_jwk"] == jwk and "seals" in body["keyset"]
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=w["user"], label="cli")
    assert Client(HTTP_AUTHORIZATION=f"Bearer {raw}").get(
        "/api/canopy-sessions/my-vault-key").json()["keyset"] is None
    UserVaultKeyset.objects.filter(user=w["user"]).delete()
    assert w["c"].get("/api/canopy-sessions/my-vault-key").status_code == 404


def test_my_key_copies_are_for_my_own_browser_only(w):
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=w["user"], label="cli")
    token = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert token.get("/api/canopy-sessions/my-keys").status_code == 403
    assert token.put("/api/canopy-sessions/my-keys", {"copies": []},
                     content_type="application/json").status_code == 403
