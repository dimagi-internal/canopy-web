"""The agent login a runner gets for ONE turn — issued while it holds the turn, dead after.

An agent's runner turn used to act as the agent with its stored, never-expiring
`canopy-pat`: a bearer secret that lets whoever holds it be the agent, sitting in a
vault and on laptops. canopy-web already knows which box claimed which turn of which
agent, so it hands that box an agent login scoped to the turn instead: a
`PersonalToken` for `Agent.user` that expires in `TTL` and is revoked as soon as the
turn stops executing on that runner. Spec:
docs/superpowers/specs/2026-10-21-turn-scoped-agent-token-design.md.

Who gets one is decided in `issue` (every refusal says why); `revoke_for_turn` and
`revoke_stale` end them. The route is `api_turn_agent_token.py`.

**Laptop sessions (session-scoped).** On a laptop (emdash) runner the turn finishes the
moment its Claude session exists, and the person goes on driving that session for as
long as they like. So there the login follows the SESSION: `issue_for_session` mints one
for a finished turn whose chat session is still active and bound to the same runner
under the same session key, and it is held exactly as long as that stays true. Every
action it takes is still tied to `turn` — the turn that opened the session, and so who
started the work — instead of to a stored login nobody can attribute.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.tokens.models import PersonalToken

from .models import Turn, TurnAgentToken
from .routing import EXECUTING

logger = logging.getLogger(__name__)

#: The longest a turn token lives. A turn that runs longer asks again.
TTL = dt.timedelta(hours=12)
#: The `AgentCredentialRead.name` an issue is logged under, beside the vault reads.
READ_NAME = "turn-agent-token"


class Refused(Exception):
    """This turn gets no agent login; the message says why."""


def turn_agent(turn: Turn):
    """The agent this turn runs AS on a runner: its own, or its chat's. Not a project
    turn's configured fallback agent — a runner runs a project turn as no agent."""
    if turn.agent_id:
        return turn.agent
    cs = getattr(turn, "chat_session", None)
    if cs is not None and cs.agent_id:
        return cs.agent
    return None


def refusal(turn: Turn) -> str:
    """Why this turn may not have an agent login, or "" when it may."""
    if turn.origin == Turn.ORIGIN_LOCAL:
        return ("a local turn is a person's own session, which acts as that person — "
                "it is never given its agent's login")
    if turn.capability:
        return (f"this turn is confined to the caller capability '{turn.capability}', and a "
                f"caller's turn is never given its agent's login")
    agent = turn_agent(turn)
    if agent is None:
        return "this turn runs as no agent"
    user = agent.user
    if user is None:
        return f"{agent.slug} has no canopy login of its own (Settings → Credentials)"
    if not user.is_active:
        return f"{agent.slug}'s canopy login is deactivated"
    return ""


def issue(turn: Turn, runner) -> dict:
    """Mint a fresh agent login for `turn`, held by `runner`. Raises Refused.

    The caller has already checked that `runner` holds the turn and may hold its
    agent. Each call mints a new token; earlier ones for the turn stay valid until
    the turn ends (a runner that lost its copy just asks again)."""
    why = refusal(turn)
    if why:
        raise Refused(why)
    agent = turn_agent(turn)
    try:
        with transaction.atomic():
            raw, token = PersonalToken.create_for_user(
                user=agent.user, ttl=TTL,
                label=f"turn {str(turn.pk)[:8]} on {runner.name} (turn-scoped)"[:200])
            TurnAgentToken.objects.create(turn=turn, runner=runner, token=token)
    except ValueError as exc:  # a system account never holds a token
        raise Refused(f"{agent.slug}'s login cannot hold a token: {exc}") from exc
    _audit(turn, runner, agent)
    return {"token": raw, "expires_at": token.expires_at, "agent_slug": agent.slug,
            "turn_id": str(turn.pk)}


def live_session(turn: Turn, runner):
    """The session `turn` opened, if it is still active and bound to `runner` under the
    turn's own session key — the laptop case, where a finished turn's work goes on. A
    chat turn names its session; an agent turn (mail, a schedule, a dispatch) is found by
    the binding it made: this runner, this session key, this agent. None otherwise."""
    # emdash only: a `computer` runner runs a person's own local sessions (they act as the
    # person), and a cloud turn ends with its process.
    if runner.kind != runner.EMDASH or not turn.session_key or turn.claimed_by_id != runner.pk:
        return None
    qs = (RunnerBinding.objects.select_related("session")
          .filter(runner=runner, session_key=turn.session_key, session__status=Session.ACTIVE))
    if turn.chat_session_id:
        qs = qs.filter(session_id=turn.chat_session_id)
    else:
        agent = turn_agent(turn)
        if agent is None:
            return None
        qs = qs.filter(session__agent=agent)
    b = qs.first()
    return b.session if b is not None else None


def issue_for_session(turn: Turn, runner, session) -> dict:
    """A login as `turn`'s agent, held while `session` stays live on `runner`."""
    why = refusal(turn)
    if why:
        raise Refused(why)
    agent = turn_agent(turn)
    try:
        with transaction.atomic():
            raw, token = PersonalToken.create_for_user(
                user=agent.user, ttl=TTL,
                label=f"session {str(session.pk)[:8]} on {runner.name} (session-scoped)"[:200])
            TurnAgentToken.objects.create(turn=turn, runner=runner, token=token, session=session)
    except ValueError as exc:
        raise Refused(f"{agent.slug}'s login cannot hold a token: {exc}") from exc
    _audit(turn, runner, agent, session=session)
    return {"token": raw, "expires_at": token.expires_at, "agent_slug": agent.slug,
            "turn_id": str(turn.pk), "session_id": str(session.pk)}


def _audit(turn: Turn, runner, agent, session=None) -> None:
    """Logged like a credential read: a row in the agent's read history, and an event.
    An audit hiccup must not deny a turn its login (the same rule as resolve)."""
    try:
        from apps.agents.services import record_credential_reads

        record_credential_reads(agent, [READ_NAME], user=runner.owner, runner=runner,
                                via="session" if session is not None else "turn", turn_id=str(turn.pk))
    except Exception:  # noqa: BLE001
        logger.exception("turn agent token: could not log the read for %s", turn.pk)
    try:
        from apps.events import services as events

        events.record(
            [{
                "source": "harness.turn_agent_tokens",
                "kind": "agent.canopy_token.issued",
                "level": "info",
                "key": f"{agent.slug}:{turn.pk}",
                "summary": (f"{agent.slug}: {'session' if session is not None else 'turn'}-scoped "
                            f"canopy login issued to {runner.name}"),
                "payload": {"agent": agent.slug, "turn": str(turn.pk), "runner": str(runner.pk),
                            "session": str(session.pk) if session is not None else "",
                            "ttl_hours": TTL.total_seconds() / 3600},
            }],
            workspace=agent.workspace,
        )
    except Exception:  # noqa: BLE001
        logger.exception("turn agent token: could not record the event for %s", turn.pk)


def _revoke(qs) -> int:
    ids = list(qs.filter(token__revoked_at__isnull=True).values_list("token_id", flat=True))
    if not ids:
        return 0
    return PersonalToken.objects.filter(pk__in=ids, revoked_at__isnull=True).update(
        revoked_at=timezone.now())


def revoke_for_turn(turn_id) -> int:
    """Revoke every TURN-scoped agent login issued for this turn (a session-scoped one
    outlives its turn by design; `revoke_stale` ends it with the session). Never raises."""
    try:
        return _revoke(TurnAgentToken.objects.filter(turn_id=turn_id, session__isnull=True))
    except Exception:  # noqa: BLE001 — the turn's own transition must still happen
        logger.exception("turn agent token: revoking %s's failed", turn_id)
        return 0


def revoke_stale() -> int:
    """Revoke every live turn login whose turn is no longer executing on the runner it
    was issued to — the catch-all for transitions made by queryset updates (requeues,
    sweeps) that never pass through `finish_turn`. Never raises."""
    try:
        held = (Q(session__isnull=True) & Q(turn__status__in=EXECUTING)
                & Q(turn__claimed_by_id=F("runner_id")))
        # A session-scoped login: held while its session is active and still bound to the
        # runner it was issued to, under the session key of the turn that opened it.
        held |= (Q(session__isnull=False) & Q(session__status=Session.ACTIVE)
                 & Q(session__runner_binding__runner_id=F("runner_id"))
                 & Q(session__runner_binding__session_key=F("turn__session_key")))
        live = TurnAgentToken.objects.filter(token__expires_at__gt=timezone.now())
        return _revoke(live.exclude(held))
    except Exception:  # noqa: BLE001
        logger.exception("turn agent token: stale sweep failed")
        return 0
