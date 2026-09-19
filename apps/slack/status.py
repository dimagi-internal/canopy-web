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


def _reach_and_cloud(turn: Turn):
    """(reach, cloud runner) for a QUEUED turn; (None, None) once it has moved on."""
    from apps.canopy_sessions import services as session_services
    from apps.harness import services as harness

    if turn.status != Turn.QUEUED:
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
    installation = dest[0]
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


def on_status(turn: Turn) -> None:
    """A status row landed on a turn of some session. Edit its line, or — for a
    turn that did not come from Slack — post one, so the thread knows."""
    if SlackTurnPost.objects.filter(turn_id=turn.pk).exists():
        refresh(turn)
    elif turn.origin != Turn.ORIGIN_SLACK:
        post(turn)
