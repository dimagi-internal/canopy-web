"""Automatic rotation of an agent's own canopy login (`CANOPY_PAT_REF`; Canopy Vault Phase 1b,
canopy-web#1549).

Its login is the one secret canopy-web issues itself, so it is the one it
can rotate without anyone typing anything. An owner or admin opts an agent in
(PATCH /api/agents/{slug}/pat-rotation, default every 30 days); then, when due:

1. mint a new PersonalToken for the agent's own login (`Agent.user`), with the
   same lifetime `mint_agent_canopy_token` gives it (`agent_token_ttl_days`, the PAT
   default) — so the interval is capped below it (`max_days`) and a rotating agent's
   token never lapses;
2. write it to the agent's login AgentCredential — a runner picks it up
   at its next `canopy cred sync`, because the values' fingerprint it keeps in
   `~/.<slug>/.env.stamp` changes (canopy-web keeps no separate
   `content_version`: the value IS the version);
3. keep the OLD token working for OVERLAP (24 h) — its `expires_at` is pulled in
   to the end of the overlap, so it dies even if nothing else runs — and then
   revoke it (`revoked_at`), which the same tick does once `revoke_after` passes.

canopy-web cannot read the stored value (the agent's vault is zero-knowledge),
so the old token is the one this job minted last — a live PersonalToken of the
agent's own login, revoked after the overlap. Every other long-lived token of that
login (the hand-minted first one above all) is shortened to the overlap too
(`_expire_strays`); a token of anyone else's, a person's stored by mistake, is
replaced and left alone.

The impersonation guard of `services.mint_agent_canopy_token` is re-checked at
every rotation against the person who opted in: once the agent's login has
signed in interactively (it may be a person's), only a workspace owner's opt-in
lets the job mint for it.

Every rotation, success or failure, is an `AgentPatRotation` row and an
`agent.credentials.pat_rotated` event.

The clock is a job on the off-request clock (apps/common/ticks.py, registered
from AgentsConfig.ready); `manage.py rotate_canopy_pats` runs one tick by hand.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.conf import settings
from django.core.cache import cache
from django.db import models, transaction
from django.utils import timezone

from apps.events import services as events
from apps.tokens.models import PersonalToken
from apps.workspaces import permissions as perms

from .. import services
from ..models import Agent, AgentCredential, AgentPatRotation
from . import zk_seal

logger = logging.getLogger(__name__)

#: The name an agent's own canopy login is stored under in canopy-web's vault. A
#: runner writes each stored value out as the environment variable OF THAT NAME
#: (`canopy cred sync`, which writes only stored values), and an agent's
#: code reads its login from `CANOPY_WEB_PAT` — so that is the name, not the
#: 1Password item name `canopy-pat` (`.env.tpl`: CANOPY_WEB_PAT=op://…/canopy-pat/…),
#: which is not even a valid variable name and would be dropped on the way.
CANOPY_PAT_REF = "CANOPY_WEB_PAT"
OVERLAP = dt.timedelta(hours=24)
MIN_DAYS, MAX_DAYS = 1, 365
TICK_CACHE_KEY = "canopy-pat-rotation:tick"


class RotationRefused(Exception):
    """This agent's canopy-pat cannot be rotated; the message says why."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def enable_refusal(agent, actor) -> RotationRefused | None:
    """Why `actor` may not opt `agent` in (or rotate it), or None. The same
    standing `mint_agent_canopy_token` asks for, decided at opt-in so a job that
    runs later without anyone present is never the first to find out."""

    # The new token is sealed to the agent's devices and members (`zks1`,
    # apps/agents/credentials/zk_seal.py), never stored readable.
    if not zk_seal.recipients_for(agent):
        return RotationRefused(f"nobody can read a new {CANOPY_PAT_REF} for {agent.slug} yet: "
                               "approve a device or add a member with a vault key", 409)
    if agent.user_id is None:
        return RotationRefused(f"{agent.slug} has no canopy login of its own (canopy user); "
                               "mint its canopy token once first, then rotation can take over")
    if not AgentCredential.objects.filter(agent=agent, name=CANOPY_PAT_REF).exists():
        return RotationRefused(f"{agent.slug} has no {CANOPY_PAT_REF} stored in canopy-web; "
                               "rotation replaces the stored value, so store it here first")
    # The guard is against minting a token for someone ELSE's account. The login
    # rotating its own token (an agent that leaked it: `rotate_own_pat`) is that
    # account acting on itself, so it passes.
    if agent.user.last_login is not None and not agent.is_own_login(actor) and (
            actor is None or not perms.can(actor, agent.workspace, perms.OWN)):
        return RotationRefused(f"{agent.user.email} has signed in to canopy, so it may be a "
                               "person's account; only a workspace owner may rotate its token", 403)
    return None


def due_at(agent):
    """When the agent's canopy-pat is next due, or None when rotation is off.
    Counted from the last rotation, else from when the value was last written."""

    if not agent.pat_rotation_enabled:
        return None
    base = agent.pat_rotated_at
    if base is None:
        cred = AgentCredential.objects.filter(agent=agent, name=CANOPY_PAT_REF).first()
        base = cred.updated_at if cred else timezone.now()
    days = min(max(MIN_DAYS, int(agent.pat_rotation_days or 30)), max_days())
    return base + dt.timedelta(days=days)


#: Rotate at least this long before the token expires.
EXPIRY_MARGIN_DAYS = 14


def max_days() -> int:
    """The longest rotation interval: the agent token's lifetime less a margin, so a
    rotating agent's token is replaced before it expires."""
    return max(MIN_DAYS, min(MAX_DAYS, services.agent_token_ttl_days() - EXPIRY_MARGIN_DAYS))


def _current_token(agent):
    """The live PersonalToken to retire: the one this job minted last for the
    agent's own login, while a canopy-pat is stored; else None. canopy-web cannot
    read the stored value, so it goes by its own record (if a person stored
    another since, that one is superseded anyway)."""

    if agent.user_id is None or not AgentCredential.objects.filter(agent=agent, name=CANOPY_PAT_REF).exists():
        return None
    last = (AgentPatRotation.objects.filter(agent=agent, status=AgentPatRotation.ROTATED,
                                            new_token__isnull=False)
            .order_by("-created_at").first())
    if last is None:
        return None
    return PersonalToken.objects.filter(pk=last.new_token_id, user_id=agent.user_id,
                                        revoked_at__isnull=True).first()


def _expire_strays(agent, *, keep, now) -> int:
    """Every OTHER live, long-lived token of the agent's own login ends with the overlap.

    canopy-web cannot read the stored value, so before its first rotation it does not
    know which token that is — and the one an agent was first given (minted by hand,
    copied to a box, leaked to a licensee: canopy-web#2265) would otherwise live out its
    full lifetime. After a rotation the vault holds the one login token the agent should
    use; any other copy is stale. Left alone: turn- and session-scoped tokens (short,
    their own lifetime — apps/harness/turn_agent_tokens.py) and OAuth grants' tokens."""
    end = now + OVERLAP
    qs = (PersonalToken.objects.filter(user_id=agent.user_id, revoked_at__isnull=True,
                                       oauth_grant__isnull=True, turn_grant__isnull=True)
          .exclude(pk=keep.pk)
          .filter(models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=end)))
    return qs.update(expires_at=end)


def rotate(agent, *, trigger: str = "scheduler", actor=None, presented_token_id=None):
    """Rotate now. Returns the AgentPatRotation row (status `rotated`), or raises
    RotationRefused after recording a `failed` row."""

    now = timezone.now()
    refusal = None
    with transaction.atomic():
        agent = Agent.objects.select_for_update(of=("self",)).select_related("user", "workspace").get(pk=agent.pk)
        # The background job acts on the standing of whoever opted the agent in.
        refusal = enable_refusal(agent, agent.pat_rotation_enabled_by if trigger == "scheduler"
                                 else actor)
        if refusal is None:
            row = _rotate_locked(agent, now=now, trigger=trigger, actor=actor,
                                 presented_token_id=presented_token_id)
    if refusal is not None:
        row = AgentPatRotation.objects.create(agent=agent, status=AgentPatRotation.FAILED,
                                              trigger=trigger, detail=str(refusal)[:500], actor=actor)
        _event(agent, row)
        raise refusal
    _event(agent, row)
    return row


def _rotate_locked(agent, *, now, trigger: str, actor, presented_token_id=None):
    """The rotation itself, inside `rotate`'s transaction (agent row locked)."""

    old = _current_token(agent)
    if old is None and presented_token_id is not None:
        # The agent's own login rotating a token it LEAKED, on an agent whose stored
        # value canopy-web cannot read (zero-knowledge) and has never rotated: nothing
        # names the current token, so retire the one the agent called with — that is
        # the leaked one. Without this, a first self-rotation minted a new token and
        # left the leaked one live (Hal, 2026-10-24: "nothing revoked").
        old = PersonalToken.objects.filter(pk=presented_token_id, user_id=agent.user_id,
                                           revoked_at__isnull=True).first()
    raw, new = PersonalToken.create_for_user(
        user=agent.user, label=f"{agent.slug} agent (rotated {now:%Y-%m-%d})"[:100],
        ttl_days=services.agent_token_ttl_days())
    services.set_agent_credentials(agent, {CANOPY_PAT_REF: raw}, user=actor, minted=True)
    revoke_after = None
    if old is not None:
        revoke_after = now + OVERLAP
        # The overlap's end is ALSO the old token's expiry: it stops working on
        # time even if no tick ever comes round to revoke it.
        if old.expires_at is None or old.expires_at > revoke_after:
            PersonalToken.objects.filter(pk=old.pk).update(expires_at=revoke_after)
    strays = _expire_strays(agent, keep=new, now=now)
    detail = ("old token stays valid for 24 h, then is revoked" if old is not None else
              "the stored value was not a live token of this agent's login; nothing revoked")
    if strays:
        detail += f"; {strays} other long-lived token(s) of its login expire in 24 h"
    row = AgentPatRotation.objects.create(
        agent=agent, status=AgentPatRotation.ROTATED, trigger=trigger, detail=detail,
        old_token=old, new_token=new, revoke_after=revoke_after, actor=actor)
    Agent.objects.filter(pk=agent.pk).update(pat_rotated_at=now)
    return row


def revoke_due(now=None) -> int:
    """Revoke every old token whose overlap has ended. Returns how many."""

    now = now or timezone.now()
    done = 0
    for row in (AgentPatRotation.objects.filter(
            status=AgentPatRotation.ROTATED, old_token__isnull=False,
            old_revoked_at__isnull=True, revoke_after__lte=now).select_related("agent")):
        with transaction.atomic():
            PersonalToken.objects.filter(pk=row.old_token_id, revoked_at__isnull=True) \
                .update(revoked_at=now)
            AgentPatRotation.objects.filter(pk=row.pk).update(old_revoked_at=now)
        done += 1
    return done


def due_agents(now=None):

    now = now or timezone.now()
    return [a for a in Agent.objects.filter(pat_rotation_enabled=True).select_related("user")
            if (d := due_at(a)) is not None and d <= now]


def tick(now=None) -> dict:
    """One pass: rotate every due agent, revoke every ended overlap. Never raises."""
    out = {"rotated": 0, "failed": 0, "revoked": 0}
    try:
        for agent in due_agents(now):
            try:
                rotate(agent, trigger="scheduler")
                out["rotated"] += 1
            except RotationRefused:
                out["failed"] += 1
                # Back off a full period rather than retrying (and recording a
                # failure) every tick; the failure row says why.

                Agent.objects.filter(pk=agent.pk).update(pat_rotated_at=timezone.now())
            except Exception:  # noqa: BLE001
                out["failed"] += 1
                logger.exception("canopy-pat rotation failed for %s", agent.slug)
        out["revoked"] = revoke_due(now)
    except Exception:  # noqa: BLE001
        logger.exception("canopy-pat rotation tick failed")
    return out


def tick_if_due(interval_s: float) -> dict | None:
    """`tick`, at most once per interval across every web process (the cache
    lock); this process runs it anyway if the cache is down."""
    try:
        if not cache.add(TICK_CACHE_KEY, timezone.now().isoformat(), max(1, int(interval_s) - 1)):
            return None
    except Exception:  # noqa: BLE001
        logger.warning("canopy-pat rotation: cache unavailable; ticking on this process's clock")
    return tick()


def status(agent, *, history: int = 10) -> dict:

    rows = AgentPatRotation.objects.filter(agent=agent).order_by("-created_at")[:history]
    return {
        "enabled": agent.pat_rotation_enabled,
        "period_days": int(agent.pat_rotation_days or 30),
        "last_rotated_at": agent.pat_rotated_at,
        "next_due_at": due_at(agent),
        "enabled_by_email": (agent.pat_rotation_enabled_by.email
                             if agent.pat_rotation_enabled_by_id else ""),
        "history": [{
            "status": r.status, "trigger": r.trigger, "detail": r.detail,
            "created_at": r.created_at, "revoke_after": r.revoke_after,
            "old_revoked_at": r.old_revoked_at,
        } for r in rows],
    }


def _event(agent, row) -> None:
    try:

        events.record([{
            "source": "agents.credentials",
            "kind": "agent.credentials.pat_rotated",
            "level": "info" if row.status == row.ROTATED else "warn",
            "key": "",
            "summary": f"{agent.slug} canopy-pat rotation {row.status} ({row.trigger}): {row.detail}"[:300],
            "payload": {"agent": agent.slug, "status": row.status, "trigger": row.trigger,
                        "rotation": row.pk,
                        "revoke_after": row.revoke_after.isoformat() if row.revoke_after else None},
        }], workspace=agent.workspace)
    except Exception:  # noqa: BLE001
        logger.exception("canopy-pat rotation event failed for %s", agent.slug)


def interval_s() -> float:
    return float(getattr(settings, "CANOPY_PAT_ROTATION_TICK_S", 900) or 0)
