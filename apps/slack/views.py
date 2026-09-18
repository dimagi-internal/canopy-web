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

from . import client, services
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
    """Say something only the sender sees, in the thread they wrote in."""
    if installation is None:
        return
    try:
        client.post_ephemeral(installation.bot_token, channel=inbound.channel_id,
                              user=inbound.slack_user_id, text=text,
                              thread_ts=inbound.reply_thread_ts)
    except Exception:  # noqa: BLE001 — a failed courtesy note must not fail the event
        logger.exception("could not post a Slack ephemeral reply")


def _inbound_from_event(body: dict) -> services.Inbound | None:
    event = body.get("event") or {}
    kind = event.get("type")
    # Never react to a bot — including ourselves, whose own replies arrive as
    # `message.im` events in a DM. `subtype` covers edits, joins, deletions.
    if event.get("bot_id") or event.get("subtype"):
        return None
    if kind == "app_mention":
        is_dm = False
    elif kind == "message" and event.get("channel_type") == "im":
        is_dm = True
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
    inbound = _inbound_from_event(body)
    if inbound is None or not inbound.slack_user_id:
        return JsonResponse({"ok": True})
    try:
        outcome = services.handle_message(inbound)
    except Exception:  # noqa: BLE001
        logger.exception("slack event failed")
        _tell(services.installation_for(inbound.team_id), inbound,
              "Something went wrong handing that to canopy. It has been logged.")
        return JsonResponse({"ok": True})
    _tell(services.installation_for(inbound.team_id), inbound, outcome.message)
    return JsonResponse({"ok": True})


def _ephemeral(text: str) -> JsonResponse:
    return JsonResponse({"response_type": "ephemeral", "text": text})


@csrf_exempt
@require_POST
def commands(request: HttpRequest) -> HttpResponse:
    """``/canopy <agent> <ask>`` · ``/canopy link`` · ``/canopy agents``.

    A slash command has no message of its own to thread under, so the bot posts
    one — "<user> asked <agent>: …" — and the conversation lives in that
    message's thread, exactly as if it had started with a mention.
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
    word = text.split(" ", 1)[0].lower()
    if word == "link":
        return _ephemeral(f"Link your canopy account: {services.link_url(team_id, slack_user_id)}")
    if word in ("", "help", "agents"):
        return _ephemeral(services.agent_list(installation))

    user, refusal = services.authorize(installation, slack_user_id)
    if refusal is not None:
        return _ephemeral(refusal.message)
    agents = {a.slug.lower(): a for a in services.enabled_agents(installation)}
    agent = agents.get(word.rstrip(":,"))
    if agent is None:
        return _ephemeral(services.agent_list(installation))
    ask = text.split(" ", 1)[1].strip() if " " in text else ""
    if not ask:
        return _ephemeral(f"What would you like `{agent.slug}` to do? `/canopy {agent.slug} <ask>`")
    try:
        root_ts = client.post_message(installation.bot_token, channel=channel_id,
                                      text=f"<@{slack_user_id}> asked *{agent.slug}*: {ask}")
    except client.SlackApiError as e:
        if e.error in ("not_in_channel", "channel_not_found"):
            return _ephemeral("I'm not in this channel. Invite me with `/invite @canopy`, then try again.")
        logger.exception("slack slash command could not post its anchor")
        return _ephemeral(f"Slack refused the post ({e.error}).")
    inbound = services.Inbound(team_id=team_id, channel_id=channel_id, slack_user_id=slack_user_id,
                               text=f"{agent.slug} {ask}", ts=root_ts, thread_ts=root_ts)
    outcome = services.handle_message(inbound)
    return _ephemeral(outcome.message)
