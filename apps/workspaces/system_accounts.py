"""System accounts — non-human workspace members that can never sign in.

A system account (`models.SystemAccount`) is an automated sender, such as AWS
CloudWatch, given the standing of a workspace member so its mail can make an
agent do work the way a person with that role would — routed, permissioned and
logged by the same machinery as everyone else (Jonathan, 2026-10-07: "it's
basically the same as a user with edit", canopy-web#1253).

Why a real `User` and not a new initiator kind: every gate in canopy already
answers "what may THIS user do here" — `agents.access.decide`, routing rules,
per-person routes, the turn-content ACL, the roster. A parallel kind would have
to be taught to each of them and would drift from them; a user with a
membership is already understood by all of them.

**What keeps it from being a person — the credential doors.** Each is refused
for a system user, and `tests/test_system_accounts.py` pins every one:

  * Google sign-in (allauth) — `CustomAccountAdapter.pre_login`; and its
    `User.email` is a synthetic `.invalid` address, which no identity provider
    can assert and `contacts.services.user_for_verified_email` never resolves.
  * Passwords — set unusable at creation.
  * Personal access tokens and OAuth/MCP access tokens — `PersonalToken.lookup`
    and `PersonalToken.create_for_user` (an OAuth access token IS a PAT).
  * Delegated (widget) tokens — `DelegatedToken.lookup`.

**How it arrives.** Only by a `SystemSender` binding in its own workspace, on a
message aligned on our own receiver's verdict — see `account_for_inbound` and
`harness.services._member_behind_email`.

**Its role is capped at editor.** A system administers nothing: it may not be
made an admin or an owner, here or through the generic member-role route
(`services.set_member_role` refuses).
"""
from __future__ import annotations

import logging
import re
import secrets

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from .models import SystemAccount, SystemSender, Workspace, WorkspaceMembership

logger = logging.getLogger(__name__)

#: The roles a system account may hold. Never admin or owner.
ROLES = (WorkspaceMembership.EDITOR, WorkspaceMembership.VIEWER)
DEFAULT_ROLE = WorkspaceMembership.EDITOR

#: `.invalid` is reserved (RFC 2606): no mailbox, no identity provider, no
#: verified-email record can ever hold an address under it.
SYNTHETIC_EMAIL_DOMAIN = "system.canopy.invalid"


class SystemAccountError(Exception):
    """`code` maps to an HTTP status at the API: invalid | conflict | not_found."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def is_system_user(user) -> bool:
    """Whether `user` is a system account. Cheap after the first call on an
    instance (the reverse one-to-one is cached)."""
    if user is None or getattr(user, "pk", None) is None:
        return False
    try:
        return user.system_account is not None
    except SystemAccount.DoesNotExist:
        return False


def account_of(user) -> SystemAccount | None:
    return user.system_account if is_system_user(user) else None


def describe(user) -> dict | None:
    """What a turn's envelope says about a system initiator, or None for a person."""
    account = account_of(user)
    if account is None:
        return None
    return {"id": account.pk, "name": account.name, "description": account.description,
            "workspace": account.workspace_id}


def _record(workspace: Workspace, kind: str, summary: str, by=None, **payload) -> None:
    """Best-effort line in the workspace event log (admins read it)."""
    try:
        from apps.events.services import record

        record([{
            "source": "workspaces.system_accounts", "kind": kind, "level": "info",
            "summary": f"{summary} by {getattr(by, 'email', None) or 'canopy'}"[:500],
            "payload": payload,
        }], workspace=workspace)
    except Exception:  # noqa: BLE001 — a log write must never fail the request
        logger.exception("could not record %s for workspace %s", kind, workspace.pk)


def _validate_role(role: str) -> str:
    if role not in ROLES:
        raise SystemAccountError(
            "invalid", f"a system account may be {' or '.join(ROLES)}, never {role!r}")
    return role


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise SystemAccountError("invalid", "a system account needs a name")
    if len(name) > 120:
        raise SystemAccountError("invalid", "name is longer than 120 characters")
    return name


def _validate_pattern(pattern: str) -> str:
    pattern = pattern or ""
    if len(pattern) > 300:
        raise SystemAccountError("invalid", "subject_pattern is longer than 300 characters")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise SystemAccountError("invalid", f"subject_pattern is not a valid regex: {exc}")
    return pattern


def _normalize_address(address: str) -> str:
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    address = (address or "").strip().lower()
    try:
        validate_email(address)
    except ValidationError:
        raise SystemAccountError("invalid", f"{address!r} is not an email address")
    return address


def membership_of(account: SystemAccount) -> WorkspaceMembership | None:
    # authz-exempt: reads the TARGET account's own row to report or change its
    # role; it decides nothing about whoever is asking.
    return WorkspaceMembership.objects.filter(
        workspace_id=account.workspace_id, user_id=account.user_id).first()


def role_of(account: SystemAccount) -> str | None:
    m = membership_of(account)
    return m.role if m is not None else None


@transaction.atomic
def create(workspace: Workspace, *, name: str, description: str = "", role: str = DEFAULT_ROLE,
           by=None) -> SystemAccount:
    """Create the account, its login-less user and its membership."""
    name = _validate_name(name)
    role = _validate_role(role)
    if SystemAccount.objects.filter(workspace=workspace, name=name).exists():
        raise SystemAccountError("conflict", f"a system account named {name!r} already exists here")
    handle = f"{slugify(name)[:40] or 'system'}-{secrets.token_hex(4)}"
    User = get_user_model()
    user = User(
        username=f"system:{workspace.slug}:{handle}"[:150],
        email=f"{workspace.slug}.{handle}@{SYNTHETIC_EMAIL_DOMAIN}",
        first_name=name[:150],
        is_active=True,     # an inactive user is refused everywhere — the turn must still run
        is_staff=False,
        is_superuser=False,
    )
    user.set_unusable_password()
    user.save()
    account = SystemAccount.objects.create(
        user=user, workspace=workspace, name=name, description=description or "", created_by=by)
    # Through the one membership door (`services._grant`), not a create here:
    # `test_only_invite_acceptance_approval_and_creation_create_memberships`.
    from .services import _grant

    _grant(workspace, user, role, invited_by=by)
    _record(workspace, "system_account.created", f"system account {name!r} created as {role}",
            by, account_id=account.pk, role=role)
    return account


@transaction.atomic
def update(account: SystemAccount, *, name: str | None = None, description: str | None = None,
           role: str | None = None, disabled: bool | None = None, by=None) -> SystemAccount:
    changes = {}
    if name is not None:
        name = _validate_name(name)
        if name != account.name:
            if SystemAccount.objects.filter(workspace_id=account.workspace_id, name=name).exclude(
                    pk=account.pk).exists():
                raise SystemAccountError(
                    "conflict", f"a system account named {name!r} already exists here")
            changes["name"] = name
            account.name = name
            account.user.first_name = name[:150]
            account.user.save(update_fields=["first_name"])
    if description is not None and description != account.description:
        account.description = description
        changes["description"] = True
    if disabled is not None and disabled != (account.disabled_at is not None):
        account.disabled_at = timezone.now() if disabled else None
        changes["disabled"] = disabled
    account.save()
    if role is not None:
        role = _validate_role(role)
        m = membership_of(account)
        if m is None:
            # Removed from the workspace earlier; re-adding it is how it comes back.
            from .services import _grant

            _grant(account.workspace, account.user, role, invited_by=by)
            changes["role"] = role
        elif m.role != role:
            m.role = role
            m.save(update_fields=["role"])
            changes["role"] = role
    if changes:
        _record(account.workspace, "system_account.updated",
                f"system account {account.name!r} changed ({', '.join(sorted(changes))})",
                by, account_id=account.pk, **{k: v for k, v in changes.items() if k != "description"})
    return account


@transaction.atomic
def delete(account: SystemAccount, *, by=None) -> None:
    """Delete the account, its user, membership and bindings. Its past turns
    keep their row (`initiator_user` is SET_NULL) — use `disabled` instead to
    keep the name on them."""
    workspace, name, pk = account.workspace, account.name, account.pk
    account.user.delete()   # cascades the account, its senders and its membership
    _record(workspace, "system_account.deleted", f"system account {name!r} deleted", by,
            account_id=pk)


def add_sender(account: SystemAccount, *, address: str, subject_pattern: str = "",
               by=None) -> SystemSender:
    address = _normalize_address(address)
    subject_pattern = _validate_pattern(subject_pattern)
    sender, created = SystemSender.objects.get_or_create(
        account=account, address=address, subject_pattern=subject_pattern)
    if created:
        _record(account.workspace, "system_account.sender_added",
                f"{address} bound to system account {account.name!r}"
                + (f" (subject ~ {subject_pattern!r})" if subject_pattern else ""),
                by, account_id=account.pk, sender_id=sender.pk)
    return sender


def remove_sender(sender: SystemSender, *, by=None) -> None:
    account = sender.account
    address = sender.address
    sender.delete()
    _record(account.workspace, "system_account.sender_removed",
            f"{address} unbound from system account {account.name!r}", by, account_id=account.pk)


def account_for_inbound(workspace_id, address: str, subject: str = "") -> SystemAccount | None:
    """The ONE enabled system account of this workspace bound to `address` whose
    subject pattern matches `subject`, or None.

    Two accounts matching the same message is ambiguous and resolves to None
    (the sender stays a contact) — a guess would hand one account's authority
    to the other's mail. The caller is responsible for the message being
    aligned; this only answers "whose binding is it".
    """
    address = (address or "").strip().lower()
    if not address or not workspace_id:
        return None
    matches = {}
    for sender in (SystemSender.objects.select_related("account", "account__user")
                   .filter(address__iexact=address, account__workspace_id=workspace_id,
                           account__disabled_at__isnull=True)):
        pattern = sender.subject_pattern
        try:
            if pattern and not re.search(pattern, subject or ""):
                continue
        except re.error:
            # Validated at write time; a bad pattern in the DB matches nothing.
            continue
        matches[sender.account_id] = sender.account
    if len(matches) > 1:
        logger.warning("mail from %s matches %d system accounts in workspace %s; treated as a contact",
                       address, len(matches), workspace_id)
        return None
    return next(iter(matches.values()), None)
