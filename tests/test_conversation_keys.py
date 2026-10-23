"""A sensitive conversation is sealed to its people: canopy-web can write it and never
read it back (spec 2026-10-23-sensitive-conversations-sealed-to-their-people §2).

The tests stand in for the person's browser with the vault private key only they hold
(`tests/fixtures/sensitive.py::vault_key`); nothing in app code can open what it seals.
"""
from __future__ import annotations

import json
import pathlib
from unittest import mock

import pytest
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric import ec
from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.test import Client

from apps.canopy_sessions import conversation_keys
from apps.canopy_sessions import services as chat_services
from apps.canopy_sessions.models import Message, RunnerBinding, Session, SessionKey, SessionKeyWrap
from apps.common import ecdh_es, encryption
from apps.harness import sensitivity
from apps.harness.models import Runner
from apps.passkeys.models import UserVaultKeyset
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.sensitive import (
    ASKED,
    REPLY,
    ROW,
    SENSITIVE,
    _converse,
    browser_said,
    open_sealed,
    rewrap_for,
    unwrap,
    vault_key,
    world,
)

pytestmark = pytest.mark.django_db
ROOT = pathlib.Path(__file__).resolve().parent.parent
VECTORS = ROOT / "docs/vault/chat-v1/chat_v1_vectors.json"


@pytest.fixture()
def w():
    return world()


def _peer(w, name="peer", *, key=True):
    u = User.objects.create_user(name, f"{name}@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=u, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    priv = vault_key(u) if key else None
    c = Client()
    c.force_login(u)
    return u, priv, c


# --- the cross-language vector ------------------------------------------------------


def test_the_committed_vector_is_what_the_server_seals_today():
    """`chat_v1_vectors.json` is the server's own output with pinned randomness; the
    browser's `sealedChat.test.ts` opens it and reproduces the wrap byte for byte."""
    assert json.loads(VECTORS.read_text()) == json.loads(json.dumps(conversation_keys.vectors()))


def test_the_vector_opens_with_the_people_s_keys_and_binds_what_it_says():
    v = json.loads(VECTORS.read_text())
    i, o = v["inputs"], v["outputs"]
    person = ec.derive_private_key(int(i["user_priv_scalar_hex"], 16), ec.SECP256R1())
    scalar = unwrap(o["wrap"], person, o["wrap_info"])
    assert scalar.hex() == i["conversation_priv_scalar_hex"]
    conv = ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())
    body = json.loads(ecdh_es.unb64u(o["sealed"][4:]))
    assert (body["c"], body["i"]) == (i["conversation"], i["turn_index"])
    assert json.loads(unwrap(body, conv, o["message_info"])) == i["words"]
    with pytest.raises(Exception):
        unwrap(body, conv, conversation_keys.message_info(i["conversation"], i["turn_index"] + 1))
    with pytest.raises(Exception):
        unwrap(o["wrap"], person, conversation_keys.wrap_info(i["conversation"], "8"))


# --- the key ------------------------------------------------------------------------


def test_a_conversation_born_sensitive_has_a_key_wrapped_to_its_starter(w):
    key = SessionKey.objects.get(session=w["hot"])
    assert key.conversation == w["hot"].pk
    [wrap] = SessionKeyWrap.objects.filter(session=w["hot"])
    assert wrap.user_id == w["user"].pk
    scalar = unwrap(wrap.wrap, w["key"], conversation_keys.wrap_info(key.conversation, w["user"].pk))
    opened = ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())
    assert ecdh_es.jwk_of(opened.public_key()) == key.public_jwk
    assert not SessionKey.objects.filter(session=w["cold"]).exists()


def test_a_conversation_that_becomes_sensitive_gets_its_key_then(w):
    chat_services.add_runner_requirements(w["cold"], ["sensitive_data"])
    assert SessionKeyWrap.objects.filter(session=w["cold"], user=w["user"]).exists()


def test_the_private_key_is_kept_nowhere(w):
    seen = []
    real = ecdh_es.generate

    def capture():
        scalar, jwk = real()
        seen.append(scalar)
        return scalar, jwk

    with mock.patch.object(ecdh_es, "generate", capture):
        s = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["user"],
                                   metadata=dict(SENSITIVE))
    [scalar] = seen
    stored = str(list(SessionKey.objects.filter(session=s).values())) + str(
        list(SessionKeyWrap.objects.filter(session=s).values()))
    for form in (scalar.hex(), ecdh_es.b64u(scalar), str(int.from_bytes(scalar, "big"))):
        assert form not in stored


def test_a_starter_with_no_vault_key_is_refused_in_a_sentence(w):
    UserVaultKeyset.objects.filter(user=w["user"]).delete()
    s = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["user"],
                               metadata=dict(SENSITIVE))
    assert not SessionKey.objects.filter(session=s).exists()
    r = w["c"].post(f"/api/canopy-sessions/{s.pk}/send", {"text": ASKED}, content_type="application/json")
    assert r.status_code == 422
    assert r.json()["detail"].startswith(
        "a sensitive conversation needs a canopy account with a vault key; set yours up at http")
    assert not Message.objects.filter(session=s).exists()
    # Rows the agent ships into a keyless sensitive conversation keep no words at all.
    _converse(s)
    assert not Message.objects.filter(session=s, sealed__isnull=False).exists()
    assert ASKED not in str(list(Message.objects.filter(session=s).values()))


# --- canopy-web cannot read it back -------------------------------------------------


def _every_text_in_the_database() -> str:
    out = []
    with connection.cursor() as cur:
        for model in django_apps.get_models():
            table = model._meta.db_table
            cur.execute(f'SELECT * FROM "{table}"')  # noqa: S608 — the test's own table names
            out.append(str(cur.fetchall()))
    return "\n".join(out)


def test_nothing_in_the_database_or_an_api_response_holds_the_words(w):
    _converse(w["hot"])
    w["c"].post(f"/api/canopy-sessions/{w['hot'].pk}/send", {"text": "canary-7f3a in the send"},
                content_type="application/json")
    db = _every_text_in_the_database()
    for words in (ASKED, REPLY, ROW, "canary-7f3a", "Amina"):
        assert words not in db
    for path in (f"/api/canopy-sessions/{w['hot'].pk}", f"/api/canopy-sessions/{w['hot'].pk}/messages?before=99999",
                 f"/api/canopy-sessions/{w['hot'].pk}/info", f"/api/canopy-sessions/{w['hot'].pk}/human-inputs"):
        body = w["c"].get(path).content.decode()
        assert "canary-7f3a" not in body and ASKED not in body and REPLY not in body, path


def test_no_key_canopy_web_holds_opens_a_sealed_row(w, settings):
    """The field key (and the legacy SECRET_KEY-derived one) open nothing; there is no
    private key column to try."""
    _converse(w["hot"])
    settings.FIELD_ENCRYPTION_ACCEPT_LEGACY = True
    sealed = [m.sealed for m in Message.objects.filter(session=w["hot"], sealed__isnull=False)]
    assert sealed and all(conversation_keys.is_envelope(s) for s in sealed)
    keys = encryption._keys() + [encryption._derive_key(settings.SECRET_KEY)]
    for s in sealed:
        for k in keys:
            for blob in (s, s[len(conversation_keys.PREFIX):]):
                with pytest.raises((InvalidToken, ValueError)):
                    Fernet(k).decrypt(blob.encode())
        with pytest.raises(Exception):
            encryption.decrypt_secret(s)
    columns = {f.name for m in (SessionKey, SessionKeyWrap) for f in m._meta.get_fields()}
    assert not columns & {"private_jwk", "private_key", "scalar", "d"}
    # And the server's own seal module has no way to open anything.
    assert not hasattr(ecdh_es, "unwrap") and not hasattr(sensitivity, "unseal")


# --- reading back -------------------------------------------------------------------


def test_the_socket_snapshot_carries_the_ciphertext_for_its_own_people(w):
    from asgiref.sync import async_to_sync
    from channels.testing import WebsocketCommunicator

    from apps.canopy_sessions.consumers import SessionConsumer

    _converse(w["hot"])

    async def snapshot():
        comm = WebsocketCommunicator(SessionConsumer.as_asgi(), f"/ws/canopy-sessions/{w['hot'].id}/")
        comm.scope.update(user=w["user"], auth_method="session", session={},
                          url_route={"kwargs": {"session_id": str(w["hot"].id)}})
        await comm.connect()
        frame = await comm.receive_json_from(timeout=5)
        await comm.disconnect()
        return frame

    frame = async_to_sync(snapshot)()
    rows = frame["data"]["messages"] if "messages" in frame.get("data", {}) else \
        frame["metadata"]["data"]["messages"] if "metadata" in frame else []
    assert rows and any(r.get("sealed") for r in rows)
    assert ASKED not in json.dumps(frame)
    key = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()["sealed_key"]
    assert ASKED in browser_said(rows, key, w["key"], w["user"].pk)


def test_a_reader_the_gate_refuses_gets_neither_the_ciphertext_nor_a_key(w):
    from apps.tokens.models import PersonalToken

    _converse(w["hot"])
    raw, _ = PersonalToken.create_for_user(user=w["user"], label="cli")
    body = Client(HTTP_AUTHORIZATION=f"Bearer {raw}").get(f"/api/canopy-sessions/{w['hot'].pk}").json()
    assert body["sealed_key"] is None and not any(m["sealed"] for m in body["messages"])
    # An ordinary conversation has neither.
    _converse(w["cold"])
    cold = w["c"].get(f"/api/canopy-sessions/{w['cold'].pk}").json()
    assert cold["sealed_key"] is None and not any(m["sealed"] for m in cold["messages"])


def test_someone_in_it_without_a_wrap_is_told_so(w):
    from apps.canopy_sessions.models import SessionParticipant

    peer, _priv, c = _peer(w)
    SessionParticipant.objects.create(session=w["hot"], user=peer, role=SessionParticipant.VIEWER)
    key = c.get(f"/api/canopy-sessions/{w['hot'].pk}").json()["sealed_key"]
    assert key["wrap"] is None and key["current"] is False and key["keyset"]["user_id"] == str(peer.pk)


# --- multiplayer --------------------------------------------------------------------


def test_adding_someone_needs_a_wrap_for_their_current_vault_key(w):
    peer, peer_priv, peer_c = _peer(w)
    url = f"/api/canopy-sessions/{w['hot'].pk}/participants"
    r = w["c"].get(f"{url}/recipient?email=peer@dimagi.com")
    assert r.status_code == 200 and r.json()["jkt"] == ecdh_es.jkt(r.json()["public_jwk"])
    no_wrap = w["c"].post(url, {"email": "peer@dimagi.com", "role": "editor"}, content_type="application/json")
    assert no_wrap.status_code == 409 and no_wrap.json()["error"] == "wrap_required"
    wrap = rewrap_for(w["hot"], w["user"], w["key"], peer)
    stale = w["c"].post(url, {"email": "peer@dimagi.com", "wrap": {**wrap, "recipient_jkt": "x" * 43}},
                        content_type="application/json")
    assert stale.status_code == 422 and "current vault key" in stale.json()["detail"]
    junk = w["c"].post(url, {"email": "peer@dimagi.com", "wrap": {**wrap, "ct": "AAAA"}},
                       content_type="application/json")
    assert junk.status_code == 422
    assert not SessionKeyWrap.objects.filter(session=w["hot"], user=peer).exists()
    ok = w["c"].post(url, {"email": "peer@dimagi.com", "role": "editor", "wrap": wrap},
                     content_type="application/json")
    assert ok.status_code == 200, ok.content
    # A role change needs no new wrap.
    assert w["c"].post(url, {"email": "peer@dimagi.com", "role": "viewer"},
                       content_type="application/json").status_code == 200
    _converse(w["hot"])
    detail = peer_c.get(f"/api/canopy-sessions/{w['hot'].pk}").json()
    assert REPLY in browser_said(detail["messages"], detail["sealed_key"], peer_priv, peer.pk)
    # Removing them takes their copy of the key with them.
    assert w["c"].delete(f"{url}/{peer.pk}").status_code == 200
    assert not SessionKeyWrap.objects.filter(session=w["hot"], user=peer).exists()


def test_someone_with_no_vault_key_cannot_be_added(w):
    _peer(w, "nokey", key=False)
    r = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}/participants/recipient?email=nokey@dimagi.com")
    assert r.status_code == 409 and r.json()["error"] == "no_vault_key"


def test_an_ordinary_conversation_adds_people_as_before(w):
    _peer(w)
    r = w["c"].post(f"/api/canopy-sessions/{w['cold'].pk}/participants", {"email": "peer@dimagi.com"},
                    content_type="application/json")
    assert r.status_code == 200
    assert not SessionKeyWrap.objects.exists() or not SessionKeyWrap.objects.filter(session=w["cold"]).exists()


# --- fork ---------------------------------------------------------------------------


def test_a_fork_carries_the_same_key_and_wraps_and_its_rows_still_open(w):
    from apps.canopy_sessions import placement
    from apps.canopy_sessions.services import TRANSCRIPT_SOURCED

    runner = Runner.objects.create(name="mbp", workspace=w["ws"], kind=Runner.EMDASH, location=Runner.LOCAL,
                                   status=Runner.ONLINE, owner=w["user"], host="jj@mbp",
                                   capabilities={"sessions": True}, engine=Runner.ENGINE_CLAUDE_CLI)
    w["hot"].metadata = {**w["hot"].metadata, TRANSCRIPT_SOURCED: True}
    w["hot"].save(update_fields=["metadata"])
    RunnerBinding.objects.create(session=w["hot"], runner=runner, session_key="src", thread_key=str(w["hot"].id))
    _converse(w["hot"])
    fork = placement.fork_session(session=w["hot"], turn_index=128, user=w["user"])
    src, copy = SessionKey.objects.get(session=w["hot"]), SessionKey.objects.get(session=fork)
    assert (copy.conversation, copy.public_jwk) == (src.conversation, src.public_jwk)
    assert set(SessionKeyWrap.objects.filter(session=fork).values_list("user_id", flat=True)) == {w["user"].pk}
    row = Message.objects.get(session=fork, role=Message.ASSISTANT)
    assert open_sealed(row.sealed, fork, w["key"], w["user"])["plaintext"] == REPLY


# --- rows from before keys were per conversation -------------------------------------


def _legacy_row(session, index, words):
    return Message.objects.create(session=session, turn_index=index, role=Message.USER, plaintext="",
                                  content={"redacted": "sensitive"},
                                  sealed=encryption.encrypt_secret(json.dumps(words)))


def test_a_legacy_row_is_not_served_until_it_is_resealed(w):
    SessionKeyWrap.objects.filter(session=w["hot"]).delete()
    SessionKey.objects.filter(session=w["hot"]).delete()
    _legacy_row(w["hot"], 0, {"plaintext": ASKED, "content": {"text": ASKED}})
    body = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()
    assert body["sealed_key"] is None and body["messages"][0]["sealed"] is None
    assert ASKED not in json.dumps(body)

    call_command("reseal_sensitive_conversations")
    body = w["c"].get(f"/api/canopy-sessions/{w['hot'].pk}").json()
    assert not body["sealed_key"]["needs_migration"]
    assert browser_said(body["messages"], body["sealed_key"], w["key"], w["user"].pk) == ASKED


def test_a_legacy_row_whose_starter_has_no_vault_key_waits(w, capsys):
    s = Session.objects.create(workspace=w["ws"], agent=w["agent"], metadata=dict(SENSITIVE))
    _legacy_row(s, 0, {"plaintext": ASKED, "content": {}})
    call_command("reseal_sensitive_conversations")
    assert f"still needs migration (its starter has no vault key): {s.pk}" in capsys.readouterr().out
    assert not conversation_keys.is_envelope(Message.objects.get(session=s).sealed)


def test_an_existing_key_is_reused_when_resealing(w):
    _legacy_row(w["hot"], 5, {"plaintext": REPLY, "content": {}})
    before = SessionKey.objects.get(session=w["hot"]).public_jwk
    done = conversation_keys.reseal_legacy()
    assert done["rows"] == 1 and SessionKey.objects.get(session=w["hot"]).public_jwk == before
    assert open_sealed(Message.objects.get(session=w["hot"], turn_index=5).sealed, w["hot"], w["key"],
                       w["user"])["plaintext"] == REPLY
