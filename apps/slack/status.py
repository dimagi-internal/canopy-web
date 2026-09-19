"""The status card on a Slack thread — which runner, what state, where on canopy.

A Slack thread used to show the ask and, eventually, the answer. Everything in
between was invisible: which box picked it up, whether it was working or
blocked, and — the case that prompted this — that the laptop running it had
been closed and nothing was going to happen until it opened again.

So every Slack-born session gets ONE bot message near the top of its thread,
edited in place as the session moves. It says what canopy-web's session header
says, derived the same way (`live_status`, `pending_menu`, the latest turn), so
the two surfaces cannot disagree about a session:

    `hal` · ⚙️ Working
    Runner `jj-mbp` (laptop) · online  ·  Open in canopy

An edit notifies nobody, which is right for "still working" and wrong for "your
runner died". Losing the runner mid-turn therefore ALSO posts a thread reply —
the offline notice — carrying the ways out: move the session to a runner that is
up (a `transfer_session`, the same operation as the web's Transfer), or retry on
the same box once it is back. When the episode ends (the box returns, the work
is moved, a new turn starts) the notice loses its buttons, so a stale one can
never be pressed.

There is no scheduler in canopy-web to notice a heartbeat going quiet, and a
dead runner cannot report its own death. `sweep` rides `sessions_reported` —
every OTHER runner's ~10s report — throttled to one pass per `SWEEP_EVERY`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import client

logger = logging.getLogger(__name__)

SWEEP_EVERY = dt.timedelta(seconds=15)
SWEEP_LOCK = "slack:status_sweep"

# The states a card can show. The EPISODE_STATES open an offline episode (a
# notice with buttons).
QUEUED, WORKING, WAITING, OFFLINE, STRANDED, DONE, FAILED, LOST, STOPPED = (
    "queued", "working", "waiting", "offline", "stranded", "done", "failed", "lost", "stopped")
#: OFFLINE: the runner died mid-turn. STRANDED: a new message is queued for a
#: runner that is not there (a reply sent after the laptop closed — the session
#: is bound to that box, so nothing else will claim it). LOST: the lease ran out.
EPISODE_STATES = {OFFLINE, STRANDED, LOST}

MOVE, RETRY = "status_move", "status_retry"
#: Marks a card's first block, so a card is recognisable among a thread's posts.
CARD_BLOCK = "canopy_status"
NOTICE_BLOCK = "canopy_status_notice"
#: How many "Move to …" buttons at most — a fleet with more boxes than this has
#: the web UI for the long tail.
MAX_MOVE_TARGETS = 3


# --- what state is the session in ----------------------------------------------

def _latest_turn(session):
    from apps.harness.models import Turn

    return (Turn.objects.filter(chat_session=session).select_related("claimed_by", "pinned_runner")
            .order_by("-created_at").first())


def _bound_runner(session):
    from apps.canopy_sessions.models import RunnerBinding

    binding = RunnerBinding.objects.select_related("runner").filter(session=session).first()
    return binding.runner if binding and binding.runner_id else None


def _when(ts) -> str:
    """Slack's own date token: rendered in each reader's timezone."""
    if ts is None:
        return "never"
    epoch = int(ts.timestamp())
    return f"<!date^{epoch}^{{date_short_pretty}} {{time}}|{ts.isoformat(timespec='minutes')}>"


def runner_label(runner) -> str:
    from apps.harness.models import Runner

    where = "cloud" if runner.location == Runner.CLOUD else "laptop"
    return f"`{runner.name}` ({where})"


def describe(session) -> dict:
    """The session's state, as canopy-web would show it. Pure read."""
    from apps.canopy_sessions.serializers import pending_menu
    from apps.harness.models import Turn

    turn = _latest_turn(session)
    runner = None
    if turn is not None:
        runner = turn.claimed_by or turn.pinned_runner
    runner = runner or _bound_runner(session)
    reachable = runner is not None and runner.is_reachable

    if turn is None:
        state = DONE
    elif turn.status in Turn.NON_TERMINAL and turn.claimed_by_id and not reachable:
        state = OFFLINE
    elif pending_menu(session) is not None:
        state = WAITING
    elif turn.status == Turn.QUEUED:
        state = STRANDED if runner is not None and not reachable else QUEUED
    elif turn.status in Turn.NON_TERMINAL:
        state = WORKING
    elif turn.status == Turn.LOST:
        state = LOST
    elif turn.status == Turn.FAILED:
        state = FAILED
    elif turn.status == Turn.CANCELLED:
        state = STOPPED
    else:
        state = DONE
    return {"state": state, "turn": turn, "runner": runner, "reachable": reachable}


def _headline(state: str, info: dict) -> str:
    runner = info["runner"]
    name = f"`{runner.name}`" if runner else "its runner"
    return {
        QUEUED: ":hourglass_flowing_sand: Queued — waiting for a runner to pick it up",
        WORKING: ":gear: Working",
        WAITING: ":raised_hand: Waiting on you — see the question below",
        OFFLINE: f":red_circle: Paused — {name} went offline mid-turn",
        STRANDED: f":red_circle: Waiting for {name}, which is offline",
        DONE: ":white_check_mark: Done — reply in this thread to continue",
        FAILED: ":warning: The last turn failed",
        LOST: f":red_circle: Interrupted — {name} went away before finishing",
        STOPPED: ":black_square_for_stop: Stopped",
    }[state]


def render(session, info: dict) -> tuple[str, list[dict]]:
    from .services import session_url

    agent = session.agent.slug if session.agent_id else "agent"
    head = f"*`{agent}`* · {_headline(info['state'], info)}"
    runner = info["runner"]
    bits = []
    if runner is not None:
        live = runner.live_status
        seen = "" if info["reachable"] else f", last seen {_when(runner.last_heartbeat_at)}"
        bits.append(f"Runner {runner_label(runner)} · {live}{seen}")
    else:
        bits.append("Runner: not picked up yet")
    # A contact cannot open canopy; a link would be a dead end (see handle_message).
    if session.created_by_id:
        bits.append(f"<{session_url(session)}|Open in canopy>")
    context = "  ·  ".join(bits)
    blocks = [
        {"type": "section", "block_id": CARD_BLOCK, "text": {"type": "mrkdwn", "text": head}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": context}]},
    ]
    return f"{head}\n{context}", blocks


# --- the offline notice ----------------------------------------------------------

def move_targets(session, exclude=None) -> list:
    """Runners this session could be moved to RIGHT NOW: up, ready, and able to
    claim its turns (`_placeable_runner` — same gate the web's Transfer uses)."""
    from apps.canopy_sessions.services import _placeable_runner
    from apps.harness.models import Runner

    out = []
    candidates = (Runner.objects.filter(paired_by__isnull=False).exclude(status=Runner.RETIRED)
                  .order_by("location", "name"))   # "cloud" < "local": cloud first, it does not get closed
    for runner in candidates:
        if exclude is not None and runner.pk == exclude.pk:
            continue
        if not runner.is_available:
            continue
        if _placeable_runner(session, runner.pk) is None:
            continue
        out.append(runner)
        if len(out) >= MAX_MOVE_TARGETS:
            break
    return out


def _button_value(session, **extra) -> str:
    return json.dumps({"s": str(session.id), **extra}, separators=(",", ":"))


def notice(session, info: dict) -> tuple[str, list[dict]]:
    runner = info["runner"]
    agent = session.agent.slug if session.agent_id else "the agent"
    name = runner_label(runner) if runner else "its runner"
    if info["state"] == OFFLINE:
        text = (f":red_circle: {name} went offline while `{agent}` was working on this "
                f"(last heartbeat {_when(runner.last_heartbeat_at if runner else None)}).\n"
                "Nothing is lost yet — if it comes back (laptop reopened), the work carries on "
                "and I'll say so here. To keep going without it, move it to a runner that's up. "
                "A move starts a fresh session there: anything not pushed from the old box stays behind.")
    elif info["state"] == STRANDED:
        text = (f":red_circle: This conversation is on {name}, which is offline "
                f"(last heartbeat {_when(runner.last_heartbeat_at if runner else None)}), so `{agent}` "
                "can't start on your message yet.\n"
                "It will be picked up when that runner is back. To go ahead now, move it to a runner "
                "that's up. A move starts a fresh session there: anything not pushed from the old box "
                "stays behind.")
    else:
        text = (f":red_circle: `{agent}`'s turn was lost — {name} went away before it finished.\n"
                "Retry it on the same runner once that's back, or move it to a runner that's up. "
                "A move starts a fresh session there: anything not pushed from the old box stays behind.")
    elements = [{"type": "button", "action_id": f"{MOVE}_{i}",
                 "text": {"type": "plain_text", "text": f"Move to {t.name}"[:75]},
                 "value": _button_value(session, r=str(t.id))}
                for i, t in enumerate(move_targets(session, exclude=runner))]
    if elements:
        elements[0]["style"] = "primary"
    if info["state"] == LOST:
        elements.append({"type": "button", "action_id": RETRY,
                         "text": {"type": "plain_text", "text": "Retry"},
                         "value": _button_value(session)})
    blocks = [{"type": "section", "block_id": NOTICE_BLOCK, "text": {"type": "mrkdwn", "text": text}}]
    if elements:
        blocks.append({"type": "actions", "block_id": "status_offline", "elements": elements})
    else:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                       "No other runner is up to take it right now."}]})
    return text, blocks


def _closed_notice(text: str) -> list[dict]:
    return [{"type": "context", "elements": [{"type": "mrkdwn", "text": text[:3000]}]}]


# --- the one entry point ---------------------------------------------------------

def refresh(session, *, create: bool = True) -> str | None:
    """Bring this Slack session's card (and offline notice) up to date.

    Idempotent and cheap when nothing changed: the rendered card is hashed and
    Slack is only called when the hash moves. Returns the state, or None for a
    session that is not Slack-born. Never raises — a card is a courtesy, and the
    callers are runner reports and ledger appends that must not fail over it.
    """
    try:
        return _refresh(session, create=create)
    except Exception:  # noqa: BLE001
        logger.exception("slack status refresh failed")
        return None


def _refresh(session, *, create: bool) -> str | None:
    from .models import SlackThreadStatus
    from .relay import session_destination

    dest = session_destination(session)
    if dest is None:
        return None
    installation, channel, thread_ts = dest
    info = describe(session)
    text, blocks = render(session, info)
    digest = hashlib.sha256(text.encode()).hexdigest()[:40]

    card = SlackThreadStatus.objects.filter(session=session).first()
    if card is None:
        if not create:
            return info["state"]
        try:
            with transaction.atomic():
                card = SlackThreadStatus.objects.create(session=session, channel_id=channel,
                                                        rendered=digest)
        except IntegrityError:
            return info["state"]          # a concurrent refresh is posting it
        try:
            card.slack_ts = client.post_message(installation.bot_token, channel=channel, text=text,
                                                thread_ts=thread_ts, blocks=blocks)
            card.save(update_fields=["slack_ts"])
        except Exception:  # noqa: BLE001
            logger.exception("could not post a Slack status card")
    elif card.rendered != digest and card.slack_ts:
        card.rendered = digest
        card.save(update_fields=["rendered", "updated_at"])
        client.update_message(installation.bot_token, channel=card.channel_id, ts=card.slack_ts,
                              text=text, blocks=blocks)

    _sync_notice(installation, channel, thread_ts, session, card, info)
    return info["state"]


def _sync_notice(installation, channel, thread_ts, session, card, info) -> None:
    from .models import SlackThreadStatus

    state, runner = info["state"], info["runner"]
    if state in EPISODE_STATES:
        # Claimed by a conditional update, so two sweeps racing post ONE notice.
        episode = f"{state}:{info['turn'].pk}"
        if card.episode == episode:
            return
        claimed = SlackThreadStatus.objects.filter(pk=card.pk, episode=card.episode) \
            .update(episode=episode)
        if not claimed:
            return
        _close_notice(installation, card, ":heavy_minus_sign: Superseded — see below.")
        text, blocks = notice(session, info)
        ts = client.post_message(installation.bot_token, channel=channel, text=text,
                                 thread_ts=thread_ts, blocks=blocks)
        SlackThreadStatus.objects.filter(pk=card.pk).update(notice_ts=ts, notice_runner=runner)
        return

    if not card.episode:
        return
    claimed = SlackThreadStatus.objects.filter(pk=card.pk, episode=card.episode).update(episode="")
    if not claimed:
        return
    was_offline = card.episode.startswith(OFFLINE + ":")
    card.refresh_from_db()
    if was_offline and runner is not None and runner.pk == card.notice_runner_id and info["reachable"]:
        outcome = f":large_green_circle: {runner_label(runner)} is back online — the work is carrying on."
        client.post_message(installation.bot_token, channel=channel, text=outcome, thread_ts=thread_ts)
    else:
        outcome = ":heavy_minus_sign: Resolved — see the status above."
    _close_notice(installation, card, outcome)


def _close_notice(installation, card, outcome: str) -> None:
    if not card.notice_ts:
        return
    try:
        client.update_message(installation.bot_token, channel=card.channel_id, ts=card.notice_ts,
                              text=outcome, blocks=_closed_notice(outcome))
    except Exception:  # noqa: BLE001 — a stale button is re-checked on click anyway
        logger.exception("could not close a Slack offline notice")
    type(card).objects.filter(pk=card.pk, notice_ts=card.notice_ts).update(notice_ts="")
    card.notice_ts = ""


# --- the sweep -------------------------------------------------------------------

def sweep(*, force: bool = False) -> int:
    """Refresh every Slack session that could be changing. Throttled.

    "Could be changing": a non-terminal turn (working, queued, blocked — any of
    which can go offline), or an open offline episode (which ends when a runner
    comes back or a new turn starts). Everything else is settled and skipped.
    """
    from apps.canopy_sessions.models import Session
    from apps.harness.models import Turn

    from .services import SLACK_THREAD_KEY

    if not force and not cache.add(SWEEP_LOCK, 1, timeout=int(SWEEP_EVERY.total_seconds())):
        return 0
    live = Session.objects.filter(metadata__has_key=SLACK_THREAD_KEY,
                                  turns__status__in=list(Turn.NON_TERMINAL))
    episodes = Session.objects.filter(slack_status__isnull=False).exclude(slack_status__episode="")
    ids = set(live.values_list("pk", flat=True)) | set(episodes.values_list("pk", flat=True))
    n = 0
    for session in Session.objects.select_related("agent").filter(pk__in=ids):
        refresh(session)
        n += 1
    return n


# --- button presses on the notice -------------------------------------------------

def act(installation, *, slack_user_id: str, channel_id: str, action: dict):
    """A Move / Retry press -> (ok, message for the presser)."""
    from apps.canopy_sessions import services as session_services
    from apps.canopy_sessions.models import Session
    from apps.harness import initiator as who
    from apps.harness import services as harness_services
    from apps.harness.models import Turn

    from .services import resolve_principal

    try:
        value = json.loads(action.get("value") or "{}")
    except ValueError:
        value = {}
    session = (Session.objects.select_related("agent")
               .filter(pk=value.get("s"), workspace=installation.workspace,
                       metadata__slack_team=installation.team_id,
                       metadata__slack_channel=channel_id).first()) if value.get("s") else None
    if session is None:
        return False, "That session is no longer here."
    principal, refusal = resolve_principal(installation, slack_user_id)
    if refusal is not None:
        return False, refusal.message
    if principal.user is None:
        return False, "Only a member of this canopy workspace can move or retry a session."

    info = describe(session)
    if info["state"] not in EPISODE_STATES:
        refresh(session)
        return False, "Nothing to do — the session is no longer stuck. See the status card."
    turn, source = info["turn"], info["runner"]
    initiator = who.for_user(principal.user, via=f"slack:{installation.team_id}",
                             assurance=principal.assurance)
    action_id = str(action.get("action_id") or "")

    if action_id == RETRY:
        session_services.send_message(
            session=session, text=turn.prompt, user=principal.user,
            client_id=f"slack-retry:{turn.pk}", origin=Turn.ORIGIN_SLACK, initiator=initiator)
        refresh(session)
        return True, f"Retrying on {runner_label(source) if source else 'the next free runner'}."

    if action_id.startswith(MOVE):
        target_id = value.get("r")
        with transaction.atomic():
            # The runner is gone, so it will never close its own turn. Close it
            # here, the way the lease sweep would in a few minutes, or the
            # transfer refuses ("a turn is still executing").
            asks = [turn.prompt]
            if info["state"] == STRANDED:
                # Held for the dead box; left queued they would run AFTER the
                # handover, on the new box, without it. Carried in the brief instead.
                stuck = list(Turn.objects.filter(chat_session=session, status=Turn.QUEUED)
                             .order_by("created_at"))
                asks = [t.prompt for t in stuck] or asks
                for t in stuck:
                    if Turn.objects.filter(pk=t.pk, status=Turn.QUEUED) \
                            .update(status=Turn.CANCELLED, finished_at=timezone.now()):
                        harness_services.append_events(t, [{"kind": "status", "payload": {
                            "status": Turn.CANCELLED, "reason": "runner_offline_moved"}}])
            if info["state"] == OFFLINE:
                now = timezone.now()
                stranded = Turn.objects.filter(chat_session=session, claimed_by=source,
                                               status__in=list(harness_services.EXECUTING))
                for t in stranded:
                    if Turn.objects.filter(pk=t.pk, status__in=list(harness_services.EXECUTING)) \
                            .update(status=Turn.LOST, finished_at=now):
                        harness_services.append_events(t, [{"kind": "status", "payload": {
                            "status": Turn.LOST, "reason": "runner_offline_moved"}}])
            brief = (
                "This came from a Slack thread. The runner holding it went offline before "
                "answering, so the ask below is NOT done. Check what (if anything) the old "
                "box pushed, then carry the ask through and answer it — your reply goes back to "
                "the Slack thread.\n\nThe ask:\n\n" + "\n\n---\n\n".join(asks)
            )
            try:
                binding, _new = session_services.transfer_session(
                    session=session, placement=str(target_id), brief=brief,
                    user=principal.user, initiator=initiator)
            except LookupError:
                # Never bound (it died before reporting a session): nothing to
                # re-point, so just run the ask on the target.
                target = session_services._placeable_runner(session, target_id)
                if target is None:
                    return False, "That runner can't take this session."
                session_services.send_message(
                    session=session, text="\n\n".join(asks), user=principal.user,
                    client_id=f"slack-move:{turn.pk}:{target.pk}", origin=Turn.ORIGIN_SLACK,
                    initiator=initiator, placement=str(target.pk))
                name = target.name
            except (ValueError, RuntimeError) as e:
                return False, f"Couldn't move it: {e}"
            else:
                name = binding.runner.name
        refresh(session)
        return True, f"Moved to `{name}` — it picks up from the ask; the reply will come back here."

    return False, "Unknown action."
