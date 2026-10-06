"""Subscribe/unsubscribe. The browser re-sends the same endpoint every time it
calls subscribe(), so subscribing twice must upsert, never duplicate."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.push.models import PushSubscription

pytestmark = pytest.mark.django_db

SUB = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/AAA",
    "p256dh": "BKxQ_key",
    "auth": "authsecret",
    "user_agent": "Mozilla/5.0 (Linux; Android 14; Pixel 7)",
}


@pytest.fixture()
def user():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture(autouse=True)
def _vapid_configured(settings):
    """Every test in this module pins BOTH VAPID keys explicitly instead of
    inheriting whatever happens to be ambient — a dev .env locally, nothing in
    CI. Both, because the endpoints gate on both (a public-key-only deployment
    accepted subscriptions and silently never sent). Tests that exercise the
    "not configured" paths blank one or both keys in their own body."""
    settings.VAPID_PUBLIC_KEY = "BPublicKeyHere"
    settings.VAPID_PRIVATE_KEY = "PrivateKeyHere"


_PRIVATE_NAME = "push.internal.example"


@pytest.fixture(autouse=True)
def _dns(monkeypatch):
    """Subscribing resolves the endpoint's host (it must be public). No test
    here may depend on real DNS: a name resolves to a public address, except
    `_PRIVATE_NAME` (a public-looking name pointing inside the VPC); an IP
    literal resolves as itself, offline."""
    import ipaddress
    import socket

    real = socket.getaddrinfo

    def fake(host, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
            return real(host, *args, **kwargs)
        except ValueError:
            addr = "10.0.0.5" if host == _PRIVATE_NAME else "142.250.80.10"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def test_vapid_public_key_is_served(client, settings):
    settings.VAPID_PUBLIC_KEY = "BPublicKeyHere"
    resp = client.get("/api/push/vapid-public-key")
    assert resp.status_code == 200
    assert resp.json() == {"public_key": "BPublicKeyHere"}


def test_vapid_public_key_503s_when_push_is_not_configured(client, settings):
    """No keys = push is off. The endpoint must say so, not hand the browser an
    empty string it would try to subscribe with."""
    settings.VAPID_PUBLIC_KEY = ""
    assert client.get("/api/push/vapid-public-key").status_code == 503


def test_subscribe_503s_when_push_is_not_configured(client, settings):
    """Empty keys mean push is off. Storing a subscription we can never send to
    just accumulates dead rows — refuse instead."""
    settings.VAPID_PUBLIC_KEY = ""
    resp = client.post("/api/push/subscribe", SUB, content_type="application/json")
    assert resp.status_code == 503
    assert not PushSubscription.objects.filter(endpoint=SUB["endpoint"]).exists()


def test_half_configured_is_not_configured(client, settings):
    """A public key with no private key can subscribe browsers but never send —
    the worst state, because it LOOKS configured. Both endpoints must 503."""
    settings.VAPID_PRIVATE_KEY = ""
    assert client.get("/api/push/vapid-public-key").status_code == 503
    resp = client.post("/api/push/subscribe", SUB, content_type="application/json")
    assert resp.status_code == 503
    assert not PushSubscription.objects.filter(endpoint=SUB["endpoint"]).exists()


def test_subscribe_rejects_overlong_crypto_keys_with_422_not_500(client):
    # p256dh/auth map to fixed-width columns (200/100); an over-length value must be
    # a 422 at the schema boundary, not a Postgres-only 500 on the write.
    resp = client.post(
        "/api/push/subscribe",
        {**SUB, "p256dh": "B" * 300},
        content_type="application/json",
    )
    assert resp.status_code == 422, resp.content


def test_subscribe_stores_the_browser(client, user):
    resp = client.post("/api/push/subscribe", SUB, content_type="application/json")
    assert resp.status_code == 201
    sub = PushSubscription.objects.get(endpoint=SUB["endpoint"])
    assert sub.user_id == user.id
    assert sub.p256dh == "BKxQ_key"


def test_subscribing_twice_upserts_rather_than_duplicating(client, user):
    client.post("/api/push/subscribe", SUB, content_type="application/json")
    rotated = {**SUB, "p256dh": "rotated_key"}
    resp = client.post("/api/push/subscribe", rotated, content_type="application/json")
    assert resp.status_code == 201
    assert PushSubscription.objects.filter(endpoint=SUB["endpoint"]).count() == 1
    assert PushSubscription.objects.get(endpoint=SUB["endpoint"]).p256dh == "rotated_key"


def test_resubscribing_claims_the_endpoint_for_the_new_user(client):
    """A shared device: the endpoint is the browser's, not the person's. If
    someone else logs in and subscribes, the endpoint must move to them — or we
    would push one person's inbox to another's phone."""
    other = User.objects.create_user("other", "other@dimagi.com", "pw")
    PushSubscription.objects.create(user=other, endpoint=SUB["endpoint"], p256dh="k", auth="a")
    resp = client.post("/api/push/subscribe", SUB, content_type="application/json")
    assert resp.status_code == 201
    sub = PushSubscription.objects.get(endpoint=SUB["endpoint"])
    assert sub.user.username == "jj"


def test_unsubscribe_removes_it(client):
    client.post("/api/push/subscribe", SUB, content_type="application/json")
    resp = client.delete(
        "/api/push/subscribe", {"endpoint": SUB["endpoint"]}, content_type="application/json"
    )
    assert resp.status_code == 204
    assert not PushSubscription.objects.filter(endpoint=SUB["endpoint"]).exists()


def test_unsubscribe_works_even_when_push_is_not_configured(client, settings):
    """Cleanup must not depend on config: someone turning notifications off on a
    deployment whose keys were pulled should still succeed."""
    client.post("/api/push/subscribe", SUB, content_type="application/json")
    settings.VAPID_PUBLIC_KEY = ""
    resp = client.delete(
        "/api/push/subscribe", {"endpoint": SUB["endpoint"]}, content_type="application/json"
    )
    assert resp.status_code == 204
    assert not PushSubscription.objects.filter(endpoint=SUB["endpoint"]).exists()


def test_unsubscribing_someone_elses_endpoint_does_nothing(client):
    other = User.objects.create_user("other", "other@dimagi.com", "pw")
    PushSubscription.objects.create(user=other, endpoint="https://x/BBB", p256dh="k", auth="a")
    resp = client.delete(
        "/api/push/subscribe", {"endpoint": "https://x/BBB"}, content_type="application/json"
    )
    assert resp.status_code == 204  # idempotent, no existence leak
    assert PushSubscription.objects.filter(endpoint="https://x/BBB").exists()


def test_anonymous_cannot_subscribe():
    resp = Client().post("/api/push/subscribe", SUB, content_type="application/json")
    assert resp.status_code == 401


def test_sessions_roll_forward_on_use():
    """Django's default sets the 2-week expiry AT LOGIN and never extends it, so
    an installed PWA would log out every fortnight regardless of use. This is the
    setting that stops that; a future 'cleanup' removing it would be silent."""
    from django.conf import settings

    assert settings.SESSION_SAVE_EVERY_REQUEST is True


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/AAA",          # not https
    "https://127.0.0.1/push",                           # loopback
    "https://169.254.169.254/latest/meta-data/",        # the metadata service
    f"https://{_PRIVATE_NAME}/push",                    # a name that resolves inside
    "https://user:pw@fcm.googleapis.com/fcm/send/AAA",  # userinfo
    "file:///etc/passwd",
])
def test_an_endpoint_canopy_must_not_post_to_is_refused(client, endpoint):
    """canopy POSTs to every stored endpoint from inside the VPC, so storing one
    that points anywhere but a public push service is a blind SSRF."""
    resp = client.post("/api/push/subscribe", {**SUB, "endpoint": endpoint},
                       content_type="application/json")
    assert resp.status_code == 422
    assert not PushSubscription.objects.filter(endpoint=endpoint).exists()


@pytest.mark.parametrize("endpoint", [
    "https://fcm.googleapis.com/fcm/send/AAA",
    "https://updates.push.services.mozilla.com/wpush/v2/gAAA",
    "https://web.push.apple.com/QAAA",
    "https://wns2-par02p.notify.windows.com/w/?token=AAA",
])
def test_every_real_browser_push_service_is_accepted(client, endpoint):
    resp = client.post("/api/push/subscribe", {**SUB, "endpoint": endpoint},
                       content_type="application/json")
    assert resp.status_code == 201
    assert PushSubscription.objects.filter(endpoint=endpoint).exists()


def test_a_send_does_not_follow_a_redirect(monkeypatch, user, settings):
    """A redirect is the cheapest way past the subscribe-time address check, so
    the send goes through a session that never follows one."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from py_vapid import Vapid

    from apps.push import services

    vapid = Vapid()
    vapid.generate_keys()
    settings.VAPID_PRIVATE_KEY = vapid
    settings.VAPID_SUBJECT = "mailto:test@dimagi.com"
    browser_key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    sent = {}

    def fake_request(self, method, url, **kwargs):
        sent.update(method=method, url=url, **kwargs)

        class Resp:
            status_code = 201
            text = ""
            headers = {}

        return Resp()

    monkeypatch.setattr(services._NoRedirectSession, "request", fake_request)
    sub = PushSubscription.objects.create(
        user=user, endpoint="https://fcm.googleapis.com/fcm/send/AAA",
        # A real P-256 public key + auth secret, so pywebpush can encrypt.
        p256dh=base64.urlsafe_b64encode(browser_key).decode().rstrip("="),
        auth=base64.urlsafe_b64encode(b"0123456789abcdef").decode().rstrip("="),
    )
    services._send_one(sub, {"title": "t"})
    assert sent["url"] == sub.endpoint
    assert sent["allow_redirects"] is False
