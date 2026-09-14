"""Recording who wrote in, and — separately, deliberately — letting them in.

Two operations that look adjacent and must not be one:

* `record_inbound_sender` runs automatically on every inbound message and
  GRANTS NOTHING.
* `promote_to_user` is an explicit act by a human, and is the only thing here
  that touches identity.

Keeping them apart is the point of the module.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import F

from .email_auth import grade_from_headers, grade_of
from .models import Contact

logger = logging.getLogger(__name__)


def _normalize(address: str) -> str:
    """A bare, lowercased address, or "" if it is not address-shaped.

    Mirrors `apps.harness.actors.normalize_actor` — same question, same answer,
    and "" is the safe result either way because it matches nothing.
    """
    from email.utils import parseaddr

    _, addr = parseaddr((address or "").strip())
    addr = addr.strip().lower()
    if "@" not in addr:
        return ""
    local, _, domain = addr.partition("@")
    return addr if local and domain else ""


@transaction.atomic
def record_inbound_sender(
    *,
    workspace,
    address: str,
    display_name: str = "",
    headers: list | None = None,
    authserv_id: str = "",
    auth_results: str = "",
) -> Contact | None:
    """Upsert the contact an inbound message came from. Returns None if unusable.

    **This grants nothing.** No membership, no role, no access to the workspace
    or to the agent. It records that a person exists and what their mail server
    could prove, so a later turn can remember them and a routing rule has
    something to read.

    Callers may pass either the full `headers` list plus our receiver's
    `authserv_id` (preferred — the trust check happens here), or a single
    pre-extracted `auth_results` string when the caller has already established
    provenance. Passing neither yields the `none` grade, which is the honest
    answer for a message we cannot vouch for, and is a perfectly normal state.

    The stored grade is a HIGH-WATER MARK: a forwarded message that breaks SPF
    must not downgrade a correspondent who has otherwise always passed DMARC.
    The latest grade is kept alongside precisely so a drop is still visible.
    """
    email = _normalize(address)
    if not email or workspace is None:
        # Unattributable mail is not an error — it is mail. Recording a contact
        # with no usable address would create a row nothing can ever match.
        return None

    if headers is not None:
        grade, raw = grade_from_headers(
            headers, authserv_id=authserv_id, from_address=email,
        )
    else:
        grade, raw = grade_of(auth_results, from_address=email), auth_results

    contact, created = Contact.objects.get_or_create(
        workspace=workspace,
        email=email,
        defaults={
            "display_name": (display_name or "").strip()[:200],
            "auth_result": grade,
            "last_auth_result": grade,
            "auth_detail": raw,
            "message_count": 1,
        },
    )
    if created:
        logger.info(
            "contact recorded: %s in %s (auth=%s) — no membership granted",
            email, workspace.pk, grade,
        )
        return contact

    updates = {"last_auth_result": grade, "message_count": F("message_count") + 1}
    if Contact.AUTH_RANK[grade] > Contact.AUTH_RANK.get(contact.auth_result, 0):
        updates["auth_result"] = grade
        updates["auth_detail"] = raw
    # Only fill a blank name. A display name is attacker-supplied on every
    # message, so letting each one overwrite would let a later sender relabel an
    # established contact.
    if not contact.display_name and (display_name or "").strip():
        updates["display_name"] = display_name.strip()[:200]

    Contact.objects.filter(pk=contact.pk).update(**updates)
    contact.refresh_from_db()
    return contact


def promote_to_user(contact: Contact, user) -> Contact:
    """Link a contact to a real canopy account, once they have authenticated.

    Separate from `record_inbound_sender` on purpose: that one runs on
    attacker-triggered input, this one is a deliberate act. Linking still does
    NOT create a membership — being known and being let in stay two decisions,
    and the second one goes through `workspaces.services` like every other
    grant.
    """
    contact.user = user
    contact.save(update_fields=["user", "last_seen_at"])
    logger.info(
        "contact %s linked to user %s (still no membership)", contact.email, user.pk,
    )
    return contact


def for_workspace(workspace_slug: str):
    """This tenant's contacts. Scoped by the caller, never global."""
    return Contact.objects.filter(workspace_id=workspace_slug).select_related("user")
