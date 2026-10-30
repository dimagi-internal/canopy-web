"""/api/agents/{slug}/pat-rotation — renewal of the agent's own canopy login.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.events import audit
from apps.passkeys import stepup
from apps.tokens import kinds as token_kinds

from .credentials import (
    pat_rotation,
)
from .gates import (
    agent_for_admin,
    agent_for_read,
)
from .schemas import (
    OwnPatRotateIn,
    OwnPatRotateOut,
    PatRotationIn,
    PatRotationOut,
)

router = Router(auth=session_auth, tags=["agents"])

@router.get("/{slug}/pat-rotation", response=PatRotationOut,
            summary="This agent's canopy-pat rotation: setting, next due, history")
@gate("agent-admin")
def get_pat_rotation(request: HttpRequest, slug: str) -> PatRotationOut:
    """Never a token. The agent's owner or an admin."""

    agent = agent_for_admin(request, slug)
    return PatRotationOut(**pat_rotation.status(agent))


@router.patch("/{slug}/pat-rotation", response=PatRotationOut,
              summary="Turn automatic canopy-pat rotation on or off")
@gate("agent-admin", setting=True)  # turning it on also asks mint_agent_canopy_token's guard
def set_pat_rotation(request: HttpRequest, slug: str, payload: PatRotationIn) -> PatRotationOut:
    """Canopy Vault (canopy-web#1549). When on, every `period_days` (default
    30) canopy-web mints a new token for the agent's own canopy login, writes
    it to the agent's `canopy-pat` credential (a runner picks it up at its next
    `canopy cred sync`), keeps the old token valid for 24 h and then revokes it.
    The agent's token expires (the PAT default, 180 days), so `period_days` is
    capped 14 days short of that and the token is always replaced first.
    Turning it on needs the agent to have its own login and a stored
    `canopy-pat`, and — when that login has ever signed in interactively — a
    workspace owner (422/403 otherwise). The owner or an admin."""

    agent = agent_for_admin(request, slug)
    before = (agent.pat_rotation_enabled, agent.pat_rotation_days)
    if payload.enabled:
        refusal = pat_rotation.enable_refusal(agent, request.user)
        if refusal:
            raise HttpError(refusal.status, str(refusal))
        if not agent.pat_rotation_enabled:
            agent.pat_rotation_enabled_by = request.user
    agent.pat_rotation_enabled = payload.enabled
    agent.pat_rotation_days = min(payload.period_days, pat_rotation.max_days())
    agent.save(update_fields=["pat_rotation_enabled", "pat_rotation_days",
                              "pat_rotation_enabled_by", "updated_at"])
    audit.note_change(scope=f"agent:{agent.slug}/pat-rotation",
                      before=f"{'on' if before[0] else 'off'}/{before[1]}d",
                      after=f"{'on' if payload.enabled else 'off'}/{payload.period_days}d")
    return PatRotationOut(**pat_rotation.status(agent))


@router.post("/{slug}/pat-rotation/rotate", response=PatRotationOut,
             summary="Rotate this agent's canopy-pat now")
@gate("agent-admin", setting=True, step_up=True)  # browser: + passkey step-up; token allowed
def rotate_pat_now(request: HttpRequest, slug: str) -> PatRotationOut:
    """The same rotation the schedule does, now, whether or not rotation is on:
    a new token in `canopy-pat`, the old one valid for 24 h then revoked. It
    changes a secret, so from a browser it needs a fresh passkey step-up (403
    `step_up_required`); a token may call it. The owner or an admin."""

    agent = agent_for_admin(request, slug)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    try:
        pat_rotation.rotate(agent, trigger="manual", actor=request.user)
    except pat_rotation.RotationRefused as exc:
        raise HttpError(exc.status, str(exc)) from exc
    agent.refresh_from_db()
    return PatRotationOut(**pat_rotation.status(agent))


@router.post("/{slug}/pat-rotation/own", response=OwnPatRotateOut,
             summary="An agent rotates its OWN canopy token because it may have leaked")
# the agent's own login only: rotating its own credential can only shorten the old
# one's life, so it needs no admin — and an agent that leaked its token must be able
# to act at once, not wait for a person (Jonathan, 2026-10-24)
@gate("agent-login", setting=True, step_up="a token call by the agent's own login; carries no secret")
def rotate_own_pat(request: HttpRequest, slug: str, payload: OwnPatRotateIn) -> OwnPatRotateOut:
    """Call this the moment you think your canopy token (CANOPY_WEB_PAT) left
    where it belongs — sent to a third-party host, pasted into a chat or a log,
    committed. Same rotation as the schedule: a new token goes into your stored
    CANOPY_WEB_PAT (each box picks it up at its next `canopy cred sync`), the old
    one keeps working for 24 h so your other boxes don't break, then is revoked.
    `reason` is recorded with the rotation and in the settings audit, where your
    owner sees it. Only the agent's own login; anyone else gets a 403."""

    agent = agent_for_read(request, slug)
    if not agent.is_own_login(request.user):
        raise HttpError(403, "only the agent's own login may rotate its own token; an admin uses /pat-rotation/rotate")
    try:
        kind = token_kinds.of_request(request)
        presented = (getattr(request, "auth_credential", None) or {}).get("id") \
            if kind is not None and kind.name == token_kinds.PAT else None
        row = pat_rotation.rotate(agent, trigger="self", actor=request.user, presented_token_id=presented)
    except pat_rotation.RotationRefused as exc:
        raise HttpError(exc.status, str(exc)) from exc
    row.detail = f"{row.detail}. Self-requested: {payload.reason}"[:500]
    row.save(update_fields=["detail"])
    audit.note_change(scope=f"agent:{agent.slug}/canopy-pat", before="current token",
                      after=f"rotated by the agent itself: {payload.reason}"[:300])
    return OwnPatRotateOut(rotated_at=row.created_at, old_token_revoked_after=row.revoke_after, detail=row.detail)

