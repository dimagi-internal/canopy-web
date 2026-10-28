"""THE record of a session's lifecycle: one `session.*` event per transition.

"What happened to session X?" must be answerable by a workspace admin from metadata
alone: when it started, when it stopped, and every change to who can see it or where
it shows up. Each such transition writes ONE row to the workspace's event log
(`apps.events`, source `canopy_sessions.lifecycle`, `GET /api/events?kind=session.`)
with ids only — never a title, prompt, message or page.

Two writers, so no path can forget:

* **Field transitions are observed on the row** (`observe`, wired to `post_init` /
  `post_save` / `post_delete` in `signals.py`): `status` (closed / archived /
  unarchived), `workspace` (moved), the Slack thread in `metadata` (bound /
  detached), the sensitivity requirement (sensitivity changed), creation and
  deletion, and every `SessionParticipant` row added, re-roled or removed. Any
  `save()` anywhere — a route, Slack's front door, the runner report, a contact's
  archive — is covered without a call at its site.
* **Everything else is called where it happens** (`record`): queryset `update()`s
  the row never sees (the runner report's closes, staleness, an agent's re-home),
  and the transitions that are not a field (reset, fork, transfer, a close asked
  of a box, a project's sharing switch).

A call site that knows more than the row — who asked, under which turn, and that an
archive is a CLOSE — says so with `acting(...)` around the write; the observer
reads it. Without it the actor is the request in flight (`request_context`: the
person or agent login, a runner's own key, a contact), else `system`.

A sensitive conversation's event carries its id, its workspace, who did it and the
transition, nothing more (`_redact`). Recording must never undo the action: each
write is its own savepoint and a failure is logged and swallowed.
"""
from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist

from apps.agents.models import Agent
from apps.common import request_context
from apps.events import services as events
from apps.harness import sensitivity
from apps.tokens import kinds
from apps.workspaces.models import Workspace

from .models import Session

logger = logging.getLogger(__name__)

SOURCE = "canopy_sessions.lifecycle"

CREATED = "session.created"
#: Archived because it ENDED: closed from canopy, archived in emdash, or gone from its
#: runner's complete reports.
CLOSED = "session.closed"
#: A close relayed to the box holding it; `session.closed` follows when its report retires it.
CLOSE_REQUESTED = "session.close_requested"
#: Archived by hand, by a contact, as stale, or because a successor took its binding over.
ARCHIVED = "session.archived"
UNARCHIVED = "session.unarchived"
DELETED = "session.deleted"
MOVED = "session.moved"
SLACK_BOUND = "session.slack_bound"
SLACK_DETACHED = "session.slack_detached"
#: Its Slack thread replaced by another (a new thread, or a re-bind elsewhere).
SLACK_REBOUND = "session.slack_rebound"
PARTICIPANT_ADDED = "session.participant_added"
PARTICIPANT_REMOVED = "session.participant_removed"
PARTICIPANT_ROLE_CHANGED = "session.participant_role_changed"
#: A person's project-level sharing switch (`ProjectSessionShare`) or a coach's review
#: grant (`SessionReviewGrant`) — a change to who can see many sessions at once.
SHARING_CHANGED = "session.sharing_changed"
SENSITIVITY_CHANGED = "session.sensitivity_changed"
RESET = "session.reset"
FORKED = "session.forked"
TRANSFERRED = "session.transferred"

KINDS = (CREATED, CLOSED, CLOSE_REQUESTED, ARCHIVED, UNARCHIVED, DELETED, MOVED, SLACK_BOUND,
         SLACK_DETACHED, SLACK_REBOUND, PARTICIPANT_ADDED, PARTICIPANT_REMOVED,
         PARTICIPANT_ROLE_CHANGED, SHARING_CHANGED, SENSITIVITY_CHANGED, RESET, FORKED, TRANSFERRED)

#: Where a session keeps its Slack thread (`apps.slack.services.SLACK_THREAD_KEY`;
#: this app knows no Slack, so the key is repeated, as `audience.py` does).
SLACK_THREAD_KEY = "slack_thread"

#: Which change values a sensitive conversation's event may still carry: the
#: transition itself, never a detail of the conversation.
_SENSITIVE_SAFE_FIELDS = frozenset({"status", "workspace", "sensitive", "participant", "role", "runner"})

_acting: contextvars.ContextVar[dict | None] = contextvars.ContextVar("session_lifecycle", default=None)


@contextmanager
def acting(*, by=None, turn=None, reason: str = "", closing: bool = False) -> Iterator[None]:
    """Say who is changing sessions in this block, under which turn and why.
    `closing=True` makes an archive a `session.closed` rather than a `session.archived`."""
    outer = _acting.get() or {}
    token = _acting.set({**outer, **{k: v for k, v in
                                     {"by": by, "turn": turn, "reason": reason, "closing": closing}.items()
                                     if v not in (None, "", False)}})
    try:
        yield
    finally:
        _acting.reset(token)


def _ctx() -> dict:
    return _acting.get() or {}


# -- who ------------------------------------------------------------------------

def _user_actor(user) -> dict:
    try:
        agent = user.agent_identity
    except ObjectDoesNotExist:
        agent = None
    except AttributeError:
        agent = None
    if agent is not None:
        return {"kind": "agent", "agent": agent.slug, "user_id": user.pk}
    return {"kind": "user", "user_id": user.pk, "email": getattr(user, "email", "") or ""}


def _request_actor() -> dict:
    """The caller in flight, from `request_context`, or `system` outside a request."""
    ctx = request_context.current()
    if not ctx:
        return {"kind": "system"}
    cred = ctx.get("credential") or {}
    if cred.get("type") == kinds.RUNNER_KEY:
        # A runner's key authenticates as its owner; the box, not the owner, acted.
        return {"kind": "runner", "runner_name": str(cred.get("label") or "")}
    if cred.get("type") == kinds.CONTACT:
        return {"kind": "contact", "credential_id": cred.get("id")}
    user_id = ctx.get("user_id")
    if user_id:
        user = get_user_model().objects.filter(pk=user_id).first()
        if user is not None:
            return _user_actor(user)
    return {"kind": "system"}


def actor_of(by=None) -> dict:
    """`by` as an actor dict: a User (a person, or an agent's own login), an Agent, a
    Runner, a Contact, a dict already shaped, or a word (`system`). None: the
    `acting` block's, else the request in flight's."""
    if by is None:
        by = _ctx().get("by")
    if by is None:
        return _request_actor()
    if isinstance(by, dict):
        return by
    if isinstance(by, str):
        return {"kind": by}
    meta = getattr(by, "_meta", None)
    label = getattr(meta, "label", "")
    if label == "agents.Agent":
        return {"kind": "agent", "agent": by.slug}
    if label == "harness.Runner":
        return {"kind": "runner", "runner": str(by.pk), "runner_name": by.name}
    if label == "contacts.Contact":
        return {"kind": "contact", "contact_id": by.pk}
    if getattr(by, "is_authenticated", False):
        return _user_actor(by)
    return {"kind": "system"}


def _turn_id(turn=None) -> str:
    """The turn behind this change: given, the `acting` block's, else the turn the
    calling request runs in (its caller token's, or the parent headers')."""
    turn = turn if turn is not None else _ctx().get("turn")
    if turn is not None:
        return str(getattr(turn, "pk", turn) or "")
    ctx = request_context.current()
    cred = ctx.get("credential") or {}
    return str(cred.get("turn_id") or (ctx.get("parent") or {}).get("turn") or "")


def _via() -> dict:
    ctx = request_context.current()
    out = {}
    if ctx.get("via_mcp"):
        out["via"] = "mcp"
        if ctx.get("mcp_tool"):
            out["mcp_tool"] = ctx["mcp_tool"]
    elif (ctx.get("credential") or {}).get("type"):
        out["via"] = str(ctx["credential"]["type"])
    if ctx.get("request_id"):
        out["request_id"] = ctx["request_id"]
    return out


def _label(actor: dict) -> str:
    return (actor.get("email") or actor.get("agent") or actor.get("runner_name")
            or (f"contact {actor['contact_id']}" if actor.get("contact_id") else "") or actor.get("kind")
            or "system")


# -- what -----------------------------------------------------------------------

def is_sensitive_meta(metadata) -> bool:
    return sensitivity.is_sensitive(SimpleNamespace(metadata=metadata or {}))


def _channel(session) -> str:
    """Where the conversation lives, in one word — never more than the channel."""
    meta = session.__dict__.get("metadata") or {}
    if meta.get(SLACK_THREAD_KEY):
        return "slack"
    if session.__dict__.get("contact_id"):
        return "panel"
    if session.__dict__.get("origin") == "runner":
        return "runner"
    return "web"


def _agent_slug(session) -> str:
    agent_id = session.__dict__.get("agent_id")
    if not agent_id:
        return ""
    cached = session._state.fields_cache.get("agent")
    if cached is not None:
        return cached.slug
    return Agent.objects.filter(pk=agent_id).values_list("slug", flat=True).first() or ""


_VERBS = {
    CREATED: "started", CLOSED: "closed", CLOSE_REQUESTED: "asked to close", ARCHIVED: "archived",
    UNARCHIVED: "unarchived", DELETED: "deleted", MOVED: "moved", SLACK_BOUND: "bound to Slack",
    SLACK_DETACHED: "detached from Slack", SLACK_REBOUND: "moved to another Slack thread",
    PARTICIPANT_ADDED: "participant added", PARTICIPANT_REMOVED: "participant removed",
    PARTICIPANT_ROLE_CHANGED: "participant role changed", SHARING_CHANGED: "sharing changed",
    SENSITIVITY_CHANGED: "sensitivity changed", RESET: "reset", FORKED: "forked",
    TRANSFERRED: "transferred",
}


def _redact(payload: dict) -> dict:
    """A sensitive conversation's event: its id, where, who, and the transition only."""
    keep = {k: payload[k] for k in ("session", "workspace", "actor", "turn", "via", "mcp_tool",
                                    "request_id", "sensitive", "reason") if k in payload}
    change = payload.get("change")
    if change:
        field = change.get("field", "")
        keep["change"] = (change if field in _SENSITIVE_SAFE_FIELDS else {"field": field})
    return keep


def build(kind: str, session, *, by=None, turn=None, change: dict | None = None,
          workspace_id: str = "", sensitive: bool | None = None, **meta) -> dict:
    """The event item for one transition of `session` (a Session, or `None` with
    `workspace_id` for a change to many sessions at once)."""
    actor = actor_of(by)
    payload = {
        "session": str(session.pk) if session is not None else "",
        "workspace": workspace_id or (session.__dict__.get("workspace_id", "") if session is not None else ""),
        "actor": actor,
        "turn": _turn_id(turn),
        **_via(),
    }
    if session is not None:
        payload.update({"agent": _agent_slug(session), "origin": session.__dict__.get("origin", ""),
                        "channel": _channel(session)})
    if change:
        payload["change"] = change
    reason = meta.pop("reason", "") or _ctx().get("reason", "")
    if reason:
        payload["reason"] = reason
    payload.update({k: v for k, v in meta.items() if v not in (None, "")})
    if sensitive is None and session is not None:
        sensitive = is_sensitive_meta(session.__dict__.get("metadata"))
    if sensitive:
        payload = _redact({**payload, "sensitive": True})
    what = f"session {payload['session'][:8]}" if payload["session"] else "sessions"
    return {
        "source": SOURCE,
        "kind": kind,
        "level": "info",
        # Blank: every transition is its own row, never a coalesced repeat.
        "key": "",
        "summary": f"{what} {_VERBS.get(kind, kind)} by {_label(actor)}"[:500],
        "payload": payload,
    }


def _write(items: list[dict]) -> None:
    by_ws: dict[str, list[dict]] = {}
    for item in items:
        ws = item["payload"].get("workspace")
        if ws:
            by_ws.setdefault(ws, []).append(item)
    for ws, rows in by_ws.items():
        try:
            events.record(rows, workspace=Workspace(pk=ws))
        except Exception:  # noqa: BLE001 — a record is never worth a failed action
            logger.exception("could not record session lifecycle events in %s", ws)


def record(kind: str, session, **kwargs) -> None:
    """Record one transition of `session`. Never raises."""
    if kind not in KINDS:
        raise ValueError(f"unknown session lifecycle kind {kind!r}; add it to lifecycle.KINDS")
    try:
        item = build(kind, session, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("could not build session lifecycle event %s", kind)
        return
    _write([item])


def record_many(kind: str, session_ids: Iterable, **kwargs) -> None:
    """The same transition of many sessions — for a queryset `update()` the row never
    saw. One event per session. Never raises."""
    ids = list(session_ids)
    if not ids:
        return
    try:
        sessions = list(Session.objects.filter(pk__in=ids).select_related("agent")
                        .only("id", "workspace_id", "agent_id", "agent__slug", "origin", "contact_id",
                              "metadata"))
        items = [build(kind, s, **kwargs) for s in sessions]
    except Exception:  # noqa: BLE001
        logger.exception("could not build session lifecycle events %s", kind)
        return
    _write(items)


# -- the observer -----------------------------------------------------------------

def snapshot(session) -> dict:
    """What the observer compares, read only from fields already loaded (a deferred
    field is never fetched just to watch it)."""
    d = session.__dict__
    snap = {}
    if "status" in d:
        snap["status"] = d["status"]
    if "workspace_id" in d:
        snap["workspace"] = d["workspace_id"]
    if "metadata" in d:
        meta = d["metadata"] or {}
        snap["slack"] = str(meta.get(SLACK_THREAD_KEY) or "")
        snap["sensitive"] = is_sensitive_meta(meta)
    return snap


#: The column each watched value lives in, for a save that names its `update_fields`.
_FIELD_COLUMNS = {"status": {"status"}, "workspace": {"workspace", "workspace_id"},
                  "slack": {"metadata"}, "sensitive": {"metadata"}}


def observe_saved(session, *, created: bool, update_fields=None) -> None:
    """Emit the transitions a save made, against the snapshot taken when the row was
    loaded (or last saved). Never raises."""
    try:
        before = getattr(session, "_lifecycle_snapshot", None) or {}
        after = snapshot(session)
        session._lifecycle_snapshot = after
        if created:
            _record_created(session)
            return
        items = []
        touched = None if update_fields is None else set(update_fields)
        for field, new in after.items():
            if touched is not None and not (_FIELD_COLUMNS[field] & touched):
                continue
            if field not in before or before[field] == new:
                continue
            items.extend(_field_items(session, field, before[field], new))
        if items:
            _write(items)
    except Exception:  # noqa: BLE001
        logger.exception("could not observe a session save")


def _field_items(session, field: str, old, new) -> list[dict]:
    change = {"field": field, "before": old, "after": new}
    if field == "status":
        if new == Session.ARCHIVED:
            kind = CLOSED if _ctx().get("closing") else ARCHIVED
        else:
            kind = UNARCHIVED
        return [build(kind, session, change=change)]
    if field == "workspace":
        # Both tenants hear it: the one it left and the one it joined.
        return [build(MOVED, session, change=change, workspace_id=old),
                build(MOVED, session, change=change, workspace_id=new)]
    if field == "slack":
        kind = SLACK_BOUND if not old else SLACK_DETACHED if not new else SLACK_REBOUND
        return [build(kind, session, change={"field": "slack_thread", "before": old, "after": new})]
    if field == "sensitive":
        # Always redacted: a conversation becoming sensitive is itself the news.
        return [build(SENSITIVITY_CHANGED, session, change=change, sensitive=True)]
    return []


def _record_created(session) -> None:
    meta = {}
    prov = session.provenance or {}
    if prov.get("forked_from"):
        meta["forked_from"] = str(prov["forked_from"])
    if session.parent_session_id:
        meta["parent_session"] = str(session.parent_session_id)
    if session.parent_turn_id:
        meta["parent_turn"] = str(session.parent_turn_id)
    by = _ctx().get("by")
    if by is None:
        by = _request_actor()
        if by.get("kind") == "system":
            # No caller canopy can name (no request, or a webhook such as Slack's):
            # the row's own author, else the box that reported it.
            try:
                by = (session.created_by if session.created_by_id
                      else session.contact if session.contact_id else None)
            except ObjectDoesNotExist:
                by = None
            if by is None:
                by = "runner" if session.origin == "runner" else "system"
    _write([build(CREATED, session, by=by, **meta)])


def observe_deleted(session) -> None:
    """A deleted session. (A workspace never takes its sessions with it:
    `Session.workspace` is PROTECT, so the log this writes into outlives it.)"""
    try:
        _write([build(DELETED, session)])
    except Exception:  # noqa: BLE001
        logger.exception("could not record a session deletion")


# -- participants ---------------------------------------------------------------

def participant_snapshot(row) -> str | None:
    return row.__dict__.get("role")


def observe_participant(row, *, created: bool = False, deleted: bool = False, origin=None,
                        update_fields=None) -> None:
    """A `SessionParticipant` row added, re-roled or removed. A row going with its
    session is that deletion, not a removal."""
    try:
        if deleted:
            model = getattr(origin, "model", None) or type(origin)
            if origin is not None and model is Session:
                return
        elif not created and update_fields is not None and "role" not in update_fields:
            return
        before = getattr(row, "_lifecycle_role", None)
        role = participant_snapshot(row)
        row._lifecycle_role = role
        if not created and not deleted and (before is None or before == role):
            return
        session = (Session.objects.select_related("agent")
                   .only("id", "workspace_id", "agent_id", "agent__slug", "origin", "contact_id", "metadata")
                   .filter(pk=row.session_id).first())
        if session is None:
            return
        if created:
            kind, change = PARTICIPANT_ADDED, {"field": "participant", "before": None,
                                               "after": {"user_id": row.user_id, "role": role}}
        elif deleted:
            kind, change = PARTICIPANT_REMOVED, {"field": "participant",
                                                 "before": {"user_id": row.user_id, "role": role},
                                                 "after": None}
        else:
            kind, change = PARTICIPANT_ROLE_CHANGED, {"field": "role", "user_id": row.user_id,
                                                      "before": before, "after": role}
        _write([build(kind, session, change=change)])
    except Exception:  # noqa: BLE001
        logger.exception("could not observe a session participant change")
