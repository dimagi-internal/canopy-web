from __future__ import annotations

import base64
import datetime
import http.client
import json
import ssl
import urllib.error
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from django.contrib.auth.models import User
from django.db import OperationalError
from django.test import Client

from apps.agents.models import Agent
from apps.mail import inbound, ses, sns
from apps.mail.models import MailDomain, MailRoute
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
FIX = Path(__file__).parent / "fixtures" / "mail"
TOPIC = "arn:aws:sns:us-east-1:858923557655:canopy-inbound-mail"
URL = "/api/mail/ses/inbound/"


@pytest.fixture()
def world(settings, monkeypatch):
    settings.MAIL_ENABLED = True
    settings.MAIL_INBOUND_TOPIC_ARN = TOPIC
    u = User.objects.create_user("jj", "jjackson@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="marshellis", display_name="M", created_by=u)
    WorkspaceMembership.objects.create(workspace=ws, user=u, role="owner")
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws)
    MailRoute.objects.create(domain=d, local_part="ada", kind="agent", agent=a)
    MailRoute.objects.create(domain=d, local_part="*", kind="forward", forward_to=u)
    calls = []
    monkeypatch.setattr(inbound, "deliver_agent", lambda r, p, k: calls.append(("agent", r.address)) or "agent")
    monkeypatch.setattr(inbound, "deliver_forward", lambda r, p, raw, rcpt: calls.append(("fwd", rcpt)) or "forwarded")
    monkeypatch.setattr(ses, "get_object", lambda key: (FIX / "plain.eml").read_bytes())
    return type("W", (), {"calls": calls, "user": u})


def note(recipients, verdict="PASS", action=None):
    return {"receipt": {"recipients": recipients, "spamVerdict": {"status": verdict},
                        "virusVerdict": {"status": "PASS"},
                        "action": action or {"type": "S3", "objectKey": "incoming/abc"}},
            "mail": {"messageId": "ses-abc"}}


def post(msg_note):
    body = {"Type": "Notification", "TopicArn": TOPIC, "Message": json.dumps(msg_note)}
    return Client().post(URL, json.dumps(body), content_type="text/plain; charset=UTF-8")


@pytest.fixture()
def signed(monkeypatch):
    monkeypatch.setattr(sns, "verify", lambda m, topic_arn: None)


def test_each_recipient_routes_once(world):
    out = inbound.handle(note(["ada@marshellis.com", "apple@marshellis.com", "x@other.org"]))
    assert [o["outcome"] for o in out] == ["agent", "forwarded", "dropped:unknown-domain"]


def test_spam_is_dropped_for_everyone(world):
    out = inbound.handle(note(["ada@marshellis.com"], verdict="FAIL"))
    assert out[0]["outcome"] == "dropped:spam" and world.calls == []


def test_key_falls_back_to_prefix_plus_message_id(world, monkeypatch):
    seen = []
    monkeypatch.setattr(ses, "get_object", lambda key: seen.append(key) or (FIX / "plain.eml").read_bytes())
    inbound.handle(note(["ada@marshellis.com"], action={"type": "S3", "objectKeyPrefix": "incoming/"}))
    assert seen == ["incoming/ses-abc"]


def test_unsigned_post_is_404(world):
    r = Client().post(URL, json.dumps({"Type": "Notification"}), content_type="text/plain; charset=UTF-8")
    assert r.status_code == 404


def test_garbage_body_is_404(world):
    assert Client().post(URL, "not json", content_type="text/plain").status_code == 404


def test_signed_notification_is_handled(world, signed):
    r = post(note(["ada@marshellis.com"]))
    assert r.status_code == 200 and world.calls == [("agent", "ada@marshellis.com")]


def test_subscription_confirmation_is_verified_then_confirmed(world, monkeypatch):
    order = []
    monkeypatch.setattr(sns, "verify", lambda m, topic_arn: order.append("verify"))
    monkeypatch.setattr(sns, "confirm", lambda m: order.append("confirm"))
    body = {"Type": "SubscriptionConfirmation", "TopicArn": TOPIC, "SubscribeURL": "https://sns.x"}
    r = Client().post(URL, json.dumps(body), content_type="text/plain")
    assert r.status_code == 200 and order == ["verify", "confirm"]


def test_unverified_subscription_is_never_confirmed(world, monkeypatch):
    def boom(m, topic_arn):
        raise sns.Invalid("no")

    monkeypatch.setattr(sns, "verify", boom)
    monkeypatch.setattr(sns, "confirm", lambda m: pytest.fail("confirmed an unverified message"))
    body = {"Type": "SubscriptionConfirmation", "TopicArn": TOPIC, "SubscribeURL": "https://evil"}
    assert Client().post(URL, json.dumps(body), content_type="text/plain").status_code == 404


def _client_error():
    return ClientError({"Error": {"Code": "SlowDown", "Message": "x"}}, "GetObject")


def test_transient_s3_failure_is_500_so_sns_retries(world, signed, monkeypatch):
    def boom(key):
        raise _client_error()

    monkeypatch.setattr(ses, "get_object", boom)
    assert post(note(["ada@marshellis.com"])).status_code == 500


def test_mail_off_is_200_dropped(world, signed, monkeypatch):
    def off(key):
        raise ses.MailOff("off")

    monkeypatch.setattr(ses, "get_object", off)
    r = post(note(["ada@marshellis.com"]))
    assert r.status_code == 200 and r.json()["outcomes"][0]["outcome"] == "dropped:mail-off"


def test_deterministic_bug_is_200_not_a_six_hour_retry_loop(world, signed, monkeypatch):
    def bug(key):
        raise KeyError("poisoned")

    monkeypatch.setattr(ses, "get_object", bug)
    r = post(note(["ada@marshellis.com"]))
    assert r.status_code == 200 and r.json()["outcomes"][0]["outcome"] == "error:KeyError"


def test_one_recipient_failing_does_not_stop_the_others(world, signed, monkeypatch):
    def bad(r, p, k):
        raise ValueError("bug")

    monkeypatch.setattr(inbound, "deliver_agent", bad)
    r = post(note(["ada@marshellis.com", "apple@marshellis.com"]))
    assert r.status_code == 200
    assert [o["outcome"] for o in r.json()["outcomes"]] == ["error:ValueError", "forwarded"]
    assert world.calls == [("fwd", "apple@marshellis.com")]


def test_transient_recipient_error_still_finishes_the_rest_then_500s(world, signed, monkeypatch):
    def bad(r, p, k):
        raise OperationalError("db gone")

    monkeypatch.setattr(inbound, "deliver_agent", bad)
    r = post(note(["ada@marshellis.com", "apple@marshellis.com"]))
    assert r.status_code == 500
    assert world.calls == [("fwd", "apple@marshellis.com")]


def test_sns_rejects_a_foreign_cert_host():
    with pytest.raises(sns.Invalid):
        sns.verify({"Type": "Notification", "TopicArn": TOPIC, "SignatureVersion": "2",
                    "SigningCertURL": "https://evil.example/cert.pem", "Signature": "x"},
                   topic_arn=TOPIC)


def test_sns_rejects_the_wrong_topic():
    with pytest.raises(sns.Invalid):
        sns.verify({"Type": "Notification", "TopicArn": "arn:aws:sns:us-east-1:1:other"},
                   topic_arn=TOPIC)


def _cert_and_key():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    return key, cert.public_bytes(serialization.Encoding.PEM)


@pytest.mark.parametrize("version", ["1", "2"])
def test_real_signature_verifies_and_a_mutated_message_fails(monkeypatch, version):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key, pem = _cert_and_key()
    monkeypatch.setattr(sns, "_fetch_cert", lambda url: pem)
    msg = {"Type": "Notification", "MessageId": "m1", "TopicArn": TOPIC, "Message": "hello",
           "Timestamp": "2026-10-12T00:00:00.000Z", "SignatureVersion": version,
           "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-abc123.pem"}
    algo = hashes.SHA1() if version == "1" else hashes.SHA256()
    sig = key.sign(sns._string_to_sign(msg), padding.PKCS1v15(), algo)
    msg["Signature"] = base64.b64encode(sig).decode()
    sns.verify(msg, topic_arn=TOPIC)  # passes
    with pytest.raises(sns.Invalid):
        sns.verify({**msg, "Message": "tampered"}, topic_arn=TOPIC)


GOOD_CERT = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-abc123.pem"


@pytest.mark.parametrize("url", [
    "https://sns.s3.amazonaws.com/SimpleNotificationService-abc123.pem",
    "https://sns.s3-us-west-2.amazonaws.com/SimpleNotificationService-abc123.pem",
    "https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-abc123.pem",
    "https://evil@sns.us-east-1.amazonaws.com/SimpleNotificationService-abc123.pem",
    "https://sns.us-east-1.amazonaws.com@evil.com/SimpleNotificationService-abc123.pem",
    "https://sns.us-east-1.amazonaws.com:8443/SimpleNotificationService-abc123.pem",
    "http://sns.us-east-1.amazonaws.com/SimpleNotificationService-abc123.pem",
    GOOD_CERT + "\n",
    GOOD_CERT + "?x=1",
    "https://sns.us-east-1.amazonaws.com/other.pem",
    "https://sns.us-east-1.amazonaws.com/a/SimpleNotificationService-abc123.pem",
])
def test_bad_cert_urls_are_invalid(url, monkeypatch):
    monkeypatch.setattr(sns, "_fetch_cert", lambda u: pytest.fail("fetched a bad URL"))
    with pytest.raises(sns.Invalid):
        sns.verify({"Type": "Notification", "TopicArn": TOPIC, "SignatureVersion": "2",
                    "SigningCertURL": url, "Signature": "x"}, topic_arn=TOPIC)


@pytest.mark.parametrize("url", [
    "https://sns.s3.amazonaws.com/?Action=ConfirmSubscription",
    "https://sns.eu-west-1.amazonaws.com/?Action=ConfirmSubscription",
    "https://u@sns.us-east-1.amazonaws.com/?x",
    "https://sns.us-east-1.amazonaws.com:444/?x",
    "http://sns.us-east-1.amazonaws.com/?x",
])
def test_bad_subscribe_urls_are_not_fetched(url, monkeypatch):
    monkeypatch.setattr(sns, "_get", lambda u: pytest.fail("fetched a bad URL"))
    with pytest.raises(sns.Invalid):
        sns.confirm({"TopicArn": TOPIC, "SubscribeURL": url})


def test_good_subscribe_url_is_fetched(monkeypatch):
    got = []
    monkeypatch.setattr(sns, "_get", got.append)
    sns.confirm({"TopicArn": TOPIC, "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription"})
    assert len(got) == 1


def _http_error(code):
    return urllib.error.HTTPError(GOOD_CERT, code, "x", {}, None)


_TRANSIENT_ERRORS = [
    lambda: urllib.error.URLError("dns"),
    lambda: TimeoutError("slow"),
    lambda: ssl.SSLError("handshake"),
    lambda: http.client.RemoteDisconnected("gone"),
    lambda: OSError("reset"),
    lambda: _http_error(503),
]


def _raiser(make):
    def boom(*_a, **_k):
        raise make()
    return boom


_SIGNED = {"Type": "Notification", "TopicArn": TOPIC, "SignatureVersion": "2", "Signature": "eA==",
           "SigningCertURL": GOOD_CERT, "Message": "{}"}


@pytest.mark.parametrize("make", [lambda: _http_error(404), lambda: _http_error(403)])
def test_cert_fetch_4xx_is_invalid_and_404(world, monkeypatch, make):
    monkeypatch.setattr(sns, "_fetch_cert", _raiser(make))
    with pytest.raises(sns.Invalid):
        sns.verify(_SIGNED, topic_arn=TOPIC)
    assert Client().post(URL, json.dumps(_SIGNED), content_type="text/plain").status_code == 404


@pytest.mark.parametrize("make", _TRANSIENT_ERRORS)
def test_transient_cert_fetch_failure_is_500_so_sns_retries(world, monkeypatch, make):
    monkeypatch.setattr(sns, "_fetch_cert", _raiser(make))
    with pytest.raises(sns.Unavailable):
        sns.verify(_SIGNED, topic_arn=TOPIC)
    assert Client().post(URL, json.dumps(_SIGNED), content_type="text/plain").status_code == 500
    assert world.calls == []


_GOOD_SUB = "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription"


def test_subscribe_4xx_is_invalid(monkeypatch):
    monkeypatch.setattr(sns, "_get", _raiser(lambda: _http_error(404)))
    with pytest.raises(sns.Invalid):
        sns.confirm({"TopicArn": TOPIC, "SubscribeURL": _GOOD_SUB})


@pytest.mark.parametrize("make", _TRANSIENT_ERRORS)
def test_transient_subscribe_failure_is_unavailable(monkeypatch, make):
    monkeypatch.setattr(sns, "_get", _raiser(make))
    with pytest.raises(sns.Unavailable):
        sns.confirm({"TopicArn": TOPIC, "SubscribeURL": _GOOD_SUB})


@pytest.mark.parametrize("url", [URL, "/api/mail/ses/events/"])
def test_transient_confirm_failure_is_500_on_both_doors(world, settings, monkeypatch, url):
    settings.MAIL_EVENTS_TOPIC_ARN = TOPIC
    monkeypatch.setattr(sns, "verify", lambda m, topic_arn: None)
    monkeypatch.setattr(sns, "_get", _raiser(lambda: TimeoutError("slow")))
    body = {"Type": "SubscriptionConfirmation", "TopicArn": TOPIC, "SubscribeURL": _GOOD_SUB}
    assert Client().post(url, json.dumps(body), content_type="text/plain").status_code == 500


@pytest.mark.parametrize("url", [URL, "/api/mail/ses/events/"])
def test_invalid_confirm_is_404_on_both_doors(world, settings, monkeypatch, url):
    settings.MAIL_EVENTS_TOPIC_ARN = TOPIC
    monkeypatch.setattr(sns, "verify", lambda m, topic_arn: None)
    monkeypatch.setattr(sns, "_get", _raiser(lambda: _http_error(404)))
    body = {"Type": "SubscriptionConfirmation", "TopicArn": TOPIC, "SubscribeURL": _GOOD_SUB}
    assert Client().post(url, json.dumps(body), content_type="text/plain").status_code == 404


def test_transient_cert_failure_on_events_door_is_500(world, settings, monkeypatch):
    settings.MAIL_EVENTS_TOPIC_ARN = TOPIC
    monkeypatch.setattr(sns, "_fetch_cert", _raiser(lambda: TimeoutError("slow")))
    assert Client().post("/api/mail/ses/events/", json.dumps(_SIGNED),
                         content_type="text/plain").status_code == 500


def test_deeply_nested_json_is_404(world):
    assert Client().post(URL, "[" * 100000, content_type="text/plain").status_code == 404


@pytest.mark.parametrize("key", ["kept/abc", "other/abc", "incoming/../kept/abc"])
def test_bad_object_key_is_refused_without_s3(world, monkeypatch, key):
    monkeypatch.setattr(ses, "get_object", lambda k: pytest.fail("read S3"))
    out = inbound.handle(note(["ada@marshellis.com"], action={"type": "S3", "objectKey": key}))
    assert out == [{"recipient": "ada@marshellis.com", "outcome": "dropped:bad-key"}]


def test_noops_never_touch_s3(world, monkeypatch):
    monkeypatch.setattr(ses, "get_object", lambda k: pytest.fail("read S3"))
    n = note(["ada@marshellis.com"])
    assert inbound.handle({**n, "notificationType": "AmazonSnsSubscriptionSucceeded"}) == []
    assert inbound.handle(note([])) == []
    no_key = {k: v for k, v in note(["ada@marshellis.com"], action={"type": "S3"}).items() if k != "mail"}
    assert inbound.handle(no_key) == []


def test_recipients_are_deduped_after_lowercasing(world):
    out = inbound.handle(note(["Ada@marshellis.com", "ada@MARSHELLIS.com", "apple@marshellis.com"]))
    assert [o["recipient"] for o in out] == ["ada@marshellis.com", "apple@marshellis.com"]
    assert len(world.calls) == 2
