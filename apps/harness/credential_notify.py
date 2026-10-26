"""Tell the people who run a box that its Claude credentials stopped working.

A cloud box posts four credential alerts to `POST /api/events/`: a credential
that needs signing in again (`claude_auth_required`), every credential at its
usage cap (`claude_usage_exhausted`), a credential whose account is out of
credit (`claude_credit_exhausted`), and the step onto the metered API key
(`claude_api_key_fallback`). Recording one tells nobody — only the admin-only
event log reads `runner.credential` rows — so a box could sit dead for a day
with its alert filed where no one looks.

So each, posted with the box's own runner token, also mails and Web-Pushes the
runner's approvers (`device_key_notify.approvers`: its owner and explicit
runner admins; `claude_auth_required` with the exception below), deep-linking
to that runner on /supervisor where its credentials are. Once per (runner, alert) per UTC day: the events API calls
this only for an item that is news today (`events.services.record`'s
`first_today`). BEST-EFFORT, like `device_key_notify`: a failed notice never
fails the recording.

`claude_auth_required` is mailed only where the `runner_credential` alert
(`alerts_runner_credential`) cannot cover it: a runner that source does not
reconcile (private — no workspace — or retired), or one whose health reports no
signed-out `claude.probe.*` check — a laptop, which posts no probe checks, or a
box whose probe was inconclusive. Where such a check exists the alert's own
blocking notice reaches the same people once, names the token by its label and
clears when the probe passes, so it is not also mailed.
"""
from __future__ import annotations

import logging
from html import escape

from django.conf import settings

from .alerts_runner_credential import out_of_credit_checks, runner_path, signed_out_checks, watches

logger = logging.getLogger(__name__)

#: The alerts a box raises about its own Claude credentials, and the subject each
#: mail carries.
CREDENTIAL_KINDS = {
    "claude_auth_required": "{name} needs a Claude sign-in",
    "claude_usage_exhausted": "{name} is out of Claude usage",
    "claude_credit_exhausted": "A Claude credential on {name} is out of credit",
    "claude_api_key_fallback": "{name} is on the metered API key",
}


def alert_covers(runner) -> bool:
    """Does the `runner_credential` alert already cover this box's sign-out —
    is it a runner that source reconciles (`watches`: in a workspace, not
    retired) AND does its health report a signed-out `claude.probe.*` check?
    A box the source skips keeps its mail."""
    return watches(runner) and bool(signed_out_checks(runner))


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
    if kind == "claude_auth_required" and alert_covers(runner):
        return result  # the runner_credential alert covers it
    if kind == "claude_credit_exhausted" and watches(runner) and out_of_credit_checks(runner):
        return result  # likewise, for an account out of credit
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
                    logger.exception("credential notice email failed: user=%s", person.pk)
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
