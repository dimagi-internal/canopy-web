"""Learning mode: a person's one switch for "coaching agents and admins may read and analyze my sessions".

The record is `CoachingOptIn` (who set it, when, who ended it), and the `individual` learning class is the same
yes (`brain.consent` keeps the two equal, so the brain's consent and learning mode are one). Where it holds is the
person's learning consent (`brain.eligibility.coaching_allows`: its workspaces, less its standing rules).

Who turns it on or off:

* the person, as themselves (`set_mine`), everywhere their consent reaches;
* an OWNER of a workspace the person is in (a parent for their kids), for them, in the workspaces that owner owns
  and only there (`set_for`): turning it on never widens what the person agreed to elsewhere
  (`consent.owner_individual_on`), turning it off where it reaches further closes it only there
  (`consent.owner_individual_off`). The person is told (a push to their browsers, and their own audit names who).

Who reads a person in learning mode is the session ACL (`canopy_sessions.access`: their coaches, leg 5; the owners
and admins of the session's workspace and the admins of the agent it was with, leg 8), read-only, never a sensitive
session, never a pull. `readers` is that list in words, shown to the person on their own settings page.
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model

from apps.agents.models import Agent, AgentAdmin
from apps.brain import consent as brain_consent
from apps.brain.eligibility import coaching_allows
from apps.canopy_sessions.access import sessions_of_q
from apps.canopy_sessions.models import CoachingOptIn, Session
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import session_reviewers
from .services import person_for

logger = logging.getLogger(__name__)

#: Where the person sees and changes it.
SETTINGS_PATH = "/settings/learning"


class Refused(ValueError):
    """The change asked for cannot be made for this person (a 422 at the route, the reason shown)."""


def live(user) -> CoachingOptIn | None:
    return session_reviewers.live(user).select_related("set_by").first()


def workspaces_on(user) -> list[str]:
    """The person's workspaces where learning mode holds now: a live opt-in, and the consent allows coaching there."""
    if live(user) is None:
        return []
    consent = brain_consent.live(user)
    return sorted(ws for ws in wsvc.user_workspace_slugs(user) if coaching_allows(consent, ws))


def _name(user) -> str:
    return (user.get_full_name() or user.email or user.username) if user is not None else ""


def _people(ids) -> list[dict]:
    agents = set(Agent.objects.exclude(user_id=None).values_list("user_id", flat=True))
    users = get_user_model().objects.filter(pk__in=set(ids) - agents, is_active=True).order_by("email")
    return [{"name": _name(u), "email": u.email or ""} for u in users]


def readers(user) -> dict:
    """Exactly who may read this person's sessions because of learning mode, in words: their coaches, the owners
    and admins of each workspace where it holds, and the admins of each agent they talk to there."""
    on_in = workspaces_on(user)
    if not on_in:
        return {"coaches": [], "workspace_admins": [], "agent_admins": []}
    covering = wsvc.with_ancestors(set(on_in))
    coaches = [c for c in session_reviewers.coaches_for(user) if c["workspace"] in covering]
    ws_admins = []
    for ws in on_in:
        people = [p for p in _people(perms.user_ids_with(ws, perms.LEARNING_READ) - {user.pk})]
        if people:
            ws_admins.append({"workspace": ws, "people": people})
    agent_ids = set(Session.objects.filter(sessions_of_q([user.pk]), agent__isnull=False, workspace_id__in=on_in)
                    .values_list("agent_id", flat=True).distinct())
    agent_admins = []
    for agent in Agent.objects.filter(pk__in=agent_ids).order_by("slug"):
        ids = set(AgentAdmin.objects.filter(agent=agent).values_list("user_id", flat=True))
        ids |= {agent.owner_id} if agent.owner_id else set()
        ids |= perms.user_ids_with(agent.workspace_id, perms.OWN)
        people = _people(ids - {user.pk})
        if people:
            agent_admins.append({"agent": agent.slug, "name": agent.name or agent.slug, "people": people})
    return {"coaches": coaches, "workspace_admins": ws_admins, "agent_admins": agent_admins}


def state(user) -> dict:
    """The person's learning mode, as they see it: on or off, who turned it on and when, where it holds, who reads."""
    row = live(user)
    by = row.set_by if row is not None else None
    return {"on": row is not None, "set_at": row.created_at if row is not None else None,
            "set_by": ({"name": _name(by), "email": by.email or "", "is_you": by.pk == user.pk}
                       if by is not None else None),
            "workspaces": workspaces_on(user), "readers": readers(user)}


def set_mine(user, *, on: bool) -> dict:
    """The person's own act: on (everywhere their consent reaches, lifting any owner's "off here") or off."""
    if on:
        brain_consent.self_individual_on(user)
    else:
        session_reviewers.opt_out(person_for(user=user), user=user)
    return state(user)


def owned_for(owner, member) -> set[str]:
    """The workspaces of `member` that `owner` owns (inherited ownership counts): where `set_for` acts."""
    return perms.slugs_with(owner, perms.OWN, within=set(wsvc.user_workspace_slugs(member)))


def set_for(member, *, on: bool, owner, workspace: str) -> list[str]:
    """An owner turns `member`'s learning mode on or off, in the workspaces of `member` they own (the pinned
    `workspace` and every other they own). Returns the workspaces changed. The person is told."""
    tree = {workspace} | wsvc.descendant_slugs({workspace})
    if not perms.can(owner, workspace, perms.OWN) or not set(wsvc.user_workspace_slugs(member)) & tree:
        raise Refused(f"they are not in {workspace} or below, or you do not own it")
    managed = owned_for(owner, member)
    before = set(workspaces_on(member))
    try:
        (brain_consent.owner_individual_on if on else brain_consent.owner_individual_off)(
            member, managed=managed, by_user=owner)
    except brain_consent.OptInRefusedError as e:
        raise Refused(str(e)) from e
    after = set(workspaces_on(member))
    changed = sorted((after - before) if on else (before - after))
    if changed and member.pk != owner.pk:
        _tell(member, on=on, owner=owner, workspaces=changed)
    return changed


def _tell(member, *, on: bool, owner, workspaces: list[str]) -> None:
    """The person hears that someone else changed their learning mode: a push to every browser they registered.
    Their settings page says who turned it on and when, and their own audit holds every change."""
    from apps.push.services import send_to_user  # noqa: PLC0415 — only an owner's act pays for it

    where = ", ".join(workspaces)
    body = (f"{_name(owner)} turned learning mode {'on' if on else 'off'} for you in {where}. "
            + ("Coaches and admins there may read your sessions, read-only." if on
               else "Nobody there reads your sessions for learning now."))
    try:
        send_to_user(member, "Learning mode", body, SETTINGS_PATH)
    except Exception:  # noqa: BLE001 — telling them never undoes the act; the settings page still shows it
        logger.exception("could not push a learning-mode change")


def members_state(workspace: str) -> list[dict]:
    """For a workspace's owners: every person in it or below, and where their learning mode holds among those."""
    tree = {workspace} | wsvc.descendant_slugs({workspace})
    agents = set(Agent.objects.exclude(user_id=None).values_list("user_id", flat=True))
    ids: set[int] = set()
    for ws in tree:
        ids |= wsvc.member_user_ids(ws)
    users = get_user_model().objects.filter(pk__in=ids - agents, is_active=True).order_by("email")
    rows = {r.user_id: r for r in CoachingOptIn.objects.filter(revoked_at__isnull=True, user_id__in=ids)
            .select_related("set_by")}
    out = []
    for u in users:
        row = rows.get(u.pk)
        here = sorted(set(workspaces_on(u)) & tree) if row is not None else []
        out.append({"user_id": u.pk, "email": u.email or "", "name": _name(u), "on": bool(here),
                    "workspaces": here, "set_at": row.created_at if row is not None else None,
                    "set_by": _name(row.set_by) if row is not None and row.set_by_id else ""})
    return out
