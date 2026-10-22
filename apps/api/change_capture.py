"""The rows a request writes, as before/after — what the settings audit records
for a route that reports nothing of its own (`apps/events/audit.py::audited`).

Inside `capturing(capture)`, every row saved or deleted is noted on `capture` as
`{"scope": "<app_label>.<model>:<pk>", "before", "after"}` with only the fields
that changed: a created row has `before=None`, a deleted one `after=None` plus
`also_deleted` (`{"app.Model": count}`) for what cascaded with it. A save that
changed nothing is not noted. Outside `capturing` nothing happens.

How each write is seen:

* `save()` — `pre_save` reads the stored row, `post_save` reads it back, so both
  sides are what the database holds, not what was assigned.
* `Model.delete()` and `QuerySet.delete()` are wrapped, NOT taken from
  `pre_delete`/`post_delete`: any listener there turns off Django's fast delete for
  its model, everywhere and always, and deleting a workspace or an agent cascades
  through high-volume tables (messages, turn events, transcripts). The rows named
  are snapshotted; what cascaded is counted from `delete()`'s own return value.
* `QuerySet.update()` and `bulk_create()` send no signal, so they are wrapped too.

Not seen: `bulk_update()`, raw SQL, m2m `add`/`remove`. A route that writes only
that way reports with `audit.note_change`.

This lives outside `apps/events`, whose log stays inert (`tests/test_events_emits_nothing.py`).
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import threading
from collections.abc import Callable

from django.db import models
from django.db.models.signals import post_save, pre_save

_current: contextvars.ContextVar[Capture | None] = contextvars.ContextVar(
    "canopy_change_capture", default=None)


class Capture:
    """What one request wrote. `plain(field, value)` turns a stored value into
    what is recorded (secrets withheld, JSON-safe); rows of a model whose
    `label_lower` is in `skip` are not noted; past `limit` rows are only counted."""

    def __init__(self, *, plain: Callable, skip: frozenset[str] = frozenset(), limit: int = 100):
        self.plain, self.skip, self.limit = plain, skip, limit
        self.notes: list[dict] = []
        self.pending: dict[int, dict | None] = {}
        self.busy = False
        self.overflow = 0

    def watches(self, model) -> bool:
        return model._meta.label_lower not in self.skip

    def add(self, scope: str, before, after, **extra) -> None:
        if len(self.notes) >= self.limit:
            self.overflow += 1
            return
        self.notes.append({"scope": scope, "before": before, "after": after, **extra})

    def result(self) -> list[dict]:
        if not self.overflow:
            return list(self.notes)
        return [*self.notes, {"scope": "more", "before": None,
                              "after": f"{self.overflow} more rows"}]

    def snapshot(self, fields, row: dict) -> dict:
        return {f.attname: self.plain(f, row.get(f.attname)) for f in fields}


@contextlib.contextmanager
def capturing(capture: Capture):
    install()
    token = _current.set(capture)
    try:
        yield capture
    finally:
        _current.reset(token)


def _fields(model, update_fields=None) -> list:
    """The concrete fields a before/after compares: not the key (it is in the
    scope) and not a timestamp the database keeps (`auto_now`)."""
    out = [f for f in model._meta.concrete_fields
           if not f.primary_key and not getattr(f, "auto_now", False)
           and not getattr(f, "auto_now_add", False)]
    if update_fields:
        names = set(update_fields)
        out = [f for f in out if f.name in names or f.attname in names]
    return out


def _stored(model, pk, fields, using) -> dict | None:
    return (model._base_manager.using(using).filter(pk=pk)
            .values(*(f.attname for f in fields)).first())


def _scope(model, pk) -> str:
    return f"{model._meta.label_lower}:{pk}"


def _active(model) -> Capture | None:
    cap = _current.get()
    return cap if cap is not None and not cap.busy and cap.watches(model) else None


def _pre_save(sender, instance, raw=False, using=None, update_fields=None, **kwargs):
    cap = _active(sender)
    if cap is None or raw:
        return
    before = None
    if not instance._state.adding and instance.pk is not None:
        before = _stored(sender, instance.pk, _fields(sender, update_fields), using)
    cap.pending[id(instance)] = before


def _post_save(sender, instance, created=False, raw=False, using=None, update_fields=None,
               **kwargs):
    cap = _current.get()
    if cap is None or raw or id(instance) not in cap.pending:
        return
    before = cap.pending.pop(id(instance))
    fields = _fields(sender, update_fields)
    after = cap.snapshot(fields, _stored(sender, instance.pk, fields, using) or {})
    if before is None:
        cap.add(_scope(sender, instance.pk), None, after)
        return
    before = cap.snapshot(fields, before)
    changed = [k for k in after if before.get(k) != after[k]]
    if changed:
        cap.add(_scope(sender, instance.pk), {k: before.get(k) for k in changed},
                {k: after[k] for k in changed})


def _cascaded(result, model, own: int) -> dict:
    """What `delete()` reports it removed besides the rows named."""
    out = dict(result[1] or {}) if isinstance(result, tuple) and len(result) > 1 else {}
    label = model._meta.label
    if label in out:
        out[label] -= own
        if out[label] <= 0:
            out.pop(label)
    return out


@contextlib.contextmanager
def _busy(cap: Capture):
    """Writes Django makes on our behalf (a cascade, a read-back) are not noted twice."""
    cap.busy = True
    try:
        yield
    finally:
        cap.busy = False


def _model_delete(original):
    @functools.wraps(original)
    def delete(self, *args, **kwargs):
        model = type(self)
        cap = _active(model)
        if cap is None or self.pk is None:
            return original(self, *args, **kwargs)
        fields, pk = _fields(model), self.pk
        row = _stored(model, pk, fields, kwargs.get("using") or self._state.db)
        with _busy(cap):
            result = original(self, *args, **kwargs)
        if row is not None:
            also = _cascaded(result, model, 1)
            cap.add(_scope(model, pk), cap.snapshot(fields, row), None,
                    **({"also_deleted": also} if also else {}))
        return result

    return delete


def _queryset_delete(original):
    @functools.wraps(original)
    def delete(self):
        cap = _active(self.model)
        if cap is None:
            return original(self)
        model, fields = self.model, _fields(self.model)
        rows = list(self.values("pk", *(f.attname for f in fields))[:cap.limit + 1])
        with _busy(cap):
            result = original(self)
        named = rows[:cap.limit]
        for row in named:
            cap.add(_scope(model, row["pk"]), cap.snapshot(fields, row), None)
        own = len(named)
        if isinstance(result, tuple) and len(result) > 1:
            own = max(own, (result[1] or {}).get(model._meta.label, 0))
        cap.overflow += own - len(named)
        also = _cascaded(result, model, own)
        if also and named:
            cap.notes[-1].setdefault("also_deleted", also)
        return result

    return delete


def _queryset_update(original):
    @functools.wraps(original)
    def update(self, **kwargs):
        cap = _active(self.model)
        if cap is None:
            return original(self, **kwargs)
        model, fields = self.model, _fields(self.model, kwargs)
        names = [f.attname for f in fields]
        before = {r["pk"]: r for r in self.values("pk", *names)[:cap.limit + 1]}
        with _busy(cap):
            count = original(self, **kwargs)
        after = {r["pk"]: r for r in model._base_manager.using(self.db)
                 .filter(pk__in=list(before)).values("pk", *names)}
        for pk, row in list(before.items())[:cap.limit]:
            b, a = cap.snapshot(fields, row), cap.snapshot(fields, after.get(pk) or {})
            changed = [k for k in a if b.get(k) != a[k]]
            if changed:
                cap.add(_scope(model, pk), {k: b[k] for k in changed}, {k: a[k] for k in changed})
        if len(before) > cap.limit:
            cap.overflow += max(count - cap.limit, 0)
        return count

    return update


def _queryset_bulk_create(original):
    @functools.wraps(original)
    def bulk_create(self, objs, *args, **kwargs):
        cap = _active(self.model)
        if cap is None:
            return original(self, objs, *args, **kwargs)
        with _busy(cap):
            result = original(self, objs, *args, **kwargs)
        fields = _fields(self.model)
        for obj in result:
            cap.add(_scope(self.model, obj.pk), None,
                    {f.attname: cap.plain(f, getattr(obj, f.attname, None)) for f in fields})
        return result

    return bulk_create


_installed = False
_lock = threading.Lock()


def install() -> None:
    """Hook the writes `capturing` sees. Idempotent; done on first use."""
    global _installed
    if _installed:
        return
    with _lock:
        if _installed:
            return
        pre_save.connect(_pre_save, dispatch_uid="canopy_change_capture_pre_save", weak=False)
        post_save.connect(_post_save, dispatch_uid="canopy_change_capture_post_save", weak=False)
        models.Model.delete = _model_delete(models.Model.delete)
        models.QuerySet.delete = _queryset_delete(models.QuerySet.delete)
        models.QuerySet.update = _queryset_update(models.QuerySet.update)
        models.QuerySet.bulk_create = _queryset_bulk_create(models.QuerySet.bulk_create)
        _installed = True
