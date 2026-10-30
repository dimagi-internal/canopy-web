"""A card as an email: the same facts, and Approve / Decline buttons that open the card with the
choice made for you to confirm (an email link must never decide by itself — mail scanners open
every link)."""
from __future__ import annotations

from html import escape

from django.conf import settings

from apps.workspaces.notifications import _button, _send

from . import cards


def _abs(path: str) -> str:
    return f"{settings.CANOPY_PUBLIC_BASE_URL.rstrip('/')}{path}"


def render(card: dict) -> tuple[str, str, str]:
    who = card["agent_name"] or card["agent_slug"] or card["filed_by"] or "An agent"
    subject = f"Approve? {card['title']}"
    link = _abs(card["url"])
    lines = [f"{who} is asking you to decide: {card['title']}", ""]
    html = [f"<p><strong>{escape(who)}</strong> is asking you to decide:</p><h3>{escape(card['title'])}</h3>"]
    if card["summary"]:
        lines += [card["summary"], ""]
        html.append(f"<p>{escape(card['summary'])}</p>")
    for e in card["evidence"][:3]:
        lines += [f"“{e['quote']}” — {e['who']}", ""]
        html.append(f"<blockquote>{escape(e['quote'])}<br><em>{escape(e['who'])}</em></blockquote>")
    if card["plan"]:
        lines += ["What will be done:"] + [f"  {i}. {s}" for i, s in enumerate(card["plan"], 1)] + [""]
        html.append("<p><strong>What will be done</strong></p><ol>"
                    + "".join(f"<li>{escape(s)}</li>" for s in card["plan"]) + "</ol>")
    if card["will_run"]:
        lines += ["If you approve:"] + [f"  {w['agent']}: {w['prompt']}" for w in card["will_run"]] + [""]
        html.append("<p><strong>If you approve</strong></p><ul>"
                    + "".join(f"<li>{escape(w['agent'])}: {escape(w['prompt'])}</li>" for w in card["will_run"])
                    + "</ul>")
    lines += [f"If you decline: {card['if_declined']}", "", f"Approve: {link}?do=approve",
              f"Decline: {link}?do=decline", f"Everything: {link}"]
    html.append(f"<p>If you decline: {escape(card['if_declined'])}</p>"
                + _button(f"{link}?do=approve", "Approve") + _button(f"{link}?do=decline", "Decline")
                + f'<p><a href="{escape(link)}">Open the whole card</a></p>')
    return subject, "\n".join(lines) + "\n", "".join(html)


def send(task, user, card: dict) -> dict | None:
    if not user.email:
        return None
    subject, text, html = render(card)
    return {} if _send(subject, text, html, user.email) == "sent" else None
