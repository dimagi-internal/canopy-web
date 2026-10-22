"""The unified alert core: the types sources speak, the source registry, and the
framework's half of an alert's lifecycle (raise / reconcile / dismiss / act).

A SOURCE is a module in the app that owns the subject (`apps/<app>/alerts.py`),
registered in `SOURCES` by dotted path — like `sources.REGISTRY` — so this
framework app never imports a product app. A source module exposes:

- `KIND: str` — the `Alert.kind` it owns.
- `SEVERITY: str` — one of `Alert.BLOCKING | ACTION | SETUP`.
- `CONFIRM_WITHIN: timedelta | None` — how long an open alert may go
  unconfirmed before the framework resolves it as stale (Task 3's tick).
- `CLOSING_ACTION_IDS: tuple[str, ...]` — non-empty: every kind offers someone
  a way to close it (a registry test holds this).
- `reconcile(now) -> list[Raise]` — the complete set of currently-true conditions.
- `actions(alert, user) -> list[Action]` — what THIS viewer may do. A viewer
  with no actions does not see the alert. A source opts into the shared dismiss
  by including `DISMISS`.
- `perform(alert, user, action_id) -> None` — runs a handler action.

The framework owns the rest: the row's status, the `AlertEvent` audit trail
(one row per change, written in the same transaction as the change, and
mirrored into `apps/events` as a coalescing `source="alert"` row), dismiss,
notification fan-out, and checking that an action is still offered before it
runs.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from importlib import import_module

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.html import escape

from apps.events import services as events
from apps.events.models import Event
from apps.push.services import send_to_user
from apps.workspaces import services as wsvc
from apps.workspaces.links import base_url
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.notifications import _button, _send, _who

from .models import Alert, AlertEvent

logger = logging.getLogger(__name__)

#: Dotted paths of source modules.
SOURCES: list[str] = [
    "apps.harness.alerts_runner_credential",
]


@dataclass(frozen=True)
class Raise:
    """One currently-true condition, as a source reports it.

    `workspace` is the subject's workspace slug (`Workspace`'s primary key).
    An empty `fingerprint` means "a digest of title + body": a change in what
    the alert says is a change in the facts.
    """

    subject_key: str
    workspace: str
    title: str
    url: str
    scopes: tuple[str, ...]
    body: str = ""
    fingerprint: str = ""

    def digest(self) -> str:
        if self.fingerprint:
            return self.fingerprint
        return hashlib.sha256(f"{self.title}\n{self.body}".encode()).hexdigest()


@dataclass(frozen=True)
class Action:
    """One thing a viewer may do about an alert.

    Exactly one shape: `href` set → a link (the fix is done on that page; never
    acted on here); `notify` set → email + push those user ids, alert stays
    open; neither → a server handler, the source's `perform`.
    """

    id: str
    label: str
    closes: bool
    href: str = ""
    notify: tuple[int, ...] = ()

    def __post_init__(self):
        # A notify never closes (the alert stays open and shows who asked whom),
        # and an action is one shape: a link is not also a notify.
        if self.notify and self.closes:
            raise ValueError(f"action {self.id!r}: a notify action cannot close the alert")
        if self.notify and self.href:
            raise ValueError(f"action {self.id!r}: an action is a link or a notify, not both")


#: The shared dismiss. A source offers it by including it in `actions()`; the
#: framework handles it (it is never passed to `source.perform`).
DISMISS = Action("dismiss", "Dismiss", closes=True)


class ActionNotOffered(Exception):  # noqa: N818 — the name the alert API and sources share
    """The action is not (or no longer) offered to this user on this alert, or
    it is a link action, which is followed in the browser, not run here."""


def source_for(kind: str):
    """The registered source module whose `KIND` is `kind`."""
    for path in SOURCES:
        try:
            mod = import_module(path)
        except ImportError:  # one unimportable source must not hide the others
            logger.exception("alert source %s cannot be imported", path)
            continue
        if mod.KIND == kind:
            return mod
    raise LookupError(f"no alert source registered for kind {kind!r}")


# --- the audit trail --------------------------------------------------------


def _log(alert: Alert, kind: str, *, at=None, actor=None, action_id: str = "", payload: dict | None = None):
    """Write one `AlertEvent` and its `apps/events` mirror. Callers hold the
    transaction that also carries the change being logged."""
    AlertEvent.objects.create(alert=alert, at=at or timezone.now(), kind=kind, actor=actor,
                              action_id=action_id, payload=payload or {})
    events.record([{
        "source": "alert",
        "key": f"{alert.pk}",
        "kind": f"alert.{kind}",
        "summary": alert.title,
        "level": Event.WARN if alert.severity == Alert.BLOCKING else Event.INFO,
    }], workspace=alert.workspace)


def _locked(alert: Alert) -> Alert:
    """Re-read `alert` under a row lock (inside the caller's transaction)."""
    return Alert.objects.select_for_update().get(pk=alert.pk)


def _sync(target: Alert, source: Alert) -> None:
    """Copy `source`'s fields onto the caller's instance, so it sees the change."""
    for f in Alert._meta.concrete_fields:
        setattr(target, f.attname, getattr(source, f.attname))


# --- reconcile --------------------------------------------------------------


def apply_reconcile(kind: str, raises: list[Raise], now) -> None:
    """Make the open alerts of `kind` match `raises`, the source's complete set.

    Per raise: new → `raised`; open with a changed fingerprint → updated +
    `changed`; dismissed with a changed fingerprint → `reopened` (and eligible
    to notify again); resolved → `reopened` (the condition came back);
    unchanged → `last_seen_at` only, no event. Then every open OR dismissed
    alert of `kind` the set no longer names resolves (`reason=source`): the
    condition cleared, so a later recurrence — even with the same fingerprint —
    reopens it (resolved → reopened) instead of staying hidden. A dismissed
    alert whose condition is still true stays dismissed.

    `now` is when the source computed its snapshot. A snapshot older than a
    person's close (`closed_at >= now`) never reopens that alert, and one older
    than a newer confirmation (`last_seen_at >= now`) never resolves it — so a
    slow tick cannot undo an action taken while it ran.
    """
    severity = source_for(kind).SEVERITY
    seen: set[str] = set()
    for r in raises:
        seen.add(r.subject_key)
        _upsert(kind, severity, r, now)
    missing = (Alert.objects.filter(kind=kind, status__in=[Alert.OPEN, Alert.DISMISSED], last_seen_at__lt=now)
               .exclude(subject_key__in=seen).values_list("pk", flat=True))
    for pk in list(missing):
        _resolve_missing(pk, now)


def _resolve_missing(pk: int, now) -> None:
    """Resolve one alert its source no longer reports — re-checked under the row
    lock, since it may have been dismissed, resolved or re-confirmed since the
    query that found it."""
    with transaction.atomic():
        row = Alert.objects.select_for_update().filter(pk=pk).first()
        if row and row.status in (Alert.OPEN, Alert.DISMISSED) and row.last_seen_at < now:
            resolve(row, reason=Alert.REASON_SOURCE)


def _find_locked(kind: str, subject_key: str) -> Alert | None:
    return Alert.objects.select_for_update().filter(kind=kind, subject_key=subject_key).first()


def _upsert(kind: str, severity: str, r: Raise, now) -> None:
    fingerprint = r.digest()
    fields = dict(workspace_id=r.workspace, scopes=list(r.scopes), severity=severity, title=r.title,
                  body=r.body, url=r.url, fingerprint=fingerprint)
    with transaction.atomic():
        alert = _find_locked(kind, r.subject_key)
        if alert is None:
            try:
                # A savepoint: a concurrent reconcile may create the same
                # subject first, and its unique-constraint error must not abort
                # the rest of this reconcile.
                with transaction.atomic():
                    alert = Alert.objects.create(kind=kind, subject_key=r.subject_key, first_seen_at=now,
                                                 last_seen_at=now, **fields)
                    _log(alert, AlertEvent.RAISED, at=now)
                return
            except IntegrityError:
                alert = Alert.objects.select_for_update().get(kind=kind, subject_key=r.subject_key)

        if alert.status != Alert.OPEN and alert.closed_at is not None and alert.closed_at >= now:
            return  # closed after this snapshot was taken: the snapshot is stale

        changed = alert.fingerprint != fingerprint
        event = None
        if alert.status == Alert.RESOLVED or (alert.status == Alert.DISMISSED and changed):
            event = AlertEvent.REOPENED
            alert.status = Alert.OPEN
            alert.resolved_reason = ""
            alert.closed_by = None
            alert.closed_at = None
            alert.notified_at = None
        elif alert.status == Alert.OPEN and changed:
            event = AlertEvent.CHANGED

        for name, value in fields.items():
            setattr(alert, name, value)
        alert.last_seen_at = now
        if event:
            alert.occurrences += 1
        alert.save()
        if event:
            _log(alert, event, at=now, payload={"fingerprint": fingerprint})


# --- state changes ----------------------------------------------------------


def resolve(alert: Alert, *, reason: str, by=None) -> None:
    """Close `alert` as fixed. `reason`: `Alert.REASON_SOURCE | ACTION | STALE`.
    A no-op on an alert that is already resolved."""
    with transaction.atomic():
        row = _locked(alert)
        if row.status != Alert.RESOLVED:
            row.status = Alert.RESOLVED
            row.resolved_reason = reason
            row.closed_by = by
            row.closed_at = timezone.now()
            row.save(update_fields=["status", "resolved_reason", "closed_by", "closed_at"])
            _log(row, AlertEvent.RESOLVED, at=row.closed_at, actor=by, payload={"reason": reason})
    _sync(alert, row)


def dismiss(alert: Alert, user) -> None:
    """The shared dismiss: off everyone's list until its fingerprint changes.
    A no-op on an alert that is not open."""
    with transaction.atomic():
        row = _locked(alert)
        if row.status == Alert.OPEN:
            row.status = Alert.DISMISSED
            row.closed_by = user
            row.closed_at = timezone.now()
            row.save(update_fields=["status", "closed_by", "closed_at"])
            _log(row, AlertEvent.DISMISSED, at=row.closed_at, actor=user)
    _sync(alert, row)


def undismiss(alert: Alert, user) -> None:
    """Put a dismissed alert back to open. A no-op on any other status. Who may
    do it is the caller's check (the API: anyone currently offered dismiss)."""
    with transaction.atomic():
        row = _locked(alert)
        if row.status == Alert.DISMISSED:
            row.status = Alert.OPEN
            row.closed_by = None
            row.closed_at = None
            row.save(update_fields=["status", "closed_by", "closed_at"])
            _log(row, AlertEvent.UNDISMISSED, actor=user)
    _sync(alert, row)


# --- actions ----------------------------------------------------------------


def visible_actions(alert: Alert, user) -> list[Action]:
    """What `user` may do on `alert` — the source's per-viewer list. A closed
    (resolved or dismissed) alert offers nothing here; who may UNDISMISS a
    dismissed alert is asked separately, with `offers_dismiss`."""
    if alert.status != Alert.OPEN:
        return []
    return list(source_for(alert.kind).actions(alert, user))


def offers_dismiss(alert: Alert, user) -> bool:
    """Does the source offer `user` the shared dismiss on `alert`, whatever the
    alert's status? The undismiss path authorizes with this, since
    `visible_actions` is empty for a dismissed alert."""
    return any(a.id == DISMISS.id for a in source_for(alert.kind).actions(alert, user))


def act(alert: Alert, user, action_id: str) -> Alert:
    """Run `action_id` for `user`, if it is still offered to them. Returns the
    alert as it now stands.

    Logs `acted`, then: `dismiss` → the shared dismiss; a notify action →
    email + push its people (the alert stays open); a handler → the source's
    `perform`, then, if the action closes, resolve (`reason=action`). A handler
    that raises leaves nothing behind — perform and its log share one
    transaction. Link actions are never run here (`ActionNotOffered`).
    """
    with transaction.atomic():
        row = _locked(alert)
        action = next((a for a in visible_actions(row, user) if a.id == action_id), None)
        if action is None:
            raise ActionNotOffered(f"{action_id!r} is not offered on alert {row.pk}")
        if action.href:
            raise ActionNotOffered(f"{action_id!r} is a link; follow {action.href}")
        _log(row, AlertEvent.ACTED, actor=user, action_id=action_id)
        if action.id == DISMISS.id:
            dismiss(row, user)
        elif not action.notify:
            source_for(row.kind).perform(row, user, action_id)
            if action.closes:
                resolve(row, reason=Alert.REASON_ACTION, by=user)
    if action.notify:
        # Outside the transaction: mail and push are slow and must not hold the
        # row lock. `notify_users` logs its own `notified` row.
        notify_users(row, list(get_user_model().objects.filter(pk__in=action.notify)), by=user)
    row.refresh_from_db()
    _sync(alert, row)
    return alert


# --- notification -----------------------------------------------------------


def _absolute(url: str) -> str:
    return url if url.startswith(("http://", "https://")) else f"{base_url()}{url}"


def notify_users(alert: Alert, users, *, by=None) -> int:
    """Email + Web Push `alert` to each of `users`; log ONE `notified` row naming
    the people it reached. Returns how many it reached (an email handed to the
    mail backend, or at least one push accepted). Never raises on a send."""
    link = _absolute(alert.url) if alert.url else base_url()
    subject = f"Canopy: {alert.title}"
    paras = [f"{_who(by)} asked you to look at this."] if by is not None else []
    paras += [alert.title] + ([alert.body] if alert.body else []) + [f"Open it: {link}"]
    text = "\n\n".join(paras) + "\n"
    html = ((f"<p>{escape(_who(by))} asked you to look at this.</p>" if by is not None else "")
            + f"<p><strong>{escape(alert.title)}</strong></p>"
            + (f"<p>{escape(alert.body)}</p>" if alert.body else "")
            + _button(link, "Open in Canopy"))

    reached: list[int] = []
    seen: set[int] = set()
    for user in users:
        if user.pk in seen:
            continue
        seen.add(user.pk)
        emailed = bool(user.email) and _send(subject, text, html, user.email) == "sent"
        pushed = send_to_user(user, alert.title, alert.body or "Needs you in Canopy", alert.url or "/")
        if emailed or pushed:
            reached.append(user.pk)

    if reached:
        with transaction.atomic():
            _log(alert, AlertEvent.NOTIFIED, actor=by,
                 payload={"to": reached, "by": by.pk if by is not None else None})
    return len(reached)


# --- the tick ---------------------------------------------------------------


def tick(now=None) -> None:
    """One pass of the alert clock: reconcile every source, expire stale
    alerts, notify new blocking ones. Registered with `apps.common.ticks`.

    A source whose `reconcile` raises is skipped ENTIRELY — a source we could
    not read says nothing about whether its conditions still hold, so nothing
    it owns resolves as "gone". Staleness is separate: a source with
    `CONFIRM_WITHIN` that has not confirmed an open alert in that long loses it
    even while its reconcile is failing (an alert nobody can re-confirm must
    not stay on screen forever).
    """
    now = now or timezone.now()
    for path in list(SOURCES):
        try:
            mod = import_module(path)
            apply_reconcile(mod.KIND, mod.reconcile(now), now)
        except Exception:  # noqa: BLE001 — one broken source must not stop the others
            logger.exception("alerts tick: source %s failed; its alerts are left as they are", path)
    for path in list(SOURCES):
        try:
            _expire_stale(import_module(path), now)
        except Exception:  # noqa: BLE001 — expiry of one source must not stop notifications
            logger.exception("alerts tick: stale expiry for source %s failed", path)
    notify_new_blocking(now)


def _expire_stale(mod, now) -> None:
    within = getattr(mod, "CONFIRM_WITHIN", None)
    if within is None:
        return
    for alert in Alert.objects.filter(kind=mod.KIND, status=Alert.OPEN, last_seen_at__lt=now - within):
        try:
            resolve(alert, reason=Alert.REASON_STALE)
        except Exception:  # noqa: BLE001 — one alert must not block the rest
            logger.exception("alerts tick: could not expire stale alert %s", alert.pk)


def notify_new_blocking(now) -> None:
    """Email + push each open `blocking` alert not yet notified, to the people
    in its workspace's organisation tree who hold a CLOSING action on it, then
    stamp `notified_at` (a raise or a reopen clears it, so each gets one).

    `notified_at` is set even when nobody was reached, so an alert with no
    eligible closer is not retried every minute (it is logged).
    """
    pending = list(Alert.objects.filter(severity=Alert.BLOCKING, status=Alert.OPEN, notified_at__isnull=True))
    if not pending:
        return
    roots = wsvc.tree_roots({a.workspace_id for a in pending})
    # Everyone holding a membership anywhere in a tree is a candidate (roles flow
    # DOWN, so an ancestor's owner has a row at the top of the tree): one query
    # for all the trees, not one per alert.
    tree_slugs: dict[str, set[str]] = {}
    for slug, root in roots.items():
        tree_slugs.setdefault(root, {root})
    below = wsvc.descendant_map(set(tree_slugs))
    for root, slugs in tree_slugs.items():
        slugs |= below.get(root, set())
    members: dict[str, set[int]] = {root: set() for root in tree_slugs}
    owner_of = {slug: root for root, slugs in tree_slugs.items() for slug in slugs}
    for slug, uid in (WorkspaceMembership.objects.filter(workspace_id__in=list(owner_of))
                      .values_list("workspace_id", "user_id")):
        members[owner_of[slug]].add(uid)
    users = {u.pk: u for u in get_user_model().objects.filter(pk__in=set().union(*members.values()), is_active=True)}

    for alert in pending:
        try:
            candidates = [users[uid] for uid in sorted(members[roots[alert.workspace_id]]) if uid in users]
            closers = [u for u in candidates if any(a.closes for a in visible_actions(alert, u))]
            if not closers:
                logger.warning("alerts tick: no one holds a closing action on blocking alert %s (%s); not notifying",
                               alert.pk, alert.title)
            elif notify_users(alert, closers) == 0:
                # Closers exist but every delivery failed (sends return, not raise):
                # leave notified_at null so the next tick retries.
                logger.warning("alerts tick: every delivery for blocking alert %s failed; will retry", alert.pk)
                continue
        except Exception:  # noqa: BLE001 — leave notified_at null: retried next tick
            logger.exception("alerts tick: notifying for alert %s failed", alert.pk)
            continue
        Alert.objects.filter(pk=alert.pk, notified_at__isnull=True).update(notified_at=now)
