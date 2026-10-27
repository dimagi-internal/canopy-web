"""Agent triggers — docs/superpowers/specs/2026-10-27-agent-triggers-design.md.

A trigger starts a turn on its agent with the authority of the person who set it
up, never more. These pin:

* aligned mail from an `inbound_mail` trigger's bound sender fires it — a SYSTEM turn
  accountable to its creator, naming the trigger — and every way that must NOT
  happen (unaligned, other subject, another agent's trigger, ambiguity, a member);
* the turn mode a trigger asks for, bounded by its creator;
* a trigger whose creator lost the right stops firing, loudly (refused + alert);
* the one Triggers API and its gates; the cron-only surfaces never see mail triggers.

Origin: every CloudWatch alarm to Hal was cancelled at enqueue for nine days
(2026-09-28 → 10-07): `no-reply@sns.amazonaws.com` was an unlisted contact under
Hal's ask-only interface. It was then made a "system account" (a fake member);
it is now Hal's trigger.
"""
from __future__ import annotations

import datetime as dt

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.interface import parse
from apps.agents.models import Agent, AgentAdmin
from apps.attention.models import Alert
from apps.harness import alerts_trigger, caller_context, schedule_turns, services, triggers, turn_mode
from apps.harness.models import AgentSchedule, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership

SNS = "no-reply@sns.amazonaws.com"
#: Hal's real interface shape: ask-only, Dimagi callers. SNS is not listed.
HAL = {"capabilities": {"ask": {"description": "Ask", "callers": ["member@dimagi.com",
                                                                  "contact@dimagi.com:verified"],
                                "tools": ["Read"]}}}


def _dkim(domain):
    """What Gmail stamps on real SNS alarm mail: DKIM signed by the From: domain."""
    return [{"name": "Authentication-Results",
             "value": f"mx.google.com; dkim=pass header.i=@{domain} header.d={domain}; "
                      f"spf=pass smtp.mailfrom=bounce.{domain}"}]


@pytest.fixture()
def w():
    boss = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    op = User.objects.create_user("op", "op@dimagi.com", "pw")
    ed = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=boss)
    for u, role in ((boss, M.OWNER), (op, M.EDITOR), (ed, M.EDITOR)):
        M.objects.create(user=u, workspace=ws, role=role)
        EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=op, turn_mode="auto")
    agent.interface = parse(HAL)
    agent.save(update_fields=["interface"])
    other = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=op)
    return {"ws": ws, "agent": agent, "other": other, "boss": boss, "op": op, "ed": ed}


def _mail_trigger(agent, by, *, pattern=r'^(ALARM|OK): "labs-', turn_mode="manual", **extra):
    return AgentSchedule.objects.create(
        agent=agent, kind=AgentSchedule.INBOUND_MAIL, name=extra.pop("name", "AWS CloudWatch alarms"),
        sender_address=SNS, subject_pattern=pattern, turn_mode=turn_mode,
        created_by=by, updated_by=by, **extra)


@pytest.fixture()
def alarms(w):
    """Set up by the agent's owner (an agent admin), as the migration records it."""
    return _mail_trigger(w["agent"], w["op"])


def _alarm(w, key, *, subject='ALARM: "labs-jj-web-cpu-high-actionable" in US East',
           sender=SNS, aligned=True, agent=None, prompt="/hal:turn"):
    ref = {"from": sender, "thread_id": f"t-{key}", "subject": subject}
    if aligned:
        ref["headers"] = _dkim(sender.rpartition("@")[2])
    turn, _ = services.enqueue_turn(agent=agent or w["agent"], origin=Turn.ORIGIN_EMAIL,
                                    idempotency_key=f"email-{key}", origin_ref=ref, prompt=prompt)
    return turn


# --- inbound mail fires the agent's trigger --------------------------------------------

def test_without_a_trigger_an_alarm_is_refused_and_the_refusal_names_the_fix(w):
    t = _alarm(w, "unbound")
    assert t.status == Turn.CANCELLED
    assert "inbound-mail trigger" in t.result_note


def test_a_bound_alarm_fires_the_trigger_as_a_system_turn_accountable_to_its_creator(w, alarms):
    t = _alarm(w, "bound")
    assert t.status == Turn.QUEUED, t.result_note
    assert (t.initiator_kind, t.initiator_via) == ("system", f"trigger:{alarms.pk}")
    assert t.initiator_user_id == w["op"].pk                 # who set it up
    assert t.initiator_contact.email == SNS                  # the sender rides along
    assert t.origin == Turn.ORIGIN_EMAIL                     # routing rules still key on mail
    assert t.origin_ref["trigger_id"] == alarms.pk
    env = caller_context.build(t)
    assert env["relationship"] == "system" and env["profile"] == "full"
    expected = {"id": alarms.pk, "name": "AWS CloudWatch alarms", "kind": "inbound_mail",
                "set_up_by": "op@dimagi.com"}
    assert env["who"]["trigger"] == expected
    assert env["trigger"]["agent_trigger"] == expected
    # Migrated triggers ask for manual: the posture a system account (an editor) had.
    assert turn_mode.for_turn(t, fresh=True).mode == "manual"


def test_a_person_has_no_trigger_in_the_envelope(w):
    ref = {"from": "ed@dimagi.com", "thread_id": "t-ed", "subject": "hi",
           "headers": [{"name": "Authentication-Results",
                        "value": "mx.google.com; dkim=pass; spf=pass; dmarc=pass header.from=dimagi.com"}]}
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_EMAIL,
                                 idempotency_key="email-ed", origin_ref=ref, prompt="x")
    env = caller_context.build(t)
    assert t.initiator_kind == "user"
    assert "trigger" not in env["who"] and env["trigger"]["agent_trigger"] is None


def test_the_triggers_own_prompt_leads_the_mail_turns(w):
    _mail_trigger(w["agent"], w["op"], prompt="/hal:labs-perf")
    t = _alarm(w, "prompted", prompt="/hal:turn")
    assert t.prompt == "/hal:labs-perf\n\n/hal:turn"


def test_the_same_message_twice_is_one_turn(w, alarms):
    assert _alarm(w, "again").pk == _alarm(w, "again").pk


def test_a_subject_outside_the_pattern_stays_a_contact(w, alarms):
    t = _alarm(w, "other-alarm", subject='ALARM: "someone-elses-alarm" in US East')
    assert t.status == Turn.CANCELLED and t.initiator_kind == "contact"


def test_unaligned_mail_from_the_bound_address_does_not_fire_it(w, alarms):
    """`From:` is forgeable; only THIS message's alignment ties it to the address."""
    t = _alarm(w, "forged", aligned=False)
    assert t.initiator_kind == "contact" and t.status == Turn.CANCELLED


def test_another_agents_trigger_does_not_apply(w):
    _mail_trigger(w["other"], w["op"])
    t = _alarm(w, "elsewhere")
    assert t.initiator_kind == "contact" and t.status == Turn.CANCELLED


def test_a_paused_trigger_does_not_fire(w, alarms):
    alarms.enabled = False
    alarms.save(update_fields=["enabled"])
    assert _alarm(w, "paused").initiator_kind == "contact"


def test_two_triggers_matching_one_message_fire_neither(w, alarms):
    _mail_trigger(w["agent"], w["op"], name="All SNS", pattern="")
    assert _alarm(w, "ambiguous").initiator_kind == "contact"


def test_a_members_own_mail_is_theirs_never_a_triggers(w):
    AgentSchedule.objects.create(agent=w["agent"], kind=AgentSchedule.INBOUND_MAIL, name="ed",
                                 sender_address="ed@dimagi.com", created_by=w["op"])
    ref = {"from": "ed@dimagi.com", "thread_id": "t-m", "subject": "x",
           "headers": [{"name": "Authentication-Results",
                        "value": "mx.google.com; dkim=pass; dmarc=pass header.from=dimagi.com"}]}
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_EMAIL,
                                 idempotency_key="email-m", origin_ref=ref, prompt="x")
    assert (t.initiator_kind, t.initiator_user_id) == ("user", w["ed"].pk)


# --- turn mode -------------------------------------------------------------------------

def test_a_trigger_that_asks_nothing_follows_the_agent_bounded_by_its_creator(w):
    _mail_trigger(w["agent"], w["op"], turn_mode="")
    assert turn_mode.for_turn(_alarm(w, "admin"), fresh=True).mode == "auto"   # owner: agent's auto


def test_auto_holds_only_while_its_creator_is_an_agent_admin(w):
    _mail_trigger(w["agent"], w["op"], turn_mode="auto")
    t = _alarm(w, "auto")
    assert turn_mode.for_turn(t, fresh=True).mode == "auto"
    w["agent"].owner = w["boss"]          # op is no longer an admin of hal
    w["agent"].save(update_fields=["owner"])
    # The trigger has stopped altogether (an inbound trigger needs an agent admin)…
    assert "no longer an admin of hal" in triggers.refusal(AgentSchedule.objects.get(turn_mode="auto"))
    # …and a turn it fired before, still queued, is withheld auto at its claim.
    resolved = turn_mode.for_turn(Turn.objects.get(pk=t.pk), fresh=True)
    assert resolved.mode == "manual" and "auto withheld" in resolved.basis


# --- authority: a stopped trigger fails loudly ------------------------------------------

def test_a_creator_who_leaves_stops_the_trigger_loudly(w, alarms):
    M.objects.filter(user=w["op"], workspace=w["ws"]).delete()
    assert "no longer a member of connect" in triggers.refusal(alarms)
    # The mail stays the contact it is…
    assert _alarm(w, "orphaned").initiator_kind == "contact"
    # …and an alert says so, on the agent's Triggers tab, to whoever may fix it.
    raised = alerts_trigger.reconcile(timezone.now())
    assert [r.subject_key for r in raised] == [f"trigger:{alarms.pk}"]
    assert raised[0].url == "/w/connect/agents/hal/triggers"
    from apps.attention import alerts

    alerts.apply_reconcile(alerts_trigger.KIND, raised, timezone.now())
    alert = Alert.objects.get(kind=alerts_trigger.KIND)
    assert [a.id for a in alerts_trigger.actions(alert, w["boss"])] == ["triggers"]
    assert alerts_trigger.actions(alert, w["ed"]) == []        # an editor may not set one up


def test_a_cron_trigger_whose_creator_lost_the_right_does_not_fire_and_consumes_its_slot(w):
    sched = AgentSchedule.objects.create(agent=w["agent"], name="weekly", prompt="/hal:turn",
                                         cron="0 9 * * 5", created_by=w["ed"])
    M.objects.filter(user=w["ed"], workspace=w["ws"]).update(role=M.VIEWER)
    slot = dt.datetime(2026, 10, 30, 9, tzinfo=dt.UTC)
    with pytest.raises(triggers.TriggerRefused, match="may no longer direct hal's work"):
        schedule_turns.fire_schedule(sched, slot)
    sched.refresh_from_db()
    assert sched.last_slot == slot and not Turn.objects.exists()


def test_a_pre_attribution_cron_row_keeps_firing(w):
    sched = AgentSchedule.objects.create(agent=w["agent"], name="old", prompt="/hal:turn",
                                         cron="0 9 * * 5")
    assert triggers.refusal(sched) is None
    turn, created = schedule_turns.fire_schedule(sched, dt.datetime(2026, 10, 30, 9, tzinfo=dt.UTC))
    assert created and turn.initiator_kind == "system"


# --- the Triggers API ------------------------------------------------------------------

def _c(user):
    c = Client()
    c.force_login(user)
    return c


MAIL = {"kind": "inbound_mail", "name": "Alarms", "sender_address": "No-Reply@SNS.amazonaws.com",
        "subject_pattern": "^ALARM", "turn_mode": "manual"}
CRON = {"name": "Weekly", "prompt": "/hal:turn", "cron": "0 9 * * 5"}


def test_an_agent_admin_makes_an_inbound_mail_trigger_and_an_editor_may_not(w):
    r = _c(w["ed"]).post("/api/agents/hal/triggers/", MAIL, content_type="application/json")
    assert r.status_code == 403 and "admin" in r.json()["detail"]
    r = _c(w["op"]).post("/api/agents/hal/triggers/", MAIL, content_type="application/json")
    assert r.status_code == 201, r.content
    body = r.json()
    assert (body["kind"], body["sender_address"], body["created_by_email"]) == (
        "inbound_mail", SNS, "op@dimagi.com")
    assert body["stopped"] is None and body["next_runs"] == []


def test_an_editor_makes_a_cron_trigger_but_not_an_auto_one(w):
    c = _c(w["ed"])
    assert c.post("/api/agents/hal/triggers/", CRON, content_type="application/json").status_code == 201
    r = c.post("/api/agents/hal/triggers/", {**CRON, "name": "Auto", "turn_mode": "auto"},
               content_type="application/json")
    assert r.status_code == 403


def test_bad_trigger_input_is_refused(w):
    c = _c(w["op"])
    for bad in ({**MAIL, "sender_address": "not-an-address"}, {**MAIL, "subject_pattern": "("},
                {**MAIL, "sender_address": ""}, {**CRON, "prompt": ""},
                {**MAIL, "cron": "0 9 * * 5"}):
        r = c.post("/api/agents/hal/triggers/", bad, content_type="application/json")
        assert r.status_code == 422, (bad, r.content)
    tid = c.post("/api/agents/hal/triggers/", MAIL, content_type="application/json").json()["id"]
    r = c.patch(f"/api/agents/hal/triggers/{tid}", {"kind": "cron"}, content_type="application/json")
    assert r.status_code == 422


def test_one_list_of_every_kind_and_the_cron_surfaces_never_see_mail(w, alarms):
    AgentSchedule.objects.create(agent=w["agent"], name="weekly", prompt="/hal:turn",
                                 cron="0 9 * * 5", created_by=w["op"])
    c = _c(w["op"])
    kinds = sorted(t["kind"] for t in c.get("/api/agents/hal/triggers/").json()["items"])
    assert kinds == ["cron", "inbound_mail"]
    assert [s["name"] for s in c.get("/api/agents/hal/schedules/").json()["items"]] == ["weekly"]
    # /schedules/{id} does not reach a mail trigger; run-now only fires cron.
    assert c.patch(f"/api/agents/hal/schedules/{alarms.pk}", {"enabled": False},
                   content_type="application/json").status_code == 404
    assert c.post(f"/api/agents/hal/triggers/{alarms.pk}/run-now").status_code == 404


def test_pause_edit_and_delete_through_the_triggers_api(w, alarms):
    c = _c(w["op"])
    r = c.patch(f"/api/agents/hal/triggers/{alarms.pk}", {"enabled": False, "subject_pattern": "^OK"},
                content_type="application/json")
    assert r.status_code == 200 and r.json()["subject_pattern"] == "^OK" and not r.json()["enabled"]
    assert _c(w["ed"]).delete(f"/api/agents/hal/triggers/{alarms.pk}").status_code == 403
    assert c.delete(f"/api/agents/hal/triggers/{alarms.pk}").status_code == 204
    assert not AgentSchedule.objects.filter(pk=alarms.pk).exists()


def test_run_now_on_a_stopped_trigger_says_why(w):
    sched = AgentSchedule.objects.create(agent=w["agent"], name="weekly", prompt="/hal:turn",
                                         cron="0 9 * * 5", created_by=w["ed"])
    AgentAdmin.objects.create(agent=w["agent"], user=w["ed"], granted_by=w["op"])
    w["ed"].is_active = False
    w["ed"].save(update_fields=["is_active"])
    r = _c(w["op"]).post(f"/api/agents/hal/triggers/{sched.pk}/run-now")
    assert r.status_code == 409 and "deactivated" in r.json()["detail"]
    listed = _c(w["op"]).get("/api/agents/hal/triggers/").json()["items"][0]
    assert "deactivated" in listed["stopped"]
