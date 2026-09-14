"""Recording who turned up, and — separately, deliberately — letting them in.

Two kinds of operation that look adjacent and must not be one:

* `record_inbound_sender` / `record_embed_visitor` run automatically on
  attacker-triggered input and GRANT NOTHING.
* `promote_to_user` is an explicit act by a human, and is the only thing here
  that touches identity.

Keeping them apart is the point of the module.

The two recorders are deliberately separate functions rather than one with a
`source=` argument. They take different identity keys, they grade different
kinds of assertion, and the only thing they truly share is the upsert-with-a-
high-water-mark shape, which is small. One function taking either would have a
body that is mostly branching on which half of its arguments are set — and the
branch that matters, "which column is this person's real key", is exactly the
one you do not want decided at runtime.
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
        # Same reasoning as the embed path below, applied to the one identifier
        # canopy CAN be sure about: an email address is personal data, and it
        # does not need to be in an application log to be correlatable.
        logger.info(
            "contact %s recorded in %s (auth=%s) — no membership granted",
            contact.pk, workspace.pk, grade,
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
        "contact %s linked to user %s (still no membership)", contact.pk, user.pk,
    )
    return contact


def for_workspace(workspace_slug: str):
    """This tenant's contacts. Scoped by the caller, never global."""
    return Contact.objects.filter(workspace_id=workspace_slug).select_related("user", "app")


@transaction.atomic
def record_embed_visitor(
    *,
    workspace,
    app,
    external_id: str,
    email: str = "",
    display_name: str = "",
    grade: str = Contact.AUTH_APP_SECRET,
    detail: str = "",
) -> Contact | None:
    """Upsert the contact behind a widget visitor. Returns None if unusable.

    **This grants nothing**, exactly as `record_inbound_sender` grants nothing.
    A host telling canopy who is looking at its page is the same kind of
    statement as a mail server telling canopy who sent a message: an assertion,
    recorded with a grade, never a key to anything.

    The identity is `(app, external_id)` — the host's own id, opaque to canopy.
    That namespace is why this is a strictly smaller grant than asserting an
    email address: an id cannot collide with a canopy user or with another
    host's people, so the worst a compromised app can do is invent its own
    contacts, which it could already do by having its own users.

    `email` is optional and is NOT an identity here. A host may know an address
    and it is worth recording, but matching an existing email-keyed contact on
    it would be believing the assertion — the very thing the grade exists to
    avoid. Linking the two is a deliberate act, like `promote_to_user`.
    """
    external_id = (external_id or "").strip()[:200]
    if not external_id or workspace is None or app is None:
        # Nothing to key on. Recording a row here would create a contact
        # nothing can ever match again, which is worse than not recording.
        return None
    if grade not in Contact.AUTH_RANK:
        grade = Contact.AUTH_NONE

    contact, created = Contact.objects.get_or_create(
        workspace=workspace,
        app=app,
        external_id=external_id,
        defaults={
            "source": Contact.SOURCE_EMBED,
            "email": _normalize(email),
            "display_name": (display_name or "").strip()[:200],
            "auth_result": grade,
            "last_auth_result": grade,
            "auth_detail": detail[:2000],
            "message_count": 1,
        },
    )
    if created:
        # The contact's PK, never the host's id for them. CodeQL flagged this
        # as clear-text logging of sensitive data and it is right for a reason
        # worth keeping: `external_id` is chosen by the host and canopy cannot
        # know what it is — an opaque uuid for one site, an email or a phone
        # number for the next. The database row holds it under an ACL; an
        # application log is read by more people, kept by different rules, and
        # shipped somewhere else entirely. A pk correlates just as well.
        logger.info(
            "contact %s recorded from %s in %s (auth=%s) — no membership granted",
            contact.pk, app.name, workspace.pk, grade,
        )
        return contact

    updates = {"last_auth_result": grade, "message_count": F("message_count") + 1}
    if Contact.AUTH_RANK[grade] > Contact.AUTH_RANK.get(contact.auth_result, 0):
        updates["auth_result"] = grade
        updates["auth_detail"] = detail[:2000]
    # Only ever fill blanks. Both of these are host-supplied on every visit, so
    # letting a later one overwrite would let a host silently relabel — or
    # re-address — an established contact.
    if not contact.display_name and (display_name or "").strip():
        updates["display_name"] = display_name.strip()[:200]
    if not contact.email and _normalize(email):
        updates["email"] = _normalize(email)

    Contact.objects.filter(pk=contact.pk).update(**updates)
    contact.refresh_from_db()
    return contact


def block(contact: Contact, *, reason: str = "") -> Contact:
    """Stop this person reaching an agent, without disconnecting the site.

    A contact is the one principal an outsider can cause canopy to create, so
    there has to be a way to refuse one person. Blocking is not a lesser form
    of deleting: the row stays, because what canopy knows about someone it has
    decided to refuse is exactly what it should not forget.
    """
    from django.utils import timezone

    contact.blocked_at = timezone.now()
    contact.blocked_reason = (reason or "").strip()[:200]
    contact.save(update_fields=["blocked_at", "blocked_reason", "last_seen_at"])
    logger.info("contact %s blocked (%s)", contact.pk, contact.blocked_reason)
    return contact


def unblock(contact: Contact) -> Contact:
    contact.blocked_at = None
    contact.blocked_reason = ""
    contact.save(update_fields=["blocked_at", "blocked_reason", "last_seen_at"])
    return contact
