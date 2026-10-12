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
import urllib.error
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.utils import timezone

from apps.admission import defaults, gmail, notices, runner_notices
from apps.admission.models import AdmissionNotice, AdmissionRule
from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.harness import services
from apps.harness.claim import claim_next_turn
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.harness.routing import ENVELOPE_VERSION, PROFILES_VERSION
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.testing import enrolled

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
                               turn_mode=Agent.MANUAL, interface=parse(CONFINING))
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
                    "blocked-sender": True, "offered-nothing": True}
    assert defaults.seed(ws.pk) == 0


def test_the_data_migration_seeds_every_existing_workspace(fleet):
    import importlib

    from django.apps import apps as registry

    AdmissionRule.objects.all().delete()
    seed = importlib.import_module("apps.admission.migrations.0002_seed_default_rules").seed
    seed(registry, None)
    assert set(AdmissionRule.objects.filter(workspace=fleet["ws"]).values_list("seed_key", flat=True)) \
        == {"email-confined-manual-cloud", "email-confined-manual-laptop"}


def test_the_refusal_migration_seeds_only_its_own_rules(fleet):
    import importlib

    from django.apps import apps as registry

    ws = fleet["ws"]
    AdmissionRule.objects.filter(workspace=ws, seed_key__in=("blocked-sender", "offered-nothing")).delete()
    _seeded(ws, "email-confined-manual-laptop").delete()  # a workspace removed a default
    importlib.import_module("apps.admission.migrations.0005_seed_refusal_rules").seed(registry, None)
    assert set(AdmissionRule.objects.filter(workspace=ws).values_list("seed_key", flat=True)) == {
        "email-confined-manual-cloud", "blocked-sender", "offered-nothing"}  # not brought back


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


def test_a_noreply_sender_gets_no_notice(fleet, django_capture_on_commit_callbacks):
    # A no-reply address cannot be a verified dimagi.com contact in the ask
    # list, so give it a rule of its own.
    hal = fleet["hal"]
    hal.interface = parse({"capabilities": {"ask": {"callers": ["contact"]}}})
    hal.save(update_fields=["interface"])
    notice = _reject_on_cloud(fleet, django_capture_on_commit_callbacks,
                              sender="no-reply@example.org", headers=[])
    assert notice.status == AdmissionNotice.SKIPPED and "no-reply" in notice.detail


@pytest.fixture
def sending(settings):
    settings.CANOPY_ADMISSION_SENDER_NOTICES = True
    with mock.patch.object(gmail, "send_reply", return_value="g-1") as send:
        yield send


def test_with_replies_on_the_sender_is_answered_once_per_thread(
        fleet, sending, django_capture_on_commit_callbacks):
    first = _reject_on_cloud(fleet, django_capture_on_commit_callbacks, thread="t-9", count=1)
    assert first.status == AdmissionNotice.SENT
    kwargs = sending.call_args.kwargs
    assert kwargs["to"] == VIKRAM and kwargs["thread_id"] == "t-9" and kwargs["message_id"] == "m-1"
    assert "automatic reply" in kwargs["body"] and "Jonathan Jackson" in kwargs["body"]

    # The same poll again: the same idempotency key, so no new turn and no notice.
    assert _mail(fleet["hal"], thread="t-9", count=1).pk == first.turn_id
    # A new message on the thread inside 24h: rejected again, but not re-answered.
    second = _reject_on_cloud(fleet, django_capture_on_commit_callbacks, thread="t-9", count=2)
    assert second.status == AdmissionNotice.SKIPPED and "24 hours" in second.detail
    assert sending.call_count == 1


def test_an_automated_message_is_never_answered(fleet, sending, django_capture_on_commit_callbacks):
    sending.side_effect = gmail.Automated("the message carries automation headers")
    notice = _reject_on_cloud(fleet, django_capture_on_commit_callbacks)
    assert notice.status == AdmissionNotice.SKIPPED and "automation" in notice.detail


def test_gmail_refuses_an_auto_submitted_message():
    assert gmail._is_automated({"auto-submitted": "auto-replied"})
    assert gmail._is_automated({"precedence": "bulk"})
    assert gmail._is_automated({"list-id": "<x.list>"})
    assert not gmail._is_automated({"auto-submitted": "no"})


def test_a_mailbox_canopy_cannot_send_from_is_left_for_the_agents_runner(
        fleet, settings, django_capture_on_commit_callbacks):
    # canopy-web#1416: hal's token is on the desktop `canopy` client (here: no
    # token at all), so canopy-web cannot send. The notice is not given up on —
    # it waits for the runner that reads hal's mailbox, carrying what it needs.
    settings.CANOPY_ADMISSION_SENDER_NOTICES = True
    fleet["hal"].email = "hal@dimagi-ai.com"
    fleet["hal"].save(update_fields=["email"])
    notice = _reject_on_cloud(fleet, django_capture_on_commit_callbacks)  # no gog-token stored
    assert notice.status == AdmissionNotice.PENDING and "gog-token" in notice.detail
    assert (notice.mailbox, notice.message_id, notice.subject) == ("hal@dimagi-ai.com", "m-1", "Can Hal help?")
    assert notice.body == notices.sender_text(fleet["hal"])


# --- the runner sends what canopy-web could not (canopy-web#1416) --------------------

@pytest.fixture
def pending(fleet, settings, django_capture_on_commit_callbacks):
    settings.CANOPY_ADMISSION_SENDER_NOTICES = True
    fleet["hal"].email = "hal@dimagi-ai.com"
    fleet["hal"].save(update_fields=["email"])
    with mock.patch.object(gmail, "send_reply", side_effect=gmail.Unavailable("desktop client")):
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

    # A day on, nobody managed it: it ends failed, saying why, on the heartbeat.
    AdmissionNotice.objects.filter(pk=pending.pk).update(
        created_at=timezone.now() - runner_notices.EXPIRES - dt.timedelta(minutes=1))
    runner_notices._last_expiry = None
    services.heartbeat(laptop, active_turn_ids=[])
    pending.refresh_from_db()
    assert pending.status == AdmissionNotice.FAILED
    assert "a runner tried and failed within 24 hours" in pending.detail


def test_a_pending_notice_silences_the_thread_like_a_sent_one(
        fleet, pending, django_capture_on_commit_callbacks):
    with mock.patch.object(gmail, "send_reply", side_effect=gmail.Unavailable("desktop client")):
        second = _reject_on_cloud(fleet, django_capture_on_commit_callbacks, thread="t-5", count=2)
    assert second.status == AdmissionNotice.SKIPPED and "24 hours" in second.detail


def test_gmail_raises_unavailable_only_when_nothing_was_sent():
    agent = mock.Mock(name="agent")
    agent.name = "Hal"
    meta = {"payload": {"headers": [{"name": "Message-ID", "value": "<m@x>"}]}}
    refused = urllib.error.HTTPError("u", 400, "bad", {}, None)
    with mock.patch.object(gmail, "access_token", return_value=("tok", "hal@dimagi-ai.com")), \
            mock.patch.object(gmail, "_call", side_effect=[meta, refused]):
        with pytest.raises(gmail.Unavailable, match="HTTP 400"):
            gmail.send_reply(agent, thread_id="t", message_id="m", to=VIKRAM, subject="s", body="b")
    # Sent, then marking it read failed: that is a sent reply, never a retry.
    with mock.patch.object(gmail, "access_token", return_value=("tok", "hal@dimagi-ai.com")), \
            mock.patch.object(gmail, "_call", side_effect=[meta, {"id": "g-1"}, urllib.error.URLError("x")]):
        assert gmail.send_reply(agent, thread_id="t", message_id="m", to=VIKRAM,
                                subject="s", body="b") == "g-1"


def test_a_send_that_may_have_gone_is_not_retried(fleet, settings, django_capture_on_commit_callbacks):
    settings.CANOPY_ADMISSION_SENDER_NOTICES = True
    with mock.patch.object(gmail, "send_reply", side_effect=TimeoutError("read timed out")):
        notice = _reject_on_cloud(fleet, django_capture_on_commit_callbacks)
    assert notice.status == AdmissionNotice.FAILED and "never answered twice" in notice.detail


# --- the refusals canopy always makes, as rules (canopy-web#1416, #1400 item 6) ----------

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
    hal.interface = parse({})  # no interface: a contact is offered nothing
    hal.save(update_fields=["interface"])
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


def test_a_rule_cannot_name_another_workspaces_agent(fleet):
    other = Workspace.objects.create(slug="elsewhere", display_name="E", created_by=fleet["jj"])
    Agent.objects.create(slug="zed", name="Zed", workspace=other)
    res = _admin_client(fleet).post("/api/workspaces/connect/admission-rules",
                                    {"agent": "zed", "origin": "email", "reason": "x"},
                                    content_type="application/json")
    assert res.status_code == 422
