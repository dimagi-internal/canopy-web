"""Reply push (S5): the agent's replies to a relay site's contacts are POSTed to the
site's `reply_webhook_url`, signed with canopy's client key.

What these pin: only a live site that asked for it, only its own contacts'
conversations; `final` is one post with all the turn's prose, `stream` a post per
piece and an `ended` one; once per (turn, seq); retried with backoff, then given up;
no words kept once delivered; the host verifies with the SDK's `verify_reply`.
"""
from __future__ import annotations

import datetime as dt
import uuid

import jwt
import pytest
from canopy_sdk.host import HostConfig, ReplyRefused, verify_reply
from canopy_sdk.host.client_keys import ClientKeyResolver
from canopy_sdk.stores import MemoryCache, MemoryJtiStore
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.embedding import assertions
from apps.harness.ledger import append_events
from apps.harness.models import Turn, TurnEvent
from apps.harness.turn_close import close_turn
from apps.host_grants import client_identity, reply_relay
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import AppCredential, HostReplyDelivery
from tests.fixtures.contact_websocket import _world

pytestmark = pytest.mark.django_db

HOOK = "https://labs.example.org/canopy/replies/"


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def w(monkeypatch):
    owner, ws, app, priv = _world()
    AppCredential.objects.filter(pk=app.pk).update(reply_webhook_url=HOOK)
    app.refresh_from_db()
    # No thread: the first attempt is the test's to make.
    started = []
    monkeypatch.setattr(reply_relay, "_deliver_soon", started.append)
    c = Client(HTTP_AUTHORIZATION=f"Bearer {_arrive(priv)}")
    sid = c.post("/api/contact/sessions", {"agent_slug": "echo"}, content_type="application/json").json()["id"]
    return {"owner": owner, "app": app, "priv": priv, "c": c, "sid": sid, "started": started}


def _arrive(priv, sub="personalid:jdoe"):
    now = int(dt.datetime.now(dt.UTC).timestamp())
    assertion = jwt.encode({"iss": "connect-labs", "sub": sub, "aud": assertions.audience(), "iat": now,
                            "exp": now + 60, "jti": str(uuid.uuid4()), "canopy_channel": "personalid",
                            "canopy_agent": "echo"}, priv, algorithm="EdDSA")
    return Client().post("/api/auth/contact-token", {"assertion": assertion, "agent_slug": "echo"},
                         content_type="application/json").json()["token"]


def _turn(w, text="when is my visit due?"):
    r = w["c"].post(f"/api/contact/sessions/{w['sid']}/send", {"text": text, "client_id": str(uuid.uuid4())},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    # Running, as a runner would have it: what the agent may do for a contact is not
    # what these are about.
    Turn.objects.filter(pk=r.json()["turn_id"]).update(status=Turn.RUNNING, result_note="")
    return Turn.objects.get(pk=r.json()["turn_id"])


def _say(turn, *texts):
    append_events(turn, [{"kind": "assistant", "payload": {"text": t}} for t in texts])


def _end(turn, status=Turn.DONE):
    assert close_turn(turn, status, from_states=list(Turn.NON_TERMINAL), chat_hooks=False)
    turn.refresh_from_db()
    return turn


def _host():
    """The host's side: its config and canopy's keys, read from canopy's own documents."""
    docs = {client_identity.client_id(): client_identity.client_metadata(),
            client_identity.jwks_uri(): client_identity.published_jwks()}
    keys = ClientKeyResolver(fetch_json=docs.__getitem__, cache=MemoryCache())
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    config = HostConfig(signing_key=Ed25519PrivateKey.generate(), app_name="connect-labs",
                        canopy_client_id=client_identity.client_id(), reply_url=HOOK)
    return config, keys


def _posted():
    sent = []

    def post(url, token):
        sent.append((url, token))
        return 204

    return sent, post


def test_final_mode_posts_the_turns_prose_once_when_it_ends(w):
    turn = _turn(w)
    _say(turn, "Your next visit is due Friday.", "Bring the card.")
    row = reply_relay.on_finished(_end(turn))
    assert row is not None and row.ended and row.error == ""
    assert row.text == "Your next visit is due Friday.\n\nBring the card."
    assert reply_relay.on_finished(turn) is None, "once per turn"

    sent, post = _posted()
    reply_relay.attempt(row, post=post)
    [(url, token)] = sent
    assert url == HOOK
    config, keys = _host()
    reply = verify_reply(token, config=config, client_keys=keys, replay_store=MemoryJtiStore())
    assert (reply.site, reply.sub, reply.session, reply.turn) == (
        "connect-labs", "personalid:jdoe", w["sid"], str(turn.pk))
    assert reply.text == "Your next visit is due Friday.\n\nBring the card." and reply.ended
    row.refresh_from_db()
    assert row.delivered_at is not None and row.text == "", "no words kept once delivered"


def test_stream_mode_posts_each_piece_then_an_end(w):
    AppCredential.objects.filter(pk=w["app"].pk).update(reply_mode=AppCredential.REPLY_STREAM)
    turn = _turn(w)
    _say(turn, "one", "two")
    rows = list(TurnEvent.objects.filter(turn=turn, kind="assistant").order_by("seq"))
    assert reply_relay.on_rows(turn, rows) == 2
    assert reply_relay.on_rows(turn, rows) == 0, "once per (turn, seq)"
    end = reply_relay.on_finished(_end(turn))
    got = list(HostReplyDelivery.objects.filter(turn=turn).order_by("seq").values_list("text", "ended"))
    assert got == [("one", False), ("two", False), ("", True)]
    assert end.seq > max(r.seq for r in rows)


def test_a_turn_that_did_not_finish_well_says_so(w):
    turn = _turn(w)
    row = reply_relay.on_finished(_end(turn, Turn.FAILED))
    assert row.ended and row.error == "failed" and row.text == ""


def test_the_close_signal_queues_it_after_commit(w, django_capture_on_commit_callbacks):
    turn = _turn(w)
    _say(turn, "hi")
    with django_capture_on_commit_callbacks(execute=True):
        close_turn(turn, Turn.DONE, from_states=list(Turn.NON_TERMINAL))
    [row] = HostReplyDelivery.objects.filter(turn=turn)
    assert row.text == "hi" and w["started"] == [row.pk]


@pytest.mark.parametrize("change", [
    {"reply_webhook_url": None},
    {"revoked_at": timezone.now()},
])
def test_only_a_live_site_that_asked_gets_pushed(w, change):
    turn = _turn(w)
    _say(turn, "hi")
    AppCredential.objects.filter(pk=w["app"].pk).update(**change)
    assert reply_relay.on_finished(_end(turn)) is None


def test_a_members_conversation_is_never_pushed(w):
    """Only a contact's own conversation goes to the site that vouched for them."""
    from apps.canopy_sessions.models import Session

    turn = _turn(w)
    Session.objects.filter(pk=w["sid"]).update(contact=None)
    _say(turn, "hi")
    assert reply_relay.on_finished(_end(turn)) is None


def test_a_widget_conversation_on_the_same_site_is_never_pushed(w):
    """The webhook is the site's, and only its relay conversations go there: a contact in the
    site's panel (no relay channel) reads the reply in the panel."""
    from apps.canopy_sessions.models import Session
    from apps.canopy_sessions.services import RELAY_CHANNEL_KEY

    turn = _turn(w)
    session = Session.objects.get(pk=w["sid"])
    Session.objects.filter(pk=session.pk).update(
        metadata={k: v for k, v in session.metadata.items() if k != RELAY_CHANNEL_KEY})
    _say(turn, "hi")
    assert reply_relay.on_finished(_end(turn)) is None


def test_a_failed_post_is_retried_with_backoff_then_given_up(w):
    turn = _turn(w)
    _say(turn, "hi")
    row = reply_relay.on_finished(_end(turn))
    now = timezone.now()
    reply_relay.attempt(row, now=now, post=lambda url, token: 503)
    row.refresh_from_db()
    assert (row.attempts, row.last_status) == (1, "HTTP 503")
    assert row.next_attempt_at == now + reply_relay.BACKOFF[0]
    assert row.text == "hi", "kept for the retry"
    assert reply_relay.deliver_due(now) == 0, "not due yet"
    for _ in range(reply_relay.MAX_ATTEMPTS - 1):
        row.refresh_from_db()
        reply_relay.attempt(row, now=row.next_attempt_at, post=lambda url, token: 500)
    row.refresh_from_db()
    assert row.abandoned_at is not None and row.attempts == reply_relay.MAX_ATTEMPTS and row.text == ""


def test_the_clock_delivers_what_is_due(w, monkeypatch):
    turn = _turn(w)
    _say(turn, "hi")
    row = reply_relay.on_finished(_end(turn))
    sent, post = _posted()
    monkeypatch.setattr(reply_relay, "_post", post)
    assert reply_relay.deliver_due(timezone.now() + dt.timedelta(seconds=1)) == 1
    row.refresh_from_db()
    assert row.delivered_at is not None and len(sent) == 1


def test_a_reply_for_another_site_or_url_is_refused(w):
    turn = _turn(w)
    _say(turn, "hi")
    row = reply_relay.on_finished(_end(turn))
    token = reply_relay.sign(row)
    config, keys = _host()
    with pytest.raises(ReplyRefused) as exc:
        verify_reply(token, config=config, audience="https://elsewhere.example.org/r",
                     client_keys=keys, replay_store=MemoryJtiStore())
    assert exc.value.code == "bad_audience"
    store = MemoryJtiStore()
    verify_reply(token, config=config, client_keys=keys, replay_store=store)
    with pytest.raises(ReplyRefused) as exc:
        verify_reply(token, config=config, client_keys=keys, replay_store=store)
    assert exc.value.code == "replayed"


def test_deleting_the_conversations_words_empties_undelivered_replies(w):
    turn = _turn(w)
    _say(turn, "private words")
    row = reply_relay.on_finished(_end(turn))
    reply_relay.forget_session([turn.pk])
    row.refresh_from_db()
    assert row.text == ""


def test_setting_the_webhook_takes_a_passkey_and_an_outbound_url(w):
    c = Client()
    c.force_login(w["owner"])
    url = f"/api/workspaces/w1/host-sites/{w['app'].pk}"
    r = c.patch(url, {"reply_webhook_url": "https://labs.example.org/other/"}, content_type="application/json")
    assert r.status_code == 403, r.content
    grant_step_up(c)
    r = c.patch(url, {"reply_webhook_url": "http://labs.example.org/plain/"}, content_type="application/json")
    assert r.status_code == 422 and "bad_host_url" in r.json()["detail"]
    r = c.patch(url, {"reply_mode": "stream"}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert (r.json()["reply_webhook_url"], r.json()["reply_mode"]) == (HOOK, "stream")
    r = c.patch(url, {"reply_webhook_url": ""}, content_type="application/json")
    assert r.status_code == 200 and r.json()["reply_webhook_url"] is None


def test_the_deletion_signal_reaches_the_replies(w):
    from apps.canopy_sessions.models import Session
    from apps.harness.signals import session_content_deleted

    turn = _turn(w)
    _say(turn, "private words")
    row = reply_relay.on_finished(_end(turn))
    session_content_deleted.send(sender=Session, session=turn.chat_session, turn_ids=[turn.pk], by=None)
    row.refresh_from_db()
    assert row.text == ""
