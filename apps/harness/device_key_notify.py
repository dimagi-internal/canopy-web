"""Tell the people who can approve a runner's device key that one is waiting
(Canopy Vault, canopy-web#1538 / #1550).

A box registering a key prints `approve_url` — to a terminal nobody may be
watching. A runner coming back from a pause, or a laptop re-enrolling, sat
with a pending key and every bound agent's turn failing until someone happened
to open Settings. So a NEW pending key, and an active key sent back to pending
by a new encryption key (`_set_ecdh`), mails and Web-Pushes everyone who can
approve it — the runner's owner and its explicit admins
(`can_administer_runner`) — deep-linking to Settings → Your machines with that
key highlighted, where approving (and giving it access) is one passkey touch.

Once per event: registration is idempotent (a re-POST of a known key returns
200 and sends nothing), so the two call sites fire only on a real transition.
BEST-EFFORT: a failed notice never fails the registration.
"""
from __future__ import annotations

import logging
from html import escape

logger = logging.getLogger(__name__)


def approvers(runner) -> list:
    """The owner and every explicit runner admin, active, de-duplicated."""
    from .models import RunnerAdmin

    people = []
    if runner.owner_id and runner.owner.is_active:
        people.append(runner.owner)
    for grant in RunnerAdmin.objects.filter(runner=runner).select_related("user"):
        if grant.user.is_active and all(p.pk != grant.user_id for p in people):
            people.append(grant.user)
    return people


def notify_pending(key, *, reapproval: bool = False) -> dict:
    """Email + Web Push each approver about `key`, now pending. Never raises."""
    from django.core.mail import EmailMultiAlternatives

    from .api_device_keys import approve_url

    result: dict = {"emailed": [], "failed": [], "not_configured": False, "pushed": 0}
    try:
        runner = key.runner
        link = approve_url(key)
        what = key.label or key.kind
        if reapproval:
            subject = f"Canopy · {runner.name} needs re-approving"
            detail = (f"{runner.name} ({what}) has a new key for reading agents' private secrets. It stays "
                      f"online and keeps running other agents, but it cannot read those secrets until you "
                      f"approve the new key — one passkey touch, which also gives it access.")
        else:
            subject = f"Canopy · approve {runner.name}"
            detail = (f"{runner.name} ({what}) is asking to be approved. Until you approve it, it cannot read the "
                      f"secrets of agents that need an approved machine, so their turns there will fail. "
                      f"Approving takes one passkey touch and also gives it access to your agents.")
        foot = "You are getting this because you administer this runner. If you did not expect it, revoke it there."
        text = f"{detail}\n\n{link}\n\n{foot}\n"
        html = (f"<p>{escape(detail)}</p><p><a href=\"{escape(link)}\">Review it under Your machines</a></p>"
                f"<p style=\"color:#78716c;font-size:13px\">{escape(foot)}</p>")
        rel = link.split("://", 1)[-1]
        rel = rel[rel.find("/"):] if "/" in rel else "/"
        for person in approvers(runner):
            if person.email:
                message = EmailMultiAlternatives(subject=subject, body=text, to=[person.email])
                message.attach_alternative(html, "text/html")
                try:
                    if message.send():
                        result["emailed"].append(person.email)
                    else:
                        result["not_configured"] = True
                except Exception:  # noqa: BLE001
                    logger.exception("device key notice email failed: to=%s", person.email)
                    result["failed"].append(person.email)
            try:
                from apps.push.services import send_to_user

                result["pushed"] += send_to_user(person, subject.removeprefix("Canopy · ").capitalize(),
                                                 detail[:180], rel)
            except Exception:  # noqa: BLE001
                logger.exception("device key notice push failed: user=%s", person.pk)
    except Exception:  # noqa: BLE001
        logger.exception("device key notice failed for %s", getattr(key, "pk", "?"))
        result["error"] = True
    return result


def notify_pending_on_commit(key, *, reapproval: bool = False) -> None:
    """Send after the row is committed, so the link opens a key that exists."""
    from django.db import transaction

    try:
        transaction.on_commit(lambda: notify_pending(key, reapproval=reapproval))
    except Exception:  # noqa: BLE001
        logger.exception("device key notice could not be scheduled")
