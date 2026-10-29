"""Site operators — the people with `User.is_superuser`.

A superuser holds the fleet-wide switches (the fleet hold, stop everything,
granting superuser) and nothing inside a workspace: workspace access is still
only a membership. That separation is the point — a person can hold the
switches without holding a workspace's keys, or hold both, and which alert mail
reaches them is a third, separate choice (`push.NotificationPreference.alert_mail`).

Granting and revoking happen here and nowhere else in the product; there must
always be at least one superuser left, or nobody could release a fleet hold.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model

from apps.push.models import NotificationPreference, set_notification_preferences


class OperatorError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _users():
    return get_user_model().objects


def operators(viewer) -> list[dict]:
    rows = _users().filter(is_superuser=True, is_active=True).order_by("email")
    muted = set(NotificationPreference.objects.filter(
        user__in=rows, alert_mail=NotificationPreference.ALERT_MAIL_ASSIGNED,
    ).values_list("user_id", flat=True))
    return [{
        "user_id": u.pk, "email": u.email, "name": (u.get_full_name() or "").strip(),
        "alert_mail": (NotificationPreference.ALERT_MAIL_ASSIGNED if u.pk in muted
                       else NotificationPreference.ALERT_MAIL_ALL),
        "is_you": u.pk == viewer.pk,
    } for u in rows]


def grant(email: str):
    """Make the account at `email` a superuser. It must exist (they signed in once):
    canopy never creates a person's account on someone else's say-so."""
    email = (email or "").strip()
    user = _users().filter(email__iexact=email, is_active=True).first() if email else None
    if user is None:
        raise OperatorError("not_found", f"no one has signed in to Canopy as {email or '(blank)'} yet — "
                                         "ask them to sign in once, then add them")
    if not user.is_superuser:
        user.is_superuser = True
        user.save(update_fields=["is_superuser"])
    return user


def revoke(user_id: int) -> None:
    user = _users().filter(pk=user_id, is_superuser=True).first()
    if user is None:
        raise OperatorError("not_found", "that person is not a superuser")
    if not _users().filter(is_superuser=True, is_active=True).exclude(pk=user.pk).exists():
        raise OperatorError("last", "Canopy needs at least one superuser — add another before removing this one")
    user.is_superuser = False
    user.save(update_fields=["is_superuser"])


def set_alert_mail(user_id: int, alert_mail: str):
    user = _users().filter(pk=user_id, is_superuser=True).first()
    if user is None:
        raise OperatorError("not_found", "that person is not a superuser")
    set_notification_preferences(user, alert_mail=alert_mail)
    return user
