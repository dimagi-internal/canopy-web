"""The people digest v2 (canopy#820): one batched, scheduled, cloud-only turn.

v1 (canopy#804) started a digest session per finished human turn. In one night
it counted agent dispatches as the human talking, and opened unaccounted
`c-canopy-people-digest-*` sessions on the owner's laptop. These tests pin the
three fixes and the switch:

  NO POST-FINISH TRIGGER. Finishing a person's turn starts nothing.

  ONE DEFINITION OF A CONVERSATION. `people.real_conversation_q`: a human, through
  chat / widget, email or Slack. Every exclusion is a test below — api dispatches
  (even in a human's name), ace-web, huddles, task approvals, schedules, digests,
  agent logins and mailboxes, system accounts, PATs, MCP calls, transfers.

  A WORK LIST, NOT A SESSION. `GET /api/people/digest-candidates/?agent=` names
  who has talked to the agent since it last digested them; the daily sweep
  enqueues ONE turn per agent, and none when the list is empty.

  NEVER A LAPTOP. The turn is `cloud_only`: an emdash or remote runner cannot
  claim it — assigned, ranked first, or pinned — and nothing claims it while the
  fleet is held.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone

from apps.agents.models import AgentAdmin, AgentTask
from apps.contacts import people
from apps.contacts import services as contacts
from apps.contacts.models import PersonDigestMark
from apps.harness import initiator as who
from apps.harness import people_digest, services
from apps.harness.claim import unclaimable_queued_turns
from apps.harness.models import FleetHold, Runner, RunnerAssignment, Turn
from apps.inbound.models import InboundMailbox
from apps.workspaces import system_accounts
from apps.workspaces.models import WorkspaceMembership
from tests.test_people_brain import _agent, _client, _digest_turns, _finish, _human_turn, _member

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _digest_on(settings):
    # OFF in production (Jonathan flips it); these tests pin what it does ON.
    settings.PEOPLE_DIGEST_ENABLED = True
    cache.clear()


@pytest.fixture()
def world():
    from apps.workspaces.models import Workspace

    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    lili = _member(ws, "lili", first_name="Lilianna", last_name="Bagnoli")
    return {"owner": owner, "ws": ws, "lili": lili,
            "ace": _agent(ws, "ace", owner), "hal": _agent(ws, "hal", owner)}


def _turn(agent, key, *, initiator, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, **kw):
    turn, _ = services.enqueue_turn(agent=agent, origin=origin, idempotency_key=key,
                                    prompt=kw.pop("prompt", "hi"), initiator=initiator, **kw)
    return turn


def _counted(agent) -> set[str]:
    return set(Turn.objects.filter(people.with_agent_q(agent))
               .filter(people.real_conversation_q())
               .values_list("idempotency_key", flat=True))


# --- 1. no post-finish trigger -----------------------------------------------------------


def test_finishing_a_persons_turn_starts_nothing(world):
    _finish(_human_turn(world["ace"], world["lili"], "t1"))
    assert not _digest_turns().exists()
    assert not hasattr(people_digest, "on_turn_finished")


# --- 2. what counts as a conversation ------------------------------------------------------


def test_chat_widget_email_and_slack_from_a_person_count(world, settings):
    ace, lili, ws = world["ace"], world["lili"], world["ws"]
    _turn(ace, "chat", initiator=who.for_user(lili, via="chat", assurance=who.SESSION))
    fatima = contacts.record_inbound_sender(workspace=ws, address="fatima@llo.org")
    _turn(ace, "widget", initiator=who.for_contact(fatima, via="widget:connect-labs"))
    _turn(ace, "email", origin=Turn.ORIGIN_EMAIL, initiator=who.for_contact(fatima, via="email"))
    _turn(ace, "slack", origin=Turn.ORIGIN_SLACK,
          initiator=who.for_user(lili, via="slack", assurance=who.SLACK_LINKED))
    assert _counted(ace) == {"chat", "widget", "email", "slack"}


def test_dispatches_schedules_and_ace_web_do_not_count_even_in_a_humans_name(world):
    ace, lili = world["ace"], world["lili"]
    as_lili = who.for_user(lili, via="api", assurance=who.SESSION)
    _turn(ace, "api", origin=Turn.ORIGIN_API, initiator=as_lili)            # a dispatch
    _turn(ace, "ace-web", origin=Turn.ORIGIN_ACE_WEB, initiator=as_lili)    # a run dispatch
    _turn(ace, "sched", origin=Turn.ORIGIN_CANOPY_SCHEDULER,
          initiator=who.system(via="schedule", accountable=lili))
    _turn(ace, "by-agent", initiator=who.for_agent("hal", via="dispatch"))
    assert _counted(ace) == set()


def test_huddle_rounds_approvals_and_digest_turns_do_not_count(world):
    ace, lili = world["ace"], world["lili"]
    chat = who.for_user(lili, via="chat", assurance=who.SESSION)
    _turn(ace, "round", initiator=chat, origin_ref={"kind": "huddle_round", "huddle": 1})
    _turn(ace, "anchor", initiator=chat, origin_ref={"kind": "huddle"})
    task = AgentTask.objects.create(agent=ace, ext_id="T1", title="ship it", origin="manual",
                                    ask_kind=AgentTask.ASK_REVIEW, idempotency_key="task-1")
    t = _turn(ace, "raised", initiator=chat)
    Turn.objects.filter(pk=t.pk).update(raised_from_task=task)
    _turn(ace, "approved", initiator=who.for_user(lili, via="approval", assurance=who.APPROVAL))
    _turn(ace, "digest", initiator=chat, origin_ref={"trigger": people_digest.TRIGGER})
    # An unrelated origin_ref key is still an ordinary conversation (the negated
    # JSON lookups are guarded: a bare one would drop every turn without the key).
    _turn(ace, "plain", initiator=chat, origin_ref={"client_id": "x"})
    assert _counted(ace) == {"plain"}


def test_programs_in_a_persons_name_do_not_count(world):
    ace, lili = world["ace"], world["lili"]
    _turn(ace, "pat", initiator=who.for_user(lili, via="chat", assurance=who.PAT))
    _turn(ace, "mcp", initiator=who.for_user(lili, via="mcp:canopy_sessions_send",
                                             assurance=who.SESSION))
    _turn(ace, "transfer", initiator=who.for_user(lili, via="transfer", assurance=who.SESSION))
    assert _counted(ace) == set()


def test_agents_and_system_accounts_are_not_people(world):
    ace, hal, ws = world["ace"], world["hal"], world["ws"]
    _turn(ace, "hal-login", initiator=who.for_user(hal.user, via="chat", assurance=who.SESSION))
    bot = system_accounts.create(ws, name="CloudWatch", by=world["owner"])
    _turn(ace, "system", initiator=who.for_user(bot.user, via="chat", assurance=who.SESSION))
    # Another agent WRITING IN: its mailbox, or an address that is an agent's login.
    InboundMailbox.objects.create(address="Eva@Dimagi-AI.com", agent=hal)
    eva_mail = contacts.record_inbound_sender(workspace=ws, address="eva@dimagi-ai.com")
    _turn(ace, "mailbox", origin=Turn.ORIGIN_EMAIL, initiator=who.for_contact(eva_mail, via="email"))
    hal_mail = contacts.record_inbound_sender(workspace=ws, address="hal@dimagi-ai.com")
    _turn(ace, "login-addr", origin=Turn.ORIGIN_EMAIL, initiator=who.for_contact(hal_mail, via="email"))
    _turn(ace, "nobody", initiator=who.unknown(via="chat"))
    assert _counted(ace) == set()


def test_an_agent_reads_back_only_real_conversations(world):
    ace, lili = world["ace"], world["lili"]
    person = contacts.person_for(user=lili)
    said = _human_turn(ace, lili, "said", prompt="the KC coach is live")
    _turn(ace, "dispatched", origin=Turn.ORIGIN_API, prompt="ACE: rebuild the app",
          initiator=who.for_user(lili, via="api", assurance=who.SESSION))
    rows = _client(ace.user).get(f"/api/people/{person.pk}/conversations/?agent=ace").json()
    assert [r["id"] for r in rows["conversations"]] == [str(said.pk)]


# --- 3. the work list --------------------------------------------------------------------


def test_candidates_route_shape_and_since(world):
    ace, lili, owner = world["ace"], world["lili"], world["owner"]
    person = contacts.person_for(user=lili)
    _human_turn(ace, lili, "l1")
    _human_turn(ace, lili, "l2")
    _human_turn(ace, owner, "j1")
    _turn(ace, "dispatch", origin=Turn.ORIGIN_API,
          initiator=who.for_user(owner, via="api", assurance=who.SESSION))
    before = timezone.now()

    r = _client(ace.user).get("/api/people/digest-candidates/?agent=ace&limit=50")
    assert r.status_code == 200, r.content
    body = r.json()
    assert set(body) == {"agent", "workspace", "candidates"}
    assert (body["agent"], body["workspace"]) == ("ace", "connect")
    by_person = {c["person"]: c for c in body["candidates"]}
    assert set(by_person) == {person.pk, contacts.person_for(user=owner).pk}
    lili_row = by_person[person.pk]
    assert set(lili_row) == {"person", "display_name", "email", "since", "conversations"}
    assert lili_row["display_name"] == "Lilianna Bagnoli"
    assert lili_row["email"] == "lili@dimagi.com"
    assert lili_row["conversations"] == 2
    # Never digested: since = 14 days back.
    since = dt.datetime.fromisoformat(lili_row["since"])
    assert abs((before - people.FIRST_LOOKBACK) - since) < dt.timedelta(minutes=1)
    assert by_person[contacts.person_for(user=owner).pk]["conversations"] == 1  # not the dispatch


def test_a_digest_by_the_agent_moves_only_its_own_watermark(world):
    ace, hal, lili, ws = world["ace"], world["hal"], world["lili"], world["ws"]
    person = contacts.person_for(user=lili)
    t1 = _human_turn(ace, lili, "a1")
    _human_turn(hal, lili, "h1")
    assert len(people.digest_candidates(ace)) == 1

    # hal writes the workspace's digest: ace's list is unchanged, hal's is empty.
    people.put_digest(person=person, workspace=ws, text="Lili leads KC.", by_agent=hal)
    assert len(people.digest_candidates(ace)) == 1
    assert people.digest_candidates(hal) == []

    # ace digests, naming what it read: the mark lands on that turn, not "now".
    people.put_digest(person=person, workspace=ws, text="Lili leads KC.", by_agent=ace,
                      source_turn_ids=[str(t1.pk)])
    mark = PersonDigestMark.objects.get(person=person, agent=ace)
    t1.refresh_from_db()
    assert mark.digested_at == t1.created_at
    assert people.digest_candidates(ace) == []

    # Something new: she is back, `since` is ace's watermark, one conversation.
    _human_turn(ace, lili, "a2")
    (row,) = people.digest_candidates(ace)
    assert dt.datetime.fromisoformat(row["since"]) == mark.digested_at
    assert row["conversations"] == 1


def test_a_watermark_never_moves_backwards(world):
    ace, lili, ws = world["ace"], world["lili"], world["ws"]
    person = contacts.person_for(user=lili)
    old = _human_turn(ace, lili, "old")
    Turn.objects.filter(pk=old.pk).update(created_at=timezone.now() - dt.timedelta(days=2))
    people.put_digest(person=person, workspace=ws, text="x", by_agent=ace)
    first = PersonDigestMark.objects.get(person=person, agent=ace).digested_at
    people.put_digest(person=person, workspace=ws, text="x", by_agent=ace,
                      source_turn_ids=[str(old.pk)])
    assert PersonDigestMark.objects.get(person=person, agent=ace).digested_at == first


def test_conversations_older_than_the_lookback_are_not_candidates(world):
    t = _human_turn(world["ace"], world["lili"], "ancient")
    Turn.objects.filter(pk=t.pk).update(
        created_at=timezone.now() - people.FIRST_LOOKBACK - dt.timedelta(hours=1))
    assert people.digest_candidates(world["ace"]) == []


def test_candidates_are_for_the_agents_login_and_its_admins_only(world):
    ace, hal, ws = world["ace"], world["hal"], world["ws"]
    url = "/api/people/digest-candidates/?agent=ace"
    assert _client(ace.user).get(url).status_code == 200            # its own login
    assert _client(world["owner"]).get(url).status_code == 200      # its owner (an admin)
    admin = _member(ws, "ann")
    AgentAdmin.objects.create(agent=ace, user=admin)
    assert _client(admin).get(url).status_code == 200
    assert _client(hal.user).get(url).status_code == 403            # another agent
    assert _client(world["lili"]).get(url).status_code == 403       # a plain member
    stranger = User.objects.create_user("x", "x@else.org", "pw")
    assert _client(stranger).get(url).status_code == 404            # not a member: no hint
    assert _client(ace.user).get("/api/people/digest-candidates/?agent=nope").status_code == 404
    assert _client(ace.user).get(url + "&limit=0").status_code == 400
    assert _client(ace.user).get(url + "&limit=201").status_code == 400


def test_limit_caps_the_list_most_recent_first(world):
    ace, ws = world["ace"], world["ws"]
    for i in range(3):
        u = _member(ws, f"p{i}")
        t = _human_turn(ace, u, f"k{i}")
        Turn.objects.filter(pk=t.pk).update(created_at=timezone.now() - dt.timedelta(hours=3 - i))
    rows = people.digest_candidates(ace, limit=2)
    assert [r["email"] for r in rows] == ["p2@dimagi.com", "p1@dimagi.com"]


# --- 4. the daily sweep ----------------------------------------------------------------------


def test_the_sweep_enqueues_one_cloud_only_system_turn_per_agent_with_candidates(world):
    ace, hal = world["ace"], world["hal"]
    _human_turn(ace, world["lili"], "t1")
    _human_turn(ace, world["owner"], "t2")

    made = people_digest.sweep()

    assert [t.agent for t in made] == [ace]                         # hal has nobody: no turn
    turn = made[0]
    assert turn.prompt.splitlines()[0] == "/canopy:people-digest --batch --agent ace --workspace connect"
    assert turn.origin_ref == {"trigger": "people_digest", "batch": True, "no_outbound": True,
                               "workspace": "connect"}
    assert turn.initiator_kind == who.SYSTEM and turn.initiator_via == "people-digest"
    assert turn.routing == Turn.CLOUD_ONLY
    assert turn.origin == Turn.ORIGIN_API
    assert people_digest.is_digest_turn(turn)
    assert not _digest_turns().filter(agent=hal).exists()


def test_the_sweep_is_once_per_agent_and_waits_for_the_last_one(world):
    ace = world["ace"]
    _human_turn(ace, world["lili"], "t1")
    assert len(people_digest.sweep()) == 1
    assert people_digest.sweep() == []                              # same day, and still queued
    Turn.objects.filter(idempotency_key__startswith=people_digest.KEY_PREFIX).update(
        status=Turn.DONE)
    assert people_digest.sweep() == []                              # same day: the key collapses it
    tomorrow = timezone.now() + dt.timedelta(days=1)
    assert len(people_digest.sweep(now=tomorrow)) == 1              # candidates still undigested


def test_the_switches(world, settings):
    ace, hal, lili = world["ace"], world["hal"], world["lili"]
    _human_turn(ace, lili, "t1")
    _human_turn(hal, lili, "t2")
    hal.people_digest_enabled = False
    hal.save()
    settings.PEOPLE_DIGEST_ENABLED = False
    assert people_digest.sweep() == [] and people_digest.maybe_sweep() == []
    settings.PEOPLE_DIGEST_ENABLED = True
    assert [t.agent for t in people_digest.sweep()] == [ace]       # hal is opted out


def test_maybe_sweep_waits_for_the_hour_then_runs_once_a_day(world, settings):
    settings.PEOPLE_DIGEST_SWEEP_HOUR_UTC = 6
    _human_turn(world["ace"], world["lili"], "t1")
    day = timezone.now().astimezone(dt.timezone.utc).replace(hour=0, minute=0, second=0,
                                                             microsecond=0)
    assert people_digest.maybe_sweep(day + dt.timedelta(hours=5)) == []
    assert len(people_digest.maybe_sweep(day + dt.timedelta(hours=6, minutes=1))) == 1
    Turn.objects.filter(idempotency_key__startswith=people_digest.KEY_PREFIX).delete()
    assert people_digest.maybe_sweep(day + dt.timedelta(hours=7)) == []    # today is done


def test_the_heartbeat_is_the_clock(world, settings):
    settings.PEOPLE_DIGEST_SWEEP_HOUR_UTC = 0
    _human_turn(world["ace"], world["lili"], "t1")
    runner = _runner(world, "box", Runner.EMDASH)
    services.heartbeat(runner, active_turn_ids=[])
    assert _digest_turns().filter(agent=world["ace"]).count() == 1


def test_the_heartbeat_starts_nothing_while_the_switch_is_off(world, settings):
    settings.PEOPLE_DIGEST_ENABLED = False
    settings.PEOPLE_DIGEST_SWEEP_HOUR_UTC = 0
    _human_turn(world["ace"], world["lili"], "t1")
    services.heartbeat(_runner(world, "box", Runner.EMDASH), active_turn_ids=[])
    assert not _digest_turns().exists()


# --- 5. placement: cloud only, never while held ------------------------------------------------


def _runner(world, name, kind, rank=0):
    owner = world["owner"]
    runner = Runner.objects.create(name=name, kind=kind, host=name, owner=owner,
                                   workspace_id="connect", status=Runner.ONLINE,
                                   last_heartbeat_at=timezone.now(),
                                   location=Runner.CLOUD if kind == Runner.CLOUD else Runner.LOCAL)
    RunnerAssignment.objects.create(agent=world["ace"], runner=runner, rank=rank)
    AgentAdmin.objects.get_or_create(agent=world["ace"], user=owner)
    return runner


def _digest(world):
    _human_turn(world["ace"], world["lili"], "t1")
    Turn.objects.filter(idempotency_key="t1").update(status=Turn.DONE)
    (turn,) = people_digest.sweep()
    return turn


def test_a_laptop_runner_cannot_claim_a_digest_turn(world):
    turn = _digest(world)
    laptop = _runner(world, "jj-mbp-cdp", Runner.EMDASH, rank=0)
    remote = _runner(world, "box", Runner.REMOTE, rank=1)
    assert services.claim_next_turn(laptop) is None
    assert services.claim_next_turn(remote) is None
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED


def test_not_even_a_pin_puts_it_on_a_laptop(world):
    turn = _digest(world)
    laptop = _runner(world, "jj-mbp-cdp", Runner.EMDASH)
    Turn.objects.filter(pk=turn.pk).update(pinned_runner=laptop)
    assert services.claim_next_turn(laptop) is None


def test_a_cloud_runner_claims_it_even_ranked_below_a_laptop(world):
    turn = _digest(world)
    laptop = _runner(world, "jj-mbp-cdp", Runner.EMDASH, rank=0)
    cloud = _runner(world, "cloud-ec2-1", Runner.CLOUD, rank=1)
    assert services.claim_next_turn(laptop) is None
    # The laptop is up and ranked first, so the cascade gives the cloud box the
    # turn once it has waited out the grace.
    Turn.objects.filter(pk=turn.pk).update(created_at=timezone.now() - dt.timedelta(minutes=5))
    claimed = services.claim_next_turn(cloud)
    assert claimed is not None and claimed.pk == turn.pk


def test_nothing_claims_a_digest_turn_while_the_fleet_is_held(world):
    turn = _digest(world)
    cloud = _runner(world, "cloud-ec2-1", Runner.CLOUD)
    FleetHold.objects.update_or_create(pk=FleetHold.SINGLETON_PK, defaults={"held": True})
    assert services.claim_next_turn(cloud) is None
    FleetHold.objects.filter(pk=FleetHold.SINGLETON_PK).update(held=False)
    claimed = services.claim_next_turn(cloud)
    assert claimed is not None and claimed.pk == turn.pk


def test_only_laptops_assigned_reads_as_unroutable_not_offline(world):
    turn = _digest(world)
    _runner(world, "jj-mbp-cdp", Runner.EMDASH)
    Turn.objects.filter(pk=turn.pk).update(created_at=timezone.now() - dt.timedelta(minutes=5))
    rows = {r["turn_id"]: r for r in unclaimable_queued_turns(world["owner"])}
    assert rows[str(turn.pk)]["kind"] == "config"
