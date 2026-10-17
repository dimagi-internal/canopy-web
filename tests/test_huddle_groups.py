# ruff: noqa: F811 — fixtures are imported from the Slack test modules and requested by name.
"""Work groups (spec 2026-10-16-work-group-huddles-design.md, layer 3).

Driven through the real REST routes and the real signed interactions webhook,
with Slack faked at the `requests` boundary (tests/test_slack.py). Pinned: the
schedule announces and never reads; only the lead's Confirm reads, and only for
the leader; a lead outside the channel and a leader that can't run a member are
refused with a plain reason; the read becomes the leader's turn; finished posts
a pointer, not the output. And Jonathan's rule — no API or web path reads.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin, AgentProject, EngagementEvent
from apps.events.models import Event
from apps.harness import services as hsvc
from apps.harness.models import AgentSchedule, Turn
from apps.huddles import groups
from apps.huddles.models import HuddleGroup, HuddleGroupRun
from apps.slack import ingest
from apps.slack.models import SlackChannelIngest
from apps.workspaces.testing import a_user
from tests.test_slack import (  # noqa: F401
    ALICE,
    BOB,
    _link_client,
    _owner,
    alice,
    click,
    configured,
    hal,
    installation,
    slack,
    ws,
)
from tests.test_slack_channel_ingest import (  # noqa: F401
    CHAN,
    _reads,
    _run,
    channel,
    hal_login,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def lead(alice, slack):
    slack.lookup["alice@dimagi.com"] = ALICE
    return alice


@pytest.fixture
def owner(ws):
    return _owner(ws)


def _create(owner, **over):
    body = {"slug": "champions", "name": "AI Champions", "topic": "Help each team adopt AI.",
            "channel": "#canopy-support", "lead_email": "alice@dimagi.com", "agents": ["hal"],
            "cron": "0 9 * * 1", "timezone": "UTC", **over}
    return _link_client(owner).post("/api/huddle-groups/", body, content_type="application/json")


@pytest.fixture
def group(channel, installation, hal_login, lead, owner):
    resp = _create(owner)
    assert resp.status_code == 201, resp.content
    return HuddleGroup.objects.get(slug="champions")


#: Before the fake channel's first message (tests/test_slack_channel_ingest.Channel).
EPOCH = datetime(2023, 1, 1, tzinfo=UTC)


def _fire(group, slot=None, since=EPOCH):
    """Announce a run. `since` stands in for where it reads from (the fake channel
    is from 2023, a real first run reads one cadence back); None keeps the real one."""
    slot = slot or timezone.now().replace(microsecond=0)
    turn, created = hsvc.fire_schedule(group.schedule, slot)
    assert turn is None
    run = HuddleGroupRun.objects.get(group=group, slot=slot)
    if since is not None:
        HuddleGroupRun.objects.filter(pk=run.pk).update(read_since=since)
        run.refresh_from_db()
    return run


def _announced(slack):
    return [p for p in slack.said("chat.postMessage") if any(
        b.get("type") == "actions" and b.get("block_id") == "group_run" for b in p.get("blocks") or [])]


# ---- saving a group ----------------------------------------------------------------

def test_saving_allow_lists_the_channel_and_makes_a_schedule_and_a_project(group, hal, lead, channel):
    entry = ingest.allowance_for(hal, CHAN)
    assert entry is not None and entry.channel_name == "canopy-support"
    s = group.schedule
    assert s.agent == hal and s.prompt == "/canopy:huddle" and s.cron == "0 9 * * 1"
    assert group.lead == lead and group.lead_slack_id == ALICE and group.single
    p = group.project
    assert p.agent == hal and p.name == "AI Champions" and p.owner_user == lead and p.status == AgentProject.ACTIVE
    assert not {"conversations.history", "conversations.replies"} & set(_reads(channel))
    assert not SlackChannelIngest.objects.exists()


def test_a_lead_outside_the_channel_is_refused(channel, installation, hal_login, lead, owner):
    channel.members = [BOB]
    resp = _create(owner)
    assert resp.status_code == 409
    assert "alice@dimagi.com is not in #canopy-support" in resp.json()["detail"]
    assert not HuddleGroup.objects.exists() and not AgentSchedule.objects.exists()


def test_a_lead_outside_the_workspace_is_refused(channel, installation, hal_login, slack, owner):
    a_user("stranger@else.org")
    slack.lookup["stranger@else.org"] = ALICE
    resp = _create(owner, lead_email="stranger@else.org")
    assert resp.status_code == 409 and "not a member of the" in resp.json()["detail"]


def test_only_an_owner_or_admin_writes_groups(channel, installation, hal_login, lead):
    resp = _create(lead)                      # an editor
    assert resp.status_code == 403


def test_the_leader_must_be_able_to_run_every_member(channel, installation, hal_login, lead, owner, ws):
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ws)
    resp = _create(owner, agents=["hal", "eva"], leader_agent="hal")
    assert resp.status_code == 409
    assert "hal can't run eva" in resp.json()["detail"]
    AgentAdmin.objects.create(agent=eva, user=hal_login)
    resp = _create(owner, agents=["hal", "eva"], leader_agent="hal")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["mode"] == "multi" and body["leader_agent"] == "hal" and body["project"] is None


def test_several_agents_need_a_leader(channel, installation, hal_login, lead, owner, ws):
    Agent.objects.create(slug="eva", name="Eva", workspace=ws)
    resp = _create(owner, agents=["hal", "eva"])
    assert resp.status_code == 422 and "leader_agent" in resp.json()["detail"]


def test_deleting_a_group_takes_the_channel_off_the_list(group, hal, owner):
    resp = _link_client(owner).delete("/api/huddle-groups/champions")
    assert resp.status_code == 204
    assert ingest.allowance_for(hal, CHAN) is None
    assert not AgentSchedule.objects.exists() and not HuddleGroup.objects.exists()


def _admin(ws, user):
    from apps.workspaces.models import WorkspaceMembership
    WorkspaceMembership.objects.filter(workspace=ws, user=user).update(role=WorkspaceMembership.ADMIN)
    return user


def test_an_agent_saves_a_group_on_the_channel_its_admin_already_allow_listed(
        channel, installation, hal, hal_login, lead, owner, ws):
    """Jonathan allow-listed #canopy-support for Ada, then Ada's own save was refused
    ("can't allow-list a channel for itself"): the entry already there is reused."""
    ingest.allow(workspace=ws, agent=hal, channel=CHAN, follow=False, note="", by=owner)
    _admin(ws, hal_login)
    resp = _create(hal_login)
    assert resp.status_code == 201, resp.content
    entry = ingest.allowance_for(hal, CHAN)
    assert entry.added_by == owner and entry.note == ""
    assert HuddleGroup.objects.get().added_allowance is False


def test_an_agent_still_cant_put_a_new_channel_on_its_own_list(channel, installation, hal, hal_login, lead, ws):
    _admin(ws, hal_login)
    resp = _create(hal_login)
    assert resp.status_code == 403 and "can't allow-list a channel for itself" in resp.json()["detail"]
    assert ingest.allowance_for(hal, CHAN) is None and not HuddleGroup.objects.exists()


def test_deleting_a_group_keeps_an_entry_it_did_not_add(channel, installation, hal, hal_login, lead, owner, ws):
    ingest.allow(workspace=ws, agent=hal, channel=CHAN, follow=False, note="", by=owner)
    assert _create(owner).status_code == 201
    assert _link_client(owner).delete("/api/huddle-groups/champions").status_code == 204
    assert ingest.allowance_for(hal, CHAN) is not None


def test_the_last_group_on_a_channel_removes_the_entry_the_first_one_added(group, hal, owner):
    assert group.added_allowance is True
    assert _create(owner, slug="second", name="Second").status_code == 201
    assert HuddleGroup.objects.get(slug="second").added_allowance is False
    assert _link_client(owner).delete("/api/huddle-groups/champions").status_code == 204
    assert ingest.allowance_for(hal, CHAN) is not None
    assert HuddleGroup.objects.get(slug="second").added_allowance is True
    assert _link_client(owner).delete("/api/huddle-groups/second").status_code == 204
    assert ingest.allowance_for(hal, CHAN) is None


# ---- the announcement ----------------------------------------------------------------

def test_the_schedule_announces_and_queues_no_turn(group, slack, channel):
    run = _fire(group)
    assert run.status == HuddleGroupRun.AWAITING_CONFIRM
    (post,) = _announced(slack)
    text = post["blocks"][0]["text"]["text"]
    assert "*AI Champions huddle* is due" in text and f"<@{ALICE}>: Confirm to have `hal` read" in text
    assert run.announce_ts == "1700000999.000100"
    assert not Turn.objects.exists()
    assert not {"conversations.history", "conversations.replies"} & set(_reads(channel))
    group.schedule.refresh_from_db()
    assert group.schedule.last_slot == run.slot
    (ev,) = EngagementEvent.objects.filter(kind="run_announced")
    assert ev.group == "champions" and ev.meta["run"] == run.pk and ev.surface == "slack"
    # The same slot fired twice is one run, one post.
    hsvc.fire_schedule(group.schedule, run.slot)
    assert HuddleGroupRun.objects.count() == 1 and len(_announced(slack)) == 1


def test_the_fire_route_answers_202_with_the_run(group, owner):
    c = _link_client(owner)
    resp = c.post("/api/harness/runners/", {"name": "box", "kind": "emdash", "capabilities": {"agents": ["hal"]}},
                  content_type="application/json")
    assert resp.status_code == 201, resp.content
    rid = resp.json()["id"]
    slot = timezone.now().replace(microsecond=0)
    resp = c.post(f"/api/harness/schedules/{group.schedule.pk}/fire?runner_id={rid}",
                  {"slot": slot.isoformat()}, content_type="application/json")
    assert resp.status_code == 202, resp.content
    assert resp.json()["announced"] is True and resp.json()["status"] == "awaiting_confirm"
    assert not Turn.objects.exists()


def test_an_announcement_whose_lead_left_the_channel_fails_quietly(group, slack, channel):
    channel.members = [BOB]
    run = _fire(group)
    assert run.status == HuddleGroupRun.FAILED and "not in #canopy-support" in run.note
    assert not _announced(slack)
    assert Event.objects.filter(kind="huddles.group_run_failed").exists()


def test_a_leader_that_lost_its_edge_fails_the_announcement(channel, installation, hal_login, lead, owner, ws,
                                                            slack):
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ws)
    grant = AgentAdmin.objects.create(agent=eva, user=hal_login)
    assert _create(owner, agents=["hal", "eva"], leader_agent="hal").status_code == 201
    grant.delete()
    run = _fire(HuddleGroup.objects.get())
    assert run.status == HuddleGroupRun.FAILED and "can't run eva" in run.note
    assert not _announced(slack)


# ---- the click ----------------------------------------------------------------

def test_only_the_leads_click_counts(group, slack, channel):
    run = _fire(group)
    click(groups.CONFIRM, str(run.pk), user=BOB, message_ts=run.announce_ts)
    (note,) = slack.said("chat.postEphemeral")
    assert note["user"] == BOB and f"<@{ALICE}>" in note["text"]
    run.refresh_from_db()
    assert run.status == HuddleGroupRun.AWAITING_CONFIRM and not SlackChannelIngest.objects.exists()


def test_skip_reads_nothing(group, slack, channel):
    run = _fire(group)
    click(groups.SKIP, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    run.refresh_from_db()
    assert run.status == HuddleGroupRun.SKIPPED
    (upd,) = slack.said("chat.update")
    assert f"Skipped by <@{ALICE}>" in upd["text"]
    assert not SlackChannelIngest.objects.exists()
    (ev,) = EngagementEvent.objects.filter(kind="run_skipped")
    assert ev.meta["run"] == run.pk and ev.user == group.lead


def test_confirm_reads_for_the_leader_then_queues_its_turn(group, slack, channel, hal):
    run = _fire(group)
    click(groups.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    run.refresh_from_db()
    assert run.status == HuddleGroupRun.READING and run.confirmed_by == group.lead
    row = SlackChannelIngest.objects.get()
    assert row.agent == hal and row.pending_ask["group_run"] == run.pk
    assert not Turn.objects.exists()
    assert EngagementEvent.objects.get(kind="run_confirmed").meta["run"] == run.pk
    _run(row)
    run.refresh_from_db()
    assert run.status == HuddleGroupRun.RUNNING
    assert "first ever message" in run.material and run.read_messages == 6
    turn = run.turn
    assert turn.agent == hal and turn.prompt == f"/canopy:huddle --group-run {run.pk}"
    assert turn.origin_ref["kind"] == "huddle_group_run" and turn.origin_ref["run"] == run.pk
    assert turn.origin_ref["group"] == "champions" and turn.origin_ref["schedule_id"] == group.schedule.pk
    row.refresh_from_db()
    assert row.read_mark and row.pending_ask == {} and row.last_read_by_slack == ALICE
    told = [p["text"] for p in slack.said("chat.postMessage") if p.get("thread_ts") == run.announce_ts]
    assert any(":books:" in t and "read 6 message(s)" in t and "Started — follow it at" in t for t in told), told
    # A late-claimed group turn is not skipped as a stale slot.
    from apps.harness.schedule_turns import skip_late_scheduled_turns

    HuddleGroupRun.objects.filter(pk=run.pk).update(slot=timezone.now() - timedelta(hours=5))
    Turn.objects.filter(pk=turn.pk).update(origin_ref={**turn.origin_ref,
                                                       "slot": (timezone.now() - timedelta(hours=5)).isoformat()})
    assert skip_late_scheduled_turns() == 0


def test_finished_posts_a_pointer_only_from_the_leaders_login(group, slack, channel, hal_login, lead):
    run = _fire(group)
    click(groups.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    _run(SlackChannelIngest.objects.get())
    url = f"/api/huddle-groups/runs/{run.pk}/finished"
    body = {"result_url": "https://canopy.test/w/x/agents/hal/projects/P1", "summary": "3 tasks"}
    assert _link_client(lead).post(url, body, content_type="application/json").status_code == 403
    resp = _link_client(hal_login).post(url, body, content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == "done"
    told = [p["text"] for p in slack.said("chat.postMessage") if p.get("thread_ts") == run.announce_ts]
    assert told[-1] == ":white_check_mark: done — 0 new tasks: https://canopy.test/w/x/agents/hal/projects/P1"
    got = _link_client(lead).get(f"/api/huddle-groups/runs/{run.pk}").json()
    assert "first ever message" in got["material"] and got["read_messages"] == 6


def test_revoking_the_read_deletes_the_runs_copy_too(group, slack, channel):
    run = _fire(group)
    click(groups.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    row = SlackChannelIngest.objects.get()
    _run(row)
    run.refresh_from_db()
    assert "first ever message" in run.material
    ingest.revoke(row, user=group.lead)
    run.refresh_from_db()
    assert run.material == ""


def test_the_read_covers_exactly_what_the_announcement_said(group, slack, channel):
    """2026-10-17: the announcement said "since yesterday 7:44 PM" (dated from a
    revoked read) and the Confirm read the whole channel. One start, both places."""
    since = datetime.fromtimestamp(1700000350, tz=UTC)
    run = _fire(group, since=None)
    HuddleGroupRun.objects.filter(pk=run.pk).update(read_since=since)
    run.refresh_from_db()
    text = groups.announcement(run)[1][0]["text"]["text"]
    assert "since <!date^1700000350^" in text
    click(groups.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    _run(SlackChannelIngest.objects.get())
    run.refresh_from_db()
    assert run.status == HuddleGroupRun.RUNNING and run.read_since == since
    assert "first ever message" not in run.material and "middle" not in run.material
    assert "last word" in run.material and "reply two" in run.material and run.read_messages == 4
    oldest = [p.get("oldest") for m, p in channel.reads if m == "conversations.history"]
    assert oldest and float(oldest[0]) == 1700000350
    told = [p["text"] for p in slack.said("chat.postMessage") if p.get("thread_ts") == run.announce_ts]
    assert any("read 4 message(s)" in t and "since <!date^1700000350^" in t and "as confirmed" in t
               for t in told), told


def test_a_first_run_reads_one_cadence_back_and_the_next_from_where_it_stopped(group, slack, channel):
    slot = timezone.now().replace(microsecond=0)
    first = _fire(group, slot=slot, since=None)
    assert first.read_since == slot - timedelta(days=7)          # "0 9 * * 1": weekly
    assert ingest.allowance_for(group.leader_agent, group.channel_id) is not None
    HuddleGroupRun.objects.filter(pk=first.pk).update(read_last_ts="1700000500.000000")
    nxt = _fire(group, slot=slot + timedelta(days=7), since=None)
    assert nxt.read_since == datetime.fromtimestamp(1700000500, tz=UTC)


def test_a_read_channel_in_between_does_not_hide_messages_from_the_group(group, slack, channel):
    run = _fire(group)
    click(groups.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    row = SlackChannelIngest.objects.get()
    _run(row)
    run.refresh_from_db()
    assert run.read_messages == 6
    # Someone's read-channel advanced the agent's mark past everything; the next
    # group run still reads what was posted since the group's own last read.
    ingest.mark_read(row, 10**9)
    nxt = _fire(group, slot=run.slot + timedelta(days=7), since=datetime.fromtimestamp(1700000350, tz=UTC))
    click(groups.CONFIRM, str(nxt.pk), user=ALICE, message_ts=nxt.announce_ts)
    _run(SlackChannelIngest.objects.get())
    nxt.refresh_from_db()
    assert nxt.read_messages == 4 and "last word" in nxt.material


def test_unconfirmed_runs_lapse_after_a_day(group, slack):
    run = _fire(group)
    HuddleGroupRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(hours=25))
    assert groups.expire_due() == 1
    run.refresh_from_db()
    assert run.status == HuddleGroupRun.EXPIRED
    assert "lapsed" in slack.said("chat.update")[-1]["text"]
    assert EngagementEvent.objects.get(kind="run_expired").meta["run"] == run.pk
    assert click(groups.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts).status_code == 200
    assert not SlackChannelIngest.objects.exists()


# ---- Jonathan's rule: no API or web path reads a group's channel ------------------------

def test_no_api_or_web_path_starts_a_read(group, slack, channel, owner, hal_login, lead):
    run = _fire(group)
    c = _link_client(owner)
    calls = [
        c.get("/api/huddle-groups/"),
        c.get("/api/huddle-groups/champions"),
        c.patch("/api/huddle-groups/champions", {"topic": "new topic"}, content_type="application/json"),
        c.get("/api/huddle-groups/champions/runs"),
        c.get(f"/api/huddle-groups/runs/{run.pk}"),
        c.post(f"/api/agents/hal/schedules/{group.schedule.pk}/run-now"),
        _link_client(hal_login).post(f"/api/huddle-groups/runs/{run.pk}/finished", {},
                                     content_type="application/json"),
        _link_client(hal_login).post("/api/slack-ingests/", {"agent": "hal", "channel": CHAN, "purpose": "x"},
                                     content_type="application/json"),
    ]
    assert [r.status_code for r in calls][:6] == [200, 200, 200, 200, 200, 202]
    assert calls[6].status_code == 409       # not running: only a confirmed, read run can finish
    for _ in range(3):
        ingest.tick()
    assert not {"conversations.history", "conversations.replies"} & set(_reads(channel))
    assert not SlackChannelIngest.objects.filter(pending_ask__has_key="group_run").exists()
    assert not Turn.objects.exists()
    # Run now announced again (a second run), and still read nothing.
    assert HuddleGroupRun.objects.filter(group=group).count() == 2
    assert set(HuddleGroupRun.objects.values_list("status", flat=True)) <= {
        HuddleGroupRun.AWAITING_CONFIRM, HuddleGroupRun.EXPIRED}


def test_a_huddle_run_for_a_group_carries_the_group_header(group, owner, hal):
    run = _fire(group)
    HuddleGroupRun.objects.filter(pk=run.pk).update(read_messages=4, read_first_ts="1700000100.000000",
                                                    read_last_ts="1700000500.000000")
    Turn.objects.create(agent=hal, idempotency_key="anchor-h1",
                        origin_ref={"kind": "huddle", "huddle": "h1", "leader": "hal", "members": ["hal"],
                                    "group": "champions", "run": run.pk})
    Turn.objects.create(agent=hal, idempotency_key="anchor-h2", origin_ref={"kind": "huddle", "huddle": "h2"})
    got = _link_client(owner).get("/api/huddles/h1").json()
    assert got["group"] == {"slug": "champions", "name": "AI Champions", "channel_name": "canopy-support",
                            "lead_email": "alice@dimagi.com", "run_id": run.pk,
                            "read": {"messages": 4, "first_ts": "1700000100.000000",
                                     "last_ts": "1700000500.000000", "since": EPOCH.isoformat()}}
    assert _link_client(owner).get("/api/huddles/h2").json()["group"] is None
