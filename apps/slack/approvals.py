"""An approval request as a Slack DM with Approve / Decline buttons (apps/approvals).

The DM carries everything needed to decide — what approving will do (rendered by the server from
the typed params), the requester's own words kept apart — so the click needs nothing else open. The
decision is taken only from the DM canopy sent to that person for that request, by the person Slack
signs the click as; it goes through `approvals.services.decide`, the same door as the web page, and
`settle` then rewrites the message to say who decided what.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.http import HttpResponse

from apps.approvals import services as approval_services
from apps.approvals.models import ApprovalDelivery, ApprovalRequest

from . import client, services
from .models import SlackInstallation, SlackUserLink

logger = logging.getLogger(__name__)

APPROVE, DECLINE = "approval_approve", "approval_decline"
ACTIONS = (APPROVE, DECLINE)
_TEXT_MAX = 2800


def _clip(text: str, n: int = _TEXT_MAX) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _abs(path: str) -> str:
    return f"{settings.CANOPY_PUBLIC_BASE_URL.rstrip('/')}{path}"


def blocks_for(card: dict, *, settled: bool = False) -> tuple[str, list[dict]]:
    """(fallback text, Block Kit blocks). `settled` replaces the buttons with the outcome."""
    who = card["requested_by"] or "Someone"
    out: list[dict] = [{"type": "header", "text": {
        "type": "plain_text", "text": _clip(("" if settled else "Approve? ") + card["title"], 140)}}]
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(card["plan"][:8], 1))
    out.append({"type": "section", "text": {"type": "mrkdwn", "text": _clip(
        f"*If you approve, this happens*\n{steps}")}})
    if card["reason"]:
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": _clip(
            f"*{who}'s reason* (their words)\n{card['reason']}")}})
    for e in card["evidence"][:2]:
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": _clip(
            f"> {e['quote']}\n_— {e['who'] or 'someone'}_"
            + (f" (<{e['source_url']}|source>)" if e["source_url"] else ""))}})
    out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _clip(
        f"*If you decline:* {card['if_declined']}  ·  needs {card['needs']}")}]})
    if settled:
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": f":white_check_mark: {card['outcome']}"}})
    else:
        out.append({"type": "actions", "block_id": "approval_decision", "elements": [
            {"type": "button", "action_id": APPROVE, "style": "primary", "value": str(card["id"]),
             "text": {"type": "plain_text", "text": "Approve"}},
            {"type": "button", "action_id": DECLINE, "style": "danger", "value": str(card["id"]),
             "text": {"type": "plain_text", "text": "Decline"}},
            {"type": "button", "action_id": "approval_open", "url": _abs(card["url"]),
             "text": {"type": "plain_text", "text": "Open the whole request"}},
        ]})
    fallback = f"Approve? {card['title']}" if not settled else f"{card['title']}: {card['outcome']}"
    return fallback, out


def _installation(req) -> SlackInstallation | None:
    """The Slack this request's workspace is connected to — or, for a workspace below another (Connect
    under Dimagi), its organisation's: a person is the same Slack user whichever workspace asks."""
    ws = req.workspace
    for slug in (ws.slug, *ws.ancestor_slugs()):
        inst = services.installation_for_workspace(slug)
        if inst is not None:
            return inst
    return None


def send(req, user, card: dict) -> dict | None:
    """DM the request to `user`, if canopy's Slack knows them. None when it can't reach them."""
    inst = _installation(req)
    if inst is None:
        return None
    link = SlackUserLink.objects.filter(installation=inst, user=user).first()
    if link is None:
        return None
    text, blocks = blocks_for(card)
    try:
        posted = client.post_message_body(inst.bot_token, channel=link.slack_user_id, text=text, blocks=blocks)
    except client.SlackApiError:
        logger.info("approvals: could not DM %s", user.pk, exc_info=True)
        return None
    return {"team": inst.team_id, "channel": posted.get("channel") or link.slack_user_id, "ts": posted["ts"]}


def settle(delivery, card: dict) -> None:
    ref = delivery.ref or {}
    inst = SlackInstallation.objects.filter(team_id=ref.get("team", "")).first()
    if inst is None or not ref.get("ts"):
        return
    text, blocks = blocks_for(card, settled=True)
    client.update_message(inst.bot_token, channel=ref["channel"], ts=ref["ts"], text=text, blocks=blocks)


def handle_click(payload: dict, action: dict) -> HttpResponse:
    """Approve / Decline on a request DM. Answers 200 whatever happens (Slack retries otherwise);
    a refusal is said to the person, privately, in the DM."""
    installation = services.installation_for(str((payload.get("team") or {}).get("id") or ""))
    if installation is None:
        return HttpResponse(status=200)
    container = payload.get("container") or {}
    channel = str((payload.get("channel") or {}).get("id") or container.get("channel_id") or "")
    ts = str(container.get("message_ts") or (payload.get("message") or {}).get("ts") or "")
    slack_user = str((payload.get("user") or {}).get("id") or "")

    def tell(text: str) -> None:
        try:
            client.post_ephemeral(installation.bot_token, channel=channel, user=slack_user, text=text)
        except Exception:  # noqa: BLE001 — the answer is a courtesy
            logger.exception("approvals: could not answer a click")

    user, _assurance, _info = services.slack_user(installation, slack_user)
    value = str(action.get("value") or "").strip()
    req = ApprovalRequest.objects.filter(pk=int(value)).first() if value.isdigit() else None
    if user is None or req is None:
        tell("canopy couldn't match you to a canopy account, or that request is gone. "
             "Open it from your canopy link instead.")
        return HttpResponse(status=200)
    # Only the DM canopy sent THIS person for THIS request may decide it.
    sent = ApprovalDelivery.objects.filter(request=req, user=user, channel=ApprovalDelivery.SLACK).first()
    if sent is None or str((sent.ref or {}).get("ts") or "") != ts:
        tell("That message isn't one canopy sent you for this request. Open it in canopy to decide it.")
        return HttpResponse(status=200)
    try:
        approval_services.decide(req.pk, user, "approve" if action.get("action_id") == APPROVE else "decline",
                                 via="slack")
    except approval_services.ApprovalError as exc:
        tell(str(exc))
    return HttpResponse(status=200)
