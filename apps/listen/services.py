"""Listen sessions: who may open one, which socket holds each role, and when one ends.

The relay itself is `consumers.ListenConsumer`. Everything here is plain sync ORM so
the REST routes, the consumer (bridged) and the off-request sweep share one rule.
Nothing here ever sees a frame. Design: docs/architecture/listen-relay.md.
"""
from __future__ import annotations

import base64
import datetime as dt
import uuid

from django.db import transaction
from django.utils import timezone

from apps.harness import runner_protocol
from apps.harness.models import Runner
from apps.realtime import groups

from .models import ROLES, ListenSession

#: Nothing connected (both roles) this long after creation: the session ends.
CONNECT_WINDOW = dt.timedelta(minutes=2)
#: A role gone this long from a live session: the session ends.
PEER_GONE = dt.timedelta(seconds=60)
#: No session lasts longer.
MAX_AGE = dt.timedelta(hours=4)
#: A socket refreshes its role this often; one silent for STALE_S has gone.
BEAT_S = 10
STALE_S = 30
SWEEP_EVERY_S = 10
#: Ended rows are kept this long (state and byte totals only), then deleted.
KEEP_ENDED = dt.timedelta(days=30)

ENDED_BY_OWNER = "ended_by_owner"
NOT_CONNECTED = "not_connected"
PEER_LEFT = "peer_gone"
TOO_LONG = "max_duration"


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def session_group(session_id) -> str:
    """Every socket on one session: `ended` notices."""
    return f"listen.{uuid.UUID(str(session_id)).hex}"


def role_group(session_id, role: str) -> str:
    """The socket holding one role: frames and `peer` notices meant for it."""
    return f"{session_group(session_id)}.{role}"


def other(role: str) -> str:
    return "laptop" if role == "phone" else "phone"


# -- runners ------------------------------------------------------------------
def runners_of(user):
    """The runners `user` may relay to: their own, not retired, not a bare computer."""
    return (Runner.objects.exclude(status=Runner.RETIRED)
            .filter(runner_protocol.acting_runner_q(user)).order_by("name"))


def is_online(runner: Runner) -> bool:
    """Its daemon has beaten recently (`Runner.is_reachable`): the control channel is up."""
    return runner.is_reachable


# -- hello --------------------------------------------------------------------
def _public_key(value, *, field: str) -> str:
    """A base64url (no padding) uncompressed P-256 point, as the phone sends it."""
    if not isinstance(value, str) or not value or len(value) > 200:
        raise Refused(422, f"hello.{field} must be a base64url public key")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        raise Refused(422, f"hello.{field} must be a base64url public key") from None
    if len(raw) != 65 or raw[0] != 4:
        raise Refused(422, f"hello.{field} must be an uncompressed P-256 point")
    return value


def clean_hello(kind: str, hello: dict) -> dict:
    """Exactly the public keys a session carries; anything else is dropped."""
    if not isinstance(hello, dict):
        raise Refused(422, "hello must be an object")
    phone_key = hello.get("phone_key")
    if kind == ListenSession.MEETING:
        phone_key = _public_key(phone_key, field="phone_key")
    elif phone_key is not None:
        raise Refused(422, "a pairing has no phone_key yet")
    label = hello.get("label") or ""
    if not isinstance(label, str):
        raise Refused(422, "hello.label must be text")
    return {"phone_eph": _public_key(hello.get("phone_eph"), field="phone_eph"),
            "phone_key": phone_key, "label": label.strip()[:80]}


# -- create / end -------------------------------------------------------------
def create(owner, *, runner_id, kind: str, hello: dict) -> ListenSession:
    if kind not in (ListenSession.PAIR, ListenSession.MEETING):
        raise Refused(422, "kind is pair or meeting")
    hello = clean_hello(kind, hello)
    runner = runners_of(owner).filter(pk=runner_id).first()
    if runner is None:
        raise Refused(404, "runner not found")
    if not is_online(runner):
        raise Refused(409, "That computer is offline")
    with transaction.atomic():
        # One meeting at a time per laptop: lock the runner row so two phones
        # can't both pass the check.
        Runner.objects.select_for_update().filter(pk=runner.pk).first()
        if kind == ListenSession.MEETING and ListenSession.objects.filter(
                runner=runner, kind=ListenSession.MEETING).exclude(state=ListenSession.ENDED).exists():
            raise Refused(409, "A meeting is already running on that computer")
        session = ListenSession.objects.create(owner=owner, runner=runner, kind=kind, hello=hello)
        transaction.on_commit(lambda: groups.publish(groups.runner_group(runner.pk), {
            "type": "runner.listen_start", "session_id": str(session.pk),
            "kind": session.kind, "hello": session.hello,
        }))
    return session


def end(session_id, reason: str) -> bool:
    """End the session (once) and close both sockets. False when it had already ended."""
    now = timezone.now()
    done = (ListenSession.objects.filter(pk=session_id).exclude(state=ListenSession.ENDED)
            .update(state=ListenSession.ENDED, end_reason=reason[:40], ended_at=now))
    if done:
        groups.publish(session_group(session_id), {"type": "listen.ended", "reason": reason})
    return bool(done)


def pending_for(runners) -> list[ListenSession]:
    """Waiting sessions on these runners made inside the connect window, oldest first."""
    since = timezone.now() - CONNECT_WINDOW
    return list(ListenSession.objects.filter(runner__in=runners, state=ListenSession.WAITING,
                                             created_at__gte=since).order_by("created_at"))


# -- roles (the consumer's bookkeeping) ----------------------------------------
def _present(session: ListenSession, role: str, now) -> bool:
    seen = getattr(session, f"{role}_seen_at")
    return bool(getattr(session, f"{role}_channel")) and seen is not None and \
        seen > now - dt.timedelta(seconds=STALE_S)


def claim_role(user, session_id, role: str, channel: str, *, runner_id=None):
    """Hold `role` on the session for this socket.

    Returns ("ok", other_present), ("ended", reason), ("taken", None) or
    ("missing", None) — missing also for a session that isn't the user's, or (for a
    runner's own key) isn't on that runner, so a stranger learns nothing."""
    now = timezone.now()
    with transaction.atomic():
        s = ListenSession.objects.select_for_update().filter(pk=session_id).first()
        if s is None or s.owner_id != user.pk or (runner_id is not None and str(s.runner_id) != str(runner_id)):
            return "missing", None
        if s.state == ListenSession.ENDED:
            return "ended", s.end_reason
        if _present(s, role, now):
            return "taken", None
        setattr(s, f"{role}_channel", channel)
        setattr(s, f"{role}_seen_at", now)
        fields = [f"{role}_channel", f"{role}_seen_at"]
        peer = _present(s, other(role), now)
        if peer:
            s.state, s.absent_since = ListenSession.LIVE, None
            s.connected_at = s.connected_at or now
            fields += ["state", "absent_since", "connected_at"]
        s.save(update_fields=fields)
        return "ok", peer


def beat(session_id, role: str, channel: str, *, sent: int = 0) -> str:
    """The socket is still there; add the bytes it relayed since the last beat.
    Returns the session's state, or "" when the role is no longer this socket's."""
    with transaction.atomic():
        s = ListenSession.objects.select_for_update().filter(pk=session_id).first()
        if s is None:
            return ""
        fields = _add_bytes(s, role, sent)
        mine = getattr(s, f"{role}_channel") == channel
        if mine:
            setattr(s, f"{role}_seen_at", timezone.now())
            fields.append(f"{role}_seen_at")
        if fields:
            s.save(update_fields=fields)
        return s.state if mine else ""


def peer_present(session_id, role: str) -> bool:
    """Whether the other role holds a live socket now, whatever web task it is on."""
    s = ListenSession.objects.filter(pk=session_id).first()
    return bool(s and s.state != ListenSession.ENDED and _present(s, other(role), timezone.now()))


def release_role(session_id, role: str, channel: str, *, sent: int = 0) -> None:
    """This socket closed: give the role up (if it still held it) and start the
    peer-gone clock on a live session."""
    with transaction.atomic():
        s = ListenSession.objects.select_for_update().filter(pk=session_id).first()
        if s is None:
            return
        fields = _add_bytes(s, role, sent)
        if getattr(s, f"{role}_channel") == channel:
            setattr(s, f"{role}_channel", "")
            setattr(s, f"{role}_seen_at", None)
            fields += [f"{role}_channel", f"{role}_seen_at"]
            if s.state == ListenSession.LIVE and s.absent_since is None:
                s.absent_since = timezone.now()
                fields.append("absent_since")
        if fields:
            s.save(update_fields=fields)


def _add_bytes(s: ListenSession, role: str, sent: int) -> list[str]:
    if sent <= 0:
        return []
    if role == "phone":
        s.bytes_up += sent
        return ["bytes_up"]
    s.bytes_down += sent
    return ["bytes_down"]


# -- the sweep ----------------------------------------------------------------
def sweep() -> int:
    """End what no socket will: never joined, a peer gone a minute, or too old.
    Also forgets long-ended rows. Returns how many sessions it ended."""
    now = timezone.now()
    ended = 0
    open_ = ListenSession.objects.exclude(state=ListenSession.ENDED)
    for s in open_.filter(created_at__lt=now - MAX_AGE).only("pk"):
        ended += end(s.pk, TOO_LONG)
    for s in open_.filter(state=ListenSession.WAITING, created_at__lt=now - CONNECT_WINDOW).only("pk"):
        ended += end(s.pk, NOT_CONNECTED)
    for s in open_.filter(state=ListenSession.LIVE):
        absent = s.absent_since
        if absent is None:
            # A socket on a web process that died never said it left: its beat went quiet.
            quiet = [getattr(s, f"{r}_seen_at") for r in ROLES if not _present(s, r, now)]
            if quiet:
                absent = min((q for q in quiet if q is not None), default=now)
                ListenSession.objects.filter(pk=s.pk, absent_since__isnull=True).update(absent_since=absent)
        if absent is not None and absent < now - PEER_GONE:
            ended += end(s.pk, PEER_LEFT)
    ListenSession.objects.filter(state=ListenSession.ENDED, ended_at__lt=now - KEEP_ENDED).delete()
    return ended
