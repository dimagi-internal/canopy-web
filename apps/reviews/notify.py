"""Email when someone sends suggested edits on a review (canopy-web#1269).

Before this, POST /suggest/ only appended to ``suggestions_json``: the guest saw
"✓ Thanks — your suggestions were sent" and nobody was told.

Who hears about it: ``settings.REVIEW_SUGGESTION_NOTIFY_TO`` — ACE, for now,
because ACE runs the DDD loops that post these reviews — with the person who
sent the suggestions cc'd, so they have a record and ACE's reply reaches them
(they are also the Reply-To). A workspace event-log line records the outcome.

BEST-EFFORT: never raises, so a mail outage can't lose a reviewer's suggestion.
"""
from __future__ import annotations

import logging
from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from apps.reviews.models import ReviewRequest
from apps.reviews.titles import narrative_title

log = logging.getLogger(__name__)


def review_path(review: ReviewRequest) -> str:
    return f"/review/{review.pk}/"


def notify_suggestion(
    review: ReviewRequest, name: str | None, count: int, *, submitter_email: str | None = None
) -> dict:
    to = [a for a in (getattr(settings, "REVIEW_SUGGESTION_NOTIFY_TO", None) or []) if a]
    cc = [submitter_email] if submitter_email and submitter_email.lower() not in {a.lower() for a in to} else []
    result: dict = {"to": to, "cc": cc, "emailed": None}
    try:
        if not to:
            result["emailed"] = "no_recipient"
        else:
            from apps.workspaces.services import _base_url, _button

            who = (name or "").strip() or submitter_email or "A guest reviewer"
            what = narrative_title(review.request_json, review.narrative_slug) or review.run_id
            link = f"{_base_url()}{review_path(review)}"
            subject = f"{who} suggested edits to “{what}”"
            tail = f"{count} suggestion{'s' if count != 1 else ''} on this review so far."
            cc_note = (f"{submitter_email} is copied on this email." if cc else
                       "They left no email address, so they are not copied.")
            text = (f"{who} sent suggested wording for “{what}”. It does not resolve the review.\n\n"
                    f"{tail} {cc_note}\n\nOpen the review to read and apply them:\n{link}\n")
            html = (f"<p>{escape(who)} sent suggested wording for <strong>{escape(what)}</strong>. "
                    f"It does not resolve the review.</p><p>{escape(tail)} {escape(cc_note)}</p>"
                    + _button(link, "Read the suggestions")
                    + f'<p style="color:#78716c;font-size:13px">{escape(link)}</p>')
            message = EmailMultiAlternatives(
                subject=subject, body=text, to=to, cc=cc,
                reply_to=[submitter_email] if submitter_email else None,
            )
            message.attach_alternative(html, "text/html")
            result["emailed"] = "sent" if message.send() else "not_configured"
    except Exception:  # noqa: BLE001 — a lost notification must never lose the suggestion
        log.exception("emailing suggestion on review %s failed", review.pk)
        result["emailed"] = "failed"
    _record(review, result)
    return result


def _record(review: ReviewRequest, result: dict) -> None:
    if review.workspace_id is None:
        return
    try:
        from apps.events.services import record

        sent = result.get("emailed") == "sent"
        record([{
            "source": "reviews.suggest", "kind": "review.suggestion",
            "level": "info" if sent else "warn",
            "summary": (f"suggested edits on review {review.pk}: emailed "
                        f"{', '.join(result['to']) or 'nobody'}"
                        + (f" (cc {', '.join(result['cc'])})" if result["cc"] else "")
                        + ("" if sent else f" — {result.get('emailed')}"))[:500],
            "payload": {"review_id": str(review.pk), **result},
        }], workspace=review.workspace)
    except Exception:  # noqa: BLE001
        log.exception("could not record suggestion event for review %s", review.pk)
