# ruff: noqa: F811 — fixtures are imported from the Slack test modules and requested by name.
"""Project check-ins (spec 2026-10-17-project-check-ins-design.md).

Driven through the real REST routes and the real signed interactions webhook,
with Slack faked at the `requests` boundary (tests/test_slack.py). Pinned: the
schedule announces and never reads; only the lead's Confirm reads, and only for
the project's agent; a lead outside the channel and an agent the project's agent
can't run are refused with a plain reason; the read becomes the agent's turn; a
check-in without a channel queues the turn and reads nothing; finished posts a
pointer, not the output. And Jonathan's rule — no API or web path reads.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin, AgentProject, EngagementEvent, ProjectParticipant
from apps.events.models import Event
from apps.harness import services as hsvc
from apps.harness.models import AgentSchedule, Turn
from apps.huddles import checkins
from apps.huddles.models import CheckinRun, ProjectCheckin
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


@pytest.fixture
def project(hal):
    return AgentProject.objects.create(agent=hal, ext_id="P1", name="AI Champions",
                                       outcome="Every team adopts AI.", status=AgentProject.ACTIVE)


URL = "/api/agents/hal/projects/P1/checkins/"
ONE = URL + "Weekly"


def _create(user, **over):
    body = {"name": "Weekly", "topic": "Help each team adopt AI.", "channel": "#canopy-support",
            "lead_email": "alice@dimagi.com", "agents": ["hal"], "cron": "0 9 * * 1", "timezone": "UTC", **over}
    return _link_client(user).post(URL, body, content_type="application/json")


@pytest.fixture
def checkin(channel, installation, hal_login, lead, owner, project):
    resp = _create(owner)
    assert resp.status_code == 201, resp.content
    return ProjectCheckin.objects.get(name="Weekly")


#: Before the fake channel's first message (tests/test_slack_channel_ingest.Channel).
EPOCH = datetime(2023, 1, 1, tzinfo=UTC)


def _fire(checkin, slot=None, since=EPOCH):
    """Announce a run. `since` stands in for where it reads from (the fake channel
    is from 2023, a real first run reads one cadence back); None keeps the real one."""
    slot = slot or timezone.now().replace(microsecond=0)
    turn, created = hsvc.fire_schedule(checkin.schedule, slot)
    assert turn is None
    run = CheckinRun.objects.get(checkin=checkin, slot=slot)
    if since is not None:
        CheckinRun.objects.filter(pk=run.pk).update(read_since=since)
        run.refresh_from_db()
    return run


def _announced(slack):
    return [p for p in slack.said("chat.postMessage") if any(
        b.get("type") == "actions" and b.get("block_id") == "group_run" for b in p.get("blocks") or [])]


# ---- saving a check-in ----------------------------------------------------------------

def test_saving_allow_lists_the_channel_and_makes_a_schedule(checkin, hal, lead, channel, project):
    entry = ingest.allowance_for(hal, CHAN)
    assert entry is not None and entry.channel_name == "canopy-support"
    s = checkin.schedule
    assert s.agent == hal and s.prompt == "/canopy:huddle" and s.cron == "0 9 * * 1"
    assert checkin.lead == lead and checkin.lead_slack_id == ALICE and checkin.format == "solo"
    assert checkin.project == project and checkin.agents == ["hal"]
    assert ProjectParticipant.objects.filter(project=project, person__user=lead, source="manual").exists()
    assert not {"conversations.history", "conversations.replies"} & set(_reads(channel))
    assert not SlackChannelIngest.objects.exists()
    got = _link_client(lead).get(URL).json()
    assert [c["name"] for c in got] == ["Weekly"] and got[0]["format"] == "solo" and got[0]["agent"] == "hal"
    assert got[0]["url"] == "/w/dimagi/agents/hal/projects/P1"


def test_a_lead_outside_the_channel_is_refused(channel, installation, hal_login, lead, owner, project):
    channel.members = [BOB]
    resp = _create(owner)
    assert resp.status_code == 409
    assert "alice@dimagi.com is not in #canopy-support" in resp.json()["detail"]
    assert not ProjectCheckin.objects.exists() and not AgentSchedule.objects.exists()


def test_a_lead_outside_the_workspace_is_refused(channel, installation, hal_login, slack, owner, project):
    a_user("stranger@else.org")
    slack.lookup["stranger@else.org"] = ALICE
    resp = _create(owner, lead_email="stranger@else.org")
    assert resp.status_code == 409 and "not a member of the" in resp.json()["detail"]


def test_only_the_projects_owner_or_an_admin_writes_check_ins(channel, installation, hal_login, lead, project):
    assert _create(lead).status_code == 403                    # an editor
    project.owner_user = lead
    project.save()
    # The project's owner may — but putting a NEW channel on an agent's list is an admin's.
    resp = _create(lead)
    assert resp.status_code == 403 and "reading list" in resp.json()["detail"]
    resp = _create(lead, channel="")
    assert resp.status_code == 201, resp.content
    assert resp.json()["channel_id"] == "" and resp.json()["can_edit"] is True


def test_the_projects_agent_must_be_able_to_run_every_other_agent(channel, installation, hal_login, lead,
                                                                   owner, ws, project):
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ws)
    resp = _create(owner, agents=["hal", "eva"])
    assert resp.status_code == 409
    assert "hal can't run eva" in resp.json()["detail"]
    AgentAdmin.objects.create(agent=eva, user=hal_login)
    resp = _create(owner, agents=["eva"])                      # the project's agent is added
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["format"] == "huddle" and body["agents"] == ["hal", "eva"] and body["agent"] == "hal"


def test_deleting_a_check_in_takes_the_channel_off_the_list_and_keeps_its_runs(checkin, hal, owner, slack):
    run = _fire(checkin)
    resp = _link_client(owner).delete(ONE)
    assert resp.status_code == 204
    assert ingest.allowance_for(hal, CHAN) is None
    assert not AgentSchedule.objects.exists() and not ProjectCheckin.objects.exists()
    run.refresh_from_db()
    assert run.checkin is None and run.project.ext_id == "P1" and run.status == CheckinRun.EXPIRED


def _admin(ws, user):
    from apps.workspaces.models import WorkspaceMembership
    WorkspaceMembership.objects.filter(workspace=ws, user=user).update(role=WorkspaceMembership.ADMIN)
    return user


def test_an_agent_saves_a_check_in_on_the_channel_its_admin_already_allow_listed(
        channel, installation, hal, hal_login, lead, owner, ws, project):
    """Jonathan allow-listed #canopy-support for Ada, then Ada's own save was refused
    ("can't allow-list a channel for itself"): the entry already there is reused."""
    ingest.allow(workspace=ws, agent=hal, channel=CHAN, follow=False, note="", by=owner)
    _admin(ws, hal_login)
    resp = _create(hal_login)
    assert resp.status_code == 201, resp.content
    entry = ingest.allowance_for(hal, CHAN)
    assert entry.added_by == owner and entry.note == ""
    assert ProjectCheckin.objects.get().added_allowance is False


def test_an_agent_still_cant_put_a_new_channel_on_its_own_list(channel, installation, hal, hal_login, lead, ws,
                                                              project):
    _admin(ws, hal_login)
    resp = _create(hal_login)
    assert resp.status_code == 403 and "can't allow-list a channel for itself" in resp.json()["detail"]
    assert ingest.allowance_for(hal, CHAN) is None and not ProjectCheckin.objects.exists()


def test_deleting_a_check_in_keeps_an_entry_it_did_not_add(channel, installation, hal, hal_login, lead, owner, ws,
                                                           project):
    ingest.allow(workspace=ws, agent=hal, channel=CHAN, follow=False, note="", by=owner)
    assert _create(owner).status_code == 201
    assert _link_client(owner).delete(ONE).status_code == 204
    assert ingest.allowance_for(hal, CHAN) is not None


def test_the_last_check_in_on_a_channel_removes_the_entry_the_first_one_added(checkin, hal, owner):
    assert checkin.added_allowance is True
    assert _create(owner, name="Second").status_code == 201
    assert ProjectCheckin.objects.get(name="Second").added_allowance is False
    assert _link_client(owner).delete(ONE).status_code == 204
    assert ingest.allowance_for(hal, CHAN) is not None
    assert ProjectCheckin.objects.get(name="Second").added_allowance is True
    assert _link_client(owner).delete(URL + "Second").status_code == 204
    assert ingest.allowance_for(hal, CHAN) is None


def test_taking_the_channel_off_releases_the_entry_it_added(checkin, hal, owner):
    resp = _link_client(owner).patch(ONE, {"channel": ""}, content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["channel_id"] == "" and ingest.allowance_for(hal, CHAN) is None


# ---- the announcement ----------------------------------------------------------------

def test_the_schedule_announces_and_queues_no_turn(checkin, slack, channel):
    run = _fire(checkin)
    assert run.status == CheckinRun.AWAITING_CONFIRM
    assert run.participants == {"agents": ["hal"], "lead_email": "alice@dimagi.com"}
    (post,) = _announced(slack)
    text = post["blocks"][0]["text"]["text"]
    assert "*AI Champions — Weekly* is due" in text and f"<@{ALICE}>: Confirm to have `hal` read" in text
    assert run.announce_ts == "1700000999.000100"
    assert not Turn.objects.exists()
    assert not {"conversations.history", "conversations.replies"} & set(_reads(channel))
    checkin.schedule.refresh_from_db()
    assert checkin.schedule.last_slot == run.slot
    (ev,) = EngagementEvent.objects.filter(kind="run_announced")
    assert ev.checkin == str(checkin.pk) and ev.meta["run"] == run.pk and ev.surface == "slack"
    # The same slot fired twice is one run, one post.
    hsvc.fire_schedule(checkin.schedule, run.slot)
    assert CheckinRun.objects.count() == 1 and len(_announced(slack)) == 1


def test_the_fire_route_answers_202_with_the_run(checkin, owner):
    c = _link_client(owner)
    resp = c.post("/api/harness/runners/", {"name": "box", "kind": "emdash", "capabilities": {"agents": ["hal"]}},
                  content_type="application/json")
    assert resp.status_code == 201, resp.content
    rid = resp.json()["id"]
    slot = timezone.now().replace(microsecond=0)
    resp = c.post(f"/api/harness/schedules/{checkin.schedule.pk}/fire?runner_id={rid}",
                  {"slot": slot.isoformat()}, content_type="application/json")
    assert resp.status_code == 202, resp.content
    assert resp.json()["announced"] is True and resp.json()["status"] == "awaiting_confirm"
    assert not Turn.objects.exists()


def test_an_announcement_whose_lead_left_the_channel_fails_quietly(checkin, slack, channel):
    channel.members = [BOB]
    run = _fire(checkin)
    assert run.status == CheckinRun.FAILED and "not in #canopy-support" in run.note
    assert not _announced(slack)
    assert Event.objects.filter(kind="huddles.checkin_run_failed").exists()


def test_an_agent_that_lost_its_edge_fails_the_announcement(channel, installation, hal_login, lead, owner, ws,
                                                            slack, project):
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ws)
    grant = AgentAdmin.objects.create(agent=eva, user=hal_login)
    assert _create(owner, agents=["hal", "eva"]).status_code == 201
    grant.delete()
    run = _fire(ProjectCheckin.objects.get())
    assert run.status == CheckinRun.FAILED and "can't run eva" in run.note
    assert not _announced(slack)


# ---- a check-in without a channel ----------------------------------------------------------

def test_a_check_in_without_a_channel_queues_the_turn_and_reads_nothing(channel, installation, hal_login, lead,
                                                                         owner, project, slack, hal):
    assert _create(owner, channel="").status_code == 201
    c = ProjectCheckin.objects.get()
    run = _fire(c, since=None)
    assert run.status == CheckinRun.RUNNING and run.read_since is None and run.channel_id == ""
    turn = run.turn
    assert turn.agent == hal and turn.prompt == f"/canopy:huddle --checkin-run {run.pk}"
    assert turn.origin_ref["kind"] == "checkin_run" and turn.origin_ref["checkin"] == c.pk
    assert not slack.said("chat.postMessage") and not SlackChannelIngest.objects.exists()
    assert not {"conversations.history", "conversations.replies"} & set(_reads(channel))
    resp = _link_client(hal_login).post(f"/api/checkins/runs/{run.pk}/finished", {"tasks_proposed": 2},
                                        content_type="application/json")
    assert resp.status_code == 200 and resp.json()["tasks_proposed"] == 2 and resp.json()["status"] == "done"
    assert not slack.said("chat.postMessage")


# ---- the click ----------------------------------------------------------------

def test_only_the_leads_click_counts(checkin, slack, channel):
    run = _fire(checkin)
    click(checkins.CONFIRM, str(run.pk), user=BOB, message_ts=run.announce_ts)
    (note,) = slack.said("chat.postEphemeral")
    assert note["user"] == BOB and f"<@{ALICE}>" in note["text"]
    run.refresh_from_db()
    assert run.status == CheckinRun.AWAITING_CONFIRM and not SlackChannelIngest.objects.exists()


def test_skip_reads_nothing(checkin, slack, channel):
    run = _fire(checkin)
    click(checkins.SKIP, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    run.refresh_from_db()
    assert run.status == CheckinRun.SKIPPED
    (upd,) = slack.said("chat.update")
    assert f"Skipped by <@{ALICE}>" in upd["text"]
    assert not SlackChannelIngest.objects.exists()
    (ev,) = EngagementEvent.objects.filter(kind="run_skipped")
    assert ev.meta["run"] == run.pk and ev.user == checkin.lead


def test_confirm_reads_for_the_projects_agent_then_queues_its_turn(checkin, slack, channel, hal):
    run = _fire(checkin)
    click(checkins.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    run.refresh_from_db()
    assert run.status == CheckinRun.READING and run.confirmed_by == checkin.lead
    row = SlackChannelIngest.objects.get()
    assert row.agent == hal and row.pending_ask["checkin_run"] == run.pk
    assert not Turn.objects.exists()
    assert EngagementEvent.objects.get(kind="run_confirmed").meta["run"] == run.pk
    _run(row)
    run.refresh_from_db()
    assert run.status == CheckinRun.RUNNING
    assert "first ever message" in run.material and run.read_messages == 6
    turn = run.turn
    assert turn.agent == hal and turn.prompt == f"/canopy:huddle --checkin-run {run.pk}"
    assert turn.origin_ref["kind"] == "checkin_run" and turn.origin_ref["run"] == run.pk
    assert turn.origin_ref["checkin"] == checkin.pk and turn.origin_ref["schedule_id"] == checkin.schedule.pk
    row.refresh_from_db()
    assert row.read_mark and row.pending_ask == {} and row.last_read_by_slack == ALICE
    told = [p["text"] for p in slack.said("chat.postMessage") if p.get("thread_ts") == run.announce_ts]
    assert any(":books:" in t and "read 6 message(s)" in t and "Started — follow it at" in t for t in told), told
    # A late-claimed check-in turn is not skipped as a stale slot.
    from apps.harness.schedule_turns import skip_late_scheduled_turns

    CheckinRun.objects.filter(pk=run.pk).update(slot=timezone.now() - timedelta(hours=5))
    Turn.objects.filter(pk=turn.pk).update(origin_ref={**turn.origin_ref,
                                                       "slot": (timezone.now() - timedelta(hours=5)).isoformat()})
    assert skip_late_scheduled_turns() == 0


def test_a_read_confirmed_before_the_rename_still_reaches_its_run(checkin, slack, channel):
    """A read confirmed while work groups still existed carries `group_run`: the run
    kept its id, so the check-in takes it from there."""
    run = _fire(checkin)
    click(checkins.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    row = SlackChannelIngest.objects.get()
    SlackChannelIngest.objects.filter(pk=row.pk).update(pending_ask={"group_run": run.pk, "by": checkin.lead_id})
    row.refresh_from_db()
    _run(row)
    run.refresh_from_db()
    assert run.status == CheckinRun.RUNNING and run.read_messages == 6


def test_finished_posts_a_pointer_only_from_the_agents_login(checkin, slack, channel, hal_login, lead):
    run = _fire(checkin)
    click(checkins.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    _run(SlackChannelIngest.objects.get())
    url = f"/api/checkins/runs/{run.pk}/finished"
    body = {"result_url": "https://canopy.test/w/x/agents/hal/projects/P1", "summary": "3 tasks"}
    assert _link_client(lead).post(url, body, content_type="application/json").status_code == 403
    resp = _link_client(hal_login).post(url, body, content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == "done" and resp.json()["tasks_proposed"] == 0
    told = [p["text"] for p in slack.said("chat.postMessage") if p.get("thread_ts") == run.announce_ts]
    assert told[-1] == ":white_check_mark: done — 0 proposed tasks: https://canopy.test/w/x/agents/hal/projects/P1"
    got = _link_client(lead).get(f"/api/checkins/runs/{run.pk}").json()
    assert "first ever message" in got["material"] and got["read_messages"] == 6


def test_the_old_work_group_run_routes_still_read_and_finish(checkin, slack, channel, hal_login, lead):
    """Kept one release: an engine that still says `--group-run` finishes its run."""
    run = _fire(checkin)
    click(checkins.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    _run(SlackChannelIngest.objects.get())
    got = _link_client(hal_login).get(f"/api/huddle-groups/runs/{run.pk}").json()
    assert got["id"] == run.pk and "first ever message" in got["material"] and got["group_name"] == "Weekly"
    resp = _link_client(hal_login).post(f"/api/huddle-groups/runs/{run.pk}/finished",
                                        {"huddle_id": "h9", "summary": "x"}, content_type="application/json")
    assert resp.status_code == 200 and resp.json()["status"] == "done" and resp.json()["huddle_id"] == "h9"
    assert _link_client(lead).get("/api/huddle-groups/").status_code in (404, 405)


def test_revoking_the_read_deletes_the_runs_copy_too(checkin, slack, channel):
    run = _fire(checkin)
    click(checkins.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    row = SlackChannelIngest.objects.get()
    _run(row)
    run.refresh_from_db()
    assert "first ever message" in run.material
    ingest.revoke(row, user=checkin.lead)
    run.refresh_from_db()
    assert run.material == ""


def test_the_read_covers_exactly_what_the_announcement_said(checkin, slack, channel):
    """2026-10-17: the announcement said "since yesterday 7:44 PM" (dated from a
    revoked read) and the Confirm read the whole channel. One start, both places."""
    since = datetime.fromtimestamp(1700000350, tz=UTC)
    run = _fire(checkin, since=None)
    CheckinRun.objects.filter(pk=run.pk).update(read_since=since)
    run.refresh_from_db()
    text = checkins.announcement(run)[1][0]["text"]["text"]
    assert "since <!date^1700000350^" in text
    click(checkins.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    _run(SlackChannelIngest.objects.get())
    run.refresh_from_db()
    assert run.status == CheckinRun.RUNNING and run.read_since == since
    assert "first ever message" not in run.material and "middle" not in run.material
    assert "last word" in run.material and "reply two" in run.material and run.read_messages == 4
    oldest = [p.get("oldest") for m, p in channel.reads if m == "conversations.history"]
    assert oldest and float(oldest[0]) == 1700000350
    told = [p["text"] for p in slack.said("chat.postMessage") if p.get("thread_ts") == run.announce_ts]
    assert any("read 4 message(s)" in t and "since <!date^1700000350^" in t and "as confirmed" in t
               for t in told), told


def test_a_first_run_reads_one_cadence_back_and_the_next_from_where_it_stopped(checkin, slack, channel):
    slot = timezone.now().replace(microsecond=0)
    first = _fire(checkin, slot=slot, since=None)
    assert first.read_since == slot - timedelta(days=7)          # "0 9 * * 1": weekly
    assert ingest.allowance_for(checkin.project.agent, checkin.channel_id) is not None
    CheckinRun.objects.filter(pk=first.pk).update(read_last_ts="1700000500.000000")
    nxt = _fire(checkin, slot=slot + timedelta(days=7), since=None)
    assert nxt.read_since == datetime.fromtimestamp(1700000500, tz=UTC)


def test_a_read_channel_in_between_does_not_hide_messages_from_the_check_in(checkin, slack, channel):
    run = _fire(checkin)
    click(checkins.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    row = SlackChannelIngest.objects.get()
    _run(row)
    run.refresh_from_db()
    assert run.read_messages == 6
    # Someone's read-channel advanced the agent's mark past everything; the next
    # check-in run still reads what was posted since the check-in's own last read.
    ingest.mark_read(row, 10**9)
    nxt = _fire(checkin, slot=run.slot + timedelta(days=7), since=datetime.fromtimestamp(1700000350, tz=UTC))
    click(checkins.CONFIRM, str(nxt.pk), user=ALICE, message_ts=nxt.announce_ts)
    _run(SlackChannelIngest.objects.get())
    nxt.refresh_from_db()
    assert nxt.read_messages == 4 and "last word" in nxt.material


def test_canopys_own_posts_are_not_material(checkin, slack, channel):
    """2026-10-17 run 2: the only "message" read was canopy's own "Confirmed by…"."""
    from tests.test_slack import BOT

    channel.history.insert(0, {"ts": "1700000600.000000", "user": BOT, "bot_id": "B1",
                               "text": ":white_check_mark: Confirmed by someone. canopy is reading this channel"})
    run = _fire(checkin)
    click(checkins.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    _run(SlackChannelIngest.objects.get())
    run.refresh_from_db()
    assert "canopy is reading this channel" not in run.material
    assert run.read_messages == 6 and "last word" in run.material


def test_unconfirmed_runs_lapse_after_a_day(checkin, slack):
    run = _fire(checkin)
    CheckinRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(hours=25))
    assert checkins.expire_due() == 1
    run.refresh_from_db()
    assert run.status == CheckinRun.EXPIRED
    assert "lapsed" in slack.said("chat.update")[-1]["text"]
    assert EngagementEvent.objects.get(kind="run_expired").meta["run"] == run.pk
    assert click(checkins.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts).status_code == 200
    assert not SlackChannelIngest.objects.exists()


# ---- the timeline ----------------------------------------------------------------

def test_the_projects_timeline_lists_runs_with_what_they_proposed(checkin, slack, channel, hal_login, lead):
    run = _fire(checkin)
    click(checkins.CONFIRM, str(run.pk), user=ALICE, message_ts=run.announce_ts)
    _run(SlackChannelIngest.objects.get())
    body = {"title": "Write the FAQ", "why": "People keep asking.", "done_when": "The FAQ is pinned.",
            "plan": ["Collect questions", "Draft"], "source_url": "https://canopy.test/w/dimagi/huddles/h1",
            "run": run.pk}
    resp = _link_client(hal_login).post("/api/agents/hal/projects/P1/tasks/propose/", body,
                                        content_type="application/json")
    assert resp.status_code == 201, resp.content
    _link_client(hal_login).post(f"/api/checkins/runs/{run.pk}/finished", {"huddle_id": "h1"},
                                 content_type="application/json")
    (row,) = _link_client(lead).get("/api/agents/hal/projects/P1/checkin-runs").json()
    assert row["id"] == run.pk and row["checkin_name"] == "Weekly" and row["status"] == "done"
    assert row["tasks_proposed"] == 1 and row["confirmed_by_email"] == "alice@dimagi.com"
    assert row["participants"] == {"agents": ["hal"], "lead_email": "alice@dimagi.com"}
    assert row["read_messages"] == 6 and row["material"] == "" and row["huddle_url"] == "/w/dimagi/huddles/h1"
    assert [t["title"] for t in row["tasks"]] == ["Write the FAQ"] and row["tasks"][0]["status"] == "suggested"
    listed = _link_client(lead).get("/api/checkins/").json()
    assert [(c["name"], c["project"]["ref"]) for c in listed] == [("Weekly", "P1")]


# ---- Jonathan's rule: no API or web path reads a check-in's channel ------------------------

def test_no_api_or_web_path_starts_a_read(checkin, slack, channel, owner, hal_login, lead):
    run = _fire(checkin)
    c = _link_client(owner)
    calls = [
        c.get("/api/checkins/"),
        c.get(ONE),
        c.patch(ONE, {"topic": "new topic"}, content_type="application/json"),
        c.get("/api/agents/hal/projects/P1/checkin-runs"),
        c.get(f"/api/checkins/runs/{run.pk}"),
        c.post(f"/api/agents/hal/schedules/{checkin.schedule.pk}/run-now"),
        c.post(ONE + "/run-now"),
        _link_client(hal_login).post(f"/api/checkins/runs/{run.pk}/finished", {},
                                     content_type="application/json"),
        _link_client(hal_login).post("/api/agents/hal/projects/P1/tasks/propose/",
                                     {"title": "t", "why": "w", "done_when": "d", "run": run.pk},
                                     content_type="application/json"),
        _link_client(hal_login).post("/api/slack-ingests/", {"agent": "hal", "channel": CHAN, "purpose": "x"},
                                     content_type="application/json"),
    ]
    assert [r.status_code for r in calls][:9] == [200, 200, 200, 200, 200, 202, 202, 409, 201]
    for _ in range(3):
        ingest.tick()
    assert not {"conversations.history", "conversations.replies"} & set(_reads(channel))
    assert not SlackChannelIngest.objects.filter(pending_ask__has_key="checkin_run").exists()
    assert not Turn.objects.exists()
    # Each Run now announced again (two more runs), and still read nothing.
    assert CheckinRun.objects.filter(checkin=checkin).count() == 3
    assert set(CheckinRun.objects.values_list("status", flat=True)) <= {
        CheckinRun.AWAITING_CONFIRM, CheckinRun.EXPIRED}


def test_a_huddle_run_for_a_check_in_carries_the_check_in_header(checkin, owner, hal):
    run = _fire(checkin)
    CheckinRun.objects.filter(pk=run.pk).update(read_messages=4, read_first_ts="1700000100.000000",
                                                read_last_ts="1700000500.000000")
    Turn.objects.create(agent=hal, idempotency_key="anchor-h1",
                        origin_ref={"kind": "huddle", "huddle": "h1", "leader": "hal", "members": ["hal"],
                                    "checkin_run": run.pk})
    Turn.objects.create(agent=hal, idempotency_key="anchor-h2", origin_ref={"kind": "huddle", "huddle": "h2"})
    got = _link_client(owner).get("/api/huddles/h1").json()
    assert got["checkin"] == {"id": checkin.pk, "name": "Weekly",
                              "project": {"agent": "hal", "ref": "P1", "name": "AI Champions", "status": "active",
                                          "url": "/w/dimagi/agents/hal/projects/P1", "workspace": "dimagi"},
                              "channel_name": "canopy-support", "lead_email": "alice@dimagi.com", "run_id": run.pk,
                              "read": {"messages": 4, "first_ts": "1700000100.000000",
                                       "last_ts": "1700000500.000000", "since": EPOCH.isoformat()}}
    assert _link_client(owner).get("/api/huddles/h2").json()["checkin"] is None
