"""Project check-ins: a project's recurring meeting (spec
docs/superpowers/specs/2026-10-17-project-check-ins-design.md).

The life of one run (`CheckinRun`):

1. **announce** — the check-in's schedule fires (harness.schedule_turns).
   * WITH a channel: no turn. canopy posts "<project> — <check-in> is due" in the
     channel with Confirm / Skip, after checking again that the lead is in the
     workspace and the channel and that the project's agent may run every other
     agent. A failed check fails the run quietly — an Event, nothing in Slack,
     nothing read.
   * WITHOUT a channel: nothing to read and nobody to ask — the run goes straight
     to `running` and the project's agent's turn is queued.
2. **click** — only the lead's click counts. Skip ends it. Confirm is the
   person's command in Slack that Jonathan's rule asks for: it starts the read
   (slack.ingest.start_group_read) for the project's agent. Nothing else does.
3. **deliver_read** — the read is done: the material is stored on the run, the
   read mark moves, the agent's turn is queued (`<schedule prompt> --checkin-run
   <id>`), and the announcement's thread is told what was read.
4. **finish** — the agent's login reports the result (and how many tasks it
   proposed in the project); the thread gets one line pointing at it. The
   content lives in canopy, not in Slack.

Unconfirmed runs expire after 24 hours (`expire_due`, on the channel-ingest loop).
"""
from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.utils import timezone

from apps.agents.models import Agent, AgentProject, AgentTask, ProjectParticipant
from apps.events.models import Event
from apps.harness.models import AgentSchedule, Turn
from apps.slack import client
from apps.slack import ingest as ingest_svc
from apps.slack.models import SlackChannelIngest
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from .models import CheckinRun, ProjectCheckin

logger = logging.getLogger(__name__)

#: Block Kit action ids on an announcement (routed in slack.views.interactions).
#: The values predate check-ins and stay: an announcement posted before the
#: rename still carries them.
CONFIRM, SKIP = "group_run_confirm", "group_run_skip"
ACTIONS = (CONFIRM, SKIP)
#: The generic command every check-in schedule runs; dispatch appends
#: ` --checkin-run <id>`. The canopy command decides: one agent → the solo
#: procedure on the project; several → the huddle.
BASE_PROMPT = "/canopy:huddle"
#: `origin_ref.kind` of a check-in run's turn. The old work-group kind is still
#: recognised wherever a turn is read, for turns queued before the rename.
TURN_KIND = "checkin_run"
TURN_KINDS = (TURN_KIND, "huddle_group_run")
EXPIRE_AFTER = dt.timedelta(hours=24)
#: A confirmed read that never came back (revoked, crashed) fails after this.
READ_TIMEOUT = dt.timedelta(hours=24)


class CheckinError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# --- bookkeeping ---------------------------------------------------------------

def _event(subject, kind: str, summary: str, *, level: str = "", **payload) -> None:
    """`subject` is a ProjectCheckin or a CheckinRun."""
    from apps.events import services as events_services

    try:
        project = subject.project
        cid = subject.pk if isinstance(subject, ProjectCheckin) else subject.checkin_id
        events_services.record([{
            "source": "huddles", "kind": f"huddles.checkin_{kind}", "level": level or Event.INFO,
            "summary": summary[:500],
            "payload": {"checkin": cid, "project": project.address, "agent": project.agent_slug or "", **payload},
        }], workspace=project.workspace)
    except Exception:  # noqa: BLE001 — bookkeeping never fails the act
        logger.exception("could not record a check-in event")


def _engage(kind: str, run: CheckinRun, *, user=None, surface: str = "slack", **meta) -> None:
    """What people actually do with runs (EngagementEvent)."""
    from apps.agents import engagement

    try:
        engagement.record(kind, workspace=run.project.workspace_id, surface=surface, user=user,
                          agent=run.project.agent, project=run.project,
                          checkin=str(run.checkin_id or ""), meta={"run": run.pk, **meta})
    except Exception:  # noqa: BLE001
        logger.exception("could not record check-in engagement")


def _who(user) -> str:
    return getattr(user, "email", "") or "someone"


def project_path(project: AgentProject) -> str:
    return project.url


def page_url(project: AgentProject) -> str:
    from apps.slack.services import public_url

    return public_url(project_path(project))


def label(checkin: ProjectCheckin) -> str:
    return f"{checkin.project.name} — {checkin.name}"


# --- the checks ----------------------------------------------------------------

def lead_refusal(*, workspace_id: str, lead, token: str, channel_id: str, channel_name: str = "") -> tuple[str, str]:
    """("", lead's Slack id) when the lead may lead, else (the plain reason, "").

    The lead must be a member of the canopy workspace AND of the Slack channel
    (`users.lookupByEmail` + paginated `conversations.members`)."""
    where = f"#{channel_name}" if channel_name else channel_id
    if lead is None:
        return "the check-in has no lead", ""
    if not wsvc.is_member(lead, workspace_id):
        return f"{_who(lead)} is not a member of the {workspace_id} canopy workspace", ""
    slack_id = client.lookup_user_id(token, lead.email or "")
    if not slack_id:
        return f"Slack has nobody with the email {_who(lead)}, so canopy can't check they are in {where}", ""
    try:
        member = client.is_channel_member(token, channel_id, slack_id)
    except (client.SlackApiError, client.RateLimited) as e:
        err = getattr(e, "error", "rate limited")
        return f"canopy can't list who is in {where} ({err}) — invite the canopy app to the channel", ""
    if not member:
        return f"{_who(lead)} is not in {where}; the lead must be in the channel", ""
    return "", slack_id


def leader_refusal(leader: Agent, members: list[Agent]) -> str:
    """"" when `leader`'s login may RUN every other member (an owner/admin edge,
    the same edge /agent-topology shows), else the plain reason naming them."""
    from apps.agents import topology

    if not members:
        return ""
    if leader.user_id is None:
        return (f"{leader.slug} has no canopy login of its own, so it can't lead a check-in with other "
                "agents — link one first")
    cant = [m.slug for m in members if not topology.can_run(leader, m)]
    if cant:
        return (f"{leader.slug} can't run {', '.join(cant)}: its login ({_who(leader.user)}) must be an owner "
                f"or admin of {'each of them' if len(cant) > 1 else 'it'} — make it an agent admin, or take "
                f"{'them' if len(cant) > 1 else 'it'} off the check-in")
    return ""


def _tree(workspace_id: str) -> set[str]:

    ws = Workspace.objects.get(slug=workspace_id)
    while ws.parent_id:
        ws = ws.parent
    return {w.slug for w, _ in wsvc.subtree(ws)}


def may_write(user, project: AgentProject) -> bool:
    """The project's owner, or an owner or admin of its workspace."""
    if not getattr(user, "is_authenticated", False):
        return False
    return project.owner_user_id == user.pk or perms.can(user, project.workspace_id, perms.INTEGRATIONS)


# --- saving a check-in -------------------------------------------------------------

def _agents(slugs: list[str], project: AgentProject) -> list[Agent]:
    """The check-in's agents, the project's own first — it is always one."""
    own = project.agent
    slugs = list(dict.fromkeys([own.slug, *(s.strip() for s in slugs if s and s.strip())]))
    tree = _tree(own.workspace_id)
    found = {a.slug: a for a in Agent.objects.select_related("user", "workspace")
             .filter(slug__in=slugs, workspace_id__in=tree)}
    missing = [s for s in slugs if s not in found]
    if missing:
        raise CheckinError(f"no agent {', '.join(missing)} in the {own.workspace_id} workspace tree", 404)
    return [found[s] for s in slugs]


def _validate_cron(cron: str, tz: str) -> None:
    from canopy_cron import next_slots, validate_timezone

    try:
        validate_timezone(tz)
        next_slots(cron, tz, now=timezone.now(), count=1)
    except Exception as e:  # noqa: BLE001
        raise CheckinError(f"cadence: {e}") from e


def save(*, project: AgentProject, by, data: dict, checkin: ProjectCheckin | None = None) -> ProjectCheckin:
    """Create (`checkin` None) or change a project's check-in, with every check the
    spec names. `data` holds the fields to set (a PATCH sends only what changes;
    `channel: ""` takes the channel off). Raises CheckinError with a plain reason."""
    from django.contrib.auth import get_user_model

    def pick(key, default=None):
        return data[key] if data.get(key) is not None else default

    agent = project.agent
    if agent is None:
        raise CheckinError("a check-in is run by the project's lead agent — give the project one first", 409)
    if not may_write(by, project):
        raise CheckinError(f"only the project's owner, or an owner or admin of {project.workspace_id}, can change "
                           "its check-ins", 403)
    name = (pick("name", getattr(checkin, "name", "")) or "").strip()
    if not name:
        raise CheckinError("`name` is required")
    clash = ProjectCheckin.objects.filter(project=project, name__iexact=name)
    if checkin is not None:
        clash = clash.exclude(pk=checkin.pk)
    if clash.exists():
        raise CheckinError(f"{project.name} already has a check-in called {name!r}", 409)
    if checkin is None:
        for k in ("lead_email", "cron"):
            if not data.get(k):
                raise CheckinError(f"`{k}` is required")
    topic = (pick("topic", getattr(checkin, "topic", "")) or "").strip()
    members = _agents(pick("agents", getattr(checkin, "agents", None) or []), project)
    why = leader_refusal(agent, [m for m in members if m.pk != agent.pk])
    if why:
        raise CheckinError(why, 409)
    lead = checkin.lead if checkin else None
    if data.get("lead_email"):
        lead = get_user_model().objects.filter(email__iexact=data["lead_email"].strip()).first()
        if lead is None:
            raise CheckinError(f"no canopy account has the email {data['lead_email']}", 404)
    if lead is None:
        raise CheckinError("a check-in needs a lead")
    if not wsvc.is_member(lead, agent.workspace_id):
        raise CheckinError(f"the lead can't lead this check-in: {_who(lead)} is not a member of the "
                           f"{agent.workspace_id} canopy workspace", 409)

    # The channel: "" (or absent on create) means none; absent on a PATCH keeps it.
    channel_ref = data.get("channel")
    if channel_ref is None and checkin is not None:
        channel_id, channel_name = checkin.channel_id, checkin.channel_name
        installation = checkin.installation
    else:
        channel_id = channel_name = ""
        installation = None
        channel_ref = (channel_ref or "").strip()
    lead_slack = ""
    if channel_ref:
        link = ingest_svc.link_for(agent.workspace_id)
        if link is None:
            raise CheckinError(f"Slack is not connected to {agent.workspace_id}", 409)
        installation = link.installation
        try:
            channel_id, channel_name = ingest_svc.resolve_channel(installation.bot_token, channel_ref)
        except ingest_svc.IngestError as e:
            raise CheckinError(str(e), e.status) from e
        channel_name = channel_name or channel_ref.lstrip("#")
    if channel_id:
        why, lead_slack = lead_refusal(workspace_id=agent.workspace_id, lead=lead, token=installation.bot_token,
                                       channel_id=channel_id, channel_name=channel_name)
        if why:
            raise CheckinError(f"the lead can't lead this check-in: {why}", 409)
    cron = pick("cron", checkin.schedule.cron if checkin and checkin.schedule else "")
    tz = pick("timezone", checkin.schedule.timezone if checkin and checkin.schedule else "UTC")
    if not cron:
        raise CheckinError("`cron` is required")
    _validate_cron(cron, tz)
    enabled = bool(pick("enabled", checkin.enabled if checkin else True))

    with transaction.atomic():
        old = (checkin.channel_id, checkin.added_allowance) if checkin else None
        if checkin is None:
            checkin = ProjectCheckin(project=project, created_by=by)
        checkin.name, checkin.topic, checkin.lead, checkin.lead_slack_id = name, topic, lead, lead_slack
        checkin.installation, checkin.channel_id, checkin.channel_name = installation, channel_id, channel_name
        checkin.enabled = enabled
        moved = old is None or old[0] != channel_id
        if channel_id and ingest_svc.allowance_for(agent, channel_id) is None:
            # Only a missing entry is added, and only by a person allowed to put a
            # channel on an agent's reading list. One already there was put on the
            # list by someone allowed to (often the agent's own admin), so it is
            # reused as is: re-adding it would overwrite who added it, and is
            # refused outright when the agent itself saves the check-in.
            if not perms.can(by, agent.workspace_id, perms.INTEGRATIONS):
                raise CheckinError(f"only an owner or admin of {agent.workspace_id} can put a channel on "
                                   f"{agent.slug}'s reading list", 403)
            try:
                ingest_svc.allow(workspace=agent.workspace, agent=agent, channel=channel_id, follow=False,
                                 note=f"check-in: {project.name} — {name}", by=by)
            except ingest_svc.IngestError as e:
                raise CheckinError(str(e), e.status) from e
            checkin.added_allowance = True
        elif moved:
            checkin.added_allowance = False
        checkin.save()
        checkin.agent_members.set(members)
        _save_schedule(checkin, cron=cron, tz=tz, by=by)
        if old and moved and old[0]:
            _release_channel(agent, old[0], by=by, keep=checkin, owned=old[1])
    _participants(checkin, members, lead)
    _event(checkin, "saved", f"{_who(by)} saved {label(checkin)}: lead {_who(lead)}, "
           f"{'#' + channel_name if channel_id else 'no channel'}, agents {', '.join(checkin.agents)}",
           user=by.pk)
    return checkin


def _save_schedule(checkin: ProjectCheckin, *, cron: str, tz: str, by) -> None:

    s = checkin.schedule or AgentSchedule(created_by=by, prompt=BASE_PROMPT)
    s.agent = checkin.project.agent
    s.name = f"Check-in: {checkin.project.name} — {checkin.name}"[:200]
    s.cron, s.timezone, s.run_once_at = cron, tz, None
    s.enabled = checkin.enabled
    s.updated_by = by
    s.save()
    if checkin.schedule_id != s.pk:
        checkin.schedule = s
        checkin.save(update_fields=["schedule", "updated_at"])


def _participants(checkin: ProjectCheckin, members: list[Agent], lead) -> None:
    """The lead is a `manual` participant; every agent other than the project's
    is a `checkin` one (its login's person). Best-effort: never fails a save."""
    from apps.agents import participants
    from apps.contacts.services import person_for

    p = checkin.project
    try:
        person = person_for(user=lead) if lead else None
        if person is not None:
            participants.ensure(p, person, source=ProjectParticipant.MANUAL, role="check-in lead")
        for m in members:
            if m.pk != p.agent_id and m.user_id:
                participants.ensure(p, person_for(user=m.user), source=ProjectParticipant.CHECKIN,
                                    role=f"{m.slug}, {checkin.name}")
    except Exception:  # noqa: BLE001
        logger.exception("could not add a check-in's participants")


def _release_channel(agent: Agent, channel_id: str, *, by, keep: ProjectCheckin | None = None,
                     owned: bool = True) -> None:
    """Take the channel off `agent`'s reading list — only when a check-in put it
    there (`owned`) and no other check-in still needs it. When another does, it
    inherits the entry, so the last check-in to go still removes it."""
    if not owned or not channel_id:
        return
    others = ProjectCheckin.objects.filter(project__agent=agent, channel_id=channel_id)
    if keep is not None:
        others = others.exclude(pk=keep.pk)
    heir = others.order_by("pk").first()
    if heir is not None:
        if not heir.added_allowance:
            heir.added_allowance = True
            heir.save(update_fields=["added_allowance", "updated_at"])
        return
    entry = ingest_svc.allowance_for(agent, channel_id)
    if entry is not None:
        ingest_svc.disallow(entry, by=by)


def delete(checkin: ProjectCheckin, *, by) -> None:
    """Delete a check-in. Its runs stay on the project's timeline."""
    from apps.harness import schedule_turns

    with transaction.atomic():
        _release_channel(checkin.project.agent, checkin.channel_id, by=by, keep=checkin,
                         owned=checkin.added_allowance)
        for run in checkin.runs.filter(status=CheckinRun.AWAITING_CONFIRM):
            CheckinRun.objects.filter(pk=run.pk).update(status=CheckinRun.EXPIRED, note="the check-in was deleted",
                                                        finished_at=timezone.now())
            _settle(run, f":wastebasket: The *{checkin.name}* check-in was deleted. Nothing was read.",
                    checkin=checkin)
        if checkin.schedule is not None:
            schedule_turns.supersede_open_turns(checkin.schedule, reason="check-in deleted")
            checkin.schedule.delete()
        _event(checkin, "deleted", f"{_who(by)} deleted {label(checkin)}", user=by.pk)
        checkin.delete()


# --- the announcement --------------------------------------------------------------

def _agents_line(slugs: list[str]) -> str:
    names = [f"`{s}`" for s in slugs]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def read_from(checkin: ProjectCheckin, slot: dt.datetime) -> dt.datetime:
    """Where a run's read starts, fixed when it is announced: the announcement
    quotes it, the lead confirms THAT, and the read is cut to it
    (slack.ingest.start_group_read / transcript `oldest`). The end of this
    check-in's last read; for its first run, one cadence before the slot — never
    the whole channel. Other reads of the channel (a `read-channel`, a revoked
    read) don't count: they weren't this check-in's."""
    prev = (checkin.runs.exclude(read_last_ts="").filter(slot__lt=slot).order_by("-slot").first())
    if prev is not None:
        return dt.datetime.fromtimestamp(float(prev.read_last_ts), tz=dt.UTC)
    from canopy_cron import next_slots

    sched = checkin.schedule
    try:
        a, b = next_slots(sched.cron, sched.timezone, now=slot, count=2)
        return slot - (b - a)
    except Exception:  # noqa: BLE001 — a cadence that can't be stepped reads one week
        return slot - dt.timedelta(days=7)


def _since(run: CheckinRun) -> str:
    return ingest_svc._slack_date(run.read_since.timestamp())


def announcement(run: CheckinRun, checkin: ProjectCheckin | None = None) -> tuple[str, list[dict]]:
    c = checkin or run.checkin
    agents = (run.participants or {}).get("agents") or c.agents
    text = (f":calendar: *{label(c)}* is due. <@{c.lead_slack_id}>: Confirm to have {_agents_line(agents)} "
            f"read what was posted here since {_since(run)} and work on it.")
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"Only <@{c.lead_slack_id}>, the check-in's lead, can confirm. Nothing here is read until then; "
            "unconfirmed, this lapses in 24 hours.")}]},
        {"type": "actions", "block_id": "group_run", "elements": [
            {"type": "button", "action_id": CONFIRM, "style": "primary", "value": str(run.pk),
             "text": {"type": "plain_text", "text": "Confirm"}},
            {"type": "button", "action_id": SKIP, "value": str(run.pk),
             "text": {"type": "plain_text", "text": "Skip"}},
        ]},
    ]
    return f"{label(c)} is due — the lead confirms to have it read this channel.", blocks


def _token(run: CheckinRun, checkin: ProjectCheckin | None = None) -> str:
    c = checkin or run.checkin
    if c is not None and c.installation_id:
        return c.installation.bot_token
    link = ingest_svc.link_for(run.project.agent.workspace_id)
    return link.installation.bot_token if link is not None else ""


def _settle(run: CheckinRun, outcome: str, *, checkin: ProjectCheckin | None = None) -> None:
    """The announcement once answered: what was asked, what happened, no buttons."""
    if not run.announce_ts or not run.channel_id:
        return
    try:
        _text, blocks = announcement(run, checkin)
        client.update_message(_token(run, checkin), channel=run.channel_id, ts=run.announce_ts,
                              text=outcome, blocks=[blocks[0], {"type": "context", "elements": [
                                  {"type": "mrkdwn", "text": outcome[:3000]}]}])
    except Exception:  # noqa: BLE001 — the run's state stands; the post is a courtesy
        logger.exception("could not update a check-in announcement")


def _thread(run: CheckinRun, text: str) -> None:
    if not run.announce_ts or not run.channel_id:
        return
    try:
        client.post_message(_token(run), channel=run.channel_id, thread_ts=run.announce_ts, text=text)
    except Exception:  # noqa: BLE001
        logger.exception("could not post in a check-in thread")


def _fail(run: CheckinRun, why: str, *, tell_thread: bool = False) -> CheckinRun:
    CheckinRun.objects.filter(pk=run.pk).update(status=CheckinRun.FAILED, note=why[:500],
                                                finished_at=timezone.now())
    run.refresh_from_db()

    _event(run, "run_failed", f"{run.checkin_name} on {run.project.name} (run {run.pk}) failed: {why}",
           level=Event.WARN, run=run.pk, reason=why[:500])
    if tell_thread:
        _thread(run, f":warning: {why}")
    return run


def _snapshot(checkin: ProjectCheckin) -> dict:
    return {"agents": list(checkin.agents or []), "lead_email": getattr(checkin.lead, "email", "") or ""}


def announce(schedule, slot: dt.datetime, *, manual: bool = False) -> tuple[CheckinRun, bool]:
    """The check-in's schedule came due: create the run (idempotent per slot) and
    — with a channel — post the announcement; without one, queue the agent's turn.
    Never a read."""
    checkin = (ProjectCheckin.objects.select_related("project", "project__agent", "project__agent__user",
                                                     "project__agent__workspace", "lead", "installation")
               .get(schedule=schedule))
    with transaction.atomic():
        run, created = CheckinRun.objects.get_or_create(
            checkin=checkin, slot=slot, defaults={
                "manual": manual, "project": checkin.project, "checkin_name": checkin.name,
                "participants": _snapshot(checkin), "channel_id": checkin.channel_id,
                "channel_name": checkin.channel_name,
                "read_since": read_from(checkin, slot) if checkin.has_channel else None})
        if created and (schedule.last_slot is None or slot > schedule.last_slot) and not manual:
            schedule.last_slot = slot
            schedule.save(update_fields=["last_slot", "updated_at"])
    if not created:
        return run, False
    # You only ever owe the newest: an older unanswered announcement lapses.
    for old in checkin.runs.filter(status=CheckinRun.AWAITING_CONFIRM).exclude(pk=run.pk):
        if CheckinRun.objects.filter(pk=old.pk, status=CheckinRun.AWAITING_CONFIRM).update(
                status=CheckinRun.EXPIRED, note="a newer run was announced", finished_at=timezone.now()):
            old.refresh_from_db()
            _settle(old, ":hourglass: Superseded by a newer announcement. Nothing was read.", checkin=checkin)
            _engage("run_expired", old, reason="superseded")
    # The checks, again: membership and access change between saves.
    if not checkin.enabled:
        return _fail(run, "the check-in is disabled"), True
    agent = checkin.project.agent
    members = list(Agent.objects.select_related("user").filter(slug__in=checkin.agents))
    why = leader_refusal(agent, [m for m in members if m.pk != agent.pk])
    if not why and len(members) != len(set(checkin.agents)):
        why = "an agent of the check-in no longer exists"
    if not why and checkin.lead is None:
        why = "the check-in has no lead"
    if not why and not checkin.has_channel and not wsvc.is_member(checkin.lead, agent.workspace_id):
        why = f"the lead no longer qualifies: {_who(checkin.lead)} is not a member of {agent.workspace_id}"
    if why:
        return _fail(run, why), True
    if not checkin.has_channel:
        from apps.harness.schedule_turns import schedule_initiator

        # Nobody confirms a check-in that reads nothing: the schedule fired it,
        # and whoever made the schedule is accountable for it — as any schedule.
        _start_turn(run, checkin, initiator=schedule_initiator(schedule, manual=manual), manual=manual)
        _event(run, "run_started", f"{label(checkin)}: no channel, so nothing to read; {agent.slug}'s turn "
               f"queued (run {run.pk})", run=run.pk, manual=manual)
        return run, True
    token = checkin.installation.bot_token
    why, lead_slack = lead_refusal(workspace_id=agent.workspace_id, lead=checkin.lead, token=token,
                                   channel_id=checkin.channel_id, channel_name=checkin.channel_name)
    if why:
        return _fail(run, f"the lead no longer qualifies: {why}"), True
    if lead_slack != checkin.lead_slack_id:
        ProjectCheckin.objects.filter(pk=checkin.pk).update(lead_slack_id=lead_slack)
        checkin.lead_slack_id = lead_slack
    text, blocks = announcement(run, checkin)
    try:
        ts = client.post_message(token, channel=checkin.channel_id, text=text, blocks=blocks)
    except client.SlackApiError as e:
        return _fail(run, f"Slack refused the announcement ({e.error})"), True
    CheckinRun.objects.filter(pk=run.pk).update(announce_ts=ts)
    run.announce_ts = ts
    _event(run, "run_announced", f"{label(checkin)} announced in <#{checkin.channel_id}> (run {run.pk})",
           run=run.pk, manual=manual)
    _engage("run_announced", run, manual=manual)
    return run, True


# --- the lead's click ----------------------------------------------------------------

def _from_click(installation, run_id: str, channel_id: str, message_ts: str) -> CheckinRun | None:
    try:
        return (CheckinRun.objects.select_related("checkin", "checkin__lead", "checkin__installation",
                                                  "project", "project__agent", "project__agent__workspace")
                .filter(pk=int(run_id), checkin__installation=installation, channel_id=channel_id,
                        announce_ts=message_ts).first())
    except (TypeError, ValueError):
        return None


def click(installation, *, run_id: str, slack_user_id: str, channel_id: str, message_ts: str,
          confirm: bool) -> str:
    """Confirm / Skip on an announcement. Returns what to tell the clicker, privately ("" for nothing)."""

    run = _from_click(installation, run_id, channel_id, message_ts)
    if run is None or run.status != CheckinRun.AWAITING_CONFIRM:
        return "That check-in is no longer waiting for an answer."
    c = run.checkin
    agent = run.project.agent
    verb = "Confirm" if confirm else "Skip"
    if not c.lead_slack_id or slack_user_id != c.lead_slack_id:
        _event(run, "click_refused", f"<@{slack_user_id}> pressed {verb} on {label(c)} and was refused: "
               "not the lead", level=Event.WARN, run=run.pk, slack_user=slack_user_id)
        return f"Only the check-in's lead, <@{c.lead_slack_id}>, can confirm or skip it."
    if not confirm:
        if not CheckinRun.objects.filter(pk=run.pk, status=CheckinRun.AWAITING_CONFIRM).update(
                status=CheckinRun.SKIPPED, note=f"skipped by <@{slack_user_id}>",
                confirmed_by_slack=slack_user_id, finished_at=timezone.now()):
            return "That check-in was already answered."
        run.refresh_from_db()
        _settle(run, f":fast_forward: Skipped by <@{slack_user_id}>. Nothing was read.")
        _event(run, "run_skipped", f"<@{slack_user_id}> skipped {label(c)} (run {run.pk})", run=run.pk)
        _engage("run_skipped", run, user=c.lead)
        return ""
    if c.lead is None or not wsvc.is_member(c.lead, agent.workspace_id):
        _fail(run, "the lead is no longer a member of the canopy workspace")
        _settle(run, ":warning: The lead no longer has access to this check-in. Nothing was read.")
        return "You're no longer a member of the canopy workspace this check-in belongs to."
    try:
        with transaction.atomic():
            if not CheckinRun.objects.filter(pk=run.pk, status=CheckinRun.AWAITING_CONFIRM).update(
                    status=CheckinRun.READING, confirmed_by=c.lead, confirmed_by_slack=slack_user_id,
                    confirmed_at=timezone.now()):
                return "That check-in was already answered."
            row = ingest_svc.start_group_read(
                agent=agent, installation=c.installation, channel_id=c.channel_id, user=c.lead,
                run_id=run.pk, purpose=label(c), oldest=run.read_since)
            CheckinRun.objects.filter(pk=run.pk).update(ingest=row)
    except ingest_svc.IngestError as e:
        _event(run, "confirm_refused", f"{label(c)} could not start reading: {e}", level=Event.WARN, run=run.pk)
        return str(e)
    run.refresh_from_db()
    _settle(run, f":white_check_mark: Confirmed by <@{slack_user_id}>. canopy is reading this channel for "
                 f"`{agent.slug}`.")
    _event(run, "run_confirmed", f"<@{slack_user_id}> confirmed {label(c)} (run {run.pk}); reading "
           f"<#{c.channel_id}> for {agent.slug}", run=run.pk)
    _engage("run_confirmed", run, user=c.lead)
    return ""


# --- the turn ----------------------------------------------------------------------

def _turn_prompt(checkin: ProjectCheckin | None, run: CheckinRun) -> str:
    base = ((checkin.schedule.prompt if checkin and checkin.schedule else "") or BASE_PROMPT).strip()
    return f"{base} --checkin-run {run.pk}"


def _start_turn(run: CheckinRun, checkin: ProjectCheckin, *, initiator, manual: bool = False,
                **updates) -> None:
    """Queue the project's agent's turn for `run` and mark it running."""
    from apps.harness import services as hsvc
    from apps.harness import schedule_turns

    sched = checkin.schedule
    project = run.project
    origin_ref = {"kind": TURN_KIND, "checkin": checkin.pk, "checkin_name": checkin.name, "run": run.pk,
                  "project": project.ext_id, "slot": run.slot.isoformat()}
    if manual:
        origin_ref["manual"] = True
    if sched is not None:
        origin_ref.update(schedule_id=sched.id, schedule_name=sched.name)
    with transaction.atomic():
        if sched is not None:
            schedule_turns.supersede_open_turns(sched, reason=f"superseded by check-in run {run.pk}")
        turn, _ = hsvc.enqueue_turn(
            agent=project.agent, origin=Turn.ORIGIN_CANOPY_SCHEDULER, idempotency_key=f"checkin-run:{run.pk}",
            prompt=_turn_prompt(checkin, run), origin_ref=origin_ref,
            routing=sched.routing if sched is not None else Turn.PREFER_LOCAL,
            initiator=initiator,
        )
        CheckinRun.objects.filter(pk=run.pk).update(status=CheckinRun.RUNNING, turn=turn, **updates)
    run.refresh_from_db()


# --- the read, delivered ----------------------------------------------------------------

def deliver_read(run_id: int, ingest_id) -> None:
    """The confirmed read is done (slack.ingest._deliver): keep the material,
    move the read mark, queue the agent's turn, tell the thread."""

    run = (CheckinRun.objects.select_related("checkin", "checkin__schedule", "checkin__installation",
                                             "checkin__lead", "project", "project__agent")
           .filter(pk=run_id).first())
    if run is None or run.status != CheckinRun.READING:
        return
    c = run.checkin
    agent = run.project.agent
    if c is None:
        _fail(run, "the check-in was deleted while its channel was being read", tell_thread=True)
        return
    row = SlackChannelIngest.objects.select_related("agent", "decided_by").get(pk=ingest_id)
    if row.status not in ingest_svc.LIVE:
        _fail(run, f"canopy couldn't read this channel for `{agent.slug}`: {row.error or row.status}",
              tell_thread=True)
        return
    since = run.read_since            # what the lead confirmed — the read is cut to it
    # Cut by the confirmed start alone, not the agent's read mark: a `read-channel`
    # in between moves that mark, and the check-in would silently skip what it read.
    # canopy's own posts (the announcement, "Confirmed by…", earlier runs' lines)
    # are not what people said: leave them out of the material.
    own = frozenset(u for u in (c.installation.bot_user_id,) if u)
    t = ingest_svc.transcript(row, oldest=since.timestamp() if since else 0.0, skip_users=own)
    ingest_svc.mark_read(row, t["mark"])
    SlackChannelIngest.objects.filter(pk=row.pk).update(last_read_by_slack=(c.lead_slack_id or "")[:32])
    from apps.harness import initiator as who

    # The lead's Confirm is why it runs: a person approving this work.
    _start_turn(run, c, initiator=who.for_user(c.lead, via=f"slack:checkin-run:{run.pk}", assurance=who.APPROVAL),
                material=t["markdown"], read_messages=t["messages"],
                read_first_ts=t["first_ts"] or "", read_last_ts=t["last_ts"] or run.read_last_ts or "")
    _event(run, "run_started", f"{label(c)}: read {t['messages']} message(s); {agent.slug}'s turn "
           f"queued (run {run.pk})", run=run.pk, turn=str(run.turn_id), messages=t["messages"])
    _thread(run, ingest_svc._what_was_read(agent.slug, t, since, as_confirmed=True)
            + f"\nStarted — follow it at {page_url(run.project)}")


# --- the finish ----------------------------------------------------------------

def proposed_tasks(run: CheckinRun):
    """The tasks this run proposed in its project (`tasks/propose/` with `run`)."""
    from apps.agents import proposals

    return AgentTask.objects.filter(project=run.project, origin_ref__kind=proposals.CHECKIN_PROPOSAL,
                                    origin_ref__run=run.pk)


def finish(run: CheckinRun, *, huddle_id: str, result_url: str, summary: str,
           tasks_proposed: int | None = None, update: dict | None = None) -> CheckinRun:
    """The project agent's login reports the run done. Idempotent once done.
    `update` ({health, body}) drafts the project's status update for its lead
    to publish (`work.post_update`: an agent's update is always a draft)."""
    if run.status == CheckinRun.DONE:
        return run
    if run.status != CheckinRun.RUNNING:
        raise CheckinError(f"run {run.pk} is {run.status}, not running", 409)
    n = tasks_proposed if tasks_proposed is not None else proposed_tasks(run).count()
    CheckinRun.objects.filter(pk=run.pk, status=CheckinRun.RUNNING).update(
        status=CheckinRun.DONE, huddle_id=(huddle_id or "")[:128], result_url=(result_url or "")[:500],
        summary=summary or "", tasks_proposed=max(0, int(n)), finished_at=timezone.now())
    run.refresh_from_db()
    if update:
        from apps.agents import work  # cycle: agents.work -> agents.models <- huddles  # noqa: PLC0415

        work.post_update(run.project, health=update["health"], body=update.get("body") or summary or "",
                         author_agent=run.project.agent, checkin_run=run)
    link = run.result_url
    if not link and run.huddle_id:
        from apps.slack.services import public_url

        link = public_url(f"/w/{run.project.agent.workspace_id}/huddles/{run.huddle_id}")
    link = link or page_url(run.project)
    n = run.tasks_proposed
    _thread(run, f":white_check_mark: done — {n} proposed task{'' if n == 1 else 's'}: {link}")
    _event(run, "run_done", f"{run.checkin_name} on {run.project.name} done (run {run.pk})", run=run.pk,
           huddle=run.huddle_id, tasks=n)
    return run


# --- the clock ----------------------------------------------------------------

def expire_due(*, now: dt.datetime | None = None) -> int:
    """Lapse announcements nobody confirmed within 24 h; fail reads that never came back."""
    now = now or timezone.now()
    n = 0
    for run in (CheckinRun.objects.select_related("checkin", "checkin__installation", "project", "project__agent")
                .filter(status=CheckinRun.AWAITING_CONFIRM, created_at__lt=now - EXPIRE_AFTER)):
        if not CheckinRun.objects.filter(pk=run.pk, status=CheckinRun.AWAITING_CONFIRM).update(
                status=CheckinRun.EXPIRED, note="nobody confirmed within 24 hours", finished_at=now):
            continue
        run.refresh_from_db()
        _settle(run, ":hourglass: Nobody confirmed within 24 hours, so this check-in lapsed. Nothing was read.")
        _event(run, "run_expired", f"{run.checkin_name} on {run.project.name} lapsed unconfirmed (run {run.pk})",
               run=run.pk)
        _engage("run_expired", run)
        n += 1
    for run in CheckinRun.objects.select_related("project").filter(
            status=CheckinRun.READING, confirmed_at__lt=now - READ_TIMEOUT):
        _fail(run, "the channel read never finished", tell_thread=True)
        n += 1
    return n
