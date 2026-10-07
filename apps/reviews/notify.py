"""Tell a review's owner that suggestions arrived (canopy-web#1269).

Before this, POST /suggest/ only appended to ``suggestions_json``: the guest saw
"✓ Thanks — your suggestions were sent" and nobody was told. A suggestion
surfaced only if someone happened to reopen that exact review URL.

The owner is whoever posted the review — usually the agent running the DDD
loop (ace@, hal@…), so the email lands in the inbox of the session that is
waiting on this gate. Same delivery as workspace access requests: email + Web
Push + a workspace event-log line. BEST-EFFORT: never raises, so a mail outage
can't lose a reviewer's suggestion.
"""
from __future__ import annotations

import logging
from html import escape

from apps.reviews.models import ReviewRequest
from apps.reviews.titles import narrative_title

log = logging.getLogger(__name__)


def review_path(review: ReviewRequest) -> str:
    return f"/review/{review.pk}/"


def notify_suggestion(review: ReviewRequest, name: str | None, count: int) -> dict:
    result: dict = {"emailed": None, "pushed": 0}
    try:
        from apps.workspaces.services import _base_url, _button, _send

        who = (name or "").strip() or "A guest reviewer"
        what = narrative_title(review.request_json, review.narrative_slug) or review.run_id
        link = f"{_base_url()}{review_path(review)}"
        subject = f"{who} suggested edits to “{what}”"
        tail = f"{count} suggestion{'s' if count != 1 else ''} on this review so far."
        text = (f"{who} sent suggested wording for “{what}”. It does not resolve the review.\n\n"
                f"{tail}\n\nOpen the review to read and apply them:\n{link}\n")
        html = (f"<p>{escape(who)} sent suggested wording for <strong>{escape(what)}</strong>. "
                f"It does not resolve the review.</p><p>{escape(tail)}</p>"
                + _button(link, "Read the suggestions")
                + f'<p style="color:#78716c;font-size:13px">{escape(link)}</p>')
        owner = review.owner
        if owner is not None and owner.email:
            result["emailed"] = _send(subject, text, html, owner.email)
            try:
                from apps.push.services import send_to_user

                result["pushed"] = send_to_user(owner, "Suggested edits", subject, review_path(review))
            except Exception:  # noqa: BLE001
                log.exception("suggestion push failed: review=%s", review.pk)
        _record(review, subject, result)
    except Exception:  # noqa: BLE001 — a lost notification must never lose the suggestion
        log.exception("notifying owner of suggestion on review %s failed", review.pk)
        result["error"] = True
    return result


def _record(review: ReviewRequest, summary: str, result: dict) -> None:
    if review.workspace_id is None:
        return
    try:
        from apps.events.services import record

        record([{
            "source": "reviews.suggest", "kind": "review.suggestion",
            "level": "info" if result.get("emailed") == "sent" else "warn",
            "summary": summary[:500],
            "payload": {"review_id": str(review.pk), **result},
        }], workspace=review.workspace)
    except Exception:  # noqa: BLE001
        log.exception("could not record suggestion event for review %s", review.pk)
