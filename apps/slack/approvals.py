"""An approval card as a Slack DM with Approve / Decline buttons (apps/approvals).

The DM carries everything needed to decide — the ask, the person's words, the plan, what
approving starts — so the click needs nothing else open. The decision is taken only from the
DM canopy sent to that person for that card, by the person Slack signs the click as; it goes
through `approvals.decide`, the same door as the web page, and `settle` then rewrites the
message (and any other copy) to say who decided what.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.http import HttpResponse

from apps.agents import work
from apps.approvals.cards import outcome_line
from apps.approvals.decide import ApprovalError, decide
from apps.approvals.models import ApprovalDelivery

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


def blocks_for(card: dict, *, settled: str = "") -> tuple[str, list[dict]]:
    """(fallback text, Block Kit blocks). `settled` replaces the buttons with the outcome."""
    who = card["agent_name"] or card["agent_slug"] or card["filed_by"] or "An agent"
    out: list[dict] = [{"type": "header", "text": {"type": "plain_text",
                                                   "text": _clip(("Approve? " if not settled else "") + card["title"], 140)}}]
    ask = f"*{who}* is asking you to decide." + (f"\n{card['summary']}" if card["summary"] else "")
    out.append({"type": "section", "text": {"type": "mrkdwn", "text": _clip(ask)}})
    for e in card["evidence"][:2]:
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": _clip(
            f"> {e['quote']}\n_— {e['who']}_" + (f" (<{e['source_url']}|source>)" if e["source_url"] else ""))}})
    if card["rationale"]:
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": _clip(f"*Why:* {card['rationale']}")}})
    if card["plan"]:
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(card["plan"][:8], 1))
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": _clip(f"*What will be done*\n{steps}")}})
    if card["will_run"]:
        runs = "\n".join(f"• `{w['agent']}`: {w['prompt'][:300]}" for w in card["will_run"][:3])
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": _clip(f"*If you approve, this starts*\n{runs}")}})
    out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _clip(
        f"*If you decline:* {card['if_declined']}")}]})
    if settled:
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": f":white_check_mark: {settled}"}})
    else:
        out.append({"type": "actions", "block_id": "approval_decision", "elements": [
            {"type": "button", "action_id": APPROVE, "style": "primary", "value": card["key"],
             "text": {"type": "plain_text", "text": "Approve"}},
            {"type": "button", "action_id": DECLINE, "style": "danger", "value": card["key"],
             "text": {"type": "plain_text", "text": "Decline"}},
            {"type": "button", "action_id": "approval_open", "url": _abs(card["url"]),
             "text": {"type": "plain_text", "text": "Open the whole card"}},
        ]})
    fallback = f"Approve? {card['title']}" if not settled else f"{card['title']}: {settled}"
    return fallback, out


def _installation(task) -> SlackInstallation | None:
    return services.installation_for_workspace(task.workspace_id)


def send(task, user, card: dict) -> dict | None:
    """DM the card to `user`, if canopy's Slack knows them. None when it can't reach them."""
    inst = _installation(task)
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
    text, blocks = blocks_for(card, settled=outcome_line(card))
    client.update_message(inst.bot_token, channel=ref["channel"], ts=ref["ts"], text=text, blocks=blocks)


def handle_click(payload: dict, action: dict) -> HttpResponse:
    """Approve / Decline on a card DM. Answers 200 whatever happens (Slack retries otherwise);
    a refusal is said to the person, privately, in the DM."""
    installation = services.installation_for(str((payload.get("team") or {}).get("id") or ""))
    if installation is None:
        return HttpResponse(status=200)
    container = payload.get("container") or {}
    channel = str((payload.get("channel") or {}).get("id") or container.get("channel_id") or "")
    ts = str(container.get("message_ts") or (payload.get("message") or {}).get("ts") or "")
    slack_user = str((payload.get("user") or {}).get("id") or "")
    key = str(action.get("value") or "").strip().upper()

    def tell(text: str) -> None:
        try:
            client.post_ephemeral(installation.bot_token, channel=channel, user=slack_user, text=text)
        except Exception:  # noqa: BLE001 — the answer is a courtesy
            logger.exception("approvals: could not answer a card click")

    user, _assurance, _info = services.slack_user(installation, slack_user)
    task = work.task_by_key(key)
    if user is None or task is None:
        tell("canopy couldn't match you to a canopy account, or that card is gone. "
             "Open it from your canopy link instead.")
        return HttpResponse(status=200)
    # Only the DM canopy sent THIS person for THIS card may decide it.
    sent = ApprovalDelivery.objects.filter(task=task, user=user, channel=ApprovalDelivery.SLACK).first()
    if sent is None or str((sent.ref or {}).get("ts") or "") != ts:
        tell("That message isn't one canopy sent you for this card. Open the card in canopy to decide it.")
        return HttpResponse(status=200)
    try:
        decide(task, user, "approve" if action.get("action_id") == APPROVE else "decline", via="slack")
    except ApprovalError as exc:
        tell(str(exc))
    return HttpResponse(status=200)
