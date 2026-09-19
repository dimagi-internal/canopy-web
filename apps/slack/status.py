"""One status line per turn in its Slack thread: where the ask is, right now.

The failure this exists for is silence. Before it, a Slack message produced a
private "sent" note on the FIRST message of a thread and nothing else, so a
message whose runner was offline looked exactly like one being worked on: both
were quiet. Now every turn on a Slack-born session gets a public line in the
thread, posted at once and edited in place as the turn moves:

* queued, a live runner will take it  -> "picking this up on <runner>"
* queued, its runner is offline       -> "waiting: <runner> is offline", plus a
  **Run on <cloud runner>** button when there is one to run it on
* queued, nothing could ever take it  -> "no runner is set up for <agent>"
* claimed / running / done / failed   -> the same line, edited
* claimed, and then its runner stopped heartbeating (a closed laptop) ->
  "paused: <runner> went offline", a thread ping (an edit notifies nobody),
  the same Run-on-cloud button, and a "back online" ping if it returns. No
  event marks that moment, so `sweep` finds it on other runners' reports.
* lost (the lease ran out on a dead runner) -> "could not finish", with the
  button while it is still the conversation's last word

A turn that started ELSEWHERE on a Slack-born session (someone continuing the
conversation from canopy-web or the phone) gets a line too, quoting what they
asked — otherwise the agent's reply lands in the thread answering a question
the thread never saw.

The button only offers; the click re-checks everything (see
`services.route_to_cloud`), because a status line can outlive its turn.
"""
from __future__ import annotations

import json
import logging

from django.db import IntegrityError, transaction

from apps.harness.models import Turn

from . import client
from .models import SlackTurnPost

logger = logging.getLogger(__name__)

ROUTE_CLOUD = "route_cloud"
PROMPT_PREVIEW = 200


def _runner_names(runners) -> str:
    names = [f"*{r.name}*" for r in runners[:3]]
    return ", ".join(names) + (" …" if len(runners) > 3 else "")


def _header(turn: Turn) -> str:
    """For a turn that did not come from Slack: who asked, and what."""
    from apps.slack.relay import to_mrkdwn

    ref = turn.origin_ref or {}
    if ref.get("transfer_from"):
        target = turn.pinned_runner.name if turn.pinned_runner_id else "another runner"
        return f":truck: Moved from *{ref['transfer_from']}* to *{target}* — handing over the thread."
    who = ""
    if turn.initiator_user_id:
        u = turn.initiator_user
        who = (u.get_full_name() or u.email or "").strip()
    elif turn.enqueued_by_id:
        who = (turn.enqueued_by.get_full_name() or turn.enqueued_by.email or "").strip()
    where = {
        Turn.ORIGIN_CANOPY_WEB_CHAT: "canopy",
        Turn.ORIGIN_ACE_WEB: "ace-web",
        Turn.ORIGIN_EMAIL: "email",
        Turn.ORIGIN_CANOPY_SCHEDULER: "a schedule",
    }.get(turn.origin, "canopy")
    prompt = " ".join((turn.prompt or "").split())
    if len(prompt) > PROMPT_PREVIEW:
        prompt = prompt[:PROMPT_PREVIEW - 1] + "…"
    by = f"{who} continued this in {where}" if who else f"Continued in {where}"
    return f":speech_balloon: {by}: _{to_mrkdwn(prompt)}_"


def render(turn: Turn, *, reach=None, cloud=None) -> tuple[str, list | None]:
    """(text, blocks) for this turn's status line. `reach` is only consulted
    while the turn is QUEUED; `cloud` is the runner a button would move it to."""
    from apps.harness import services as harness

    from .services import session_url

    session = turn.chat_session
    agent = f"`{session.agent.slug}`" if session is not None and session.agent_id else "the agent"
    runner = f"*{turn.claimed_by.name}*" if turn.claimed_by_id else "a runner"
    button = None
    status = turn.status
    if status == Turn.QUEUED:
        if turn.pinned_runner_id and reach is not None and reach.kind == harness.LIVE:
            line = f":hourglass_flowing_sand: Sent to *{turn.pinned_runner.name}* — {agent} will pick this up there."
        elif reach is not None and reach.kind == harness.LIVE:
            line = f":hourglass_flowing_sand: {agent} is picking this up on {_runner_names(reach.runners)}."
        elif reach is not None and reach.kind == harness.OFFLINE:
            line = (f":double_vertical_bar: Queued — {agent}'s runner {_runner_names(reach.runners)} "
                    "is offline, so nothing is working on this yet. It runs when the runner is back.")
        else:
            line = (f":warning: Queued, but no runner is set up to run {agent} — nothing will pick "
                    "this up until its routing is fixed.")
        if cloud is not None and (reach is None or reach.kind != harness.LIVE):
            line += f" A runner admin can send it to *{cloud.name}* now."
            button = cloud
    elif runner_gone(turn):
        # Claimed, and then its box stopped heartbeating — a closed laptop. The
        # turn still reads RUNNING (a dead runner cannot say otherwise, and the
        # lease takes up to 15 minutes to run out), so this is the one state the
        # status alone gets wrong.
        seen = _when(turn.claimed_by.last_heartbeat_at)
        line = (f":double_vertical_bar: Paused — {runner} went offline while {agent} was working "
                f"on this (last seen {seen}). It carries on if the runner comes back.")
        if cloud is not None:
            line += (f" A runner admin can move it to *{cloud.name}* now — a fresh session there, "
                     f"so anything {runner} had not pushed stays behind.")
            button = cloud
    elif status == Turn.CLAIMED or status == Turn.RUNNING:
        line = f":gear: {agent} is working on this on {runner}."
    elif status == Turn.NEEDS_HUMAN:
        line = f":raised_hand: {agent} is waiting on a person, on {runner}."
    elif status == Turn.DONE:
        line = f":white_check_mark: {agent} finished this on {runner}."
    elif status == Turn.CANCELLED:
        line = ":heavy_minus_sign: Cancelled."
    elif status == Turn.MISSED:
        line = ":heavy_minus_sign: Missed — nothing picked it up in time."
    elif status == Turn.LOST and turn.claimed_by_id:
        line = f":x: {agent} could not finish this — {runner} went away before it was done."
        if cloud is not None:
            line += f" A runner admin can run it again on *{cloud.name}*."
            button = cloud
    else:  # FAILED / LOST — the relay posts the reason as its own message
        line = f":x: {agent} could not finish this" + (f" on {runner}." if turn.claimed_by_id else ".")

    lines = [] if turn.origin == Turn.ORIGIN_SLACK else [_header(turn)]
    lines.append(line)
    # A contact's session has no owner who could open it; a link would be a dead end.
    if session is not None and session.created_by_id:
        lines.append(f"<{session_url(session)}|Open in canopy>")
    text = "\n".join(lines)
    if button is None:
        return text, None
    value = json.dumps({"s": str(session.id), "t": str(turn.id), "r": str(button.id)},
                       separators=(",", ":"))
    return text, [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}},
        {"type": "actions", "block_id": "route", "elements": [{
            "type": "button", "action_id": ROUTE_CLOUD, "style": "primary", "value": value,
            "text": {"type": "plain_text", "text": f"Run on {button.name}"[:75]},
        }]},
    ]


def _load(turn: Turn) -> Turn:
    return (Turn.objects.select_related("chat_session", "chat_session__agent", "claimed_by",
                                        "pinned_runner", "initiator_user", "enqueued_by")
            .get(pk=turn.pk))


def runner_gone(turn: Turn) -> bool:
    """Claimed and not finished, on a runner that has stopped heartbeating.

    `is_reachable`, not `is_available`: a paused or degraded box still has its
    daemon up and will finish what it holds, so neither is "gone".
    """
    return (turn.status in (Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN)
            and turn.claimed_by_id is not None and not turn.claimed_by.is_reachable)


def movable_lost(turn: Turn) -> bool:
    """A lost turn is worth re-running only while it is the conversation's last
    word — once anything newer exists, the thread has moved on without it."""
    return (turn.status == Turn.LOST and turn.chat_session_id is not None
            and not Turn.objects.filter(chat_session_id=turn.chat_session_id,
                                        created_at__gt=turn.created_at).exists())


def _when(ts) -> str:
    """A Slack date token, so each reader sees it in their own timezone."""
    if ts is None:
        return "never"
    return f"<!date^{int(ts.timestamp())}^{{time}}|{ts.isoformat(timespec='minutes')}>"


def _reach_and_cloud(turn: Turn):
    """(reach, cloud runner): reach only for a QUEUED turn; a cloud runner
    wherever a button could rescue it — queued with no live runner, stranded
    on a runner that went offline mid-turn, or lost."""
    from apps.canopy_sessions import services as session_services
    from apps.harness import services as harness

    if turn.status != Turn.QUEUED:
        if turn.chat_session_id and (runner_gone(turn) or movable_lost(turn)):
            return None, session_services.available_cloud_runner(turn.chat_session)
        return None, None
    reach = harness.turn_reach(turn)
    cloud = None
    if reach.kind != harness.LIVE and turn.chat_session_id:
        cloud = session_services.available_cloud_runner(turn.chat_session)
    return reach, cloud


def post(turn: Turn) -> SlackTurnPost | None:
    """Post this turn's status line into its thread, once. None if the session
    is not Slack-born, or the line was already posted."""
    from .relay import _log_failure, session_destination

    turn = _load(turn)
    dest = session_destination(turn.chat_session)
    if dest is None:
        return None
    installation, channel, thread_ts = dest
    try:
        with transaction.atomic():
            record = SlackTurnPost.objects.create(turn=turn, channel_id=channel)
    except IntegrityError:
        return None
    reach, cloud = _reach_and_cloud(turn)
    text, blocks = render(turn, reach=reach, cloud=cloud)
    try:
        record.slack_ts = client.post_message(installation.bot_token, channel=channel, text=text,
                                              thread_ts=thread_ts, blocks=blocks)
        record.rendered = text
        record.save(update_fields=["slack_ts", "rendered"])
    except Exception as e:  # noqa: BLE001 — a status line must never fail the send or the append
        logger.exception("could not post a Slack status line")
        _log_failure(installation, turn, channel, str(e))
        return record
    # The runner may have claimed it while we were posting; the claim's own
    # signal found no ts yet and edited nothing, so catch up here.
    refresh(turn)
    return record


def refresh(turn: Turn) -> bool:
    """Re-render this turn's status line if it changed. Returns whether it edited."""
    from .relay import session_destination

    record = SlackTurnPost.objects.filter(turn_id=turn.pk).exclude(slack_ts="").first()
    if record is None:
        return False
    turn = _load(turn)
    dest = session_destination(turn.chat_session)
    if dest is None:
        return False
    installation, _channel, thread_ts = dest
    _sync_offline_notice(installation, thread_ts, turn, record)
    reach, cloud = _reach_and_cloud(turn)
    text, blocks = render(turn, reach=reach, cloud=cloud)
    if text == record.rendered:
        return False
    try:
        client.update_message(installation.bot_token, channel=record.channel_id, ts=record.slack_ts,
                              text=text, blocks=blocks or [
                                  {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}])
    except Exception:  # noqa: BLE001 — a stale status line is cosmetic
        logger.exception("could not edit a Slack status line")
        return False
    record.rendered = text
    record.save(update_fields=["rendered"])
    return True


PENDING = "pending"


def _sync_offline_notice(installation, thread_ts: str, turn: Turn, record: SlackTurnPost) -> None:
    """Ping the thread when a turn's runner dies mid-turn, and when it returns.

    The status line is edited either way, but an edit notifies nobody. Only the
    two edges get a reply: going offline, and coming back while the turn is
    still running. A turn that ends while its runner is gone (moved, lost) just
    clears the marker — its line says what happened.
    """
    gone = runner_gone(turn)
    if gone and not record.offline_notice_ts:
        if not SlackTurnPost.objects.filter(pk=record.pk, offline_notice_ts="") \
                .update(offline_notice_ts=PENDING):
            return
        agent = f"`{turn.chat_session.agent.slug}`" if turn.chat_session.agent_id else "the agent"
        text = (f":double_vertical_bar: *{turn.claimed_by.name}* went offline while {agent} was "
                "working on this — see the status line above for what happens next.")
        try:
            ts = client.post_message(installation.bot_token, channel=record.channel_id, text=text,
                                     thread_ts=thread_ts)
        except Exception:  # noqa: BLE001
            logger.exception("could not post a Slack offline notice")
            ts = PENDING
        SlackTurnPost.objects.filter(pk=record.pk).update(offline_notice_ts=ts)
        record.offline_notice_ts = ts
    elif not gone and record.offline_notice_ts:
        if not SlackTurnPost.objects.filter(pk=record.pk, offline_notice_ts=record.offline_notice_ts) \
                .update(offline_notice_ts=""):
            return
        record.offline_notice_ts = ""
        if turn.status in (Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN):
            try:
                client.post_message(installation.bot_token, channel=record.channel_id, thread_ts=thread_ts,
                                    text=f":arrow_forward: *{turn.claimed_by.name}* is back online — "
                                         "carrying on.")
            except Exception:  # noqa: BLE001
                logger.exception("could not post a Slack back-online notice")


SWEEP_EVERY_SECONDS = 15
SWEEP_LOCK = "slack:status_sweep"


def sweep(*, force: bool = False) -> int:
    """Re-check every status line that could be going stale. Throttled.

    Needed because the failure it catches produces no event at all: a runner
    whose laptop closes simply stops heartbeating, and a dead runner cannot
    report its own death. So this rides `sessions_reported` — every OTHER
    runner's ~10s report — at most once per SWEEP_EVERY_SECONDS across the
    fleet (a cache lock, so all web workers share it). "Could be going stale":
    the turn is not finished, or an offline ping is still outstanding.
    """
    from django.core.cache import cache
    from django.db.models import Q

    if not force and not cache.add(SWEEP_LOCK, 1, timeout=SWEEP_EVERY_SECONDS):
        return 0
    live = (SlackTurnPost.objects.exclude(slack_ts="")
            .filter(Q(turn__status__in=list(Turn.NON_TERMINAL)) | ~Q(offline_notice_ts=""))
            .select_related("turn"))
    n = 0
    for record in live:
        try:
            refresh(record.turn)
        except Exception:  # noqa: BLE001 — one bad line must not stop the rest
            logger.exception("slack status sweep: refresh failed")
        n += 1
    return n


def on_status(turn: Turn) -> None:
    """A status row landed on a turn of some session. Edit its line, or — for a
    turn that did not come from Slack — post one, so the thread knows."""
    if SlackTurnPost.objects.filter(turn_id=turn.pk).exists():
        refresh(turn)
    elif turn.origin != Turn.ORIGIN_SLACK:
        post(turn)
