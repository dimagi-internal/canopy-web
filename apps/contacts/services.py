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
        # Keyed like `uniq_contact_per_workspace_email`, which only binds email
        # contacts: a Slack or widget contact may carry the same address, and
        # without this the lookup finds both and raises.
        source=Contact.SOURCE_EMAIL,
        email=email,
        defaults={
            # The same correspondent writing to agents in two tenants is two
            # contacts and ONE person. Recorded now, while it is knowable.
            "person": person_for(email=email),
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
def resolve_arrival(*, app, contact: Contact, claims: dict):
    """The existing canopy USER a widget visitor is, or None — never a new one.
    (Issued a DelegatedToken with assurance `host_signed`.)

    Who-is-asking §2 (D1: no dynamic user creation). In order:

      1. The contact is already linked to a user (`promote_to_user`): that user.
      2. The site signed `email_verified: true` for an address that exactly
         one active canopy user already holds as a VERIFIED allauth email:
         link the contact and return that user.
      3. Otherwise None — the visitor is a contact, as before.

    Either way the user must be a member of the site's workspace, and that is
    the gate: someone allowed in this tenant arrives as themselves, anyone else
    is a contact. There used to be a second, per-site opt-in on top of it — a
    list of email domains the site could resolve, empty by default — which
    meant every member arrived as a contact until an owner found the setting
    (2026-09-24, Jonathan: "they should always arrive as themselves if they are
    allowed to"). What it bounded was a stolen site key speaking for existing
    users; membership bounds that to this tenant's members, through this one
    site, as short-lived `host_signed` tokens the site's revocation ends.
    Linking grants nothing (see `promote_to_user`); arriving as a user means
    that user's OWN ACL applies.
    """
    from allauth.account.models import EmailAddress
    from django.contrib.auth import get_user_model

    from apps.workspaces import services as wsvc

    User = get_user_model()
    user = None
    if contact.user_id:
        user = User.objects.filter(pk=contact.user_id, is_active=True).first()
    else:
        email = _normalize(str(claims.get("email") or ""))
        if email and claims.get("email_verified") is True:
            ids = list(EmailAddress.objects.filter(email__iexact=email, verified=True)
                       .values_list("user_id", flat=True).distinct()[:2])
            if len(ids) == 1:
                user = User.objects.filter(pk=ids[0], is_active=True).first()
    # A question about the VISITOR's membership, asked through the one authorizer.
    if user is None or not wsvc.is_member(user, contact.workspace_id):
        return None
    if contact.user_id is None:
        promote_to_user(contact, user)
    return user


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
            # One site, several tenants: this visitor's record here and their
            # record in another tenant are the same human, and `(app,
            # external_id)` is the site's own name for them, so it is provable
            # rather than a guess.
            "person": person_for(app=app, external_id=external_id),
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


def record_slack_user(
    *,
    workspace,
    team_id: str,
    slack_user_id: str,
    email: str = "",
    display_name: str = "",
    grade: str = Contact.AUTH_SLACK,
) -> Contact | None:
    """Upsert the contact behind a Slack user who is not a workspace member.

    **This grants nothing**, like the other two recorders. Someone who can post
    in a channel an agent is invited to — a guest from a partner org, or a
    colleague with no canopy membership — reaches that agent the way an
    emailer reaches its inbox: as a person canopy knows, never as a member.

    Keyed on Slack's own ids (`<team>:<user>`), which Slack signs on every
    event. The profile email is recorded as a DESCRIPTION, not matched on:
    joining this row to an email contact because the addresses agree would be
    believing an assertion the grade does not cover.
    """
    team_id, slack_user_id = (team_id or "").strip(), (slack_user_id or "").strip()
    if workspace is None or not team_id or not slack_user_id:
        return None
    if grade not in (Contact.AUTH_SLACK, Contact.AUTH_SLACK_MEMBER):
        grade = Contact.AUTH_SLACK
    contact, created = Contact.objects.get_or_create(
        workspace=workspace,
        source=Contact.SOURCE_SLACK,
        external_id=f"{team_id}:{slack_user_id}"[:200],
        defaults={
            # Deliberately no `person`: a Slack id is scoped to its workspace's
            # Slack, not to the human, and matching on the address Slack
            # reports would be believing an assertion rather than keying on an
            # identity. A null here reads as "cannot tell", which is true.
            "email": _normalize(email),
            "display_name": (display_name or "").strip()[:200],
            "auth_result": grade,
            "last_auth_result": grade,
            "message_count": 1,
        },
    )
    if created:
        logger.info("contact %s recorded from Slack in %s — no membership granted",
                    contact.pk, workspace.pk)
        return contact
    updates = {"last_auth_result": grade, "message_count": F("message_count") + 1}
    if Contact.AUTH_RANK[grade] > Contact.AUTH_RANK.get(contact.auth_result, 0):
        updates["auth_result"] = grade
    # Fill blanks only, for the same reason as the widget: a later message must
    # not be able to relabel an established contact.
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


def person_for(*, app=None, external_id: str = "", email: str = ""):
    """The `Person` behind this contact, creating it if canopy has not met them
    before. `None` when there is nothing to key on.

    The ONE place "are these the same person" is answered, so two callers
    cannot answer it differently. Keyed on what the world already uses to name
    them — a site's own id, or an address that IS the identity — never on an
    address a site merely asserted.
    """
    from .models import Person

    external_id = (external_id or "").strip()[:200]
    if app is not None and external_id:
        # The signer, not the row: every tenant registers a system itself, so
        # one system is many rows (see `Person.signer`).
        signer = app.signer()
        if not signer:
            return None
        row, _ = Person.objects.get_or_create(issuer=app.name, signer=signer,
                                              external_id=external_id)
        return row
    address = _normalize(email)
    if address:
        row, _ = Person.objects.get_or_create(issuer="", signer="", external_id="",
                                              email=address)
        return row
    return None
