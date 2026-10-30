"""Alert source `agent_token_expiring`: an agent's own canopy token is about to expire,
and nothing will replace it.

An agent's own canopy token expires like any PAT (`services.agent_token_ttl_days`).
Automatic rotation (`credentials/pat_rotation.py`) replaces one canopy-web stores in
the agent's Canopy vault well before then. With rotation off nothing replaces it, and once
it lapses every call the agent makes with it fails. So within `WARN_WITHIN` of the expiry of the agent's
newest stored login token, with rotation off, its admins are told how to re-mint it.

Resolves when a token that lives past the window exists (re-minted), or rotation is
turned on. Turn-scoped logins (`TurnAgentToken`, hours long) are not stored logins and
never count.
"""
from __future__ import annotations

from datetime import timedelta

from apps.attention.alerts import DISMISS, Action, Raise
from apps.tokens.models import PersonalToken

from .models import Agent

KIND = "agent_token_expiring"
SEVERITY = "action"
CONFIRM_WITHIN: timedelta | None = None
CLOSING_ACTION_IDS = ("remint", "dismiss")
WARN_WITHIN = timedelta(days=14)


def settings_path(workspace: str, slug: str) -> str:
    return f"/w/{workspace}/agents/{slug}/settings"


def _stored_tokens(agent, now):
    return (PersonalToken.objects.filter(user_id=agent.user_id, revoked_at__isnull=True,
                                         turn_grant__isnull=True)
            .exclude(expires_at__lte=now))


def expiring(agent, now):
    """The expiry of `agent`'s newest live stored token when it falls inside the window
    and nothing will replace it; else None."""
    if agent.user_id is None or agent.pat_rotation_enabled:
        return None
    tokens = _stored_tokens(agent, now)
    if tokens.filter(expires_at__isnull=True).exists():
        return None  # a token minted before agent tokens expired: nothing lapses
    latest = tokens.order_by("-expires_at").values_list("expires_at", flat=True).first()
    if latest is None or latest > now + WARN_WITHIN:
        return None
    return latest


def reconcile(now) -> list[Raise]:
    out: list[Raise] = []
    for agent in Agent.objects.filter(user__isnull=False, pat_rotation_enabled=False).order_by("slug"):
        when = expiring(agent, now)
        if when is None:
            continue
        out.append(Raise(
            subject_key=f"agent:{agent.slug}:canopy-token",
            workspace=agent.workspace_id,
            title=f"{agent.name or agent.slug}'s canopy login expires {when:%Y-%m-%d}",
            url=settings_path(agent.workspace_id, agent.slug),
            scopes=(f"agent:{agent.slug}", f"workspace:{agent.workspace_id}"),
            body=("Nothing rotates it. Turn on automatic rotation under Settings → Security → Canopy login, "
                  "or give the agent a fresh login there."),
            fingerprint=f"{when:%Y-%m-%d}",
        ))
    return out


def _agent_of(alert):
    parts = alert.subject_key.split(":")
    if len(parts) != 3 or parts[0] != "agent" or parts[2] != "canopy-token":
        return None
    return Agent.objects.filter(slug=parts[1], workspace_id=alert.workspace_id).first()


def actions(alert, user) -> list[Action]:
    """Only the agent's admins: they are the ones who can mint its token."""
    if not getattr(user, "is_authenticated", False):
        return []
    agent = _agent_of(alert)
    if agent is None or not agent.is_admin(user):
        return []
    return [Action("remint", "Open its credentials", True,
                   href=settings_path(agent.workspace_id, agent.slug)), DISMISS]


def perform(alert, user, action_id) -> None:
    # `remint` is a link and `dismiss` is the framework's: nothing runs here.
    raise ValueError(f"agent_token_expiring has no handler for {action_id!r}")
