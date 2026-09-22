"""Slack's two webhook doors: the Events API and the ``/canopy`` slash command.

Bare Django views, not Ninja: the body must be read RAW to check its signature,
and Slack's request/response shapes are its own. Allowlisted as ``/api/slack/``
in ``apps/common/middleware.py`` and self-enforcing via the signing secret, the
same arrangement as ``/api/inbound/``.

Response rules that look odd and are deliberate:

* **Anything verified gets a 200**, including a message we refuse. A non-2xx
  makes Slack redeliver, so a refusal on the wire becomes a retry storm; the
  refusal goes back to the human as an ephemeral message instead.
* **Unconfigured is 503**, not 401 — the deployment is not set up, which is a
  different fact from "your signature is wrong", and should read as one.
"""
from __future__ import annotations

import json
import logging

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.events import services as events_services
from apps.events.models import Event
from apps.workspaces.models import Workspace

from . import client, relay, services
from .verify import SignatureError, verify_slack_signature

logger = logging.getLogger(__name__)


def _verified(request: HttpRequest) -> HttpResponse | None:
    """None when the request is Slack's; otherwise the response to send."""
    if not services.is_configured():
        return HttpResponse("Slack is not configured on this deployment.", status=503)
    try:
        verify_slack_signature(
            secret=services.signing_secret(),
            body=request.body,
            timestamp=request.headers.get("X-Slack-Request-Timestamp", ""),
            signature=request.headers.get("X-Slack-Signature", ""),
        )
    except SignatureError as e:
        logger.warning("slack signature rejected: %s", e)
        return HttpResponse(status=401)
    return None


def _tell(installation, inbound: services.Inbound, text: str) -> None:
    """Say something only the sender sees, where they will see it.

    Threaded ONLY when they wrote inside a thread. For a top-level message,
    threading under that message is invisible: an ephemeral reply leaves no
    "1 reply" marker, so the note exists only for someone who opens a thread
    they have no reason to open. That is how the first live mention on labs
    (2026-09-19) got its "link your account" reply and looked like silence.
    """
    if installation is None:
        return
    try:
        client.post_ephemeral(installation.bot_token, channel=inbound.channel_id,
                              user=inbound.slack_user_id, text=text,
                              thread_ts=inbound.thread_ts)
    except Exception as e:  # noqa: BLE001 — a failed courtesy note must not fail the event
        logger.exception("could not post a Slack ephemeral reply")
        _record(installation, inbound, "reply_failed", str(e), level=Event.ERROR)


def _record(installation, inbound: services.Inbound, status: str, summary: str,
            level: str = "", outcome: services.Outcome | None = None) -> None:
    """Every message canopy did NOT turn into a turn leaves a row in the fleet log.

    The access log only says `POST /api/slack/events 200` whatever happened, so
    "I mentioned it and nothing happened" was answerable only by elimination.
    Coalesced per (status, sender, channel): a user retrying while unlinked
    bumps one row's count instead of writing one per attempt.
    """
    if installation is None:
        return
    # The tenant the message was about, else the Slack's home tenant: a Slack
    # can serve several, and a refusal that resolved to none still needs a row.
    workspace_id = services.log_workspace(installation, outcome)
    if workspace_id is None:
        return
    try:
        events_services.record([{
            "source": "slack",
            "kind": f"slack.{status}",
            "level": level or Event.WARN,
            "key": f"{status}:{inbound.slack_user_id}:{inbound.channel_id}",
            "summary": summary[:500],
            "payload": {"team": inbound.team_id, "channel": inbound.channel_id,
                        "user": inbound.slack_user_id, "ts": inbound.ts},
        }], workspace=Workspace.objects.get(pk=workspace_id))
    except Exception:  # noqa: BLE001 — bookkeeping must not fail the event
        logger.exception("could not record a Slack event")


def _inbound_from_event(body: dict) -> services.Inbound | None:
    event = body.get("event") or {}
    kind = event.get("type")
    # Never react to a bot — including ourselves, whose own replies arrive as
    # `message.im` events in a DM. `subtype` covers edits, joins, deletions.
    if event.get("bot_id") or event.get("subtype"):
        return None
    follow = False
    if kind == "app_mention":
        is_dm = False
    elif kind == "message" and event.get("channel_type") == "im":
        is_dm = True
    elif kind == "message" and event.get("channel_type") in ("channel", "group") \
            and event.get("thread_ts"):
        # A plain reply in a channel thread. Slack sends EVERY message in every
        # channel the bot is in once `message.channels` is subscribed; only
        # replies in a thread canopy is already part of go any further, and
        # that check (`events`) happens before anything is recorded.
        is_dm, follow = False, True
    else:
        return None
    return services.Inbound(
        team_id=str(body.get("team_id") or event.get("team") or ""),
        channel_id=str(event.get("channel") or ""),
        slack_user_id=str(event.get("user") or ""),
        text=str(event.get("text") or ""),
        ts=str(event.get("ts") or ""),
        thread_ts=str(event.get("thread_ts") or ""),
        is_dm=is_dm,
        follow=follow,
    )


@csrf_exempt
@require_POST
def events(request: HttpRequest) -> HttpResponse:
    refused = _verified(request)
    if refused is not None:
        return refused
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return HttpResponse(status=400)
    if body.get("type") == "url_verification":
        return JsonResponse({"challenge": body.get("challenge", "")})
    if body.get("type") != "event_callback":
        return JsonResponse({"ok": True})
    if str((body.get("event") or {}).get("type") or "") == "agent_session_stopped":
        return _stop_from_slack(body)
    inbound = _inbound_from_event(body)
    if inbound is None or not inbound.slack_user_id:
        return JsonResponse({"ok": True})
    installation = services.installation_for(inbound.team_id)
    if installation is None:
        # Nothing to reply with (no bot token) and no tenant to log against.
        logger.warning("slack event for a team with no installation: %s", inbound.team_id)
        return JsonResponse({"ok": True})
    if inbound.follow:
        # A reply that mentions the bot also arrives as `app_mention`, which
        # handles it; and a thread canopy is not in is none of canopy's business.
        # Dropped here, unread and unrecorded.
        if f"<@{installation.bot_user_id}>" in inbound.text or \
                not services.has_thread_session(installation, inbound):
            return JsonResponse({"ok": True})
    try:
        outcome = services.handle_message(inbound)
    except Exception as e:  # noqa: BLE001
        logger.exception("slack event failed")
        _record(installation, inbound, "failed", repr(e), level=Event.ERROR)
        _tell(installation, inbound, "Something went wrong handing that to canopy. It has been logged.")
        return JsonResponse({"ok": True})
    if outcome.status not in services.OK_STATUSES:
        _record(installation, inbound, outcome.status, outcome.message, outcome=outcome)
        _tell(installation, inbound, outcome.message)
    elif outcome.status in (services.ANSWERED, services.MOVED, services.NOTHING_QUEUED):
        _tell(installation, inbound, outcome.message)
    # A SENT message is acknowledged by its public status line in the thread
    # (apps/slack/status.py) — picked up, or blocked and why — so no private note.
    return JsonResponse({"ok": True})


def _ephemeral(text: str) -> JsonResponse:
    return JsonResponse({"response_type": "ephemeral", "text": text})


@csrf_exempt
@require_POST
def commands(request: HttpRequest) -> HttpResponse:
    """``/canopy <agent> <ask>`` · ``/<agent> <ask>`` · ``/canopy link`` · ``/canopy agents``.

    A slash command has no message of its own to thread under, so the bot posts
    one — "<user> asked <agent>: …" — and the conversation lives in that
    message's thread, exactly as if it had started with a mention.

    Every command is registered in the Slack APP's config, not here, and Slack
    shows every one to everyone in the workspace. So `/hal` is just a command
    whose name is an agent's slug: whether hal answers is decided here, per
    request, by the same switch and the same member/contact rule as a mention.
    """
    refused = _verified(request)
    if refused is not None:
        return refused
    post = request.POST
    team_id, channel_id = post.get("team_id", ""), post.get("channel_id", "")
    slack_user_id, text = post.get("user_id", ""), (post.get("text") or "").strip()
    installation = services.installation_for(team_id)
    if installation is None:
        return _ephemeral("This Slack workspace isn't connected to canopy.")
    command = str(post.get("command") or "").lstrip("/").lower()
    if command and command != "canopy":
        # `/hal what's on today?` is `/canopy hal what's on today?`.
        text = f"{command} {text}".strip()
    word = text.split(" ", 1)[0].lower()
    if word == "link":
        return _ephemeral(f"Link your canopy account: {services.link_url(team_id, slack_user_id)}")
    if word in ("", "help", "agents"):
        return _ephemeral(services.agent_list(installation)
                          + " `/canopy cloud` sends anything of yours stuck behind an offline"
                          " runner to a cloud runner (runner admins only).")
    if word == services.CLOUD_WORD:
        return _ephemeral(services.route_mine_to_cloud(installation, slack_user_id).message)

    agents = {a.slug.lower(): a for a in services.enabled_agents(installation)}
    agent = agents.get(word.rstrip(":,"))
    if agent is None:
        return _ephemeral(services.agent_list(installation))
    # Checked before the anchor is posted, against the AGENT's tenant: a
    # blocked sender must not get a public "asked hal" line for nothing.
    _principal, refusal = services.resolve_principal(installation, slack_user_id, agent.workspace_id)
    if refusal is not None:
        return _ephemeral(refusal.message)
    ask = text.split(" ", 1)[1].strip() if " " in text else ""
    if not ask:
        usage = f"/{agent.slug}" if command == agent.slug else f"/canopy {agent.slug}"
        return _ephemeral(f"What would you like `{agent.slug}` to do? `{usage} <ask>`")
    anchor = f"<@{slack_user_id}> asked *{agent.slug}*: {ask}"
    try:
        root_ts = client.post_message(installation.bot_token, channel=channel_id, text=anchor)
    except client.SlackApiError as e:
        if e.error in ("not_in_channel", "channel_not_found"):
            return _ephemeral("I'm not in this channel. Invite me with `/invite @canopy`, then try again.")
        logger.exception("slack slash command could not post its anchor")
        return _ephemeral(f"Slack refused the post ({e.error}).")
    # The anchor BECOMES the status line: one message that says what was asked
    # and what is happening to it, rather than two saying half each.
    inbound = services.Inbound(team_id=team_id, channel_id=channel_id, slack_user_id=slack_user_id,
                               text=f"{agent.slug} {ask}", ts=root_ts, thread_ts=root_ts,
                               adopt_ts=root_ts, adopt_prefix=anchor)
    outcome = services.handle_message(inbound)
    return _ephemeral(outcome.message)


@csrf_exempt
@require_POST
def interactions(request: HttpRequest) -> HttpResponse:
    """Button presses on a question post (Block Kit `block_actions`).

    Signed exactly like the other two doors. Only OUR buttons are acted on: a
    checkbox or radio toggle also arrives here, one payload per click, and is
    just a change of selection that the eventual Submit carries in `state`.
    """
    from . import menus, status

    refused = _verified(request)
    if refused is not None:
        return refused
    try:
        payload = json.loads(request.POST.get("payload") or "{}")
    except ValueError:
        return HttpResponse(status=400)
    if payload.get("type") != "block_actions":
        return HttpResponse(status=200)
    action = (payload.get("actions") or [{}])[0]
    action_id = str(action.get("action_id") or "")
    if not (action_id.startswith(menus.PICK)
            or action_id in (menus.SUBMIT, menus.DISMISS, status.ROUTE_CLOUD)):
        return HttpResponse(status=200)
    installation = services.installation_for(str((payload.get("team") or {}).get("id") or ""))
    if installation is None:
        return HttpResponse(status=200)
    message = payload.get("message") or {}
    container = payload.get("container") or {}
    inbound = services.Inbound(
        team_id=installation.team_id,
        channel_id=str((payload.get("channel") or {}).get("id") or container.get("channel_id") or ""),
        slack_user_id=str((payload.get("user") or {}).get("id") or ""),
        text="", ts=str(container.get("message_ts") or message.get("ts") or ""),
        thread_ts=str(message.get("thread_ts") or ""),
    )
    try:
        if action_id == status.ROUTE_CLOUD:
            outcome = services.route_from_click(
                installation, slack_user_id=inbound.slack_user_id, channel_id=inbound.channel_id,
                action=action,
            )
        else:
            outcome = services.answer_from_click(
                installation, slack_user_id=inbound.slack_user_id, channel_id=inbound.channel_id,
                message_ts=inbound.ts, action=action, state=payload.get("state") or {},
            )
    except Exception as e:  # noqa: BLE001
        logger.exception("slack interaction failed")
        _record(installation, inbound, "failed", repr(e), level=Event.ERROR)
        return HttpResponse(status=200)
    if outcome.status not in services.OK_STATUSES:
        _record(installation, inbound, outcome.status, outcome.message, outcome=outcome)
        _tell(installation, inbound, outcome.message)
    elif outcome.status in (services.STALE, services.MOVED):
        _tell(installation, inbound, outcome.message)
    return HttpResponse(status=200)


def _stop_from_slack(body: dict) -> HttpResponse:
    """Slack's own Stop button, beside the "Working…" indicator it draws.

    The button exists only because the app subscribes to this event, and it is
    the one control the native indicator carries. Stop the same way canopy's own
    stop does — cancel the turns, then interrupt the terminal, since an agent
    turn is fire-and-continue and a turn-shaped cancel alone would find nothing
    (see canopy_sessions.services.interrupt_session).
    """
    from apps.canopy_sessions import services as session_services

    from . import status

    event = body.get("event") or {}
    installation = services.installation_for(str(body.get("team_id") or event.get("team") or ""))
    if installation is None:
        return JsonResponse({"ok": True})
    channel = str(event.get("channel_id") or event.get("channel") or "")
    thread_ts = str(event.get("thread_ts") or "")
    key = services.thread_key(installation.team_id, channel,
                              thread_ts or services.DM_ANCHOR)
    session = (services.tenant_sessions(installation).select_related("agent")
               .filter(**{f"metadata__{services.SLACK_THREAD_KEY}": key})
               .order_by("-created_at").first())
    if session is None:
        return JsonResponse({"ok": True})
    try:
        session_services.cancel_session_turns(session)
        session_services.interrupt_session(session)
    except Exception:  # noqa: BLE001 — a stop that half-lands must still settle the indicator
        logger.exception("slack stop failed")
    dest = relay.session_destination(session)
    if dest is not None:
        status.sync_session(session, dest)
    return JsonResponse({"ok": True})
