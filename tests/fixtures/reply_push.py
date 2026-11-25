"""A relay site's contact conversation, for reply-push and reply-latency tests: the
site's webhook, a person's contact token, the agent's words on a turn, and its close."""
from __future__ import annotations

import datetime as dt
import uuid

import jwt
from django.test import Client

from apps.embedding import assertions
from apps.harness.ledger import append_events
from apps.harness.models import Turn
from apps.harness.turn_close import close_turn

HOOK = "https://labs.example.org/canopy/replies/"


def _arrive(priv, sub="personalid:jdoe"):
    now = int(dt.datetime.now(dt.UTC).timestamp())
    assertion = jwt.encode({"iss": "connect-labs", "sub": sub, "aud": assertions.audience(), "iat": now,
                            "exp": now + 60, "jti": str(uuid.uuid4()), "canopy_channel": "personalid",
                            "canopy_agent": "echo"}, priv, algorithm="EdDSA")
    return Client().post("/api/auth/contact-token", {"assertion": assertion, "agent_slug": "echo"},
                         content_type="application/json").json()["token"]


def _say(turn, *texts):
    append_events(turn, [{"kind": "assistant", "payload": {"text": t}} for t in texts])


def _end(turn, status=Turn.DONE):
    assert close_turn(turn, status, from_states=list(Turn.NON_TERMINAL), chat_hooks=False)
    turn.refresh_from_db()
    return turn
