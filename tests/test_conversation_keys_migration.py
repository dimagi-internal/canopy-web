"""Migration 0049: every row a sensitive conversation sealed under the field key moves to
the conversation's own key, wrapped to its starter; one whose starter has no vault key
stays where it was, to be moved later (`reseal_sensitive_conversations`)."""
from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.canopy_sessions import conversation_keys
from apps.common import ecdh_es
from apps.common.encryption import encrypt_secret
from tests.fixtures.sensitive import unwrap

# needs_migrations: it walks the real migration graph (ci.yml, `Backend pytest (migrations)`).
pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("canopy_sessions", "0048_message_sealed_encrypted"), ("passkeys", "0003_vault_keyset_replaced_at")]
AFTER = [("canopy_sessions", "0049_conversation_keys")]
WORDS = {"plaintext": "Amina had 12 visits", "content": {"text": "Amina had 12 visits"}}


def test_legacy_rows_are_resealed_to_their_conversation_and_wrapped_to_the_starter():
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old = executor.loader.project_state(BEFORE).apps
    User = old.get_model("auth", "User")
    Workspace = old.get_model("workspaces", "Workspace")
    Session = old.get_model("canopy_sessions", "Session")
    Message = old.get_model("canopy_sessions", "Message")
    Keyset = old.get_model("passkeys", "UserVaultKeyset")
    starter = User.objects.create(username="jj", email="jj@dimagi.com")
    nokey = User.objects.create(username="nk", email="nk@dimagi.com")
    priv = ec.generate_private_key(ec.SECP256R1())
    Keyset.objects.create(user=starter, public_jwk=ecdh_es.jwk_of(priv.public_key()), seals=[])
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=starter)
    meta = {"runner_requirements": ["sensitive_data"]}
    hot = Session.objects.create(workspace=ws, created_by=starter, metadata=meta)
    stuck = Session.objects.create(workspace=ws, created_by=nokey, metadata=meta)
    for s in (hot, stuck):
        Message.objects.create(session=s, turn_index=3, role="user", plaintext="",
                               content={"redacted": "sensitive"}, sealed=encrypt_secret(json.dumps(WORDS)))

    executor.loader.build_graph()
    executor.migrate(AFTER)
    new = executor.loader.project_state(AFTER).apps
    Message = new.get_model("canopy_sessions", "Message")
    key = new.get_model("canopy_sessions", "SessionKey").objects.get(session_id=hot.pk)
    wrap = new.get_model("canopy_sessions", "SessionKeyWrap").objects.get(session_id=hot.pk)
    assert wrap.user_id == starter.pk and wrap.recipient_jkt == ecdh_es.jkt(ecdh_es.jwk_of(priv.public_key()))
    sealed = Message.objects.get(session_id=hot.pk).sealed
    assert conversation_keys.is_envelope(sealed) and "Amina" not in sealed
    scalar = unwrap(wrap.wrap, priv, conversation_keys.wrap_info(key.conversation, starter.pk))
    conv = ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())
    body = json.loads(ecdh_es.unb64u(sealed[len(conversation_keys.PREFIX):]))
    assert body["i"] == 3 and json.loads(unwrap(body, conv, conversation_keys.message_info(body["c"], 3))) == WORDS
    # The starter with no vault key: left under the field key, with no key made.
    assert not conversation_keys.is_envelope(Message.objects.get(session_id=stuck.pk).sealed)
    assert not new.get_model("canopy_sessions", "SessionKey").objects.filter(session_id=stuck.pk).exists()
