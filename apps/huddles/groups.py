"""Work groups: a lead, a Slack channel, agents and a cadence (spec
docs/superpowers/specs/2026-10-16-work-group-huddles-design.md, layer 3).

The life of one run (`HuddleGroupRun`):

1. **announce** — the group's schedule fires (harness.schedule_turns). No turn:
   canopy posts "<group> huddle is due" in the channel with Confirm / Skip, after
   checking again that the lead is in the workspace and the channel and that
   the leader may run every member. A failed check fails the run quietly — an
   Event, nothing in Slack, nothing read.
2. **click** — only the lead's click counts. Skip ends it. Confirm is the
   person's command in Slack that Jonathan's rule asks for: it starts the read
   (slack.ingest.start_group_read) for the leader agent. Nothing else does.
3. **deliver_read** — the read is done: the material is stored on the run, the
   read mark moves, the leader's turn is queued (`<schedule prompt> --group-run
   <id>`), and the announcement's thread is told what was read.
4. **finish** — the leader's login reports the result; the thread gets one line
   pointing at it. The content lives in canopy, not in Slack.

Unconfirmed runs expire after 24 hours (`expire_due`, on the channel-ingest loop).
"""
from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.utils import timezone

from apps.agents.models import Agent, AgentProject, AgentTask
from apps.slack import client
from apps.slack import ingest as ingest_svc
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from .models import HuddleGroup, HuddleGroupRun

logger = logging.getLogger(__name__)

#: Block Kit action ids on an announcement (routed in slack.views.interactions).
CONFIRM, SKIP = "group_run_confirm", "group_run_skip"
ACTIONS = (CONFIRM, SKIP)
#: The generic command every group schedule runs; dispatch appends ` --group-run <id>`.
#: The canopy command decides: one agent → the single-agent procedure on the
#: group's project; several → the huddle.
BASE_PROMPT = "/canopy:huddle"
EXPIRE_AFTER = dt.timedelta(hours=24)
#: A confirmed read that never came back (revoked, crashed) fails after this.
READ_TIMEOUT = dt.timedelta(hours=24)


class GroupError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# --- bookkeeping ---------------------------------------------------------------

def _event(group: HuddleGroup, kind: str, summary: str, *, level: str = "", **payload) -> None:
    from apps.events import services as events_services
    from apps.events.models import Event

    try:
        events_services.record([{
            "source": "huddles", "kind": f"huddles.group_{kind}", "level": level or Event.INFO,
            "summary": summary[:500], "payload": {"group": group.slug, **payload},
        }], workspace=group.workspace)
    except Exception:  # noqa: BLE001 — bookkeeping never fails the act
        logger.exception("could not record a work group event")


def _engage(kind: str, run: HuddleGroupRun, *, user=None, surface: str = "slack", **meta) -> None:
    """What people actually do with runs (layer 1's EngagementEvent)."""
    from apps.agents import engagement

    try:
        g = run.group
        engagement.record(kind, workspace=g.workspace, surface=surface, user=user, agent=g.leader_agent,
                          group=g.slug, meta={"run": run.pk, **meta})
    except Exception:  # noqa: BLE001
        logger.exception("could not record work group engagement")


def _who(user) -> str:
    return getattr(user, "email", "") or "someone"


def page_url(group: HuddleGroup) -> str:
    from apps.slack.services import public_url

    return public_url(f"/w/{group.workspace_id}/huddles/groups/{group.slug}")


# --- the checks ----------------------------------------------------------------

def lead_refusal(*, workspace_id: str, lead, token: str, channel_id: str, channel_name: str = "") -> tuple[str, str]:
    """("", lead's Slack id) when the lead may lead, else (the plain reason, "").

    The lead must be a member of the canopy workspace AND of the Slack channel
    (`users.lookupByEmail` + paginated `conversations.members`)."""
    where = f"#{channel_name}" if channel_name else channel_id
    if lead is None:
        return "the work group has no lead", ""
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

    if leader.user_id is None:
        return (f"{leader.slug} has no canopy login of its own, so it can't run a work group — "
                "link one first")
    cant = [m.slug for m in members if not topology.can_run(leader, m)]
    if cant:
        return (f"{leader.slug} can't run {', '.join(cant)}: its login ({_who(leader.user)}) must be an owner "
                f"or admin of {'each of them' if len(cant) > 1 else 'it'} — make it an agent admin, or pick "
                "another leader")
    return ""


def _tree(workspace_id: str) -> set[str]:
    from apps.workspaces.models import Workspace

    ws = Workspace.objects.get(slug=workspace_id)
    while ws.parent_id:
        ws = ws.parent
    return {w.slug for w, _ in wsvc.subtree(ws)}


# --- saving a group ----------------------------------------------------------------

def _agents(slugs: list[str], workspace_id: str) -> list[Agent]:
    slugs = list(dict.fromkeys(s.strip() for s in slugs if s and s.strip()))
    if not slugs:
        raise GroupError("a work group needs at least one agent")
    tree = _tree(workspace_id)
    found = {a.slug: a for a in Agent.objects.select_related("user", "workspace")
             .filter(slug__in=slugs, workspace_id__in=tree)}
    missing = [s for s in slugs if s not in found]
    if missing:
        raise GroupError(f"no agent {', '.join(missing)} in the {workspace_id} workspace tree", 404)
    return [found[s] for s in slugs]


def _leader(slug: str, members: list[Agent], workspace_id: str) -> Agent:
    if not slug:
        if len(members) == 1:
            return members[0]
        raise GroupError("a work group with more than one agent needs `leader_agent`, the agent that runs it")
    for m in members:
        if m.slug == slug:
            return m
    (leader,) = _agents([slug], workspace_id)
    return leader


def _validate_cron(cron: str, tz: str) -> None:
    from canopy_cron import next_slots, validate_timezone

    try:
        validate_timezone(tz)
        next_slots(cron, tz, now=timezone.now(), count=1)
    except Exception as e:  # noqa: BLE001
        raise GroupError(f"cadence: {e}") from e


def save(*, workspace, by, data: dict, group: HuddleGroup | None = None) -> HuddleGroup:
    """Create (`group` None) or change a work group, with every check the spec
    names. `data` holds the fields to set (a PATCH sends only what changes).
    Raises GroupError with a plain reason."""
    from django.contrib.auth import get_user_model

    def pick(key, default=None):
        return data[key] if data.get(key) is not None else default

    if group is None:
        slug = (data.get("slug") or "").strip().lower()
        if not slug or slug == "runs":
            raise GroupError("`slug` is required (and may not be \"runs\")")
        if HuddleGroup.objects.filter(workspace=workspace, slug=slug).exists():
            raise GroupError(f"there is already a work group '{slug}' here", 409)
        for k in ("name", "channel", "lead_email", "agents", "cron"):
            if not data.get(k):
                raise GroupError(f"`{k}` is required")
    else:
        slug = group.slug
    name = (pick("name", getattr(group, "name", "")) or "").strip()
    topic = (pick("topic", getattr(group, "topic", "")) or "").strip()
    members = _agents(pick("agents", getattr(group, "agents", [])), workspace.slug)
    leader = _leader((pick("leader_agent", "") or (group.leader_agent.slug if group else "")).strip(),
                     members, workspace.slug)
    if not perms.can(by, leader.workspace_id, perms.INTEGRATIONS):
        raise GroupError(f"only an owner or admin of {leader.workspace_id} can put a channel on "
                         f"{leader.slug}'s reading list", 403)
    why = leader_refusal(leader, [m for m in members if m.pk != leader.pk])
    if why:
        raise GroupError(why, 409)
    link = ingest_svc.link_for(leader.workspace_id)
    if link is None:
        raise GroupError(f"Slack is not connected to {leader.workspace_id}", 409)
    token = link.installation.bot_token
    channel_ref = pick("channel", "")
    if channel_ref:
        try:
            channel_id, channel_name = ingest_svc.resolve_channel(token, channel_ref)
        except ingest_svc.IngestError as e:
            raise GroupError(str(e), e.status) from e
        channel_name = channel_name or channel_ref.lstrip("#")
    else:
        channel_id, channel_name = group.channel_id, group.channel_name
    lead = group.lead if group else None
    if data.get("lead_email"):
        lead = get_user_model().objects.filter(email__iexact=data["lead_email"].strip()).first()
        if lead is None:
            raise GroupError(f"no canopy account has the email {data['lead_email']}", 404)
    why, lead_slack = lead_refusal(workspace_id=workspace.slug, lead=lead, token=token,
                                   channel_id=channel_id, channel_name=channel_name)
    if why:
        raise GroupError(f"the lead can't lead this group: {why}", 409)
    cron = pick("cron", group.schedule.cron if group and group.schedule else "")
    tz = pick("timezone", group.schedule.timezone if group and group.schedule else "UTC")
    if not cron:
        raise GroupError("`cron` is required")
    _validate_cron(cron, tz)
    enabled = bool(pick("enabled", group.enabled if group else True))

    with transaction.atomic():
        old = (group.leader_agent, group.channel_id) if group else None
        if group is None:
            group = HuddleGroup(workspace=workspace, slug=slug, created_by=by)
        group.name, group.topic, group.lead, group.lead_slack_id = name, topic, lead, lead_slack
        group.installation, group.channel_id, group.channel_name = link.installation, channel_id, channel_name
        group.agents = [m.slug for m in members]
        group.leader_agent, group.enabled = leader, enabled
        group.save()
        _save_schedule(group, cron=cron, tz=tz, by=by)
        try:
            ingest_svc.allow(workspace=leader.workspace, agent=leader, channel=channel_id, follow=False,
                             note=f"work group {name}", by=by)
        except ingest_svc.IngestError as e:
            raise GroupError(str(e), e.status) from e
        if old and old != (leader, channel_id):
            _release_channel(old[0], old[1], by=by, keep=group)
        if group.single:
            _ensure_project(group, leader, lead)
        elif group.project_id:
            group.project = None
            group.save(update_fields=["project", "updated_at"])
    _event(group, "saved", f"{_who(by)} saved work group {group.name}: lead {_who(lead)}, #{channel_name}, "
           f"agents {', '.join(group.agents)}, led by {leader.slug}", user=by.pk)
    return group


def _save_schedule(group: HuddleGroup, *, cron: str, tz: str, by) -> None:
    from apps.harness.models import AgentSchedule

    s = group.schedule or AgentSchedule(created_by=by, prompt=BASE_PROMPT)
    s.agent = group.leader_agent
    s.name = f"Work group: {group.name} ({group.slug})"[:200]
    s.cron, s.timezone, s.run_once_at = cron, tz, None
    s.enabled = group.enabled
    s.updated_by = by
    s.save()
    if group.schedule_id != s.pk:
        group.schedule = s
        group.save(update_fields=["schedule", "updated_at"])


def _ensure_project(group: HuddleGroup, agent: Agent, lead) -> None:
    from apps.agents import participants
    from apps.agents.models import ProjectParticipant
    from apps.agents.services import next_project_ext_id
    from apps.contacts.services import person_for

    p = group.project
    if p is None or p.agent_id != agent.pk:
        p = AgentProject.objects.create(agent=agent, ext_id=next_project_ext_id(agent), name=group.name[:200],
                                        outcome=group.topic, status=AgentProject.ACTIVE, owner_user=lead,
                                        notes=f"The {group.name} work group's project (#{group.channel_name}).")
        group.project = p
        group.save(update_fields=["project", "updated_at"])
    elif p.owner_user_id != getattr(lead, "pk", None):
        p.owner_user = lead
        p.save(update_fields=["owner_user", "updated_at"])
    try:
        person = person_for(user=lead) if lead else None
        if person is not None:
            participants.ensure(p, person, source=ProjectParticipant.MANUAL, role="work group lead")
    except Exception:  # noqa: BLE001 — a participant is never worth a failed save
        logger.exception("could not add the lead as a participant")


def _release_channel(agent: Agent, channel_id: str, *, by, keep: HuddleGroup | None = None) -> None:
    """Take the channel off `agent`'s reading list, unless another group still needs it."""
    others = HuddleGroup.objects.filter(leader_agent=agent, channel_id=channel_id)
    if keep is not None:
        others = others.exclude(pk=keep.pk)
    if others.exists():
        return
    entry = ingest_svc.allowance_for(agent, channel_id)
    if entry is not None:
        ingest_svc.disallow(entry, by=by)


def delete(group: HuddleGroup, *, by) -> None:
    from apps.harness import services as hsvc

    with transaction.atomic():
        _release_channel(group.leader_agent, group.channel_id, by=by, keep=group)
        for run in group.runs.filter(status=HuddleGroupRun.AWAITING_CONFIRM):
            _settle(run, f":wastebasket: The *{group.name}* work group was deleted. Nothing was read.")
        if group.schedule is not None:
            hsvc.supersede_open_turns(group.schedule, reason="work group deleted")
            group.schedule.delete()
        _event(group, "deleted", f"{_who(by)} deleted work group {group.name}", user=by.pk)
        group.delete()


# --- the announcement --------------------------------------------------------------

def _agents_line(group: HuddleGroup) -> str:
    names = [f"`{s}`" for s in group.agents]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _since(group: HuddleGroup) -> str:
    entry = ingest_svc.allowance_for(group.leader_agent, group.channel_id)
    row = None
    if entry is not None:
        from apps.slack.models import SlackChannelIngest

        row = (SlackChannelIngest.objects.filter(allowance=entry, read_mark__gt=0, read_at__isnull=False)
               .order_by("-read_at").first())
    return ingest_svc._slack_date(row.read_at.timestamp()) if row else "the channel began (its first read)"


def announcement(run: HuddleGroupRun) -> tuple[str, list[dict]]:
    g = run.group
    text = (f":calendar: *{g.name} huddle* is due. <@{g.lead_slack_id}>: Confirm to have {_agents_line(g)} "
            f"read what was posted here since {_since(g)} and work on it.")
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"Only <@{g.lead_slack_id}>, the work group's lead, can confirm. Nothing here is read until then; "
            "unconfirmed, this lapses in 24 hours.")}]},
        {"type": "actions", "block_id": "group_run", "elements": [
            {"type": "button", "action_id": CONFIRM, "style": "primary", "value": str(run.pk),
             "text": {"type": "plain_text", "text": "Confirm"}},
            {"type": "button", "action_id": SKIP, "value": str(run.pk),
             "text": {"type": "plain_text", "text": "Skip"}},
        ]},
    ]
    return f"{g.name} huddle is due — the lead confirms to have it read this channel.", blocks


def _settle(run: HuddleGroupRun, outcome: str) -> None:
    """The announcement once answered: what was asked, what happened, no buttons."""
    if not run.announce_ts:
        return
    _text, blocks = announcement(run)
    try:
        client.update_message(run.group.installation.bot_token, channel=run.group.channel_id, ts=run.announce_ts,
                              text=outcome, blocks=[blocks[0], {"type": "context", "elements": [
                                  {"type": "mrkdwn", "text": outcome[:3000]}]}])
    except Exception:  # noqa: BLE001 — the run's state stands; the post is a courtesy
        logger.exception("could not update a work group announcement")


def _thread(run: HuddleGroupRun, text: str) -> None:
    if not run.announce_ts:
        return
    try:
        client.post_message(run.group.installation.bot_token, channel=run.group.channel_id,
                            thread_ts=run.announce_ts, text=text)
    except Exception:  # noqa: BLE001
        logger.exception("could not post in a work group thread")


def _fail(run: HuddleGroupRun, why: str, *, tell_thread: bool = False) -> HuddleGroupRun:
    HuddleGroupRun.objects.filter(pk=run.pk).update(status=HuddleGroupRun.FAILED, note=why[:500],
                                                    finished_at=timezone.now())
    run.refresh_from_db()
    from apps.events.models import Event

    _event(run.group, "run_failed", f"{run.group.name} huddle (run {run.pk}) failed: {why}", level=Event.WARN,
           run=run.pk, reason=why[:500])
    if tell_thread:
        _thread(run, f":warning: {why}")
    return run


def announce(schedule, slot: dt.datetime, *, manual: bool = False) -> tuple[HuddleGroupRun, bool]:
    """The group's schedule came due: create the run (idempotent per slot) and
    post the announcement. Never a turn and never a read."""
    group = HuddleGroup.objects.select_related("workspace", "lead", "leader_agent", "leader_agent__user",
                                               "installation").get(schedule=schedule)
    with transaction.atomic():
        run, created = HuddleGroupRun.objects.get_or_create(group=group, slot=slot, defaults={"manual": manual})
        if created and (schedule.last_slot is None or slot > schedule.last_slot) and not manual:
            schedule.last_slot = slot
            schedule.save(update_fields=["last_slot", "updated_at"])
    if not created:
        return run, False
    # You only ever owe the newest: an older unanswered announcement lapses.
    for old in group.runs.filter(status=HuddleGroupRun.AWAITING_CONFIRM).exclude(pk=run.pk):
        if HuddleGroupRun.objects.filter(pk=old.pk, status=HuddleGroupRun.AWAITING_CONFIRM).update(
                status=HuddleGroupRun.EXPIRED, note="a newer run was announced", finished_at=timezone.now()):
            old.refresh_from_db()
            _settle(old, ":hourglass: Superseded by a newer announcement. Nothing was read.")
            _engage("run_expired", old, reason="superseded")
    # The checks, again: membership and access change between saves.
    if not group.enabled:
        return _fail(run, "the work group is disabled"), True
    members = list(Agent.objects.select_related("user").filter(slug__in=group.agents))
    why = leader_refusal(group.leader_agent, [m for m in members if m.pk != group.leader_agent_id])
    if not why and len(members) != len(group.agents):
        why = "an agent of the group no longer exists"
    token = group.installation.bot_token
    lead_slack = ""
    if not why:
        why, lead_slack = lead_refusal(workspace_id=group.workspace_id, lead=group.lead, token=token,
                                       channel_id=group.channel_id, channel_name=group.channel_name)
        why = f"the lead no longer qualifies: {why}" if why else ""
    if why:
        return _fail(run, why), True
    if lead_slack != group.lead_slack_id:
        HuddleGroup.objects.filter(pk=group.pk).update(lead_slack_id=lead_slack)
        group.lead_slack_id = lead_slack
    text, blocks = announcement(run)
    try:
        ts = client.post_message(token, channel=group.channel_id, text=text, blocks=blocks)
    except client.SlackApiError as e:
        return _fail(run, f"Slack refused the announcement ({e.error})"), True
    HuddleGroupRun.objects.filter(pk=run.pk).update(announce_ts=ts)
    run.announce_ts = ts
    _event(group, "run_announced", f"{group.name} huddle announced in <#{group.channel_id}> (run {run.pk})",
           run=run.pk, manual=manual)
    _engage("run_announced", run, manual=manual)
    return run, True


# --- the lead's click ----------------------------------------------------------------

def _from_click(installation, run_id: str, channel_id: str, message_ts: str) -> HuddleGroupRun | None:
    try:
        return (HuddleGroupRun.objects.select_related("group", "group__workspace", "group__lead",
                                                      "group__leader_agent", "group__installation")
                .filter(pk=int(run_id), group__installation=installation, group__channel_id=channel_id,
                        announce_ts=message_ts).first())
    except (TypeError, ValueError):
        return None


def click(installation, *, run_id: str, slack_user_id: str, channel_id: str, message_ts: str,
          confirm: bool) -> str:
    """Confirm / Skip on an announcement. Returns what to tell the clicker, privately ("" for nothing)."""
    from apps.events.models import Event

    run = _from_click(installation, run_id, channel_id, message_ts)
    if run is None or run.status != HuddleGroupRun.AWAITING_CONFIRM:
        return "That huddle is no longer waiting for an answer."
    g = run.group
    verb = "Confirm" if confirm else "Skip"
    if not g.lead_slack_id or slack_user_id != g.lead_slack_id:
        _event(g, "click_refused", f"<@{slack_user_id}> pressed {verb} on the {g.name} huddle and was refused: "
               "not the lead", level=Event.WARN, run=run.pk, slack_user=slack_user_id)
        return f"Only the work group's lead, <@{g.lead_slack_id}>, can confirm or skip this huddle."
    if not confirm:
        if not HuddleGroupRun.objects.filter(pk=run.pk, status=HuddleGroupRun.AWAITING_CONFIRM).update(
                status=HuddleGroupRun.SKIPPED, note=f"skipped by <@{slack_user_id}>",
                confirmed_by_slack=slack_user_id, finished_at=timezone.now()):
            return "That huddle was already answered."
        run.refresh_from_db()
        _settle(run, f":fast_forward: Skipped by <@{slack_user_id}>. Nothing was read.")
        _event(g, "run_skipped", f"<@{slack_user_id}> skipped the {g.name} huddle (run {run.pk})", run=run.pk)
        _engage("run_skipped", run, user=g.lead)
        return ""
    if g.lead is None or not wsvc.is_member(g.lead, g.workspace_id):
        _fail(run, "the lead is no longer a member of the canopy workspace")
        _settle(run, ":warning: The lead no longer has access to this work group. Nothing was read.")
        return "You're no longer a member of the canopy workspace this work group belongs to."
    try:
        with transaction.atomic():
            if not HuddleGroupRun.objects.filter(pk=run.pk, status=HuddleGroupRun.AWAITING_CONFIRM).update(
                    status=HuddleGroupRun.READING, confirmed_by=g.lead, confirmed_by_slack=slack_user_id,
                    confirmed_at=timezone.now()):
                return "That huddle was already answered."
            row = ingest_svc.start_group_read(
                agent=g.leader_agent, installation=g.installation, channel_id=g.channel_id, user=g.lead,
                run_id=run.pk, purpose=f"{g.name} work group huddle")
            HuddleGroupRun.objects.filter(pk=run.pk).update(ingest=row)
    except ingest_svc.IngestError as e:
        _event(g, "confirm_refused", f"the {g.name} huddle could not start reading: {e}", level=Event.WARN,
               run=run.pk)
        return str(e)
    run.refresh_from_db()
    _settle(run, f":white_check_mark: Confirmed by <@{slack_user_id}>. canopy is reading this channel for "
                 f"`{g.leader_agent.slug}`.")
    _event(g, "run_confirmed", f"<@{slack_user_id}> confirmed the {g.name} huddle (run {run.pk}); reading "
           f"<#{g.channel_id}> for {g.leader_agent.slug}", run=run.pk)
    _engage("run_confirmed", run, user=g.lead)
    return ""


# --- the read, delivered ----------------------------------------------------------------

def _turn_prompt(group: HuddleGroup, run: HuddleGroupRun) -> str:
    base = (group.schedule.prompt if group.schedule else "") or BASE_PROMPT
    return f"{base.strip()} --group-run {run.pk}"


def deliver_read(run_id: int, ingest_id) -> None:
    """The confirmed read is done (slack.ingest._deliver): keep the material,
    move the read mark, queue the leader's turn, tell the thread."""
    from apps.harness import initiator as who
    from apps.harness import services as hsvc
    from apps.harness.models import Turn
    from apps.slack.models import SlackChannelIngest

    run = (HuddleGroupRun.objects.select_related("group", "group__schedule", "group__leader_agent",
                                                 "group__installation", "group__lead", "group__workspace")
           .filter(pk=run_id).first())
    if run is None or run.status != HuddleGroupRun.READING:
        return
    g = run.group
    row = SlackChannelIngest.objects.select_related("agent", "decided_by").get(pk=ingest_id)
    if row.status not in ingest_svc.LIVE:
        _fail(run, f"canopy couldn't read this channel for `{g.leader_agent.slug}`: {row.error or row.status}",
              tell_thread=True)
        return
    prev_at = row.read_at if row.read_mark else None
    t = ingest_svc.transcript(row, after=row.read_mark)
    ingest_svc.mark_read(row, t["mark"])
    SlackChannelIngest.objects.filter(pk=row.pk).update(last_read_by_slack=(g.lead_slack_id or "")[:32])
    sched = g.schedule
    origin_ref = {"kind": "huddle_group_run", "group": g.slug, "run": run.pk, "slot": run.slot.isoformat()}
    if sched is not None:
        origin_ref.update(schedule_id=sched.id, schedule_name=sched.name)
    with transaction.atomic():
        if sched is not None:
            hsvc.supersede_open_turns(sched, reason=f"superseded by work group run {run.pk}")
        turn, _ = hsvc.enqueue_turn(
            agent=g.leader_agent, origin=Turn.ORIGIN_CANOPY_SCHEDULER, idempotency_key=f"group-run:{run.pk}",
            prompt=_turn_prompt(g, run), origin_ref=origin_ref,
            routing=sched.routing if sched is not None else Turn.PREFER_LOCAL,
            # The lead's Confirm is why it runs: a person approving this work.
            initiator=who.for_user(g.lead, via=f"slack:group-run:{run.pk}", assurance=who.APPROVAL),
        )
        HuddleGroupRun.objects.filter(pk=run.pk).update(
            status=HuddleGroupRun.RUNNING, material=t["markdown"], read_messages=t["messages"],
            read_first_ts=t["first_ts"] or "", read_last_ts=t["last_ts"] or "", read_since=prev_at, turn=turn)
    run.refresh_from_db()
    _event(g, "run_started", f"{g.name} huddle: read {t['messages']} message(s); {g.leader_agent.slug}'s turn "
           f"queued (run {run.pk})", run=run.pk, turn=str(turn.pk), messages=t["messages"])
    _thread(run, ingest_svc._what_was_read(g.leader_agent.slug, t, prev_at)
            + f"\nStarted — follow it at {page_url(g)}")


# --- the finish ----------------------------------------------------------------

def _count_line(run: HuddleGroupRun) -> str:
    g = run.group
    if not g.single and run.huddle_id:
        tail = (f"/huddles/{run.huddle_id}", f"/huddles/{run.huddle_id}/")
        if any(f.name == "source_url" for f in AgentProject._meta.get_fields()):
            n = sum(AgentProject.objects.filter(source_url__endswith=t).count() for t in tail)
            return f"{n} proposed project{'' if n == 1 else 's'}"
        n = sum(AgentTask.objects.filter(source_url__endswith=t).count() for t in tail)
        return f"{n} new task{'' if n == 1 else 's'}"
    since = run.confirmed_at or run.created_at
    n = AgentTask.objects.filter(project=g.project, created_at__gte=since).count() if g.project_id else 0
    return f"{n} new task{'' if n == 1 else 's'}"


def finish(run: HuddleGroupRun, *, huddle_id: str, result_url: str, summary: str) -> HuddleGroupRun:
    """The leader's login reports the run done. Idempotent once done."""
    if run.status == HuddleGroupRun.DONE:
        return run
    if run.status != HuddleGroupRun.RUNNING:
        raise GroupError(f"run {run.pk} is {run.status}, not running", 409)
    HuddleGroupRun.objects.filter(pk=run.pk, status=HuddleGroupRun.RUNNING).update(
        status=HuddleGroupRun.DONE, huddle_id=(huddle_id or "")[:128], result_url=(result_url or "")[:500],
        summary=summary or "", finished_at=timezone.now())
    run.refresh_from_db()
    g = run.group
    link = run.result_url
    if not link and run.huddle_id:
        from apps.slack.services import public_url

        link = public_url(f"/w/{g.workspace_id}/huddles/{run.huddle_id}")
    link = link or page_url(g)
    _thread(run, f":white_check_mark: done — {_count_line(run)}: {link}")
    _event(g, "run_done", f"{g.name} huddle done (run {run.pk})", run=run.pk, huddle=run.huddle_id)
    return run


# --- the clock ----------------------------------------------------------------

def expire_due(*, now: dt.datetime | None = None) -> int:
    """Lapse announcements nobody confirmed within 24 h; fail reads that never came back."""
    now = now or timezone.now()
    n = 0
    for run in (HuddleGroupRun.objects.select_related("group", "group__installation", "group__workspace",
                                                      "group__leader_agent")
                .filter(status=HuddleGroupRun.AWAITING_CONFIRM, created_at__lt=now - EXPIRE_AFTER)):
        if not HuddleGroupRun.objects.filter(pk=run.pk, status=HuddleGroupRun.AWAITING_CONFIRM).update(
                status=HuddleGroupRun.EXPIRED, note="nobody confirmed within 24 hours", finished_at=now):
            continue
        run.refresh_from_db()
        _settle(run, ":hourglass: Nobody confirmed within 24 hours, so this huddle lapsed. Nothing was read.")
        _event(run.group, "run_expired", f"{run.group.name} huddle lapsed unconfirmed (run {run.pk})", run=run.pk)
        _engage("run_expired", run)
        n += 1
    for run in HuddleGroupRun.objects.select_related("group").filter(
            status=HuddleGroupRun.READING, confirmed_at__lt=now - READ_TIMEOUT):
        _fail(run, "the channel read never finished", tell_thread=True)
        n += 1
    return n
