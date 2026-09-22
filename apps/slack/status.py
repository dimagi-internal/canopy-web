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

from apps.harness import turn_status as _ts
from apps.harness.models import Turn

from . import client
from .models import SlackTurnPost

logger = logging.getLogger(__name__)

ROUTE_CLOUD = "route_cloud"
PROMPT_PREVIEW = 200


def _runner_names(names) -> str:
    """Bold, at most three, then an ellipsis. Takes NAMES — the shared status
    carries strings rather than model rows so the same value can ride a socket."""
    shown = [f"*{n}*" for n in names[:3]]
    return ", ".join(shown) + (" …" if len(names) > 3 else "")


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
    """(text, blocks) for this turn's status line — Slack's VOICE for the shared
    state in `harness.turn_status`. `reach` is only consulted while the turn is
    QUEUED; `cloud` is the runner a button would move it to.

    Slack asks for the state rather than deriving it so its words and the chat
    kit's cannot drift apart: the same eleven states reach a thread, a canopy
    chat page and an embedded widget, and only the wording is ours.
    """
    from apps.harness import turn_status as ts

    from .services import session_url

    st = ts.derive(turn, reach=reach, cloud=cloud)
    session = turn.chat_session
    agent = f"`{st.agent_slug}`" if st.agent_slug else "the agent"
    runner = f"*{st.claimed_by}*" if st.claimed_by else "a runner"
    button = None
    if st.state == ts.PICKING_UP and st.pinned:
        line = f":hourglass_flowing_sand: Sent to *{st.runners[0]}* — {agent} will pick this up there."
    elif st.state == ts.PICKING_UP:
        line = f":hourglass_flowing_sand: {agent} is picking this up on {_runner_names(st.runners)}."
    elif st.state == ts.WAITING_RUNNER:
        line = (f":double_vertical_bar: Queued — {agent}'s runner {_runner_names(st.runners)} "
                "is offline, so nothing is working on this yet. It runs when the runner is back.")
    elif st.state == ts.UNROUTED:
        line = (f":warning: Queued, but no runner is set up to run {agent} — nothing will pick "
                "this up until its routing is fixed.")
    elif st.state == ts.PAUSED:
        seen = _when(st.last_seen_at)
        line = (f":double_vertical_bar: Paused — {runner} went offline while {agent} was working "
                f"on this (last seen {seen}). It carries on if the runner comes back.")
        if cloud is not None:
            line += (f" A runner admin can move it to *{cloud.name}* now — a fresh session there, "
                     f"so anything {runner} had not pushed stays behind.")
            button = cloud
    elif st.state == ts.WORKING:
        line = f":gear: {agent} is working on this on {runner}."
    elif st.state == ts.BLOCKED:
        line = f":raised_hand: {agent} is waiting on a person, on {runner}."
    elif st.state == ts.DONE:
        line = f":white_check_mark: {agent} finished this on {runner}."
    elif st.state == ts.CANCELLED:
        line = ":heavy_minus_sign: Cancelled."
    elif st.state == ts.MISSED:
        line = ":heavy_minus_sign: Missed — nothing picked it up in time."
    elif st.state == ts.LOST and st.claimed_by:
        line = f":x: {agent} could not finish this — {runner} went away before it was done."
        if cloud is not None:
            line += f" A runner admin can run it again on *{cloud.name}*."
            button = cloud
    else:  # FAILED / LOST — the relay posts the reason as its own message
        line = f":x: {agent} could not finish this" + (f" on {runner}." if st.claimed_by else ".")

    # The queued rescue offer, appended after the line it qualifies. Only the
    # two queued states nothing is going to resolve on its own — an ask a live
    # runner is already picking up needs no rescue.
    if st.state in (ts.WAITING_RUNNER, ts.UNROUTED) and cloud is not None:
        line += f" A runner admin can send it to *{cloud.name}* now."
        button = cloud

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


def slack_status_for(turn: Turn) -> str:
    """This turn, as one of Slack's four agent-session states.

    Deliberately derived from the SAME turn the text line is rendered from, in
    the same call, so the native indicator and the words can never disagree —
    a spinner still turning under a line that says "could not finish" would be
    worse than no spinner at all.

    * PROCESSING — picked up and working; Slack draws "Working…" and a Stop.
    * SUSPENDED — it needs a person: blocked on a question, or stuck behind a
      runner that is offline (which only a human can resolve, by moving it or
      by opening the laptop).
    * ACTIVE — nothing in flight; ready for the next message.
    """
    st = _ts.resolve(turn)
    # `stuck` is the shared projection's own name for "nothing is moving and
    # only a person can change that" — which is precisely what SUSPENDED means
    # to Slack. It already folds in the blocked-on-a-dialog case, so a spinner
    # can never sit over an unanswered question.
    if st.stuck:
        return client.SUSPENDED
    if st.pending:
        return client.PROCESSING
    return client.ACTIVE


def sync_indicator(turn: Turn, dest) -> bool:
    """Tell Slack what its own indicator should show for this turn's thread.

    Best-effort by construction: `set_session_status` answers False wherever the
    app is not declared an agent, and a raised error is swallowed here, because
    the status LINE is the load-bearing half and must post either way.
    """
    installation, channel, thread_ts = dest
    try:
        return client.set_session_status(installation.bot_token, channel=channel,
                                         thread_ts=thread_ts, status=slack_status_for(turn))
    except Exception:  # noqa: BLE001 — the indicator is the garnish, the line is the meal
        logger.exception("could not set the Slack agent-session status")
        return False


def sync_session(session, dest) -> bool:
    """The indicator for a SESSION, where no turn is the subject.

    The case that needs it: a runner reports its agent blocked on a question,
    which on an emdash session is not a Turn at all. Falls through to the
    session's latest turn when there is one, so this never contradicts the line.
    """
    from apps.canopy_sessions.serializers import pending_menu

    latest = (Turn.objects.select_related("chat_session", "claimed_by")
              .filter(chat_session=session).order_by("-created_at").first())
    if latest is not None:
        return sync_indicator(latest, dest)
    installation, channel, thread_ts = dest
    want = client.SUSPENDED if pending_menu(session) is not None else client.ACTIVE
    try:
        return client.set_session_status(installation.bot_token, channel=channel,
                                         thread_ts=thread_ts, status=want)
    except Exception:  # noqa: BLE001
        logger.exception("could not set the Slack agent-session status")
        return False


def _load(turn: Turn) -> Turn:
    return (Turn.objects.select_related("chat_session", "chat_session__agent", "claimed_by",
                                        "pinned_runner", "initiator_user", "enqueued_by")
            .get(pk=turn.pk))


#: Both moved to `harness.turn_status` when the state machine did — they are
#: facts about a turn, not about Slack. Re-exported because `slack/services.py`
#: reaches for them through this module, and because a reader looking for the
#: Slack status logic should still find them from here.
runner_gone = _ts.runner_gone
movable_lost = _ts.movable_lost


def _when(ts) -> str:
    """A Slack date token, so each reader sees it in their own timezone."""
    if ts is None:
        return "never"
    return f"<!date^{int(ts.timestamp())}^{{time}}|{ts.isoformat(timespec='minutes')}>"


#: The same gather every channel needs before it can render. Lives with the
#: state machine now; kept under its old name here so the call sites below read
#: as they always did.
_reach_and_cloud = _ts.reach_and_cloud


def _with_prefix(record: SlackTurnPost, text: str, blocks: list | None) -> tuple[str, list | None]:
    """The rendered line, under the words the adopted message already carried."""
    if not record.prefix:
        return text, blocks
    text = f"{record.prefix}\n{text}"
    if blocks:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": record.prefix[:3000]}}, *blocks]
    return text, blocks


#: Where a turn carries what its Slack status line must remember from the
#: request that asked it: `{"adopt_ts": ..., "prefix": ...}`. Written by
#: `services._send` via `send_message(origin_ref=...)`.
ORIGIN_REF_KEY = "slack"


def adoption(turn: Turn) -> tuple[str, str]:
    """(adopt_ts, prefix) for this turn's line.

    `adopt_ts` makes an EXISTING message the status line instead of posting a
    new one — the slash command's anchor, which would otherwise be followed
    immediately by a second message about the same ask. `prefix` is that
    message's own words, kept above the status on every later edit.

    Read off the TURN, not passed in, because the line is posted from a signal
    that holds nothing but the turn. It used to be passed in by the one caller
    that had the request in hand, which was also why only that caller could
    post first: any other path that won the race claimed the row without the
    anchor and posted a second message beside it.
    """
    ref = (turn.origin_ref or {}).get(ORIGIN_REF_KEY) or {}
    return str(ref.get("adopt_ts") or ""), str(ref.get("prefix") or "")


def post(turn: Turn) -> SlackTurnPost | None:
    """Post this turn's status line into its thread, once. None if the session
    is not Slack-born, or the line was already posted.

    Adopts the slash command's anchor when the turn carries one — see
    `adoption`.
    """
    from .relay import _destination, _log_failure

    turn = _load(turn)
    adopt_ts, prefix = adoption(turn)
    dest = _destination(turn)
    if dest is None:
        return None
    installation, channel, thread_ts = dest
    try:
        with transaction.atomic():
            record = SlackTurnPost.objects.create(turn=turn, channel_id=channel,
                                                  slack_ts=adopt_ts, prefix=prefix[:300])
    except IntegrityError:
        sync_indicator(turn, dest)      # someone else owns the line; the state still moved
        return None
    if adopt_ts:
        # Already in the thread: edit it into the status line rather than post.
        refresh(turn)
        return record
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
    # signal found no ts yet and edited nothing, so catch up here. This also
    # sets the native indicator for the first time — hence no separate call
    # above, which would spend two API calls to say one thing.
    refresh(turn)
    return record


def refresh(turn: Turn) -> bool:
    """Re-render this turn's status line if it changed. Returns whether it edited."""
    from .relay import _destination

    record = SlackTurnPost.objects.filter(turn_id=turn.pk).exclude(slack_ts="").first()
    if record is None:
        return False
    turn = _load(turn)
    dest = _destination(turn)
    if dest is None:
        return False
    installation, _channel, thread_ts = dest
    sync_indicator(turn, dest)
    _sync_offline_notice(installation, thread_ts, turn, record)
    reach, cloud = _reach_and_cloud(turn)
    text, blocks = _with_prefix(record, *render(turn, reach=reach, cloud=cloud))
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
    """A turn on some session was enqueued, or moved. Edit its line, or post one.

    Driven by the same two harness signals as the chat feed
    (`canopy_sessions.status_feed`): `turn_status_changed` at enqueue and a
    `status` row after that — so a Slack thread and a chat page learn about an
    ask at the same moment, from the same event.

    Origin-agnostic on purpose. It used to post only for turns that did NOT come
    from Slack, because a Slack turn was posted by an explicit call in
    `services._send` that alone knew the anchor to adopt — and posting here first
    would have lost it. That left the case this now covers: somebody continues a
    Slack-born conversation from canopy-web or a phone while its runner is
    OFFLINE. Nothing ever claims the turn, so no `status` row lands, so the
    thread heard nothing at all — the exact silence this module exists to end.
    Now the anchor rides on the turn (`adoption`), any path may post first, and
    the row's unique constraint makes the rest no-ops.
    """
    if SlackTurnPost.objects.filter(turn_id=turn.pk).exists():
        refresh(turn)
    else:
        post(turn)
