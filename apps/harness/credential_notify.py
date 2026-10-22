"""Tell the people who run a box that its Claude credentials stopped working.

A cloud box posts three credential alerts to `POST /api/events/`: a credential
that needs signing in again (`claude_auth_required`), every credential at its
usage cap (`claude_usage_exhausted`), and the step onto the metered API key
(`claude_api_key_fallback`). Recording one tells nobody — only the admin-only
event log reads `runner.credential` rows — so a box could sit dead for a day
with its alert filed where no one looks.

So the last two, posted with the box's own runner token, also mail and
Web-Push the runner's approvers (`device_key_notify.approvers`: its owner and
explicit runner admins), deep-linking to that runner on /supervisor where its
credentials are. Once per (runner, alert) per UTC day: the events API calls
this only for an item that is news today (`events.services.record`'s
`first_today`). BEST-EFFORT, like `device_key_notify`: a failed notice never
fails the recording.

`claude_auth_required` is no longer mailed here: a signed-out credential is the
`runner_credential` alert (`alerts_runner_credential`), whose own blocking
notice reaches the same people once, names the token by its label, and clears
itself when the box's probe passes again.
"""
from __future__ import annotations

import logging
from html import escape

from django.conf import settings

logger = logging.getLogger(__name__)

#: The credential alerts a box raises that are mailed, and the subject each mail
#: carries. (`claude_auth_required` is the `runner_credential` alert instead.)
CREDENTIAL_KINDS = {
    "claude_usage_exhausted": "{name} is out of Claude usage",
    "claude_api_key_fallback": "{name} is on the metered API key",
}


def runner_path(runner) -> str:
    """The runner's detail on /supervisor, app-relative (/supervisor is not a
    tenant page, so it has no `/w/<ws>/` prefix)."""
    return f"/supervisor?tab=runners&runner={runner.pk}"


def runner_url(runner) -> str:
    """`runner_path` on this deployment's public base URL."""
    base = (getattr(settings, "CANOPY_PUBLIC_BASE_URL", "") or "").rstrip("/")
    return f"{base}{runner_path(runner)}"


def notify(runner, item: dict) -> dict:
    """Mail + push each approver about one credential alert. Never raises."""
    from django.core.mail import EmailMultiAlternatives

    from .device_key_notify import approvers

    result: dict = {"emailed": [], "failed": [], "pushed": 0}
    kind = str((item or {}).get("kind") or "")
    if kind not in CREDENTIAL_KINDS:
        return result
    try:
        link = runner_url(runner)
        subject = "Canopy · " + CREDENTIAL_KINDS[kind].format(name=runner.name)
        detail = str(item.get("summary") or "").strip() or subject
        foot = "You are getting this because you administer this runner."
        text = f"{detail}\n\nOpen {runner.name}'s credentials: {link}\n\n{foot}\n"
        html = (f"<p>{escape(detail)}</p>"
                f"<p><a href=\"{escape(link)}\">Open {escape(runner.name)}'s credentials</a></p>"
                f"<p style=\"color:#78716c;font-size:13px\">{escape(foot)}</p>")
        rel = runner_path(runner)
        for person in approvers(runner):
            if person.email:
                message = EmailMultiAlternatives(subject=subject, body=text, to=[person.email])
                message.attach_alternative(html, "text/html")
                try:
                    if message.send():
                        result["emailed"].append(person.email)
                except Exception:  # noqa: BLE001
                    logger.exception("credential notice email failed: to=%s", person.email)
                    result["failed"].append(person.email)
            try:
                from apps.push.services import send_to_user

                result["pushed"] += send_to_user(person, subject.removeprefix("Canopy · "),
                                                 detail[:180], rel)
            except Exception:  # noqa: BLE001
                logger.exception("credential notice push failed: user=%s", person.pk)
    except Exception:  # noqa: BLE001
        logger.exception("credential notice failed for runner %s", getattr(runner, "pk", "?"))
        result["error"] = True
    return result
