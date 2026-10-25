"""Which agent an embedded panel opens on, for this visitor.

One ranking, decided here so every door answers the same way:

1. **The page chose** — a token bound to one agent (`panel_context(agent=...)`,
   key-mode `PAGE_AGENTS`, the signed `canopy_agent` claim). The site offers that
   agent alone, so it is also the only one listed.
2. **The person chose** — their `PreferredAgent`, one for everywhere.
3. **The site chose** — its `AppCredentialAgent.is_default`.

Each counts only when the agent is among the ones this panel may offer this
visitor (the site's allowlist ∩ what they can reach): a preference grants
nothing. Below these, the panel itself falls back to the agent the host named
at `canopy.init` (an SDK's `AGENT_SLUG`), then to the only agent on offer, then
to the picker — those are facts about the page, not about canopy's data.
"""
from __future__ import annotations

from dataclasses import dataclass

from apps.agents.models import PreferredAgent
from apps.tokens.models import AppCredentialAgent


@dataclass(frozen=True)
class Defaults:
    #: The agent to open on, or None to leave it to the panel's own fallbacks.
    default_id: int | None
    #: The person's own preference, when it is on offer here (None otherwise).
    preferred_id: int | None


def resolve(offered_ids, *, app, user=None, bound_agent_id=None) -> Defaults:
    """The visitor's default among `offered_ids` (the agents this panel lists)."""
    offered = set(offered_ids)
    preferred_id = None
    if user is not None and getattr(user, "is_authenticated", False):
        pref = PreferredAgent.objects.filter(user=user).values_list("agent_id", flat=True).first()
        preferred_id = pref if pref in offered else None
    if bound_agent_id and bound_agent_id in offered:
        return Defaults(default_id=bound_agent_id, preferred_id=preferred_id)
    if preferred_id is not None:
        return Defaults(default_id=preferred_id, preferred_id=preferred_id)
    site = (AppCredentialAgent.objects.filter(app=app, is_default=True)
            .values_list("agent_id", flat=True).first()) if app is not None else None
    return Defaults(default_id=site if site in offered else None, preferred_id=None)


def preferred_slug(user) -> str:
    """The person's preferred agent's slug, or ""."""
    pref = PreferredAgent.objects.filter(user=user).select_related("agent").first()
    return pref.agent.slug if pref else ""


def set_preferred(user, agent) -> None:
    """Make `agent` the person's default, or clear it with None. The caller has
    already checked the person may reach it."""
    if agent is None:
        PreferredAgent.objects.filter(user=user).delete()
        return
    PreferredAgent.objects.update_or_create(user=user, defaults={"agent": agent})
