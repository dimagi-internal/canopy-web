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
import re
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


#: `platform.system()` names, as the canopy CLI's default key label carries them
#: ("DIM-SA-A126 (Darwin)"). "Darwin" read to a Mac owner as a machine they had
#: never heard of (2026-10-21), so a notice names the kind of computer instead.
_SYSTEMS = {"Darwin": "Mac", "macOS": "Mac", "Windows": "Windows PC", "Linux": "Linux machine"}


def machine(key) -> str:
    """The computer a key belongs to, in words its owner recognises:
    "the Mac named DIM-SA-A126" for a default `host (System)` label, else the
    label as given, else the key's kind."""
    label = (key.label or "").strip()
    found = re.fullmatch(r"(.+?) \((\w+)\)", label)
    if found and found.group(2) in _SYSTEMS:
        return f"the {_SYSTEMS[found.group(2)]} named {found.group(1)}"
    return label or f"a {key.kind} key"


def notify_pending(key, *, reapproval: bool = False) -> dict:
    """Email + Web Push each approver about `key`, now pending. Never raises."""
    from .api_device_keys import approve_url

    result: dict = {"emailed": [], "failed": [], "not_configured": False, "pushed": 0}
    try:
        runner = key.runner
        link = approve_url(key)
        where = f"{runner.name}, the canopy runner on {machine(key)},"
        if reapproval:
            subject = f"Canopy · {runner.name} needs re-approving"
            detail = (f"{where} has a new key for reading agents' private secrets. It stays "
                      f"online and keeps running other agents, but it cannot read those secrets until you "
                      f"approve the new key — one passkey touch, which also gives it access.")
        else:
            subject = f"Canopy · approve {runner.name}"
            detail = (f"{where} is asking to be approved. The runner asked by itself when it started: it "
                      f"registers its machine's key so it can read agents' secrets. Until you approve it, it "
                      f"cannot read the secrets of agents that need an approved machine, so their turns there "
                      f"will fail. "
                      f"Approving takes one passkey touch and also gives it access to your agents.")
        foot = "You are getting this because you administer this runner. If you did not expect it, revoke it there."
        _send(approvers(runner), subject=subject, detail=detail, link=link, foot=foot,
              link_text="Review it under Your machines", result=result)
    except Exception:  # noqa: BLE001
        logger.exception("device key notice failed for %s", getattr(key, "pk", "?"))
        result["error"] = True
    return result


def _send(people, *, subject: str, detail: str, link: str, foot: str, link_text: str, result: dict) -> None:
    """Mail and Web-Push `people` one notice deep-linking to `link`. Each failure
    is logged and counted in `result`; none raises."""
    from django.core.mail import EmailMultiAlternatives

    text = f"{detail}\n\n{link}\n\n{foot}\n"
    html = (f"<p>{escape(detail)}</p><p><a href=\"{escape(link)}\">{escape(link_text)}</a></p>"
            f"<p style=\"color:#78716c;font-size:13px\">{escape(foot)}</p>")
    rel = link.split("://", 1)[-1]
    rel = rel[rel.find("/"):] if "/" in rel else "/"
    for person in people:
        if person.email:
            message = EmailMultiAlternatives(subject=subject, body=text, to=[person.email])
            message.attach_alternative(html, "text/html")
            try:
                if message.send():
                    result["emailed"].append(person.email)
                else:
                    result["not_configured"] = True
            except Exception:  # noqa: BLE001
                logger.exception("runner notice email failed: to=%s", person.email)
                result["failed"].append(person.email)
        try:
            from apps.push.services import send_to_user

            result["pushed"] += send_to_user(person, subject.removeprefix("Canopy · ").capitalize(),
                                             detail[:180], rel)
        except Exception:  # noqa: BLE001
            logger.exception("runner notice push failed: user=%s", person.pk)


def notify_invitation(invitation, link: str) -> dict:
    """Mail + push the person who invited a new box its approve link (spec
    2026-10-19 #9). Only they can approve it, so only they are told. Never raises."""
    result: dict = {"emailed": [], "failed": [], "not_configured": False, "pushed": 0}
    try:
        person = invitation.created_by
        if not person.is_active:
            return result
        subject = f"Canopy · approve the new runner {invitation.name}"
        detail = (f"You invited a new cloud runner, {invitation.name}, into {invitation.workspace_id} "
                  f"(instance role {invitation.role_arn}). It cannot pair until you approve it — one "
                  f"passkey touch. The invitation runs out in an hour.")
        foot = "You are getting this because you created this invitation. If you did not, ignore it: it expires unused."
        _send([person], subject=subject, detail=detail, link=link, foot=foot,
              link_text="Review and approve it", result=result)
    except Exception:  # noqa: BLE001
        logger.exception("runner invitation notice failed for %s", getattr(invitation, "pk", "?"))
        result["error"] = True
    return result


def notify_invitation_on_commit(invitation, link: str) -> None:
    from django.db import transaction

    try:
        transaction.on_commit(lambda: notify_invitation(invitation, link))
    except Exception:  # noqa: BLE001
        logger.exception("runner invitation notice could not be scheduled")


def notify_pending_on_commit(key, *, reapproval: bool = False) -> None:
    """Send after the row is committed, so the link opens a key that exists."""
    from django.db import transaction

    try:
        transaction.on_commit(lambda: notify_pending(key, reapproval=reapproval))
    except Exception:  # noqa: BLE001
        logger.exception("device key notice could not be scheduled")


def notify_rekeyed(key, *, adopted: bool = False) -> dict:
    """Tell the runner's approvers its key was just replaced by its box re-pairing.
    Approval-free for a unit re-attaching on its own machine, so it must never be
    silent: if nobody expected it, revoking the key is one click. Never raises."""
    from .api_device_keys import approve_url

    result: dict = {"emailed": [], "failed": [], "not_configured": False, "pushed": 0}
    try:
        runner = key.runner
        subject = f"Canopy · {runner.name} has a new key"
        how = ("moved into an isolation unit" if adopted
               else "re-paired its isolation unit (a replaced or rebuilt instance)")
        detail = (f"{runner.name} {how}: its sign-in key was replaced. It keeps its runner, its routing "
                  f"and its access. If you did not replace or rebuild this box, revoke the key now.")
        foot = "You are getting this because you administer this runner."
        _send(approvers(runner), subject=subject, detail=detail, link=approve_url(key), foot=foot,
              link_text="Review it under Your machines", result=result)
    except Exception:  # noqa: BLE001
        logger.exception("re-key notice failed for %s", getattr(key, "pk", "?"))
        result["error"] = True
    return result


def notify_rekeyed_on_commit(key, *, adopted: bool = False) -> None:
    from django.db import transaction

    transaction.on_commit(lambda: notify_rekeyed(key, adopted=adopted))
