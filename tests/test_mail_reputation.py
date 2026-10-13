from __future__ import annotations

import datetime as dt
import json

import pytest
from django.contrib.auth.models import User
from django.db import OperationalError
from django.utils import timezone

from apps.mail import reputation, sns
from apps.mail.models import MailDomain, MailEvent, MailMessage, MailRoute, MailThread
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture()
def d():
    u = User.objects.create_user("jj", "jj@x.com", "pw")
    ws = Workspace.objects.create(slug="m", display_name="M", created_by=u)
    return MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")


def _events(d, kind, n, *, hard=False, ago=dt.timedelta(hours=2)):
    at = timezone.now() - ago
    MailEvent.objects.bulk_create([MailEvent(domain=d, kind=kind, hard=hard, at=at) for _ in range(n)])


def _ev(kind="Bounce", source="ada@marshellis.com", **kw):
    ev = {"eventType": kind, "mail": {"messageId": "m1", "source": source,
                                      "timestamp": timezone.now().isoformat()}}
    ev.update(kw)
    return ev


def test_under_minimum_volume_never_suspends(d):
    _events(d, "Send", 10); _events(d, "Bounce", 5)
    assert reputation.evaluate(d) is None


def test_bounce_rate_over_4_percent_suspends(d):
    _events(d, "Send", 100); _events(d, "Bounce", 5)
    assert "bounce" in reputation.evaluate(d)
    d.refresh_from_db(); assert d.status == "suspended"


def test_complaint_rate_over_0_08_percent_suspends(d):
    _events(d, "Send", 2000); _events(d, "Complaint", 2)
    assert "complaint" in reputation.evaluate(d)


def test_hard_bounce_burst_suspends_regardless_of_volume(d):
    _events(d, "Bounce", 11, hard=True, ago=dt.timedelta(minutes=10))
    assert "burst" in reputation.evaluate(d)


def test_record_maps_an_ses_event_to_the_sending_domain(d):
    reputation.record(_ev(bounce={"bounceType": "Permanent"}))
    e = MailEvent.objects.get(); assert e.kind == "Bounce" and e.hard


def test_record_accepts_notification_type_and_display_name_source(d):
    ev = _ev(source="Ada L <ada@marshellis.com>")
    ev["notificationType"] = ev.pop("eventType")
    reputation.record(ev)
    assert MailEvent.objects.get().domain_id == d.id


def test_record_ignores_missing_source_subdomain_and_unknown_kind(d):
    reputation.record({"eventType": "Bounce", "mail": {"messageId": "m"}})
    reputation.record(_ev(source="x@bounce.marshellis.com"))
    reputation.record(_ev(kind="Open"))
    assert MailEvent.objects.count() == 0


def test_record_updates_message_delivery_status(d):
    route = MailRoute.objects.create(domain=d, local_part="a", kind="forward", forward_to=d.workspace.created_by)
    th = MailThread.objects.create(workspace=d.workspace, route=route)
    MailMessage.objects.create(thread=th, workspace=d.workspace, direction="out", message_id="<x>",
                               ses_message_id="m1", delivery_status="sent")
    reputation.record(_ev("Delivery"))
    assert MailMessage.objects.get().delivery_status == "delivered"


def test_evaluate_does_not_overwrite_an_existing_suspension(d):
    MailDomain.objects.filter(pk=d.pk).update(status="suspended", suspended_reason="manual")
    _events(d, "Send", 100); _events(d, "Bounce", 10)
    reputation.evaluate(d)
    d.refresh_from_db(); assert d.suspended_reason == "manual"


@pytest.fixture()
def post(client, settings, monkeypatch):
    settings.MAIL_EVENTS_TOPIC_ARN = "arn:events"
    calls = {"confirm": 0}
    monkeypatch.setattr(sns, "verify", lambda msg, *, topic_arn: None)
    monkeypatch.setattr(sns, "confirm", lambda msg: calls.__setitem__("confirm", calls["confirm"] + 1))

    def _post(msg):
        return client.post("/api/mail/ses/events/", data=json.dumps(msg), content_type="application/json")
    _post.calls = calls
    return _post


def test_events_endpoint_records_a_notification(d, post):
    r = post({"Type": "Notification", "Message": json.dumps(_ev("Send"))})
    assert r.status_code == 200 and MailEvent.objects.filter(kind="Send").count() == 1


def test_events_endpoint_confirms_subscription(d, post):
    r = post({"Type": "SubscriptionConfirmation"})
    assert r.status_code == 200 and post.calls["confirm"] == 1


def test_events_endpoint_404_when_verification_fails(d, post, monkeypatch):
    def bad(msg, *, topic_arn):
        raise sns.Invalid("no")
    monkeypatch.setattr(sns, "verify", bad)
    assert post({"Type": "Notification"}).status_code == 404


def test_events_endpoint_poison_event_is_200_transient_db_is_500(d, post, monkeypatch):
    msg = {"Type": "Notification", "Message": json.dumps(_ev("Send"))}
    monkeypatch.setattr(reputation, "record", lambda e, **kw: (_ for _ in ()).throw(KeyError("x")))
    assert post(msg).status_code == 200
    monkeypatch.setattr(reputation, "record", lambda e, **kw: (_ for _ in ()).throw(OperationalError("db")))
    assert post(msg).status_code == 500


def test_a_redelivered_sns_message_counts_once(d, post):
    msg = {"Type": "Notification", "MessageId": "sns-1",
           "Message": json.dumps(_ev(bounce={"bounceType": "Permanent"}))}
    assert post(msg).status_code == 200 and post(msg).status_code == 200
    assert MailEvent.objects.filter(kind="Bounce").count() == 1
    assert MailEvent.objects.get().sns_message_id == "sns-1"


def test_two_sns_bounces_for_one_ses_message_count_twice(d, post):
    # one multi-recipient send can bounce per recipient: same SES id, distinct SNS messages
    for sns_id in ("sns-1", "sns-2"):
        msg = {"Type": "Notification", "MessageId": sns_id, "Message": json.dumps(_ev())}
        assert post(msg).status_code == 200
    assert MailEvent.objects.filter(kind="Bounce", ses_message_id="m1").count() == 2


def test_events_without_an_sns_message_id_are_not_deduped(d):
    for _ in range(2):
        reputation.record({"eventType": "Send", "mail": {"source": "ada@marshellis.com"}})
    assert MailEvent.objects.count() == 2
