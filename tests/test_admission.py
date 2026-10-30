"""Admission rules: turns that can never complete are rejected before they run.

The incident (canopy-web#1400, turn 506cbfed, 2026-10-10): a DMARC-verified
contact emailed hal; hal's `ask` lists `contact@dimagi.com:verified`, so the
turn was confined; it resolved manual; a cloud runner claimed it; the ask
procedure drafted a reply and waited for a human yes nobody could give. 32s of
LLM, no reply, no notice. The seeded workspace rule now rejects that
combination at the claim: CANCELLED, never claimed, a sender notice and an
owner notice recorded, and no debugger turn.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.utils import timezone

from apps.admission import defaults, notices, runner_notices
from apps.admission.models import AdmissionNotice, AdmissionRule, AdmissionSettings
from apps.agents.models import Agent, AgentGrant
from apps.harness import services
from apps.harness.claim import claim_next_turn
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.harness.routing import ENVELOPE_VERSION, PROFILES_VERSION
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.testing import enrolled
from tests.access_helpers import publish

pytestmark = pytest.mark.django_db
M = WorkspaceMembership

VIKRAM = "vkumar@dimagi.com"
PASS_ALL = ("mx.google.com; dkim=pass header.i=@dimagi.com; spf=pass "
            "smtp.mailfrom=dimagi.com; dmarc=pass header.from=dimagi.com")
VERIFIED = [{"name": "Authentication-Results", "value": PASS_ALL}]
CONFINING = {"capabilities": {"ask": {"callers": ["contact@dimagi.com:verified"]}}}
CAPS = {"sessions": True, "profiles": PROFILES_VERSION, "envelope": ENVELOPE_VERSION}


def _runner(name, kind, owner):
    return enrolled(Runner.objects.create(name=name, kind=kind, owner=owner, status=Runner.ONLINE,
                                 last_heartbeat_at=timezone.now(), capabilities=CAPS))


@pytest.fixture
def fleet():
    User = get_user_model()
    jj = User.objects.create_user(username="jj", email="jj@dimagi.com", first_name="Jonathan",
                                  last_name="Jackson")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    M.objects.create(workspace=ws, user=jj, role=M.OWNER)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=jj,
                               turn_mode=Agent.MANUAL)
    publish(hal, CONFINING)
    cloud = _runner("cloud-ec2-2", Runner.CLOUD, jj)
    laptop = _runner("jj-mbp", Runner.EMDASH, jj)
    return {"jj": jj, "ws": ws, "hal": hal, "cloud": cloud, "laptop": laptop}


def _route(agent, runner):
    RunnerAssignment.objects.filter(agent=agent).delete()
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)


def _mail(agent, *, sender=VIKRAM, thread="t-1", count=1, headers=VERIFIED):
    turn, _ = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key=f"email-{agent.slug}-{thread}-{count}",
        origin_ref={"from": sender, "subject": "Can Hal help?", "thread_id": thread,
                    "message_id": f"m-{count}", "headers": headers},
    )
    return turn


def _claim(runner, capture):
    with capture(execute=True):
        return claim_next_turn(runner)


def _replies_on(fleet):
    AdmissionSettings.objects.update_or_create(workspace=fleet["ws"],
                                               defaults={"sender_notices_enabled": True})


def _seeded(ws, key="email-confined-manual-cloud"):
    return AdmissionRule.objects.get(workspace=ws, seed_key=key)


# --- the regression ----------------------------------------------------------------

def test_a_confined_manual_email_on_a_cloud_runner_is_rejected_not_run(
        fleet, django_capture_on_commit_callbacks):
    hal, cloud = fleet["hal"], fleet["cloud"]
    _route(hal, cloud)
    turn = _mail(hal)
    assert turn.status == Turn.QUEUED and turn.capability == "ask"  # confined, waits for the claim

    with mock.patch("apps.harness.auto_debug.on_turn_failed") as debug:
        assert _claim(cloud, django_capture_on_commit_callbacks) is None
    debug.assert_not_called()

    turn.refresh_from_db()
    rule = _seeded(fleet["ws"])
    assert turn.status == Turn.CANCELLED
    assert turn.result_note.startswith(f"rejected: admission rule {rule.pk} ")
    assert rule.reason in turn.result_note
    assert turn.claimed_by_id is None and not turn.session_key  # no session ever started
    assert turn.events.filter(kind="status", payload__admission_rule=rule.pk).exists()

    rule.refresh_from_db()
    assert rule.fired_count == 1 and rule.last_fired_at is not None
    sender = AdmissionNotice.objects.get(turn=turn, kind=AdmissionNotice.SENDER)
    owner = AdmissionNotice.objects.get(turn=turn, kind=AdmissionNotice.OWNER)
    # The sender's reply is recorded but held: automatic replies ship OFF.
    assert (sender.status, sender.recipient, sender.thread_id) == (AdmissionNotice.HELD, VIKRAM, "t-1")
    assert (owner.status, owner.recipient) == (AdmissionNotice.SENT, "jj@dimagi.com")
    # The owner's email is off-thread and owner-only, and carries the fix links.
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["jj@dimagi.com"]
    body = mail.outbox[0].body
    assert VIKRAM in body and f"/w/connect/settings/admission-rules?rule={rule.pk}" in body
    assert "/w/connect/agents/hal/settings#running" in body
    # ...and says, in words, what the sender was told.
    assert "Reply:   none: automatic replies are off in this workspace" in body
    # ...and lands on their Waiting on you.
    task = hal.tasks.get(idempotency_key=f"admission:{turn.pk}")
    assert task.ask_kind == "review" and task.waiting_on_user == fleet["jj"]


def test_the_same_turn_on_a_laptop_still_runs_because_the_laptop_rule_ships_off(
        fleet, django_capture_on_commit_callbacks):
    hal, laptop = fleet["hal"], fleet["laptop"]
    assert _seeded(fleet["ws"], "email-confined-manual-laptop").enabled is False
    _route(hal, laptop)
    turn = _mail(hal)
    claimed = _claim(laptop, django_capture_on_commit_callbacks)
    assert claimed is not None and claimed.pk == turn.pk and claimed.status == Turn.CLAIMED
    assert not AdmissionNotice.objects.exists()


def test_a_full_profile_or_auto_turn_is_not_touched(fleet, django_capture_on_commit_callbacks):
    hal, cloud = fleet["hal"], fleet["cloud"]
    _route(hal, cloud)
    Agent.objects.filter(pk=hal.pk).update(turn_mode=Agent.AUTO)
    turn = _mail(Agent.objects.get(pk=hal.pk))
    assert _claim(cloud, django_capture_on_commit_callbacks).pk == turn.pk


# --- the workspace controls it -----------------------------------------------------

def _admin_client(fleet):
    c = Client()
    c.force_login(fleet["jj"])
    return c


def test_disabling_the_seeded_rule_through_the_api_lets_cloud_run_it(
        fleet, django_capture_on_commit_callbacks):
    hal, cloud = fleet["hal"], fleet["cloud"]
    rule = _seeded(fleet["ws"])
    res = _admin_client(fleet).patch(f"/api/workspaces/connect/admission-rules/{rule.pk}",
                                     {"enabled": False}, content_type="application/json")
    assert res.status_code == 200 and res.json()["enabled"] is False
    _route(hal, cloud)
    turn = _mail(hal)
    assert _claim(cloud, django_capture_on_commit_callbacks).pk == turn.pk


def test_a_disabled_agent_copy_does_not_override_but_an_allow_rule_does(
        fleet, django_capture_on_commit_callbacks):
    hal, cloud, ws = fleet["hal"], fleet["cloud"], fleet["ws"]
    _route(hal, cloud)
    base = dict(origin="email", access="confined", turn_mode="manual", runner_kind="cloud",
                reason="copy")
    AdmissionRule.objects.create(workspace=ws, agent=hal, enabled=False, **base)
    first = _mail(hal, thread="t-1")
    _claim(cloud, django_capture_on_commit_callbacks)
    first.refresh_from_db()
    assert first.status == Turn.CANCELLED  # the workspace-wide rule still fired

    AdmissionRule.objects.create(workspace=ws, agent=hal, action=AdmissionRule.ALLOW, **base)
    second = _mail(hal, thread="t-2")
    assert _claim(cloud, django_capture_on_commit_callbacks).pk == second.pk


def test_a_rule_with_no_runner_condition_rejects_at_enqueue(fleet, django_capture_on_commit_callbacks):
    hal = fleet["hal"]
    AdmissionRule.objects.create(workspace=fleet["ws"], origin="email", principal="contact",
                                 reason="contacts may not email hal", notify_sender=False)
    with django_capture_on_commit_callbacks(execute=True):
        turn = _mail(hal)
    assert turn.status == Turn.CANCELLED and "contacts may not email hal" in turn.result_note
    assert list(AdmissionNotice.objects.values_list("kind", flat=True)) == [AdmissionNotice.OWNER]


def test_another_workspaces_rule_never_applies(fleet, django_capture_on_commit_callbacks):
    other = Workspace.objects.create(slug="elsewhere", display_name="E", created_by=fleet["jj"])
    AdmissionRule.objects.filter(workspace=fleet["ws"]).update(enabled=False)
    assert AdmissionRule.objects.filter(workspace=other, enabled=True).exists()
    _route(fleet["hal"], fleet["cloud"])
    turn = _mail(fleet["hal"])
    assert _claim(fleet["cloud"], django_capture_on_commit_callbacks).pk == turn.pk


# --- seeding -------------------------------------------------------------------------

def test_a_new_workspace_is_seeded_and_seeding_is_idempotent(fleet):
    ws = Workspace.objects.create(slug="fresh", display_name="Fresh", created_by=fleet["jj"])
    keys = {r.seed_key: r.enabled for r in AdmissionRule.objects.filter(workspace=ws)}
    assert keys == {"email-confined-manual-cloud": True, "email-confined-manual-laptop": False,
                    "blocked-sender": True, "offered-nothing": True,
                    "no-runner-paused": True, "no-runner-offline": True}
    assert defaults.seed(ws.pk) == 0


def test_origin_choices_track_the_turn_vocabulary():
    assert dict(AdmissionRule.ORIGIN_CHOICES).keys() == dict(Turn.ORIGIN_CHOICES).keys()


# --- sender-notice safety ------------------------------------------------------------

def _reject_on_cloud(fleet, capture, **kw):
    _route(fleet["hal"], fleet["cloud"])
    turn = _mail(fleet["hal"], **kw)
    _claim(fleet["cloud"], capture)
    turn.refresh_from_db()
    assert turn.status == Turn.CANCELLED
    return AdmissionNotice.objects.get(turn=turn, kind=AdmissionNotice.SENDER)


@pytest.mark.parametrize("sender", [
    "no-reply@example.org", "comments-noreply@docs.google.com",
    "drive-shares-dm-noreply@google.com", "notifications.noreply@example.org",
    "bounce+abc@example.org",
])
def test_a_noreply_sender_gets_no_notice(fleet, django_capture_on_commit_callbacks, sender):
    # A no-reply address cannot be a verified dimagi.com contact in the ask
    # list, so give it a rule of its own.
    hal = fleet["hal"]
    publish(hal, {"capabilities": {"ask": {"callers": ["contact"]}}})
    notice = _reject_on_cloud(fleet, django_capture_on_commit_callbacks,
                              sender=sender, headers=[])
    assert notice.status == AdmissionNotice.SKIPPED and "no-reply" in notice.detail


@pytest.mark.parametrize("sender", ["noreen@example.org", "postmasterclass@example.org",
                                    "vikram@noreply-free.example.org"])
def test_a_person_whose_address_merely_contains_the_letters_is_still_a_person(sender):
    from apps.admission.notices import _NOREPLY

    assert not _NOREPLY.search(sender.partition("@")[0])


def test_another_agents_address_gets_no_notice_however_the_turn_was_typed(
        fleet, django_capture_on_commit_callbacks):
    hal = fleet["hal"]
    publish(hal, {"capabilities": {"ask": {"callers": ["contact"]}}})
    Agent.objects.create(slug="ada", name="Ada", workspace=fleet["ws"], owner=fleet["jj"],
                         email="ada@dimagi-ai.com")
    notice = _reject_on_cloud(fleet, django_capture_on_commit_callbacks,
                              sender="Ada <ada@dimagi-ai.com>", headers=[])
    assert notice.status == AdmissionNotice.SKIPPED and "another agent" in notice.detail


def test_with_replies_on_the_sender_is_answered_once_per_thread(fleet, django_capture_on_commit_callbacks):
    # A Gmail thread's reply is the runner's to send: canopy-web holds no mailbox token it can open.
    _replies_on(fleet)
    first = _reject_on_cloud(fleet, django_capture_on_commit_callbacks, thread="t-9", count=1)
    assert first.status == AdmissionNotice.PENDING
    assert first.message_id == "m-1"
    assert "automatic reply" in first.body and "Jonathan Jackson" in first.body

    # The same poll again: the same idempotency key, so no new turn and no notice.
    assert _mail(fleet["hal"], thread="t-9", count=1).pk == first.turn_id
    # A new message on the thread inside 24h: rejected again, but not re-answered.
    second = _reject_on_cloud(fleet, django_capture_on_commit_callbacks, thread="t-9", count=2)
    assert second.status == AdmissionNotice.SKIPPED and "24 hours" in second.detail


def test_a_mailbox_canopy_cannot_send_from_is_left_for_the_agents_runner(
        fleet, django_capture_on_commit_callbacks):
    # canopy-web never sends from an agent's Gmail: the notice waits for the runner
    # that reads hal's mailbox, carrying what it needs.
    _replies_on(fleet)
    fleet["hal"].email = "hal@dimagi-ai.com"
    fleet["hal"].save(update_fields=["email"])
    notice = _reject_on_cloud(fleet, django_capture_on_commit_callbacks)
    assert notice.status == AdmissionNotice.PENDING and "runner" in notice.detail
    assert (notice.mailbox, notice.message_id, notice.subject) == ("hal@dimagi-ai.com", "m-1", "Can Hal help?")
    assert notice.body == notices.sender_text(fleet["hal"])


def test_a_canopy_hosted_thread_is_answered_from_its_own_address_not_gmail(
        fleet, django_capture_on_commit_callbacks, settings, monkeypatch):
    # An SES turn's thread_id is a MailThread uuid: Gmail would answer the wrong thread
    # (or none). The reply goes out through the mail app, from the thread's address.
    from apps.mail import ses
    from apps.mail.models import MailDomain, MailMessage, MailRoute, MailThread

    settings.MAIL_ENABLED = True
    sent = []
    monkeypatch.setattr(ses, "send_raw", lambda **kw: sent.append(kw) or "ses-1")
    _replies_on(fleet)
    hal = fleet["hal"]
    d = MailDomain.objects.create(workspace=fleet["ws"], name="dimagi.ai", status="verified")
    r = MailRoute.objects.create(domain=d, local_part="hal", kind="agent", agent=hal)
    t = MailThread.objects.create(workspace=fleet["ws"], route=r, subject="Can Hal help?",
                                  message_count=1)
    MailMessage.objects.create(thread=t, workspace=fleet["ws"], direction="in", mailbox=r.address,
                               message_id="<in-1@dimagi.com>", from_addr=VIKRAM, subject="Can Hal help?")
    _route(hal, fleet["cloud"])
    turn, _ = services.enqueue_turn(
        agent=hal, origin=Turn.ORIGIN_EMAIL, idempotency_key=f"email-hal-{t.uuid}-1",
        origin_ref={"from": VIKRAM, "subject": "Can Hal help?", "thread_id": str(t.uuid),
                    "backend": "ses", "mailbox": r.address, "message_id": "<in-1@dimagi.com>",
                    "headers": VERIFIED})
    _claim(fleet["cloud"], django_capture_on_commit_callbacks)
    notice = AdmissionNotice.objects.get(turn=turn, kind=AdmissionNotice.SENDER)
    assert notice.status == AdmissionNotice.SENT, notice.detail
    assert sent[0]["from_addr"] == "hal@dimagi.ai" and sent[0]["to"] == [VIKRAM]
    assert b"In-Reply-To: <in-1@dimagi.com>" in sent[0]["raw"]
    out = MailMessage.objects.get(direction="out")
    assert out.thread == t and "automatic reply" in out.text


def test_a_hosted_reply_the_mail_app_refuses_is_failed_not_left_for_a_runner(
        fleet, django_capture_on_commit_callbacks):
    # No runner reads an SES mailbox, so PENDING would wait forever.
    _replies_on(fleet)
    _route(fleet["hal"], fleet["cloud"])
    turn, _ = services.enqueue_turn(
        agent=fleet["hal"], origin=Turn.ORIGIN_EMAIL, idempotency_key="email-hal-x-1",
        origin_ref={"from": VIKRAM, "subject": "s", "backend": "ses", "headers": VERIFIED,
                    "thread_id": "00000000-0000-0000-0000-000000000000"})
    _claim(fleet["cloud"], django_capture_on_commit_callbacks)
    notice = AdmissionNotice.objects.get(turn=turn, kind=AdmissionNotice.SENDER)
    assert notice.status == AdmissionNotice.FAILED and "no such thread" in notice.detail


def test_a_pending_gmail_notice_keeps_the_mailbox_the_turn_came_in_on(
        fleet, django_capture_on_commit_callbacks):
    _replies_on(fleet)
    fleet["hal"].email = "hal@dimagi-ai.com"
    fleet["hal"].save(update_fields=["email"])
    _route(fleet["hal"], fleet["cloud"])
    turn, _ = services.enqueue_turn(
        agent=fleet["hal"], origin=Turn.ORIGIN_EMAIL, idempotency_key="email-hal-g-1",
        origin_ref={"from": VIKRAM, "subject": "s", "thread_id": "g-1", "message_id": "m-1",
                    "backend": "gmail", "mailbox": "hal.alt@dimagi-ai.com", "headers": VERIFIED})
    _claim(fleet["cloud"], django_capture_on_commit_callbacks)
    notice = AdmissionNotice.objects.get(turn=turn, kind=AdmissionNotice.SENDER)
    assert notice.status == AdmissionNotice.PENDING and notice.mailbox == "hal.alt@dimagi-ai.com"


# --- the runner sends what canopy-web could not (canopy-web#1416) --------------------

@pytest.fixture
def pending(fleet, django_capture_on_commit_callbacks):
    _replies_on(fleet)
    fleet["hal"].email = "hal@dimagi-ai.com"
    fleet["hal"].save(update_fields=["email"])
    notice = _reject_on_cloud(fleet, django_capture_on_commit_callbacks, thread="t-5")
    assert notice.status == AdmissionNotice.PENDING
    return notice


def _runner_client(runner):
    c = Client()
    c.force_login(runner.owner)
    return c


def _lease(runner, mailboxes):
    res = _runner_client(runner).post(f"/api/harness/runners/{runner.pk}/admission-notices",
                                      {"mailboxes": mailboxes}, content_type="application/json")
    assert res.status_code == 200, res.content
    return res.json()["notices"]


def _report(runner, notice_id, outcome, detail=""):
    res = _runner_client(runner).post(
        f"/api/harness/runners/{runner.pk}/admission-notices/{notice_id}",
        {"outcome": outcome, "detail": detail}, content_type="application/json")
    assert res.status_code == 200, res.content
    return res.json()["ok"]


def test_the_runner_leases_a_pending_notice_sends_it_and_reports_it(fleet, pending):
    laptop = fleet["laptop"]
    [leased] = _lease(laptop, {"hal": "HAL@dimagi-ai.com"})
    assert leased == {
        "id": pending.pk, "agent": "hal", "agent_name": "Hal", "mailbox": "hal@dimagi-ai.com",
        "thread_id": "t-5", "message_id": "m-1", "to": VIKRAM, "subject": "Can Hal help?",
        "body": notices.sender_text(fleet["hal"]),
    }
    # Leased: nobody else gets it while this runner holds it.
    assert _lease(fleet["cloud"], {"hal": "hal@dimagi-ai.com"}) == []
    assert _report(laptop, pending.pk, "sent", "replied (gmail message g-7)") is True
    pending.refresh_from_db()
    assert pending.status == AdmissionNotice.SENT and "g-7" in pending.detail
    # Final: not leased again, and not reported twice.
    assert _lease(laptop, {"hal": "hal@dimagi-ai.com"}) == []
    assert _report(laptop, pending.pk, "sent") is False


def test_a_notice_goes_only_to_a_runner_that_may_hold_the_agent_and_reads_its_mailbox(fleet, pending):
    stranger = get_user_model().objects.create_user(username="s", email="s@dimagi.com")
    M.objects.create(workspace=fleet["ws"], user=stranger, role=M.EDITOR)
    theirs = _runner("their-mbp", Runner.EMDASH, stranger)
    assert _lease(theirs, {"hal": "hal@dimagi-ai.com"}) == []           # not one of hal's admins
    assert _lease(fleet["laptop"], {"hal": "other@dimagi-ai.com"}) == []  # not the mailbox written to
    assert _lease(fleet["laptop"], {"eva": "hal@dimagi-ai.com"}) == []    # not the agent
    assert [n["id"] for n in _lease(fleet["laptop"], {"hal": "hal@dimagi-ai.com"})] == [pending.pk]


def test_a_failed_send_is_retried_after_the_lease_and_given_up_on_after_a_day(fleet, pending):
    laptop = fleet["laptop"]
    assert _lease(laptop, {"hal": "hal@dimagi-ai.com"})
    assert _report(laptop, pending.pk, "failed", "gog: no auth") is True
    pending.refresh_from_db()
    assert pending.status == AdmissionNotice.PENDING and "gog: no auth" in pending.detail
    assert _lease(laptop, {"hal": "hal@dimagi-ai.com"}) == []  # still leased
    AdmissionNotice.objects.filter(pk=pending.pk).update(
        claimed_at=timezone.now() - runner_notices.LEASE - dt.timedelta(seconds=1))
    assert [n["id"] for n in _lease(fleet["cloud"], {"hal": "hal@dimagi-ai.com"})] == [pending.pk]

    # A day on, nobody managed it: it ends failed, saying why, on the off-request clock.
    AdmissionNotice.objects.filter(pk=pending.pk).update(
        created_at=timezone.now() - runner_notices.EXPIRES - dt.timedelta(minutes=1))
    runner_notices._last_expiry = None
    from apps.common import ticks

    ticks.jobs()["admission.expire_runner_notices"].fn()
    pending.refresh_from_db()
    assert pending.status == AdmissionNotice.FAILED
    assert "a runner tried and failed within 24 hours" in pending.detail


def test_a_pending_notice_silences_the_thread_like_a_sent_one(
        fleet, pending, django_capture_on_commit_callbacks):
    second = _reject_on_cloud(fleet, django_capture_on_commit_callbacks, thread="t-5", count=2)
    assert second.status == AdmissionNotice.SKIPPED and "24 hours" in second.detail


def _block(fleet, email=VIKRAM):
    from apps.contacts.models import Contact

    _mail(fleet["hal"], thread="t-0")  # records the contact
    contact = Contact.objects.get(workspace=fleet["ws"], email=email)
    contact.blocked_at, contact.blocked_reason = timezone.now(), "spam"
    contact.save(update_fields=["blocked_at", "blocked_reason"])


def test_a_blocked_senders_refusal_fires_its_rule_and_by_default_tells_nobody(
        fleet, django_capture_on_commit_callbacks):
    _block(fleet)
    with django_capture_on_commit_callbacks(execute=True):
        turn = _mail(fleet["hal"], thread="t-b")
    rule = _seeded(fleet["ws"], "blocked-sender")
    assert turn.status == Turn.CANCELLED and turn.result_note == "not run: the sender is blocked (spam)"
    assert turn.events.filter(kind="status", payload__admission_rule=rule.pk).exists()
    rule.refresh_from_db()
    assert rule.fired_count == 1
    assert not AdmissionNotice.objects.filter(turn=turn).exists() and mail.outbox == []


def test_a_refusal_rule_sets_the_notices_but_can_never_let_the_turn_run(
        fleet, django_capture_on_commit_callbacks):
    _block(fleet)
    rule = _seeded(fleet["ws"], "blocked-sender")
    rule.notify_owner = True
    rule.save()
    with django_capture_on_commit_callbacks(execute=True):
        turn = _mail(fleet["hal"], thread="t-c")
    owner = AdmissionNotice.objects.get(turn=turn, kind=AdmissionNotice.OWNER)
    assert owner.status == AdmissionNotice.SENT and mail.outbox[0].to == ["jj@dimagi.com"]
    assert not AdmissionNotice.objects.filter(turn=turn, kind=AdmissionNotice.SENDER).exists()

    # Disabled, it records nothing — and the sender is STILL refused.
    rule.refresh_from_db()
    rule.enabled = False
    rule.save()
    with django_capture_on_commit_callbacks(execute=True):
        again = _mail(fleet["hal"], thread="t-d")
    assert again.status == Turn.CANCELLED and not again.events.filter(
        payload__has_key="admission_rule").exists()
    rule.refresh_from_db()
    assert rule.fired_count == 1


def test_a_person_offered_nothing_is_refused_and_their_rule_fires(
        fleet, django_capture_on_commit_callbacks):
    hal = fleet["hal"]
    AgentGrant.objects.filter(agent=hal).delete()
    publish(hal, None)  # nothing published, no contact line: a contact is offered nothing
    rule = _seeded(fleet["ws"], "offered-nothing")
    rule.notify_owner = True
    rule.save()
    with django_capture_on_commit_callbacks(execute=True):
        turn = _mail(hal, thread="t-n")
    assert turn.status == Turn.CANCELLED and turn.result_note.startswith("not run:")
    rule.refresh_from_db()
    assert rule.fired_count == 1
    assert AdmissionNotice.objects.get(turn=turn, kind=AdmissionNotice.OWNER).status == AdmissionNotice.SENT
    # An ordinary turn is never matched by a refusal rule.
    assert _seeded(fleet["ws"], "blocked-sender").fired_count == 0


def test_a_refusal_rule_cannot_be_an_allow_or_wait_for_a_runner(fleet):
    admin = _member(fleet, M.ADMIN, "a@dimagi.com")
    url = "/api/workspaces/connect/admission-rules"
    for body in ({"sender": "blocked", "action": "allow", "reason": "let them in"},
                 {"access": "none", "runner_kind": "cloud", "reason": "x"}):
        res = admin.post(url, body, content_type="application/json")
        assert res.status_code in (400, 422), res.content
    ok = admin.post(url, {"origin": "slack", "access": "none", "reason": "tell me",
                          "notify_owner": True}, content_type="application/json")
    assert ok.status_code in (200, 201), ok.content
    assert ok.json()["refusal"] is True and "offered nothing" in ok.json()["summary"]


# --- the settings API ------------------------------------------------------------------

def _member(fleet, role, email):
    u = get_user_model().objects.create_user(username=email, email=email)
    M.objects.create(workspace=fleet["ws"], user=u, role=role)
    c = Client()
    c.force_login(u)
    return c


def test_any_member_reads_the_rules_and_only_an_admin_changes_them(fleet):
    viewer = _member(fleet, M.VIEWER, "v@dimagi.com")
    admin = _member(fleet, M.ADMIN, "a@dimagi.com")
    body = viewer.get("/api/workspaces/connect/admission-rules").json()
    assert body["can_manage"] is False and body["sender_notices_enabled"] is False
    assert {r["summary"] for r in body["rules"]} >= {
        "Email, confined, manual, on a Cloud runner"}
    new = {"origin": "slack", "principal": "contact", "reason": "no Slack guests"}
    url = "/api/workspaces/connect/admission-rules"
    assert viewer.post(url, new, content_type="application/json").status_code == 403
    res = admin.post(url, new, content_type="application/json")
    assert res.status_code == 201 and res.json()["created_by"] == "a@dimagi.com"
    rid = res.json()["id"]
    assert admin.get(url).json()["can_manage"] is True
    assert admin.delete(f"{url}/{rid}").status_code == 204
    assert admin.post(url, {"reason": "everything"}, content_type="application/json").status_code == 422


def test_automatic_replies_are_a_workspace_switch_only_an_admin_flips(
        fleet, django_capture_on_commit_callbacks):
    viewer = _member(fleet, M.VIEWER, "v2@dimagi.com")
    admin = _member(fleet, M.ADMIN, "a2@dimagi.com")
    url = "/api/workspaces/connect/admission-settings"
    assert viewer.put(url, {"sender_notices_enabled": True},
                      content_type="application/json").status_code == 403
    res = admin.put(url, {"sender_notices_enabled": True}, content_type="application/json")
    assert res.status_code == 200 and res.json() == {"sender_notices_enabled": True}
    assert viewer.get("/api/workspaces/connect/admission-rules").json()["sender_notices_enabled"] is True
    # Another workspace's switch is its own: a new one starts off.
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=fleet["jj"])
    assert not AdmissionSettings.sender_notices_on(other.pk)
    # On, the reply is queued for the runner rather than held.
    notice = _reject_on_cloud(fleet, django_capture_on_commit_callbacks, thread="t-s")
    assert notice.status == AdmissionNotice.PENDING


def test_a_rule_cannot_name_another_workspaces_agent(fleet):
    other = Workspace.objects.create(slug="elsewhere", display_name="E", created_by=fleet["jj"])
    Agent.objects.create(slug="zed", name="Zed", workspace=other)
    res = _admin_client(fleet).post("/api/workspaces/connect/admission-rules",
                                    {"agent": "zed", "origin": "email", "reason": "x"},
                                    content_type="application/json")
    assert res.status_code == 422


# --- run in auto mode where nobody is watching ------------------------------------

def _auto_on_cloud(fleet, **kw):
    return AdmissionRule.objects.create(
        workspace=fleet["ws"], agent=fleet["hal"], runner_kind="cloud",
        action=AdmissionRule.AUTO, reason="no laptop is free, so nobody would approve it", **kw)


def test_an_auto_rule_runs_the_unwatched_cloud_turn_in_auto_instead_of_rejecting_it(
        fleet, django_capture_on_commit_callbacks):
    rule = _auto_on_cloud(fleet)
    _route(fleet["hal"], fleet["cloud"])
    turn = _mail(fleet["hal"])
    claimed = _claim(fleet["cloud"], django_capture_on_commit_callbacks)
    assert claimed is not None and claimed.pk == turn.pk
    assert claimed.turn_mode == "auto" and f"admission rule {rule.pk}" in claimed.turn_mode_basis
    assert not AdmissionNotice.objects.filter(turn=turn).exists()  # nothing was rejected
    rule.refresh_from_db()
    assert rule.fired_count == 1
    from apps.events.models import Event

    assert Event.objects.filter(kind="admission.auto", payload__turn_id=str(turn.pk)).exists()


def test_on_a_watched_laptop_the_auto_rule_does_not_apply_and_the_turn_stays_manual(
        fleet, django_capture_on_commit_callbacks):
    rule = _auto_on_cloud(fleet)
    _route(fleet["hal"], fleet["laptop"])
    turn = _mail(fleet["hal"])
    claimed = _claim(fleet["laptop"], django_capture_on_commit_callbacks)
    assert claimed.pk == turn.pk and claimed.turn_mode == "manual"
    rule.refresh_from_db()
    assert rule.fired_count == 0


def test_an_auto_rule_never_raises_a_manual_someone_asked_for(fleet, django_capture_on_commit_callbacks):
    _auto_on_cloud(fleet)
    _route(fleet["hal"], fleet["cloud"])
    from apps.harness import initiator as who

    turn, _ = services.enqueue_turn(agent=fleet["hal"], origin=Turn.ORIGIN_API,
                                    idempotency_key="dispatch-manual", prompt="do it",
                                    initiator=who.for_user(fleet["jj"], via="api", assurance="pat"))
    Turn.objects.filter(pk=turn.pk).update(requested_turn_mode="manual",
                                           requested_turn_mode_by=fleet["jj"])
    claimed = _claim(fleet["cloud"], django_capture_on_commit_callbacks)
    assert claimed.pk == turn.pk and claimed.turn_mode == "manual"
    assert "dispatch by jj@dimagi.com" in claimed.turn_mode_basis


def test_an_auto_rule_cannot_be_about_refused_or_unclaimed_turns(fleet):
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        AdmissionRule.objects.create(workspace=fleet["ws"], sender=AdmissionRule.BLOCKED,
                                     action=AdmissionRule.AUTO, reason="x")


# --- a thread first handled manual on a laptop, then claimed by the cloud ---------
# canopy-web#1474: the session rung kept every later turn manual and `auto_rule`
# refused to raise it, so a cloud runner claimed the turn in manual, nobody could
# approve the reply, and the turn ended `done` having sent nothing.

def _owner_mail(fleet, **kw):
    from allauth.account.models import EmailAddress

    EmailAddress.objects.get_or_create(user=fleet["jj"], email="jj@dimagi.com",
                                       defaults={"verified": True, "primary": True})
    return _mail(fleet["hal"], sender="Jonathan Jackson <jj@dimagi.com>", **kw)


def _thread_started_manual(turn):
    """An earlier turn on the same email thread (its chat session) ran manual."""
    assert turn.chat_session_id  # an email thread is one session
    Turn.objects.create(chat_session_id=turn.chat_session_id, origin=Turn.ORIGIN_EMAIL,
                        idempotency_key=f"first-{turn.pk}", status=Turn.DONE,
                        turn_mode="manual")


def test_a_verified_full_sender_on_a_thread_that_started_manual_is_raised_on_the_cloud(
        fleet, django_capture_on_commit_callbacks):
    rule = _auto_on_cloud(fleet)
    _route(fleet["hal"], fleet["cloud"])
    turn = _owner_mail(fleet)
    assert not turn.capability  # the owner: the agent's whole profile
    _thread_started_manual(turn)
    claimed = _claim(fleet["cloud"], django_capture_on_commit_callbacks)
    assert claimed is not None and claimed.pk == turn.pk
    assert claimed.turn_mode == "auto" and f"admission rule {rule.pk}" in claimed.turn_mode_basis


def test_on_a_laptop_the_thread_that_started_manual_stays_manual(
        fleet, django_capture_on_commit_callbacks):
    _auto_on_cloud(fleet)
    _route(fleet["hal"], fleet["laptop"])
    turn = _owner_mail(fleet)
    _thread_started_manual(turn)
    claimed = _claim(fleet["laptop"], django_capture_on_commit_callbacks)
    assert claimed.pk == turn.pk
    assert (claimed.turn_mode, claimed.turn_mode_basis) == ("manual", "session started manual")


def test_an_unverified_message_on_a_thread_that_started_manual_is_not_raised(
        fleet, django_capture_on_commit_callbacks):
    rule = _auto_on_cloud(fleet)
    _route(fleet["hal"], fleet["cloud"])
    turn = _owner_mail(fleet, headers=[])  # unproven, so a contact, confined
    _thread_started_manual(turn)
    assert _claim(fleet["cloud"], django_capture_on_commit_callbacks) is None
    turn.refresh_from_db()
    # Not raised: it stays manual, so the confined-on-cloud rule rejects it out loud.
    assert turn.status == Turn.CANCELLED and turn.turn_mode != "auto"
    rule.refresh_from_db()
    assert rule.fired_count == 0


def test_a_confined_caller_on_a_thread_that_started_manual_is_not_raised(
        fleet, django_capture_on_commit_callbacks):
    rule = _auto_on_cloud(fleet)
    _route(fleet["hal"], fleet["cloud"])
    turn = _mail(fleet["hal"])  # VIKRAM: confined to `ask`
    assert turn.capability == "ask"
    _thread_started_manual(turn)
    _claim(fleet["cloud"], django_capture_on_commit_callbacks)
    turn.refresh_from_db()
    assert turn.turn_mode != "auto"
    rule.refresh_from_db()
    assert rule.fired_count == 0
