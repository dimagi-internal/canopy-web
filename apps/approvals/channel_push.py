"""A request as a Web Push: the title says what is asked, the body says who asks, the tap opens the
request with everything on it."""
from __future__ import annotations

from apps.push.services import send_to_user

BODY_MAX = 160


def body(card: dict) -> str:
    what = card["summary"] or (card["plan"][0] if card["plan"] else "")
    who = card["requested_by"]
    text = f"{who} asks: {what}".strip() if who else what
    return text if len(text) <= BODY_MAX else text[: BODY_MAX - 1].rstrip() + "…"


def send(req, user, card: dict) -> dict | None:
    sent = send_to_user(user, title=f"Approve? {card['title']}"[:100], body=body(card), url=card["url"],
                        urgency="high")
    return {} if sent else None
