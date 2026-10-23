"""A sensitive conversation and an ordinary one in the same workspace (`world`), and
the turn, transcript and socket helpers the sensitive-data suites share
(`tests/test_sensitive_storage.py`, `tests/test_sensitive_read_doors.py`).
"""
from __future__ import annotations

import uuid

from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions import transcript_ingest as chat_transcript_ingest
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

SENSITIVE = {"runner_requirements": ["sensitive_data"]}
ROW = "patient Amina, HIV+, visit 2026-10-01"
ASKED = "how many visits did Amina have?"
REPLY = "Amina has had 12 visits"


def vault_key(user):
    """Give `user` a vault keyset, as their browser would; returns its private key, which
    only the test (standing in for that browser) holds."""
    from cryptography.hazmat.primitives.asymmetric import ec

    from apps.common import ecdh_es
    from apps.passkeys.models import UserVaultKeyset

    priv = ec.generate_private_key(ec.SECP256R1())
    UserVaultKeyset.objects.update_or_create(user=user, defaults={
        "public_jwk": ecdh_es.jwk_of(priv.public_key()), "seals": []})
    return priv


def unwrap(wrap: dict, priv, info: str) -> bytes:
    """The browser's `unwrapEcdhEs`, for a test standing in for it. Never app code."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    from apps.common.ecdh_es import unb64u

    epk = unb64u(wrap["epk"])
    z = priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), epk))
    me = priv.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    kek = HKDF(algorithm=hashes.SHA256(), length=32, salt=epk + me, info=info.encode()).derive(z)
    return AESGCM(kek).decrypt(unb64u(wrap["n"]), unb64u(wrap["ct"]), info.encode())


def open_sealed(sealed: str, session, person_priv, user) -> dict:
    """What a person's browser reads from one stored `cc1:` row: unwrap their copy of
    the conversation key, then open the envelope. Test-only."""
    import json

    from cryptography.hazmat.primitives.asymmetric import ec

    from apps.canopy_sessions import conversation_keys
    from apps.canopy_sessions.models import SessionKey, SessionKeyWrap
    from apps.common.ecdh_es import unb64u

    key = SessionKey.objects.get(session=session)
    wrap = SessionKeyWrap.objects.get(session=session, user=user)
    scalar = unwrap(wrap.wrap, person_priv, conversation_keys.wrap_info(key.conversation, user.pk))
    conv = ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())
    body = json.loads(unb64u(sealed[len(conversation_keys.PREFIX):]))
    words = unwrap(body, conv, conversation_keys.message_info(body["c"], body["i"], body.get("r")))
    return json.loads(words)


def browser_said(messages, sealed_key: dict, person_priv, user_id) -> str:
    """What the reader's browser shows of `messages` (REST or socket rows): it unwraps
    its copy of the conversation key from `sealed_key` — as the API returned it — and
    opens each row's `sealed`. Test-only."""
    import json

    from cryptography.hazmat.primitives.asymmetric import ec

    from apps.canopy_sessions import conversation_keys
    from apps.common.ecdh_es import unb64u

    scalar = unwrap(sealed_key["wrap"], person_priv,
                    conversation_keys.wrap_info(sealed_key["conversation"], user_id))
    conv = ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())
    out = []
    for m in messages:
        if m.get("sealed"):
            body = json.loads(unb64u(m["sealed"][len(conversation_keys.PREFIX):]))
            words = json.loads(unwrap(body, conv, conversation_keys.message_info(body["c"], body["i"], body.get("r"))))
            out.append(words["plaintext"])
        else:
            out.append(m["plaintext"])
    return " ".join(out)


def rewrap_for(session, from_user, from_priv, to_user, to_priv_public_jwk=None) -> dict:
    """What an adder's browser sends to add `to_user`: its own copy of the key, opened
    and wrapped again to their vault key. Test-only."""
    from apps.canopy_sessions import conversation_keys
    from apps.canopy_sessions.models import SessionKey, SessionKeyWrap
    from apps.common import ecdh_es
    from apps.passkeys.models import UserVaultKeyset

    key = SessionKey.objects.get(session=session)
    mine = SessionKeyWrap.objects.get(session=session, user=from_user)
    scalar = unwrap(mine.wrap, from_priv, conversation_keys.wrap_info(key.conversation, from_user.pk))
    theirs = to_priv_public_jwk or UserVaultKeyset.objects.get(user=to_user).public_jwk
    return {**ecdh_es.wrap(scalar, theirs, conversation_keys.wrap_info(key.conversation, to_user.pk)),
            "recipient_jkt": ecdh_es.jkt(theirs)}


def world() -> dict:
    """An owner, their workspace, agent and runner, a sensitive session (`hot`) and an
    ordinary one (`cold`), and a browser signed in as the owner (`c`)."""
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    # A sensitive conversation is sealed to its people's vault keys: the owner has one.
    key = vault_key(user)
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="labsdata", name="labsdata", workspace=ws, owner=user)
    runner = Runner.objects.create(name="box", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, owner=user)
    hot = Session.objects.create(workspace=ws, agent=agent, created_by=user, title="hot",
                                 metadata=dict(SENSITIVE))
    cold = Session.objects.create(workspace=ws, agent=agent, created_by=user, title="cold")
    c = Client()
    c.force_login(user)
    return {"user": user, "ws": ws, "agent": agent, "runner": runner, "hot": hot, "cold": cold,
            "c": c, "key": key}


def _turn(session, **kw):
    return Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API,
                               idempotency_key=uuid.uuid4().hex, **{"prompt": "q", **kw})


def _converse(session):
    """A turn's rows as the runner ships them (`persist_transcript_rows`)."""
    chat_transcript_ingest.persist_transcript_rows(session, [
        {"index": 0, "role": "user", "text": ASKED},
        {"index": 64, "role": "tool_use", "text": "",
         "content": {"id": "tu1", "name": "site_call", "input": {"tool": "data_query",
                                                                   "arguments": {"q": "Amina"}}}},
        {"index": 65, "role": "tool_result", "text": ROW,
         "content": {"tool_use_id": "tu1", "is_error": False, "text": ROW}},
        {"index": 128, "role": "assistant", "text": REPLY},
    ])


def _socket(session, user, *, auth_method="session"):
    from asgiref.sync import async_to_sync
    from channels.testing import WebsocketCommunicator

    from apps.canopy_sessions.consumers import SessionConsumer

    async def go():
        comm = WebsocketCommunicator(SessionConsumer.as_asgi(), f"/ws/canopy-sessions/{session.id}/")
        comm.scope.update(user=user, auth_method=auth_method, session={},
                          url_route={"kwargs": {"session_id": str(session.id)}})
        connected, _ = await comm.connect()
        await comm.disconnect()
        return connected
    return async_to_sync(go)()


def _unowned_thread(w):
    """A runner-discovered conversation nobody started: the agent's admins stand in
    as its owner (`access.role_for`) — they read and share it, but are not IN it."""
    s = Session.objects.create(workspace=w["ws"], agent=w["agent"], origin=Session.ORIGIN_RUNNER,
                               metadata=dict(SENSITIVE))
    RunnerBinding.objects.create(session=s, runner=w["runner"], session_key="unowned")
    return s


def minted(user) -> Client:
    """A browser holding a session cookie minted from a token (`/api/debug/mint-session/`)."""
    from apps.common.views_debug import DEBUG_SESSION_MARKER

    c = Client()
    c.force_login(user)
    session = c.session
    session[DEBUG_SESSION_MARKER] = {"via": "pat"}
    session.save()
    return c


def mcp_as(user):
    """The MCP server's auth context for `user`, as a signed-in MCP client's token sets it."""
    import contextlib

    from fastmcp.server.auth import AccessToken
    from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

    @contextlib.contextmanager
    def ctx():
        access = AccessToken(token="test-token", client_id=str(user.pk), scopes=["canopy:user"],
                             claims={"sub": str(user.pk), "user_id": user.pk, "email": user.email})
        tok = auth_context_var.set(AuthenticatedUser(access))
        try:
            yield
        finally:
            auth_context_var.reset(tok)
    return ctx()
