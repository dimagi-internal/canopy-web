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

from apps.harness.models import Turn, TurnEvent
from apps.harness.turn_close import close_turn
from apps.host_grants import client_identity, reply_relay
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import AppCredential, HostReplyDelivery
from tests.fixtures.contact_websocket import _world
from tests.fixtures.reply_push import HOOK, _arrive, _end, _say

pytestmark = pytest.mark.django_db



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


def _turn(w, text="when is my visit due?"):
    r = w["c"].post(f"/api/contact/sessions/{w['sid']}/send", {"text": text, "client_id": str(uuid.uuid4())},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    # Running, as a runner would have it: what the agent may do for a contact is not
    # what these are about.
    Turn.objects.filter(pk=r.json()["turn_id"]).update(status=Turn.RUNNING, result_note="")
    return Turn.objects.get(pk=r.json()["turn_id"])


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


# --- a site declares its reply webhook in its signed site.json -------------------------

ORIGIN = "https://labs.example.org"
DECLARED = f"{ORIGIN}/messaging/canopy/replies/"


@pytest.fixture()
def declares(w, monkeypatch):
    """The site registered on `ORIGIN` with no webhook of a person's, publishing a
    `site.json` signed with whatever key the test says (its own, by default)."""
    from canopy_sdk import site as site_wire

    from apps.host_grants import outbound, site_needs

    AppCredential.objects.filter(pk=w["app"].pk).update(
        reply_webhook_url=None, allowed_frame_origins=[ORIGIN])
    published = {}

    def publish(reply_url=DECLARED, reply_mode="final", key=None):
        cache.clear()
        facts = {"origins": [ORIGIN], "reply_url": reply_url, "reply_mode": reply_mode}
        published["token"] = site_wire.sign_site_document(
            key or w["priv"], site_wire.site_document("connect-labs", site=facts))

    monkeypatch.setattr(site_needs, "document_url", lambda jwks_url: f"{ORIGIN}/labs/canopy/site.json")
    monkeypatch.setattr(site_needs, "_get", lambda url: published["token"])
    # The address check resolves the host; this one is the test's.
    monkeypatch.setattr(outbound, "refuse_private", lambda host: None)
    publish()
    return publish


def _app(w):
    return AppCredential.objects.get(pk=w["app"].pk)


def test_a_declared_webhook_on_the_sites_origin_is_adopted(w, declares):
    declares(reply_mode="stream")
    push = reply_relay.push_for(_app(w))
    assert (push.url, push.mode, push.source) == (DECLARED, "stream", "site")
    turn = _turn(w)
    _say(turn, "Your next visit is due Friday.")
    assert reply_relay.on_rows(turn, list(TurnEvent.objects.filter(turn=turn, kind="assistant"))) == 1
    row = reply_relay.on_finished(_end(turn))
    sent, post = _posted()
    reply_relay.attempt(row, post=post)
    [(url, token)] = sent
    assert url == DECLARED
    assert jwt.decode(token, options={"verify_signature": False})["aud"] == DECLARED


def test_a_declared_webhook_off_the_sites_origin_is_ignored(w, declares):
    declares(reply_url="https://elsewhere.example.org/replies/")
    assert reply_relay.push_for(_app(w)) is None
    from apps.host_grants import site_needs

    declared = site_needs.declared_reply(_app(w))
    assert not declared.adopted and "not one of this site's registered origins" in declared.reason
    turn = _turn(w)
    _say(turn, "hi")
    assert reply_relay.on_finished(_end(turn)) is None


def test_a_declared_webhook_in_a_document_the_site_did_not_sign_is_ignored(w, declares):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    declares(key=Ed25519PrivateKey.generate())
    assert reply_relay.push_for(_app(w)) is None
    turn = _turn(w)
    _say(turn, "hi")
    assert reply_relay.on_finished(_end(turn)) is None


def test_a_declared_webhook_that_is_not_https_is_refused(w, declares):
    from apps.host_grants import site_needs

    AppCredential.objects.filter(pk=w["app"].pk).update(allowed_frame_origins=["http://labs.example.org"])
    declares(reply_url="http://labs.example.org/messaging/canopy/replies/")
    declared = site_needs.declared_reply(_app(w))
    assert declared.url and not declared.adopted and "https" in declared.reason
    assert reply_relay.push_for(_app(w)) is None


def test_a_declared_webhook_that_fails_the_outbound_checks_is_refused(w, declares, monkeypatch):
    from apps.host_grants import outbound, site_needs

    def private(host):
        raise outbound.OutboundError(f"{host!r} resolves to 10.0.0.5, inside private address space")

    monkeypatch.setattr(outbound, "refuse_private", private)
    declares()
    declared = site_needs.declared_reply(_app(w))
    assert not declared.adopted and "private address space" in declared.reason


def test_a_persons_webhook_wins_over_the_declared_one(w, declares):
    c = Client()
    c.force_login(w["owner"])
    grant_step_up(c)
    r = c.patch(f"/api/workspaces/w1/host-sites/{w['app'].pk}", {"reply_webhook_url": HOOK},
                content_type="application/json")
    assert r.status_code == 200, r.content
    assert (r.json()["reply_webhook_url"], r.json()["reply_webhook_set_by"]) == (HOOK, "boss@dimagi.com")
    push = reply_relay.push_for(_app(w))
    assert (push.url, push.source, push.set_by) == (HOOK, "person", "boss@dimagi.com")
    turn = _turn(w)
    _say(turn, "hi")
    row = reply_relay.on_finished(_end(turn))
    sent, post = _posted()
    reply_relay.attempt(row, post=post)
    assert [u for u, _ in sent] == [HOOK]
    # Cleared, the site's own declaration takes over again, with no one recorded.
    r = c.patch(f"/api/workspaces/w1/host-sites/{w['app'].pk}", {"reply_webhook_url": ""},
                content_type="application/json")
    assert r.json()["reply_webhook_set_by"] is None
    assert reply_relay.push_for(_app(w)).source == "site"


def test_the_needs_route_says_where_replies_go_and_on_whose_word(w, declares):
    c = Client()
    c.force_login(w["owner"])
    r = c.get(f"/api/workspaces/w1/host-sites/{w['app'].pk}/needs")
    assert r.status_code == 200, r.content
    got = r.json()["reply_push"]
    assert (got["url"], got["mode"], got["source"], got["declared_url"]) == (DECLARED, "final", "site", DECLARED)
    declares(reply_url="https://elsewhere.example.org/replies/")
    got = c.get(f"/api/workspaces/w1/host-sites/{w['app'].pk}/needs").json()["reply_push"]
    assert got["source"] == "" and got["url"] == "" and got["declared_reason"]
