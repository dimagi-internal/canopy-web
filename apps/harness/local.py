"""Local use of an agent: a person's own Claude Code session, recorded as a turn.

Three objects (canopy-web#1704, `docs/architecture/access.md` § Local use):

* a **private runner** (`models.PrivateRunner`) — the person's machine, registered once
  with their own token; it never claims anything;
* a **local turn** (`Turn.ORIGIN_LOCAL`) — one Claude Code session there with one
  agent, opened by the session's own SessionStart hook. It is a real turn: the
  person is its initiator, it carries the caller envelope, the turn mode, the
  credential reads and (when the session ends) the transcript;
* a **license** (person × agent, `apps/agents/licenses.py`) — requested when the
  first local turn opens, issued at once when the caller list gives the person
  the whole agent; the turn records it (`origin_ref.license`), and credential
  reads during the turn go out under it.

A local turn is RUNNING from the moment it opens and is never queued, claimed or
routed. It does not serialize the agent (`one_executing_turn_per_agent` and
`routing.busy_agent_ids` exclude it): it is the person's session, not the agent's
one continuous one. Its lease is long and renewed by the session's prompts; a
session that dies without closing lands `lost` through the ordinary sweep.

Mode: only an agent admin may run a local turn `auto`, and only when they ask for
it; every other turn — an editor's, a `full:`-rule member's — is `manual`, exactly
as access.md says of `auto` everywhere else. Stamped once, at open.
"""
from __future__ import annotations

import datetime as dt
import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import initiator as who
from .ledger import append_events
from .models import Turn, PrivateRunner
from .turn_mode import AUTO, MANUAL

#: A local turn's lease. A person's session can sit idle for hours between prompts;
#: the prompt hook renews it (throttled), and only a session silent this long is
#: swept `lost`.
LOCAL_LEASE = dt.timedelta(hours=8)


class LocalRefusedError(Exception):
    """The person may not run this agent locally. `str()` says why and what to ask for."""

    def __init__(self, reason: str, *, status: int = 403):
        super().__init__(reason)
        self.status = status


def register_private_runner(user, *, name: str, host: str = "",
                            runner=None) -> tuple[PrivateRunner, bool]:
    """The person's private runner called `name`, creating it on first use (idempotent
    per person and name — re-registering the same machine is not an error). A
    retired one is brought back. `runner` — a Runner the person owns, the runner app
    on this same machine — links the two (canopy-web#1705)."""
    name = (name or "").strip()[:100]
    if not name:
        raise ValueError("a private runner needs a name")
    try:
        with transaction.atomic():
            ws, created = PrivateRunner.objects.get_or_create(
                owner=user, name=name, defaults={"host": (host or "")[:200]})
    except IntegrityError:  # a concurrent register of the same name
        ws, created = PrivateRunner.objects.get(owner=user, name=name), False
    fields = []
    if host and ws.host != host[:200]:
        ws.host = host[:200]
        fields.append("host")
    if ws.retired_at is not None:
        ws.retired_at = None
        fields.append("retired_at")
    if runner is not None and ws.runner_id != runner.pk:
        if runner.owner_id != user.pk:
            raise ValueError("a private runner can only be linked to a runner you own")
        # One machine, one private runner: release the link from any other row first.
        PrivateRunner.objects.filter(runner=runner).exclude(pk=ws.pk).update(runner=None)
        ws.runner = runner
        fields.append("runner")
    if fields:
        ws.save(update_fields=fields)
    return ws, created


def private_runner_of(user, private_runner_id) -> PrivateRunner | None:
    """The caller's OWN live private runner, or None — never someone else's."""
    try:
        wid = uuid.UUID(str(private_runner_id))
    except (TypeError, ValueError):
        return None
    return PrivateRunner.objects.filter(pk=wid, owner=user, retired_at__isnull=True).first()


def authorize(agent, user):
    """The person's ACTIVE license for `agent` (`apps/agents/licenses.py`), issued now
    when the caller list gives them the whole agent. Raises LocalRefusedError
    otherwise: a confined capability is a door canopy holds (the `ask` path), not
    something a session on someone's own machine can be held to."""
    from apps.agents import licenses

    try:
        return licenses.request(agent, user)
    except licenses.LicenseRefusedError as exc:
        raise LocalRefusedError(str(exc)) from exc


def mode_for(agent, user, requested: str = "") -> tuple[str, str]:
    """(mode, basis) for a local turn. `auto` only for an agent admin who asked."""
    email = getattr(user, "email", "") or "a member"
    from apps.workspaces import role_cap

    cap = role_cap.cap_of(user)
    if cap:
        email = f"{email} acting as {cap}"
    if requested == AUTO:
        if agent.is_admin(user):
            return AUTO, f"local turn: auto requested by {email} (admin of {agent.slug})"
        return MANUAL, (f"local turn: auto withheld — {email} is not an admin of {agent.slug}; "
                        "outbound needs an admin")
    if agent.is_admin(user):
        return MANUAL, f"local turn: {email} (admin of {agent.slug}) — manual; ask for auto to act unreviewed"
    return MANUAL, f"local turn: {email} — manual; outbound needs an admin of {agent.slug}"


def _key(agent, claude_session_id: str) -> str:
    return f"local-{agent.slug}-{claude_session_id}"[:128]


def open_turn(request, agent, *, private_runner: PrivateRunner, claude_session_id: str,
              cwd: str = "", requested_mode: str = "", acts_as: str = "") -> tuple[Turn, bool]:
    """Open — or re-open — the local turn for one Claude Code session.

    Idempotent per (agent, Claude session): `/resume` and `/clear` fire SessionStart
    again and must land on the same turn, renewed, rather than a second one. A
    session whose turn already ended (it was closed, or swept `lost` after a long
    sleep) gets a NEW turn, so a closed turn's transcript is never appended to by a
    later life of the session. `acts_as` opens it as a lower role (role_cap): the
    license, the mode and every read of the turn are judged at that role. Returns
    (turn, created)."""
    from apps.workspaces import role_cap

    user = role_cap.apply(request.user, acts_as)
    acts_as = role_cap.cap_of(user)
    claude_session_id = (claude_session_id or "").strip()[:100]
    if not claude_session_id:
        raise ValueError("claude_session_id is required")
    from apps.agents import licenses

    lic = authorize(agent, user)
    licenses.touch(lic, private_runner=private_runner)
    now = timezone.now()
    key = _key(agent, claude_session_id)
    live = (Turn.objects.filter(agent=agent, origin=Turn.ORIGIN_LOCAL, initiator_user=user,
                                session_key=claude_session_id, status=Turn.RUNNING)
            .order_by("-created_at").first())
    if live is not None:
        renew(live, private_runner=private_runner, now=now)
        return live, False
    if Turn.objects.filter(idempotency_key=key).exists():
        key = f"{key[:100]}-{uuid.uuid4().hex[:12]}"
    mode, basis = mode_for(agent, user, requested_mode)
    asker = who.for_request(request, via=Turn.ORIGIN_LOCAL)
    if getattr(request, "runner_token", None) is not None:
        # The owner's session, signed in with their own private runner's device key
        # (the API checked the box is private and is this private runner's): as strong
        # a claim as their PAT, and one that cannot expire unnoticed.
        asker = who.for_user(user, via=Turn.ORIGIN_LOCAL, assurance=who.PRIVATE_RUNNER_KEY)
    turn = Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_LOCAL, routing=Turn.LOCAL_ONLY,
        idempotency_key=key, status=Turn.RUNNING, private_runner=private_runner,
        origin_ref={"private_runner": {"id": str(private_runner.pk), "name": private_runner.name},
                    "license": {"id": lic.pk, "basis": lic.basis},
                    "claude_session_id": claude_session_id, "cwd": (cwd or "")[:300],
                    **({"acts_as": acts_as} if acts_as else {})},
        claimed_at=now, started_at=now, lease_expires_at=now + LOCAL_LEASE,
        session_key=claude_session_id, enqueued_by=user,
        turn_mode=mode, turn_mode_basis=basis[:320],
        **asker.fields(),
    )
    append_events(turn, [{"kind": "status", "payload": {
        "status": Turn.RUNNING, "local": True, "private_runner": private_runner.name}}])
    PrivateRunner.objects.filter(pk=private_runner.pk).update(last_seen_at=now)
    return turn, True


def renew(turn: Turn, *, private_runner: PrivateRunner | None = None, now=None) -> Turn:
    """Push a live local turn's lease out again. A no-op on a turn that has ended."""
    now = now or timezone.now()
    Turn.objects.filter(pk=turn.pk, status=Turn.RUNNING).update(
        lease_expires_at=now + LOCAL_LEASE)
    wid = private_runner.pk if private_runner is not None else turn.private_runner_id
    if wid:
        PrivateRunner.objects.filter(pk=wid).update(last_seen_at=now)
    turn.refresh_from_db(fields=["status", "lease_expires_at"])
    return turn


def is_initiator(turn: Turn, user) -> bool:
    """A local turn is reported on (renewed, finished, transcript appended) by the
    person whose session it is — there is no box that claimed it."""
    return (turn.origin == Turn.ORIGIN_LOCAL and turn.initiator_user_id is not None
            and turn.initiator_user_id == getattr(user, "pk", None))
